from __future__ import annotations

from collections.abc import Callable
from difflib import SequenceMatcher
from io import BytesIO
from pathlib import Path
import re
from time import monotonic, perf_counter, sleep
from typing import Any
from urllib.parse import urlparse
from zipfile import BadZipFile, ZipFile

import httpx

from subtitle_sidecar.providers.base import DownloadedSubtitle, DownloadedSubtitleMember, ProviderSearchReport, SubtitleCandidate, SubtitleSearchRequest


SUBDL_V2_API_BASE_URL = "https://api.subdl.com/api/v2"
SUBDL_DOWNLOAD_BASE_URL = "https://dl.subdl.com"
SUBDL_SITE_BASE_URL = "https://subdl.com"
SUPPORTED_FORMATS = {"srt", "ass", "ssa", "vtt", "sub"}
MAX_SUBDL_PAGES = 20


class SubdlApiError(RuntimeError):
    pass


class SubdlProvider:
    """Official SubDL v2 adapter with local Chinese-language normalization."""

    name = "subdl"

    def __init__(self, *, api_key: str, timeout_seconds: float = 15.0, requests_per_minute: int = 20, use_api_key_for_downloads: bool = False, client: Any | None = None, clock: Callable[[], float] = monotonic, sleeper: Callable[[float], None] = sleep) -> None:
        self._api_key = api_key.strip()
        self._timeout_seconds = timeout_seconds
        self._requests_per_minute = max(1, requests_per_minute)
        self._use_api_key_for_downloads = use_api_key_for_downloads
        self._client = client or httpx
        self._clock = clock
        self._sleeper = sleeper
        self._last_api_request_at: float | None = None
        self._reporter: Callable[[ProviderSearchReport], None] | None = None

    def set_reporter(self, reporter: Callable[[ProviderSearchReport], None]) -> None:
        self._reporter = reporter

    def search(self, request: SubtitleSearchRequest) -> list[SubtitleCandidate]:
        started_at = perf_counter()
        self._emit(ProviderSearchReport(provider=self.name, status="started"))
        if not self._api_key:
            self._emit(ProviderSearchReport(provider=self.name, status="skipped", duration_ms=_duration_ms(started_at), error="missing_api_key", reason="missing_api_key"))
            return []
        try:
            candidates, strategy = self._search_with_fallbacks(request)
        except Exception as error:
            self._emit(ProviderSearchReport(provider=self.name, status="failed", duration_ms=_duration_ms(started_at), error=_safe_error(error)))
            raise
        self._emit(ProviderSearchReport(provider=self.name, status="completed", candidate_count=len(candidates), duration_ms=_duration_ms(started_at), reason=strategy))
        return candidates

    def _search_with_fallbacks(self, request: SubtitleSearchRequest) -> tuple[list[SubtitleCandidate], str]:
        attempts: list[str] = []
        for strategy, query in _search_queries(request):
            label = _search_label(strategy, query)
            titles = self._v2_get("/movies/search", {"q": query, "type": _media_type(request), "limit": 5}).get("results") or []
            selected_work = _select_work(titles, request)
            if selected_work is None:
                attempts.append(f"{label}：未识别影片")
                self._emit_progress(
                    label,
                    error="subdl_title_not_found",
                    search_context={"strategy": strategy, "query": query},
                )
                continue
            sd_id = str(selected_work["sd_id"])
            candidates, pages = self._search_pages(sd_id, request, work=selected_work)
            attempts.append(f"{label}：{len(candidates)} 条/{pages} 页")
            self._emit_progress(
                label,
                candidate_count=len(candidates),
                search_context={
                    "strategy": strategy,
                    "query": query,
                    "sd_id": sd_id,
                    "pages": pages,
                },
            )
            if candidates:
                return candidates, "；".join(attempts)
        return [], "；".join(attempts) or "无可用检索信息"

    def _search_pages(
        self,
        sd_id: str,
        request: SubtitleSearchRequest,
        *,
        work: dict[str, Any] | None = None,
    ) -> tuple[list[SubtitleCandidate], int]:
        candidates: list[SubtitleCandidate] = []
        page = 1
        total_pages = 1
        while page <= min(total_pages, MAX_SUBDL_PAGES):
            params: dict[str, Any] = {"sd_id": sd_id, "unpack": 1, "subs_per_page": 30, "page": page}
            if request.season is not None:
                params["season"] = request.season
            if request.episode is not None:
                params["episode"] = request.episode
            payload = self._v2_get("/subtitles/search", params)
            total_pages = max(1, int(payload.get("totalPages") or 1))
            for subtitle in payload.get("subtitles") or []:
                if isinstance(subtitle, dict):
                    candidates.extend(_to_candidates(subtitle, request, work=work))
            page += 1
        return candidates, min(total_pages, MAX_SUBDL_PAGES)

    def download(self, candidate: SubtitleCandidate, target_dir: Path) -> DownloadedSubtitle:
        path = candidate.raw_metadata.get("subdl_download_path")
        if not isinstance(path, str) or not _is_download_path(path):
            raise SubdlApiError("subdl_candidate_missing_download_path")
        headers = {"Accept": "application/octet-stream"}
        if self._use_api_key_for_downloads:
            headers["X-API-Key"] = self._api_key
        response = self._client.get(f"{SUBDL_DOWNLOAD_BASE_URL}{path}", headers=headers, timeout=self._timeout_seconds, follow_redirects=True)
        _raise_for_status(response, "subdl_download_failed")
        content = bytes(response.content)
        if not content:
            raise SubdlApiError("subdl_download_empty")
        target_dir.mkdir(parents=True, exist_ok=True)
        filename = _safe_filename(str(candidate.raw_metadata.get("subdl_filename") or candidate.title))
        if candidate.raw_metadata.get("subdl_archive"):
            members = _extract_subtitle_archive(content, target_dir, filename)
            return DownloadedSubtitle(candidate=candidate, path=members[0].path, members=tuple(members))
        if Path(filename).suffix.lower().lstrip(".") not in SUPPORTED_FORMATS:
            raise SubdlApiError("subdl_download_unsupported_format")
        output = target_dir / f"subdl-{filename}"
        output.write_bytes(content)
        return DownloadedSubtitle(
            candidate=candidate,
            path=output,
            members=(DownloadedSubtitleMember(path=output, filename=filename),),
        )

    def usage(self) -> dict[str, Any]:
        return self._v2_get("/me", {})

    def _v2_get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        self._wait_for_rate_limit()
        response = self._client.get(f"{SUBDL_V2_API_BASE_URL}{path}", params=params, headers={"Authorization": f"Bearer {self._api_key}", "Accept": "application/json"}, timeout=self._timeout_seconds, follow_redirects=True)
        _raise_for_status(response, "subdl_request_failed")
        try:
            payload = response.json()
        except ValueError as error:
            raise SubdlApiError("subdl_invalid_response") from error
        if not isinstance(payload, dict):
            raise SubdlApiError("subdl_invalid_response")
        if payload.get("status") is False:
            raise SubdlApiError(str(payload.get("error") or "subdl_api_error"))
        return payload

    def _wait_for_rate_limit(self) -> None:
        now = self._clock()
        if self._last_api_request_at is not None:
            delay = 60.0 / self._requests_per_minute - (now - self._last_api_request_at)
            if delay > 0:
                self._sleeper(delay)
        self._last_api_request_at = self._clock()

    def _emit_progress(
        self,
        reason: str,
        candidate_count: int | None = None,
        error: str | None = None,
        search_context: dict[str, Any] | None = None,
    ) -> None:
        self._emit(
            ProviderSearchReport(
                provider=self.name,
                status="progress",
                candidate_count=candidate_count,
                reason=reason,
                error=error,
                search_context=search_context or {},
            )
        )

    def _emit(self, report: ProviderSearchReport) -> None:
        if self._reporter:
            self._reporter(report)


def _search_queries(request: SubtitleSearchRequest) -> list[tuple[str, str]]:
    values = [
        ("imdb_id", request.imdb_id),
        ("tmdb_id", request.tmdb_id),
        ("original_title", request.original_title),
        ("title", request.title),
        ("file_name", request.video_path.name),
    ]
    result: list[tuple[str, str]] = []
    seen: set[str] = set()
    for strategy, value in values:
        normalized = str(value or "").strip()
        if normalized and normalized.casefold() not in seen:
            seen.add(normalized.casefold())
            result.append((strategy, normalized))
    return result


def _search_label(strategy: str, query: str) -> str:
    names = {
        "imdb_id": "IMDb",
        "tmdb_id": "TMDB",
        "original_title": "原始标题",
        "title": "标题",
        "file_name": "文件名",
    }
    return f"{names.get(strategy, strategy)} {query}"


def _select_sd_id(results: Any, request: SubtitleSearchRequest) -> str | None:
    selected = _select_work(results, request)
    return str(selected["sd_id"]) if selected is not None else None


def _select_work(results: Any, request: SubtitleSearchRequest) -> dict[str, Any] | None:
    if not isinstance(results, list):
        return None
    items = [item for item in results if isinstance(item, dict) and item.get("sd_id")]
    if not items:
        return None

    for key, expected in (("imdb_id", request.imdb_id), ("tmdb_id", request.tmdb_id)):
        if not expected:
            continue
        for item in items:
            if _same_external_id(item.get(key), expected):
                return item

    expected_years = {
        year
        for year in (request.year, *request.alternate_years)
        if isinstance(year, int)
    }
    expected_titles = [
        title
        for title in (request.original_title, request.title)
        if str(title or "").strip()
    ]
    if expected_years:
        eligible_items = [
            item
            for item in items
            if not (years := _subdl_work_years(item))
            or any(
                abs(actual - expected) <= 1
                for actual in years
                for expected in expected_years
            )
        ]
        if not eligible_items:
            return None
        items = eligible_items

    def score(item: dict[str, Any]) -> tuple[float, float]:
        years = _subdl_work_years(item)
        if not expected_years or not years:
            year_score = 0.0
        elif any(abs(actual - expected) <= 1 for actual in years for expected in expected_years):
            year_score = 2.0
        else:
            year_score = -2.0
        title_score = max(
            (
                _subdl_title_similarity(actual, expected)
                for actual in _subdl_work_titles(item)
                for expected in expected_titles
            ),
            default=0.0,
        )
        return year_score, title_score

    best = max(enumerate(items), key=lambda pair: (*score(pair[1]), -pair[0]))[1]
    # Text-search responses may omit the release year.  In that case, do not
    # silently accept SubDL's first result unless its work title still bears a
    # meaningful resemblance to one of the requested titles.  Exact IMDb/TMDB
    # matches have already returned above and do not need this fallback guard.
    if expected_titles:
        title_similarity = score(best)[1]
        if title_similarity < 0.45:
            return None
    return best


def _same_external_id(actual: Any, expected: Any) -> bool:
    left = str(actual or "").strip().casefold()
    right = str(expected or "").strip().casefold()
    if not left or not right:
        return False
    return left == right or left.lstrip("t") == right.lstrip("t")


def _subdl_work_years(item: dict[str, Any]) -> set[int]:
    years: set[int] = set()
    for key in ("year", "release_year", "first_air_date", "release_date"):
        match = re.search(r"(?<!\d)(?:19|20)\d{2}(?!\d)", str(item.get(key) or ""))
        if match is not None:
            years.add(int(match.group(0)))
    return years


def _subdl_work_titles(item: dict[str, Any]) -> list[str]:
    return [
        text
        for key in ("name", "title", "original_name", "original_title")
        if (text := str(item.get(key) or "").strip())
    ]


def _subdl_title_similarity(actual: str, expected: str) -> float:
    left = "".join(re.findall(r"[a-z0-9]+|[\u3400-\u9fff]+", actual.casefold()))
    right = "".join(re.findall(r"[a-z0-9]+|[\u3400-\u9fff]+", expected.casefold()))
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    if left in right or right in left:
        return 0.9
    return SequenceMatcher(a=left, b=right).ratio()


def _to_candidates(
    subtitle: dict[str, Any],
    request: SubtitleSearchRequest,
    *,
    work: dict[str, Any] | None = None,
) -> list[SubtitleCandidate]:
    language = subtitle.get("language") or subtitle.get("lang")
    if not _is_chinese(language) or not _matches_episode(subtitle, request):
        return []
    subtitle_page = _source_url(subtitle.get("subtitlePage"))
    release = str(subtitle.get("release_name") or subtitle.get("name") or "")
    result: list[SubtitleCandidate] = []
    for file_info in subtitle.get("unpack_files") or []:
        if not isinstance(file_info, dict) or not _matches_episode(file_info, request):
            continue
        path = _download_path(file_info.get("url"))
        filename = _safe_filename(str(file_info.get("name") or "subtitle.srt"))
        extension = str(file_info.get("format") or Path(filename).suffix.lstrip(".")).lower()
        if path and extension in SUPPORTED_FORMATS:
            result.append(
                _candidate(
                    filename,
                    extension,
                    path,
                    language,
                    subtitle_page,
                    release,
                    archive=False,
                    work=work,
                )
            )
    if result:
        return result
    path = _download_path(subtitle.get("url"))
    if path and path.lower().endswith(".zip"):
        filename = _safe_filename(str(subtitle.get("name") or "subtitle.zip"))
        return [
            _candidate(
                f"{Path(filename).stem}.srt",
                "srt",
                path,
                language,
                subtitle_page,
                release,
                archive=True,
                work=work,
            )
        ]
    return []


def _candidate(
    filename: str,
    extension: str,
    path: str,
    language: Any,
    source_url: str,
    release: str,
    *,
    archive: bool,
    work: dict[str, Any] | None = None,
) -> SubtitleCandidate:
    value = str(language or "").upper()
    bilingual = "BG" in value
    work = work or {}
    return SubtitleCandidate(
        provider="subdl",
        language=(
            "zh-hant"
            if "BIG_5" in value or "HANT" in value or "TW" in value
            else "zh-cn"
        ),
        is_bilingual=bilingual,
        format=extension,
        title=filename,
        source_url=source_url,
        release_info=release,
        confidence=0.76 + (0.08 if bilingual else 0.0),
        raw_metadata={
            "subdl_download_path": path,
            "subdl_filename": filename,
            "subdl_language": str(language or ""),
            "subdl_archive": archive,
            "subdl_work_titles": _subdl_work_titles(work),
            "subdl_work_years": sorted(_subdl_work_years(work)),
        },
    )


def _extract_subtitle_archive(
    content: bytes,
    target_dir: Path,
    filename: str,
) -> list[DownloadedSubtitleMember]:
    try:
        with ZipFile(BytesIO(content)) as archive:
            members = [member for member in archive.infolist() if not member.is_dir() and Path(member.filename).suffix.lower().lstrip(".") in SUPPORTED_FORMATS]
            if not members:
                raise SubdlApiError("subdl_archive_has_no_supported_subtitle")
            extracted: list[DownloadedSubtitleMember] = []
            for index, member in enumerate(members, start=1):
                member_name = _safe_filename(member.filename or filename)
                output = target_dir / f"subdl-{index}-{member_name}"
                output.write_bytes(archive.read(member))
                extracted.append(DownloadedSubtitleMember(path=output, filename=member_name))
            return extracted
    except BadZipFile as error:
        raise SubdlApiError("subdl_download_invalid_archive") from error


def _media_type(request: SubtitleSearchRequest) -> str:
    return "tv" if request.media_type.lower() in {"episode", "tv", "tvshow"} else "movie"


def _matches_episode(item: dict[str, Any], request: SubtitleSearchRequest) -> bool:
    return _matches_media_number(item.get("season"), request.season) and _matches_media_number(
        item.get("episode"), request.episode
    )


def _matches_media_number(actual: Any, expected: int | None) -> bool:
    if expected is None:
        return True
    if isinstance(actual, bool):
        return True
    if isinstance(actual, int):
        return actual in {0, expected}
    text = str(actual or "").strip()
    if not text:
        return True
    match = re.fullmatch(r"(?:S|E|Season\s*|Episode\s*)?0*(\d{1,3})", text, re.IGNORECASE)
    return match is None or int(match.group(1)) in {0, expected}


def _is_chinese(language: Any) -> bool:
    value = str(language or "").upper().replace("-", "_")
    return value in {"ZH", "ZH_BG", "ZH_CN", "ZH_TW", "ZH_HANS", "ZH_HANT", "BIG_5_CODE", "CHINESE_BG_CODE"}


def _source_url(value: Any) -> str:
    path = urlparse(str(value or "")).path
    return f"{SUBDL_SITE_BASE_URL}{path}" if path.startswith("/") else SUBDL_SITE_BASE_URL


def _download_path(value: Any) -> str | None:
    path = urlparse(str(value or "")).path
    return path if _is_download_path(path) else None


def _is_download_path(path: str) -> bool:
    return path.startswith("/subtitle/") and ".." not in path


def _safe_filename(value: str) -> str:
    name = Path(value).name
    return name if name and name != "." else "subtitle.srt"


def _raise_for_status(response: Any, error_code: str) -> None:
    try:
        response.raise_for_status()
    except httpx.HTTPError as error:
        raise SubdlApiError(error_code) from error


def _safe_error(error: Exception) -> str:
    if isinstance(error, SubdlApiError):
        return str(error)
    if isinstance(error, httpx.TimeoutException):
        return "provider_timeout"
    if isinstance(error, httpx.HTTPError):
        return "provider_request_failed"
    return error.__class__.__name__


def _duration_ms(started_at: float) -> int:
    return int((perf_counter() - started_at) * 1000)
