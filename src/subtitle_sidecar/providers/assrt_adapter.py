from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from io import BytesIO
import math
from pathlib import Path
import re
import subprocess
from tempfile import TemporaryDirectory
from time import monotonic, perf_counter, sleep
from typing import Any
from zipfile import BadZipFile, ZipFile

import httpx

from subtitle_sidecar.media.identity import analyze_release_years
from subtitle_sidecar.providers.base import (
    DownloadedSubtitle,
    DownloadedSubtitleMember,
    ProviderSearchReport,
    SubtitleCandidate,
    SubtitleSearchRequest,
)
from subtitle_sidecar.providers.negative_cache import ProviderNegativeCache


ASSRT_API_BASE_URL = "https://api.assrt.net"
ASSRT_PAGE_URL = "https://assrt.net/xml/sub/{directory}/{subtitle_id}.xml"
SUPPORTED_FORMATS = {"srt", "ass", "ssa", "vtt"}
MAX_ARCHIVE_MEMBERS = 300
MAX_ARCHIVE_LISTED_MEMBERS = 2000
MAX_ARCHIVE_UNCOMPRESSED_BYTES = 100 * 1024 * 1024
MAX_DIRECT_FILE_CANDIDATES = 3
MAX_DIRECT_FILE_FAILURES = 3
ASSRT_DETAIL_CACHE_SECONDS = 15 * 60
MAX_ASSRT_DETAIL_CACHE_ENTRIES = 256


class AssrtApiError(RuntimeError):
    pass


class AssrtProvider:
    """Official ASSRT API adapter. It never persists short-lived download URLs."""

    name = "assrt"

    def __init__(
        self,
        *,
        token: str,
        timeout_seconds: float = 15.0,
        requests_per_minute: int = 5,
        client: Any | None = None,
        clock: Callable[[], float] = monotonic,
        sleeper: Callable[[float], None] = sleep,
        negative_cache: ProviderNegativeCache | None = None,
    ) -> None:
        self._token = token.strip()
        self._timeout_seconds = timeout_seconds
        self._requests_per_minute = max(1, requests_per_minute)
        self._client = client or httpx
        self._clock = clock
        self._sleeper = sleeper
        self._last_api_request_at: float | None = None
        self._reporter: Callable[[ProviderSearchReport], None] | None = None
        self._negative_cache = negative_cache
        self._detail_cache: dict[int, tuple[float, dict[str, Any]]] = {}

    def set_reporter(self, reporter: Callable[[ProviderSearchReport], None]) -> None:
        self._reporter = reporter

    def search(self, request: SubtitleSearchRequest) -> list[SubtitleCandidate]:
        started_at = perf_counter()
        self._emit(ProviderSearchReport(provider=self.name, status="started"))
        if not self._token:
            self._emit(
                ProviderSearchReport(
                    provider=self.name,
                    status="skipped",
                    duration_ms=_duration_ms(started_at),
                    error="missing_token",
                    reason="missing_token",
                )
            )
            return []

        try:
            candidates = []
            used_query = None
            for query in _search_queries(request):
                used_query = query
                cache_key = _negative_cache_key(request, query)
                if self._negative_cache is not None and self._negative_cache.contains(cache_key):
                    self._emit(
                        ProviderSearchReport(
                            provider=self.name,
                            status="progress",
                            candidate_count=0,
                            reason=query,
                            search_context={
                                "strategy": _query_strategy(request, query),
                                "cache": "12h_negative_hit",
                                "remaining_seconds": round(
                                    self._negative_cache.remaining_seconds(cache_key)
                                ),
                            },
                        )
                    )
                    continue
                query_started_at = perf_counter()
                payload = self._api_get("/v1/sub/search", {"q": query, "cnt": 15})
                subtitles = ((payload.get("sub") or {}).get("subs") or [])
                candidates = [
                    candidate
                    for subtitle in subtitles
                    if isinstance(subtitle, dict)
                    for candidate in [_to_candidate(subtitle, request=request)]
                    if candidate is not None and _is_relevant_to_request(subtitle, request)
                ]
                self._emit(
                    ProviderSearchReport(
                        provider=self.name,
                        status="progress",
                        candidate_count=len(candidates),
                        duration_ms=_duration_ms(query_started_at),
                        reason=query,
                        search_context={"strategy": _query_strategy(request, query)},
                    )
                )
                if candidates:
                    candidates = self._enrich_candidate_quality(candidates)
                    break
                if self._negative_cache is not None:
                    self._negative_cache.remember(cache_key)
        except Exception as error:
            self._emit(
                ProviderSearchReport(
                    provider=self.name,
                    status="failed",
                    duration_ms=_duration_ms(started_at),
                    error=_safe_error(error),
                )
            )
            raise

        self._emit(
            ProviderSearchReport(
                provider=self.name,
                status="completed",
                candidate_count=len(candidates),
                duration_ms=_duration_ms(started_at),
                reason=used_query,
            )
        )
        return candidates

    def download(self, candidate: SubtitleCandidate, target_dir: Path) -> DownloadedSubtitle:
        subtitle_id = candidate.raw_metadata.get("assrt_subtitle_id")
        if not isinstance(subtitle_id, int):
            raise AssrtApiError("assrt_candidate_missing_id")
        detail_item = self._detail_item(subtitle_id)
        _assert_movie_year_compatible(candidate, _detail_filenames(detail_item))
        target_dir.mkdir(parents=True, exist_ok=True)
        direct_files = _supported_direct_files(detail_item.get("filelist"))
        members = []
        if len(direct_files) <= MAX_DIRECT_FILE_CANDIDATES:
            members = _download_direct_files(
                client=self._client,
                selected=direct_files,
                subtitle_id=subtitle_id,
                target_dir=target_dir,
                timeout_seconds=self._timeout_seconds,
            )
        if not members:
            members = _download_archive(
                client=self._client,
                detail=detail_item,
                subtitle_id=subtitle_id,
                target_dir=target_dir,
                timeout_seconds=self._timeout_seconds,
            )
        try:
            _assert_movie_year_compatible(candidate, (member.filename for member in members))
        except AssrtApiError:
            _discard_downloaded_members(members)
            raise
        return DownloadedSubtitle(candidate=candidate, path=members[0].path, members=tuple(members))

    def _enrich_candidate_quality(
        self,
        candidates: list[SubtitleCandidate],
    ) -> list[SubtitleCandidate]:
        enriched: list[SubtitleCandidate] = []
        total = len(candidates)
        for index, candidate in enumerate(candidates, start=1):
            subtitle_id = candidate.raw_metadata.get("assrt_subtitle_id")
            if not isinstance(subtitle_id, int):
                enriched.append(candidate)
                continue
            try:
                detail = self._detail_item(subtitle_id)
            except Exception as error:
                enriched.append(candidate)
                self._emit(
                    ProviderSearchReport(
                        provider=self.name,
                        status="progress",
                        reason="quality_detail",
                        search_context={
                            "subtitle_id": subtitle_id,
                            "index": index,
                            "total": total,
                            "status": "failed",
                            "error": _safe_error(error),
                        },
                    )
                )
                continue

            downloads = _as_optional_int(
                _first_present(detail, "down_count", "download_count", "downloads")
            )
            views = _as_optional_int(_first_present(detail, "view_count", "views"))
            detail_vote_score = _first_present(detail, "vote_score", "score")
            vote_score = _as_optional_float(
                detail_vote_score
                if detail_vote_score is not None
                else candidate.raw_metadata.get("assrt_vote_score")
            )
            quality = _assrt_provider_quality(downloads=downloads, vote_score=vote_score)
            metadata = dict(candidate.raw_metadata)
            metadata.update(
                {
                    "assrt_downloads": downloads,
                    "assrt_views": views,
                    "assrt_vote_score": vote_score,
                }
            )
            enriched.append(
                replace(
                    candidate,
                    provider_quality=quality,
                    raw_metadata=metadata,
                )
            )
            self._emit(
                ProviderSearchReport(
                    provider=self.name,
                    status="progress",
                    reason="quality_detail",
                    search_context={
                        "subtitle_id": subtitle_id,
                        "index": index,
                        "total": total,
                        "status": "completed",
                        "downloads": downloads or 0,
                        "views": views or 0,
                        "vote_score": vote_score or 0.0,
                        "provider_quality": round(quality, 4) if quality is not None else 0.0,
                    },
                )
            )
        return enriched

    def _detail_item(self, subtitle_id: int) -> dict[str, Any]:
        now = self._clock()
        cached = self._detail_cache.get(subtitle_id)
        if cached is not None and now - cached[0] <= ASSRT_DETAIL_CACHE_SECONDS:
            return cached[1]

        detail = self._api_get("/v1/sub/detail", {"id": subtitle_id})
        subtitles = ((detail.get("sub") or {}).get("subs") or [])
        if not subtitles or not isinstance(subtitles[0], dict):
            raise AssrtApiError("assrt_subtitle_not_found")
        detail_item = subtitles[0]
        self._detail_cache[subtitle_id] = (self._clock(), detail_item)
        if len(self._detail_cache) > MAX_ASSRT_DETAIL_CACHE_ENTRIES:
            oldest_id = min(self._detail_cache, key=lambda key: self._detail_cache[key][0])
            self._detail_cache.pop(oldest_id, None)
        return detail_item

    def quota(self) -> int:
        payload = self._api_get("/v1/user/quota", {})
        quota = ((payload.get("user") or {}).get("quota"))
        if not isinstance(quota, int):
            raise AssrtApiError("assrt_quota_unavailable")
        return quota

    def _api_get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        if not self._token:
            raise AssrtApiError("assrt_missing_token")
        self._wait_for_rate_limit()
        response = self._client.get(
            f"{ASSRT_API_BASE_URL}{path}",
            params=params,
            headers={"Authorization": f"Bearer {self._token}", "Accept": "application/json"},
            timeout=self._timeout_seconds,
            follow_redirects=True,
        )
        _raise_for_status(response, "assrt_request_failed")
        try:
            payload = response.json()
        except ValueError as error:
            raise AssrtApiError("assrt_invalid_response") from error
        if not isinstance(payload, dict):
            raise AssrtApiError("assrt_invalid_response")
        status = payload.get("status")
        if status != 0:
            raise AssrtApiError(_assrt_error_code(payload))
        return payload

    def _wait_for_rate_limit(self) -> None:
        now = self._clock()
        minimum_interval = 60.0 / self._requests_per_minute
        if self._last_api_request_at is not None:
            delay = minimum_interval - (now - self._last_api_request_at)
            if delay > 0:
                self._sleeper(delay)
        self._last_api_request_at = self._clock()

    def _emit(self, report: ProviderSearchReport) -> None:
        if self._reporter is not None:
            self._reporter(report)


def _search_query(request: SubtitleSearchRequest) -> str:
    if request.media_type.lower() in {"episode", "tv", "tvshow"} and request.season and request.episode:
        query = f"{request.title} S{request.season:02d}E{request.episode:02d}"
    else:
        query = request.title.strip()
    if len(query) >= 3:
        return query
    return request.video_path.stem[:200]


def _search_queries(request: SubtitleSearchRequest) -> list[str]:
    """Prefer season-pack discovery for episodes, then fall back to exact episode names."""
    primary = _search_query(request)
    if request.media_type.lower() not in {"episode", "tv", "tvshow"}:
        titles = [
            title
            for value in (request.title, request.original_title)
            if len(title := str(value or "").strip()) >= 3
        ]
        queries = (
            [f"{title} {request.year}" for title in titles]
            if request.year is not None
            else []
        )
        queries.extend(titles)
        return _unique_queries(queries) or [primary]
    queries: list[str] = []
    for value in (request.title, request.original_title):
        series_title = _strip_episode_suffix(str(value or "").strip())
        season_query = (
            f"{series_title} S{request.season:02d}"
            if series_title and request.season is not None
            else series_title
        )
        if len(season_query) >= 3 and season_query.casefold() not in {query.casefold() for query in queries}:
            queries.append(season_query)
    if primary.casefold() not in {query.casefold() for query in queries}:
        queries.append(primary)
    return queries


def _query_strategy(request: SubtitleSearchRequest, query: str) -> str:
    if request.media_type.lower() not in {"episode", "tv", "tvshow"}:
        return "movie_title_year" if request.year and str(request.year) in query else "movie_title"
    exact_marker = (
        f"S{request.season:02d}E{request.episode:02d}"
        if request.season is not None and request.episode is not None
        else ""
    )
    return "episode_exact" if exact_marker and exact_marker.casefold() in query.casefold() else "season_pack"


def _negative_cache_key(request: SubtitleSearchRequest, query: str) -> tuple[object, ...]:
    strategy = _query_strategy(request, query)
    if strategy == "season_pack":
        media_identity = request.series_id or request.tmdb_id or request.imdb_id or request.title.casefold()
        return ("assrt", strategy, media_identity, request.season, query.casefold())
    if strategy == "episode_exact":
        media_identity = request.series_id or request.tmdb_id or request.imdb_id or request.title.casefold()
        return (
            "assrt",
            strategy,
            media_identity,
            request.season,
            request.episode,
            query.casefold(),
        )
    media_identity = request.tmdb_id or request.imdb_id or request.title.casefold()
    return ("assrt", strategy, media_identity, request.year, query.casefold())


def _unique_queries(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        key = value.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


def _strip_episode_suffix(value: str) -> str:
    return re.sub(r"\s*(?:[-_. ]+)?s\d{1,2}e\d{1,3}\s*$", "", value, flags=re.IGNORECASE).strip()


def _is_relevant_to_request(subtitle: dict[str, Any], request: SubtitleSearchRequest) -> bool:
    """Reject obvious ASSRT text-search collisions before ranking candidates."""
    requested = _chinese_title(request.title)
    subtitle_id = _subtitle_id(subtitle)
    candidate = _chinese_title(_subtitle_title(subtitle, subtitle_id or 0))
    if not requested or not candidate:
        return True
    return requested in candidate or candidate in requested


def _chinese_title(value: str) -> str:
    return "".join(re.findall(r"[\u3400-\u9fff]", value))


def _to_candidate(
    subtitle: dict[str, Any],
    *,
    request: SubtitleSearchRequest,
) -> SubtitleCandidate | None:
    subtitle_id = _subtitle_id(subtitle)
    if subtitle_id is None or not _is_chinese(subtitle):
        return None
    description = _language_description(subtitle)
    bilingual = "双语" in description or _language_flag(subtitle, "langdou")
    vote_score = _as_float(subtitle.get("vote_score") or subtitle.get("score"))
    confidence = min(0.95, 0.55 + min(vote_score, 100.0) / 250.0)
    return SubtitleCandidate(
        provider="assrt",
        language="zh-hant" if "繁" in description else "zh-cn",
        is_bilingual=bilingual,
        format=_format_for(subtitle.get("subtype") or subtitle.get("m_subtype")),
        title=_subtitle_title(subtitle, subtitle_id),
        source_url=ASSRT_PAGE_URL.format(
            directory=str(subtitle_id)[:3],
            subtitle_id=subtitle_id,
        ),
        release_info=str(
            subtitle.get("videoname")
            or subtitle.get("m_videoname")
            or subtitle.get("m_title")
            or subtitle.get("release_site")
            or ""
        ),
        confidence=confidence,
        raw_metadata={
            "assrt_subtitle_id": subtitle_id,
            "assrt_subtype": str(subtitle.get("subtype") or subtitle.get("m_subtype") or ""),
            "assrt_language": description,
            "assrt_vote_score": vote_score,
            "expected_media_type": request.media_type,
            "expected_year": request.year,
            "expected_titles": [request.title, request.original_title],
        },
    )


def _detail_filenames(detail: dict[str, Any]) -> list[str]:
    names = [str(detail.get(key) or "") for key in ("filename", "videoname", "native_name")]
    for item in detail.get("filelist") or []:
        if isinstance(item, dict) and item.get("f"):
            names.append(str(item["f"]))
    return [name for name in names if name]


def _assert_movie_year_compatible(
    candidate: SubtitleCandidate,
    filenames: Any,
) -> None:
    metadata = candidate.raw_metadata
    if str(metadata.get("expected_media_type") or "").lower() != "movie":
        return
    expected_year = metadata.get("expected_year")
    if not isinstance(expected_year, int):
        return
    expected_titles = metadata.get("expected_titles") or []
    evidence = analyze_release_years(
        filenames,
        expected_year=expected_year,
        expected_titles=expected_titles,
    )
    if evidence.has_conflict:
        years = ",".join(str(year) for year in sorted(evidence.years))
        raise AssrtApiError(f"assrt_detail_year_mismatch:{years}:{expected_year}")


def _is_chinese(subtitle: dict[str, Any]) -> bool:
    description = _language_description(subtitle)
    if "中" in description or "双语" in description or _language_flag(subtitle, "langdou"):
        return True
    langlist = ((subtitle.get("lang") or {}).get("langlist") or {})
    extras = subtitle.get("m_extras") or {}
    return any(
        any(marker in str(key).lower() for marker in ("chi", "chs", "cht", "zho", "zh"))
        for key, value in langlist.items()
        if _is_truthy(value)
    ) or any(
        marker in str(key).lower() and _is_truthy(value)
        for key, value in extras.items()
        for marker in ("chi", "chs", "cht", "zho", "zh")
    )


def _language_flag(subtitle: dict[str, Any], flag: str) -> bool:
    langlist = ((subtitle.get("lang") or {}).get("langlist") or {})
    extras = subtitle.get("m_extras") or {}
    expected = flag.casefold()
    return any(
        str(key).casefold() == expected and _is_truthy(value)
        for values in (langlist, extras)
        for key, value in values.items()
    )


def _is_truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().casefold() in {"1", "true", "yes", "on"}
    return bool(value)


def _language_description(subtitle: dict[str, Any]) -> str:
    language = subtitle.get("lang") or {}
    if isinstance(language, dict) and language.get("desc"):
        return str(language["desc"])
    return str(subtitle.get("m_lang") or "")


def _subtitle_id(subtitle: dict[str, Any]) -> int | None:
    for value in (subtitle.get("id"), subtitle.get("fileid")):
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _subtitle_title(subtitle: dict[str, Any], subtitle_id: int) -> str:
    for key in ("native_name", "title", "m_title", "videoname", "m_videoname"):
        value = subtitle.get(key)
        if value:
            return str(value)
    return str(subtitle_id)


def _format_for(subtype: Any) -> str:
    value = str(subtype or "").lower()
    for extension in SUPPORTED_FORMATS:
        if extension in value:
            return extension
    return "srt"


def _supported_direct_files(filelist: Any) -> list[tuple[str, str]]:
    if not isinstance(filelist, list):
        return []
    result: list[tuple[str, str]] = []
    for item in filelist:
        if not isinstance(item, dict):
            continue
        filename = str(item.get("f") or "")
        url = str(item.get("url") or "")
        if url and Path(filename).suffix.lower().lstrip(".") in SUPPORTED_FORMATS:
            result.append((url, filename))
    chinese = [item for item in result if _is_chinese_filename(item[1])]
    # Modern ASSRT season packs can expose hundreds of direct links. Prefer the
    # explicitly tagged Chinese tracks, but keep the old fallback for uploads
    # whose filenames do not carry a language marker.
    return chinese or result


def _is_chinese_filename(filename: str) -> bool:
    normalized = filename.casefold()
    return any(
        marker in normalized
        for marker in (
            "[chi]",
            "[chs]",
            "[cht]",
            "[zho]",
            "[zh]",
            "zh-cn",
            "zh-hans",
            "zh-tw",
            "zh-hant",
            "chinese",
            "简中",
            "繁中",
            "中文",
        )
    )


def _download_direct_files(
    *,
    client: Any,
    selected: list[tuple[str, str]],
    subtitle_id: int,
    target_dir: Path,
    timeout_seconds: float,
) -> list[DownloadedSubtitleMember]:
    members: list[DownloadedSubtitleMember] = []
    consecutive_failures = 0
    for index, (url, filename) in enumerate(selected, start=1):
        try:
            response = client.get(url, timeout=timeout_seconds, follow_redirects=True)
            _raise_for_status(response, "assrt_download_failed")
        except (AssrtApiError, httpx.HTTPError):
            consecutive_failures += 1
            if consecutive_failures >= MAX_DIRECT_FILE_FAILURES:
                _discard_downloaded_members(members)
                return []
            continue
        content = bytes(response.content)
        if not content:
            consecutive_failures += 1
            if consecutive_failures >= MAX_DIRECT_FILE_FAILURES:
                _discard_downloaded_members(members)
                return []
            continue
        consecutive_failures = 0
        safe_name = _safe_filename(filename)
        path = target_dir / f"assrt-{subtitle_id}-{index}-{safe_name}"
        path.write_bytes(content)
        members.append(DownloadedSubtitleMember(path=path, filename=safe_name))
    return members


def _discard_downloaded_members(members: list[DownloadedSubtitleMember]) -> None:
    for member in members:
        member.path.unlink(missing_ok=True)


def _download_archive(
    *,
    client: Any,
    detail: dict[str, Any],
    subtitle_id: int,
    target_dir: Path,
    timeout_seconds: float,
) -> list[DownloadedSubtitleMember]:
    url = str(detail.get("url") or "")
    filename = str(detail.get("filename") or detail.get("sub_name") or "")
    if not url or not filename:
        raise AssrtApiError("assrt_no_supported_direct_file")
    response = client.get(url, timeout=timeout_seconds, follow_redirects=True)
    _raise_for_status(response, "assrt_download_failed")
    content = bytes(response.content)
    if not content:
        raise AssrtApiError("assrt_download_empty")
    suffix = Path(filename).suffix.lower()
    if suffix == ".zip":
        return _extract_zip_members(content, subtitle_id, target_dir)
    if suffix in {".rar", ".7z"}:
        return _extract_7zip_members(content, suffix, subtitle_id, target_dir)
    raise AssrtApiError("assrt_unsupported_archive")


def _extract_7zip_members(
    content: bytes,
    suffix: str,
    subtitle_id: int,
    target_dir: Path,
) -> list[DownloadedSubtitleMember]:
    if len(content) > MAX_ARCHIVE_UNCOMPRESSED_BYTES:
        raise AssrtApiError("assrt_archive_too_large")
    with TemporaryDirectory(prefix="assrt-archive-") as temp_value:
        temp_dir = Path(temp_value)
        archive_path = temp_dir / f"archive{suffix}"
        output_dir = temp_dir / "output"
        output_dir.mkdir()
        archive_path.write_bytes(content)
        listing = _run_7zip(["7z", "l", "-slt", str(archive_path)])
        names = _validated_7zip_members(listing.stdout)
        supported = [name for name in names if Path(name).suffix.lower().lstrip(".") in SUPPORTED_FORMATS]
        chinese = [name for name in supported if _is_chinese_filename(name)]
        selected = chinese or supported
        if not selected:
            raise AssrtApiError("assrt_no_supported_archive_member")
        if len(selected) > MAX_ARCHIVE_MEMBERS:
            raise AssrtApiError("assrt_archive_too_large")
        if suffix == ".rar":
            _run_unar(
                ["unar", "-q", "-f", "-D", "-o", str(output_dir), str(archive_path)]
            )
        else:
            _run_7zip(["7z", "x", "-y", "-bd", "-bso0", "-bsp0", f"-o{output_dir}", str(archive_path), *selected])
        members: list[DownloadedSubtitleMember] = []
        total_size = 0
        for index, name in enumerate(selected, start=1):
            source = (output_dir / name).resolve()
            if output_dir.resolve() not in source.parents or not source.is_file():
                continue
            total_size += source.stat().st_size
            if total_size > MAX_ARCHIVE_UNCOMPRESSED_BYTES:
                raise AssrtApiError("assrt_archive_too_large")
            safe_name = _safe_filename(name)
            destination = target_dir / f"assrt-{subtitle_id}-{index}-{safe_name}"
            destination.write_bytes(source.read_bytes())
            members.append(DownloadedSubtitleMember(path=destination, filename=safe_name))
    if not members:
        raise AssrtApiError("assrt_no_supported_archive_member")
    return members


def _run_7zip(command: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(command, check=True, capture_output=True, text=True, timeout=60)
    except (FileNotFoundError, subprocess.SubprocessError) as error:
        raise AssrtApiError("assrt_archive_extract_failed") from error


def _run_unar(command: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(command, check=True, capture_output=True, text=True, timeout=120)
    except (FileNotFoundError, subprocess.SubprocessError) as error:
        raise AssrtApiError("assrt_archive_extract_failed") from error


def _validated_7zip_members(output: str) -> list[str]:
    members = _parse_7zip_member_records(output)
    if len(members) > MAX_ARCHIVE_LISTED_MEMBERS:
        raise AssrtApiError("assrt_archive_too_large")
    safe_names: list[str] = []
    total_size = 0
    for value, size in members:
        normalized = value.replace("\\", "/")
        path = Path(normalized)
        if not normalized or path.is_absolute() or ".." in path.parts:
            raise AssrtApiError("assrt_unsafe_archive_member")
        total_size += max(0, size)
        if total_size > MAX_ARCHIVE_UNCOMPRESSED_BYTES:
            raise AssrtApiError("assrt_archive_too_large")
        safe_names.append(normalized)
    return safe_names


def _parse_7zip_member_records(output: str) -> list[tuple[str, int]]:
    records: list[tuple[str, int]] = []
    current_path: str | None = None
    current_size = 0
    for line in output.splitlines():
        if line.startswith("Path = "):
            if current_path is not None:
                records.append((current_path, current_size))
            current_path = line[7:].strip()
            current_size = 0
        elif line.startswith("Size = ") and current_path is not None:
            try:
                current_size = int(line[7:].strip())
            except ValueError:
                current_size = 0
    if current_path is not None:
        records.append((current_path, current_size))
    return records[1:] if records else []


def _parse_7zip_members(output: str) -> list[str]:
    members: list[str] = []
    for raw_value, _ in _parse_7zip_member_records(output):
        value = raw_value.replace("\\", "/")
        path = Path(value)
        if not value or path.is_absolute() or ".." in path.parts:
            continue
        members.append(value)
    return members


def _extract_zip_members(
    content: bytes,
    subtitle_id: int,
    target_dir: Path,
) -> list[DownloadedSubtitleMember]:
    try:
        archive = ZipFile(BytesIO(content))
    except BadZipFile as error:
        raise AssrtApiError("assrt_invalid_archive") from error
    with archive:
        files = [
            info
            for info in archive.infolist()
            if not info.is_dir() and Path(info.filename).suffix.lower().lstrip(".") in SUPPORTED_FORMATS
        ]
        total_size = sum(info.file_size for info in files)
        if len(files) > MAX_ARCHIVE_MEMBERS or total_size > MAX_ARCHIVE_UNCOMPRESSED_BYTES:
            raise AssrtApiError("assrt_archive_too_large")
        members: list[DownloadedSubtitleMember] = []
        for index, info in enumerate(files, start=1):
            member_name = _safe_filename(info.filename)
            payload = archive.read(info)
            if not payload:
                continue
            path = target_dir / f"assrt-{subtitle_id}-{index}-{member_name}"
            path.write_bytes(payload)
            members.append(DownloadedSubtitleMember(path=path, filename=member_name))
    if not members:
        raise AssrtApiError("assrt_no_supported_archive_member")
    return members


def _safe_filename(value: str) -> str:
    name = Path(value).name
    return name if name and name != "." else "subtitle.srt"


def _assrt_error_code(payload: dict[str, Any]) -> str:
    status = payload.get("status")
    return f"assrt_api_error_{status}" if isinstance(status, int) else "assrt_api_error"


def _raise_for_status(response: Any, error_code: str) -> None:
    try:
        response.raise_for_status()
    except httpx.HTTPError as error:
        raise AssrtApiError(error_code) from error


def _safe_error(error: Exception) -> str:
    if isinstance(error, AssrtApiError):
        return str(error)
    if isinstance(error, httpx.TimeoutException):
        return "provider_timeout"
    if isinstance(error, httpx.HTTPError):
        return "provider_request_failed"
    return error.__class__.__name__


def _duration_ms(started_at: float) -> int:
    return int((perf_counter() - started_at) * 1000)


def _as_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _as_optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _first_present(values: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in values and values[key] is not None and values[key] != "":
            return values[key]
    return None


def _as_optional_int(value: Any) -> int | None:
    number = _as_optional_float(value)
    if number is None:
        return None
    return max(0, int(number))


def _assrt_provider_quality(
    *,
    downloads: int | None,
    vote_score: float | None,
) -> float | None:
    if downloads is None and vote_score is None:
        return None
    download_signal = min(math.log10(max(downloads or 0, 0) + 1) / 5.0, 1.0)
    vote_signal = min(max(vote_score or 0.0, 0.0) / 100.0, 1.0)
    return download_signal * 0.8 + vote_signal * 0.2
