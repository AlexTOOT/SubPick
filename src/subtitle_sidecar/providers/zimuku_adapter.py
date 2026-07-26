from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
import base64
import json
import math
import re
import shutil
import subprocess
from tempfile import TemporaryDirectory
from time import monotonic, perf_counter, sleep
from typing import Any, Protocol
from urllib.parse import unquote, urljoin, urlparse
from uuid import uuid4
from zipfile import BadZipFile, ZipFile, is_zipfile

from bs4 import BeautifulSoup
import httpx
from PIL import Image, ImageOps

from subtitle_sidecar.providers.base import (
    DownloadedSubtitle,
    DownloadedSubtitleMember,
    ProviderSearchReport,
    SubtitleCandidate,
    SubtitleSearchRequest,
)


ZIMUKU_SITE_URL = "https://srtku.com"
ZIMUKU_PUBLIC_URL = "https://zimuku.org"
SUPPORTED_FORMATS = {"srt", "ass", "ssa", "vtt"}
MAX_ARCHIVE_MEMBERS = 300
MAX_ARCHIVE_UNCOMPRESSED_BYTES = 100 * 1024 * 1024
MAX_CAPTURED_CAPTCHAS = 100
CAPTCHA_IMAGE_RE = re.compile(
    r"data:image/(?:bmp|png|jpeg);base64,([A-Za-z0-9+/=\s]+)", re.IGNORECASE
)
CAPTCHA_LOCATION_RE = re.compile(
    r"self\.location\s*=\s*[\"']([^\"']+)[\"']\s*\+\s*stringToHex\(",
    re.IGNORECASE,
)
SEARCH_REDIRECT_RE = re.compile(r"url\s*=\s*'([^']*)'\s*\+\s*url", re.IGNORECASE)


class ZimukuError(RuntimeError):
    pass


class ZimukuCaptchaRecognitionError(ZimukuError):
    def __init__(self, message: str, *, answer: str = "", recorded: bool = False) -> None:
        super().__init__(message)
        self.answer = answer
        self.recorded = recorded


class CaptchaSolver(Protocol):
    def solve(self, image: bytes) -> str:
        ...


class FailedCaptchaRecorder:
    """Persist bounded diagnostics only when captcha troubleshooting is enabled."""

    def __init__(self, directory: Path | None) -> None:
        self._directory = directory

    def record(self, image: bytes, answer: str, *, reason: str, solver: str) -> Path | None:
        if self._directory is None:
            return None
        try:
            self._directory.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now(UTC)
            stem = f"{timestamp:%Y%m%dT%H%M%S}-{uuid4().hex[:8]}"
            image_path = self._directory / f"{stem}{_captcha_extension(image)}"
            metadata_path = self._directory / f"{stem}.json"
            image_path.write_bytes(image)
            metadata_path.write_text(
                json.dumps(
                    {
                        "created_at": timestamp.isoformat(),
                        "answer": answer,
                        "reason": reason,
                        "solver": solver,
                        "image": image_path.name,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            self._prune()
            return metadata_path
        except OSError:
            return None

    def _prune(self) -> None:
        if self._directory is None:
            return
        metadata_files = sorted(
            self._directory.glob("*.json"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        for metadata_path in metadata_files[MAX_CAPTURED_CAPTCHAS:]:
            try:
                payload = json.loads(metadata_path.read_text(encoding="utf-8"))
                image_name = str(payload.get("image") or "")
                if image_name and Path(image_name).name == image_name:
                    (self._directory / image_name).unlink(missing_ok=True)
                metadata_path.unlink(missing_ok=True)
            except (OSError, ValueError, TypeError):
                continue


class MoviePilotOcrSolver:
    """Client for the small HTTP contract exposed by MoviePilot-OCR."""

    name = "moviepilot_ocr"
    CHECK_EXPECTED_ANSWER = "06394"

    def __init__(
        self,
        *,
        base_url: str,
        timeout_seconds: float = 30.0,
        client: Any | None = None,
    ) -> None:
        value = base_url.strip().rstrip("/")
        if value.endswith("/captcha/base64"):
            self._endpoint = value
            self._base_url = value.removesuffix("/captcha/base64")
        else:
            self._base_url = value
            self._endpoint = f"{value}/captcha/base64"
        self._timeout_seconds = timeout_seconds
        self._client = client or httpx
        self.last_check_answer = ""

    def solve(self, image: bytes) -> str:
        if not self._base_url:
            raise ZimukuError("zimuku_ocr_not_configured")
        answers: list[str] = []
        for payload_image in (image, _prepare_captcha_for_ocr(image)):
            answer = self._recognize(payload_image)
            answers.append(answer)
            if answer.isdigit() and 4 <= len(answer) <= 6:
                return answer
        raise ZimukuCaptchaRecognitionError(
            "zimuku_ocr_answer_invalid",
            answer="; ".join(
                f"{label}={answer or '<empty>'}"
                for label, answer in zip(("raw", "preprocessed"), answers, strict=True)
            ),
        )

    def _recognize(self, image: bytes) -> str:
        response = self._client.post(
            self._endpoint,
            json={"base64_img": base64.b64encode(image).decode("ascii")},
            timeout=self._timeout_seconds,
        )
        _raise_for_status(response, "zimuku_ocr_request_failed")
        try:
            payload = response.json()
        except ValueError as error:
            raise ZimukuError("zimuku_ocr_response_invalid") from error
        if not isinstance(payload, dict):
            raise ZimukuError("zimuku_ocr_response_invalid")
        return str(payload.get("result") or "").strip()

    def check_available(self) -> int:
        """POST a deterministic captcha and expose its answer for API validation."""
        started_at = perf_counter()
        if not self._base_url:
            raise ZimukuError("zimuku_ocr_not_configured")
        self.last_check_answer = self._recognize(_build_ocr_check_image())
        return _duration_ms(started_at)


class CaptchaSolverChain:
    """Try local/free OCR first and rotate after a rejected answer."""

    name = "captcha_solver_chain"

    def __init__(
        self,
        solvers: Iterable[CaptchaSolver],
        *,
        recorder: FailedCaptchaRecorder,
    ) -> None:
        self._solvers = tuple(solvers)
        self._recorder = recorder
        self._next_index = 0
        self.last_solver_name = ""

    def solve(self, image: bytes) -> str:
        if not self._solvers:
            raise ZimukuError("zimuku_captcha_solver_not_configured")
        failures: list[Exception] = []
        for offset in range(len(self._solvers)):
            index = (self._next_index + offset) % len(self._solvers)
            solver = self._solvers[index]
            solver_name = _solver_name(solver)
            try:
                answer = solver.solve(image)
            except ZimukuCaptchaRecognitionError as error:
                self._recorder.record(
                    image,
                    error.answer,
                    reason="invalid_answer",
                    solver=solver_name,
                )
                error.recorded = True
                failures.append(error)
                continue
            except ZimukuError as error:
                failures.append(error)
                continue
            self.last_solver_name = solver_name
            self._next_index = (index + 1) % len(self._solvers)
            return answer
        raise failures[-1] if failures else ZimukuError("zimuku_captcha_solver_not_configured")


class AntiCaptchaImageSolver:
    """Small Anti-Captcha ImageToText client used only for Zimuku challenges."""

    def __init__(
        self,
        *,
        api_key: str,
        timeout_seconds: float = 120.0,
        client: Any | None = None,
        sleeper: Callable[[float], None] = sleep,
    ) -> None:
        self._api_key = api_key.strip()
        self._timeout_seconds = timeout_seconds
        self._client = client or httpx
        self._sleeper = sleeper

    def solve(self, image: bytes) -> str:
        if not self._api_key:
            raise ZimukuError("zimuku_captcha_solver_not_configured")
        payload = self._post(
            "https://api.anti-captcha.com/createTask",
            {
                "clientKey": self._api_key,
                "task": {
                    "type": "ImageToTextTask",
                    "body": base64.b64encode(image).decode("ascii"),
                    "numeric": 1,
                    "minLength": 4,
                    "maxLength": 6,
                },
            },
        )
        task_id = payload.get("taskId")
        if not isinstance(task_id, int):
            raise ZimukuError(_anti_captcha_error(payload, "zimuku_captcha_create_failed"))
        deadline = monotonic() + self._timeout_seconds
        while monotonic() < deadline:
            self._sleeper(2.0)
            result = self._post(
                "https://api.anti-captcha.com/getTaskResult",
                {"clientKey": self._api_key, "taskId": task_id},
            )
            if result.get("errorId"):
                raise ZimukuError(_anti_captcha_error(result, "zimuku_captcha_failed"))
            if result.get("status") == "ready":
                answer = str((result.get("solution") or {}).get("text") or "").strip()
                if answer.isdigit():
                    return answer
                raise ZimukuError("zimuku_captcha_invalid_answer")
        raise ZimukuError("zimuku_captcha_timeout")

    def balance(self) -> float:
        payload = self._post(
            "https://api.anti-captcha.com/getBalance",
            {"clientKey": self._api_key},
        )
        if payload.get("errorId"):
            raise ZimukuError(_anti_captcha_error(payload, "zimuku_captcha_balance_failed"))
        try:
            return float(payload["balance"])
        except (KeyError, TypeError, ValueError) as error:
            raise ZimukuError("zimuku_captcha_balance_invalid") from error

    def _post(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = self._client.post(url, json=payload, timeout=self._timeout_seconds)
        _raise_for_status(response, "zimuku_captcha_request_failed")
        try:
            value = response.json()
        except ValueError as error:
            raise ZimukuError("zimuku_captcha_response_invalid") from error
        if not isinstance(value, dict):
            raise ZimukuError("zimuku_captcha_response_invalid")
        return value


@dataclass(frozen=True)
class _SearchQuery:
    value: str
    title: str
    title_source: str
    strategy: str


class ZimukuProvider:
    """Independent Zimuku page adapter with explicit captcha handling."""

    name = "zimuku"

    def __init__(
        self,
        *,
        anti_captcha_api_key: str = "",
        moviepilot_ocr_url: str = "",
        base_url: str = ZIMUKU_SITE_URL,
        timeout_seconds: float = 30.0,
        request_delay_seconds: float = 1.0,
        captcha_debug_dir: Path | None = None,
        client: Any | None = None,
        captcha_solver: CaptchaSolver | None = None,
        clock: Callable[[], float] = monotonic,
        sleeper: Callable[[float], None] = sleep,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._request_delay_seconds = max(0.0, request_delay_seconds)
        self._clock = clock
        self._sleeper = sleeper
        self._last_request_at: float | None = None
        self._client = client or httpx.Client(
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
                ),
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
            },
            follow_redirects=True,
        )
        self._captcha_recorder = FailedCaptchaRecorder(captcha_debug_dir)
        self._anti_captcha_solver = (
            AntiCaptchaImageSolver(
                api_key=anti_captcha_api_key,
                timeout_seconds=max(60.0, timeout_seconds),
                sleeper=sleeper,
            )
            if anti_captcha_api_key.strip()
            else None
        )
        configured_solvers: list[CaptchaSolver] = []
        if moviepilot_ocr_url.strip():
            configured_solvers.append(
                MoviePilotOcrSolver(
                    base_url=moviepilot_ocr_url,
                    timeout_seconds=timeout_seconds,
                )
            )
        if self._anti_captcha_solver is not None:
            configured_solvers.append(self._anti_captcha_solver)
        self._captcha_solver = captcha_solver or (
            CaptchaSolverChain(configured_solvers, recorder=self._captcha_recorder)
            if configured_solvers
            else None
        )
        self._reporter: Callable[[ProviderSearchReport], None] | None = None

    def set_reporter(self, reporter: Callable[[ProviderSearchReport], None]) -> None:
        self._reporter = reporter

    def search(self, request: SubtitleSearchRequest) -> list[SubtitleCandidate]:
        started_at = perf_counter()
        self._emit(ProviderSearchReport(provider=self.name, status="started"))
        last_query: _SearchQuery | None = None
        try:
            candidates: list[SubtitleCandidate] = []
            for query in _search_queries(request):
                last_query = query
                candidates = self._search_one(request, query)
                self._emit(
                    ProviderSearchReport(
                        provider=self.name,
                        status="progress",
                        candidate_count=len(candidates),
                        reason=query.value,
                        search_context=_search_context(request, query),
                    )
                )
                if candidates:
                    break
        except Exception as error:
            self._emit(
                ProviderSearchReport(
                    provider=self.name,
                    status="failed",
                    duration_ms=_duration_ms(started_at),
                    error=_safe_error(error),
                    reason=last_query.value if last_query else None,
                    search_context=(
                        _search_context(request, last_query) if last_query is not None else None
                    ),
                )
            )
            raise
        self._emit(
            ProviderSearchReport(
                provider=self.name,
                status="completed",
                candidate_count=len(candidates),
                duration_ms=_duration_ms(started_at),
                reason=last_query.value if last_query else None,
                search_context=(
                    _search_context(request, last_query) if last_query is not None else None
                ),
            )
        )
        return candidates

    def download(self, candidate: SubtitleCandidate, target_dir: Path) -> DownloadedSubtitle:
        detail_path = str(candidate.raw_metadata.get("zimuku_detail_path") or "")
        if not detail_path:
            raise ZimukuError("zimuku_candidate_missing_detail_path")
        detail_url = self._site_url(detail_path)
        detail = self._request(detail_url)
        soup = BeautifulSoup(detail.text, "html.parser")
        first_link = soup.select_one("a#down1[href]")
        if first_link is None:
            raise ZimukuError("zimuku_download_page_missing")
        intermediate_url = self._site_url(str(first_link.get("href") or ""), detail_url)
        intermediate = self._request(intermediate_url, headers={"Referer": detail_url})
        soup = BeautifulSoup(intermediate.text, "html.parser")
        download_link = soup.select_one("a[rel~=nofollow][href]")
        if download_link is None:
            raise ZimukuError("zimuku_download_link_missing")
        final_url = self._site_url(str(download_link.get("href") or ""), intermediate_url)
        response = self._request(final_url, headers={"Referer": detail_url})
        content = bytes(response.content or b"")
        if not content:
            raise ZimukuError("zimuku_download_empty")
        filename = _response_filename(response, final_url)
        target_dir.mkdir(parents=True, exist_ok=True)
        members = _materialize_download(content, filename, target_dir)
        preferred = max(members, key=lambda member: _member_preference(member.filename, candidate))
        return DownloadedSubtitle(candidate=candidate, path=preferred.path, members=tuple(members))

    def captcha_balance(self) -> float:
        if self._anti_captcha_solver is None:
            raise ZimukuError("zimuku_captcha_solver_not_configured")
        return self._anti_captcha_solver.balance()

    def _search_one(
        self,
        request: SubtitleSearchRequest,
        query: _SearchQuery,
    ) -> list[SubtitleCandidate]:
        response = self._request(f"{self._base_url}/search", params={"q": query.value})
        response = self._follow_search_redirects(response)
        work_pages = _matching_work_pages(response.text, request=request, query=query)
        candidates: list[SubtitleCandidate] = []
        for work_title, work_path in work_pages:
            page = self._request(self._site_url(work_path))
            candidates.extend(
                _parse_work_results(
                    page.text,
                    work_title=work_title,
                    request=request,
                    query=query,
                    request_base_url=self._base_url,
                )
            )
        if candidates:
            return _deduplicate_candidates(candidates)
        # Some result templates inline rows but do not expose a separate work page.
        return _parse_search_results(
            response.text,
            request=request,
            query=query,
            request_base_url=self._base_url,
        )

    def _follow_search_redirects(self, response: Any) -> Any:
        for _ in range(4):
            parts = SEARCH_REDIRECT_RE.findall(response.text or "")
            if not parts:
                return response
            response = self._request(self._site_url("".join(reversed(parts))))
        raise ZimukuError("zimuku_search_redirect_loop")

    def _request(self, url: str, **kwargs: Any) -> Any:
        follow_redirects = kwargs.pop("follow_redirects", True)
        previous_attempt: tuple[bytes, str, str] | None = None
        for _ in range(3):
            self._wait_between_requests()
            response = self._client.get(
                url,
                timeout=self._timeout_seconds,
                follow_redirects=follow_redirects,
                **kwargs,
            )
            content_type = str(
                (getattr(response, "headers", {}) or {}).get("Content-Type") or ""
            ).casefold()
            status_code = int(getattr(response, "status_code", 200) or 200)
            html = (response.text or "") if status_code == 404 or "html" in content_type else ""
            image = _captcha_image(html)
            if image is None:
                _raise_for_status(response, "zimuku_request_failed")
                return response
            if previous_attempt is not None:
                previous_image, previous_answer, previous_solver = previous_attempt
                self._captcha_recorder.record(
                    previous_image,
                    previous_answer,
                    reason="rejected_by_zimuku",
                    solver=previous_solver,
                )
                previous_attempt = None
            if self._captcha_solver is None:
                raise ZimukuError("zimuku_captcha_required")
            try:
                answer = self._captcha_solver.solve(image)
            except ZimukuCaptchaRecognitionError as error:
                if not error.recorded:
                    self._captcha_recorder.record(
                        image,
                        error.answer,
                        reason="invalid_answer",
                        solver=_solver_name(self._captcha_solver),
                    )
                raise
            solver_name = str(
                getattr(self._captcha_solver, "last_solver_name", "")
                or _solver_name(self._captcha_solver)
            )
            self._submit_captcha(response, answer)
            previous_attempt = (image, answer, solver_name)
        if previous_attempt is not None:
            previous_image, previous_answer, previous_solver = previous_attempt
            self._captcha_recorder.record(
                previous_image,
                previous_answer,
                reason="rejected_by_zimuku",
                solver=previous_solver,
            )
        raise ZimukuError("zimuku_captcha_rejected")

    def _submit_captcha(self, challenge: Any, answer: str) -> None:
        challenge_url = str(getattr(challenge, "url", "") or self._base_url)
        cookies = getattr(self._client, "cookies", None)
        if cookies is None or not hasattr(cookies, "set"):
            raise ZimukuError("zimuku_client_cookie_support_required")
        cookies.set("srcurl", _string_to_hex(challenge_url))
        match = CAPTCHA_LOCATION_RE.search(challenge.text or "")
        verify_path = match.group(1) if match else "/?security_verify_img="
        verify_url = self._site_url(f"{verify_path}{_string_to_hex(answer)}")
        self._wait_between_requests()
        self._client.get(
            verify_url,
            timeout=self._timeout_seconds,
            follow_redirects=False,
        )

    def _site_url(self, value: str, parent: str | None = None) -> str:
        absolute = urljoin(parent or f"{self._base_url}/", value)
        parsed = urlparse(absolute)
        base = urlparse(self._base_url)
        if parsed.netloc.lower() in {"zimuku.org", "www.zimuku.org", "srtku.com", "www.srtku.com"}:
            return parsed._replace(scheme=base.scheme, netloc=base.netloc).geturl()
        return absolute

    def _wait_between_requests(self) -> None:
        now = self._clock()
        if self._last_request_at is not None:
            delay = self._request_delay_seconds - (now - self._last_request_at)
            if delay > 0:
                self._sleeper(delay)
        self._last_request_at = self._clock()

    def _emit(self, report: ProviderSearchReport) -> None:
        if self._reporter is not None:
            self._reporter(report)


def _search_queries(request: SubtitleSearchRequest) -> list[_SearchQuery]:
    titles: list[tuple[str, str]] = []
    for source, value in (("title", request.title), ("original_title", request.original_title)):
        title = _strip_episode_suffix(str(value or "").strip())
        if len(title) >= 2 and title.casefold() not in {item[0].casefold() for item in titles}:
            titles.append((title, source))
    if not titles:
        titles.append((request.video_path.stem[:160], "filename"))
    episode = request.media_type.lower() in {"episode", "tv", "tvshow"}
    queries: list[_SearchQuery] = []
    if episode and request.season is not None:
        for title, source in titles:
            queries.append(
                _SearchQuery(
                    value=f"{title} S{request.season:02d}",
                    title=title,
                    title_source=source,
                    strategy="season_pack",
                )
            )
        if request.episode is not None:
            for title, source in titles:
                queries.append(
                    _SearchQuery(
                        value=f"{title} S{request.season:02d}E{request.episode:02d}",
                        title=title,
                        title_source=source,
                        strategy="episode_fallback",
                    )
                )
    else:
        for title, source in titles:
            value = f"{title} {request.year}" if request.year else title
            queries.append(
                _SearchQuery(
                    value=value,
                    title=title,
                    title_source=source,
                    strategy="title_year" if request.year else "title",
                )
            )
    return queries


def _search_context(request: SubtitleSearchRequest, query: _SearchQuery) -> dict[str, str | int]:
    context: dict[str, str | int] = {
        "query": query.value,
        "strategy": query.strategy,
        "title": query.title,
        "title_source": query.title_source,
        "media_type": request.media_type,
    }
    for key, value in (
        ("year", request.year),
        ("season", request.season),
        ("episode", request.episode),
        ("imdb_id", request.imdb_id),
        ("tmdb_id", request.tmdb_id),
    ):
        if value is not None and value != "":
            context[key] = value
    return context


def _parse_search_results(
    html: str,
    *,
    request: SubtitleSearchRequest,
    query: _SearchQuery,
    request_base_url: str,
) -> list[SubtitleCandidate]:
    soup = BeautifulSoup(html, "html.parser")
    candidates: list[SubtitleCandidate] = []
    for item in soup.select("div.item"):
        title_link = item.select_one("p.tt.clearfix a[href]") or item.select_one("p.tt a[href]")
        if title_link is None:
            continue
        work_title = title_link.get_text(" ", strip=True)
        if not _work_matches_request(work_title, request, query):
            continue
        candidates.extend(
            _parse_candidate_rows(
                item.select("div.sublist tbody tr"),
                work_title=work_title,
                request=request,
                query=query,
                request_base_url=request_base_url,
            )
        )
    return _deduplicate_candidates(candidates)


def _matching_work_pages(
    html: str,
    *,
    request: SubtitleSearchRequest,
    query: _SearchQuery,
) -> list[tuple[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    pages: list[tuple[str, str]] = []
    for item in soup.select("div.item"):
        title_link = item.select_one("p.tt.clearfix a[href]") or item.select_one("p.tt a[href]")
        if title_link is None:
            continue
        work_title = title_link.get_text(" ", strip=True)
        if _work_matches_request(work_title, request, query):
            pages.append((work_title, str(title_link.get("href") or "")))
    return pages


def _parse_work_results(
    html: str,
    *,
    work_title: str,
    request: SubtitleSearchRequest,
    query: _SearchQuery,
    request_base_url: str,
) -> list[SubtitleCandidate]:
    soup = BeautifulSoup(html, "html.parser")
    rows = soup.select("div.sublist tbody tr") or soup.select("tbody tr")
    return _parse_candidate_rows(
        rows,
        work_title=work_title,
        request=request,
        query=query,
        request_base_url=request_base_url,
    )


def _parse_candidate_rows(
    rows: Iterable[Any],
    *,
    work_title: str,
    request: SubtitleSearchRequest,
    query: _SearchQuery,
    request_base_url: str,
) -> list[SubtitleCandidate]:
    candidates: list[SubtitleCandidate] = []
    work_year = _first_release_year(work_title)
    for row in rows:
        detail = row.select_one("td.first a[href]") or row.select_one("a[href*='/detail/']")
        if detail is None:
            continue
        href = str(detail.get("href") or "")
        detail_path = urlparse(urljoin(request_base_url, href)).path
        if not detail_path.startswith("/detail/"):
            continue
        language_text = " ".join(
            str(image.get("alt") or image.get("title") or image.get("src") or "")
            for image in row.select("img")
        )
        language = _zimuku_language(language_text)
        if language is None:
            continue
        subtitle_title = str(detail.get("title") or detail.get_text(" ", strip=True)).strip()
        format_name = _row_format(row, subtitle_title)
        if format_name is None:
            continue
        bilingual = _is_bilingual(language_text, subtitle_title)
        quality = _quality_score(row)
        downloads = _download_count(row)
        confidence = min(
            0.95,
            0.64 + min(quality, 10.0) * 0.02 + (0.05 if bilingual else 0.0)
            + min(math.log10(downloads + 1), 5.0) * 0.01,
        )
        candidates.append(
            SubtitleCandidate(
                provider="zimuku",
                language=language,
                is_bilingual=bilingual,
                format=format_name,
                title=subtitle_title or Path(detail_path).stem,
                source_url=urljoin(ZIMUKU_PUBLIC_URL, detail_path),
                release_info=subtitle_title,
                confidence=confidence,
                raw_metadata={
                    "zimuku_detail_path": detail_path,
                    "zimuku_work_title": work_title,
                    "zimuku_work_year": work_year,
                    "zimuku_language": language_text.strip(),
                    "zimuku_quality": quality,
                    "zimuku_downloads": downloads,
                    "zimuku_query": query.value,
                    "expected_media_type": request.media_type,
                    "expected_year": request.year,
                    "expected_season": request.season,
                    "expected_episode": request.episode,
                    "expected_titles": [request.title, request.original_title],
                },
            )
        )
    return candidates


def _deduplicate_candidates(candidates: Iterable[SubtitleCandidate]) -> list[SubtitleCandidate]:
    result: list[SubtitleCandidate] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = candidate.source_url.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(candidate)
    return result


def _work_matches_request(
    work_title: str,
    request: SubtitleSearchRequest,
    query: _SearchQuery,
) -> bool:
    episode = request.media_type.lower() in {"episode", "tv", "tvshow"}
    if not episode and request.year is not None:
        years = set(_release_years(work_title))
        if years and request.year not in years:
            return False
    if episode and request.season is not None:
        detected = _season_number(work_title)
        if detected is None:
            if request.season != 1:
                return False
        elif detected != request.season:
            return False
    expected = _normalize_title(query.title)
    actual = _normalize_title(work_title)
    if not expected or not actual:
        return True
    chinese_expected = "".join(re.findall(r"[\u3400-\u9fff]", expected))
    chinese_actual = "".join(re.findall(r"[\u3400-\u9fff]", actual))
    if chinese_expected and chinese_actual:
        return chinese_expected in chinese_actual or chinese_actual in chinese_expected
    tokens = {token for token in re.findall(r"[a-z0-9]+", expected) if len(token) > 1}
    actual_tokens = set(re.findall(r"[a-z0-9]+", actual))
    return not tokens or len(tokens & actual_tokens) >= max(1, math.ceil(len(tokens) * 0.6))


def _season_number(value: str) -> int | None:
    for pattern in (r"\bS(?:eason)?[ ._-]*0*(\d{1,2})\b", r"\bSeason[ ._-]+0*(\d{1,2})\b"):
        match = re.search(pattern, value, re.IGNORECASE)
        if match:
            return int(match.group(1))
    match = re.search(r"第\s*([一二三四五六七八九十百零两\d]+)\s*季", value)
    if not match:
        return None
    token = match.group(1)
    if token.isdigit():
        return int(token)
    return _chinese_number(token)


def _chinese_number(value: str) -> int | None:
    digits = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    if value in digits:
        return digits[value]
    if "十" in value:
        left, _, right = value.partition("十")
        tens = digits.get(left, 1) if left else 1
        ones = digits.get(right, 0) if right else 0
        return tens * 10 + ones
    return None


def _zimuku_language(value: str) -> str | None:
    normalized = value.casefold()
    simplified = any(token in normalized for token in ("简体", "簡體", "chs", "china", "chinese"))
    traditional = any(token in normalized for token in ("繁体", "繁體", "cht", "big5", "hongkong"))
    generic = "中文" in normalized or "jollyroger" in normalized
    if traditional and not simplified:
        return "zh-hant"
    if simplified or traditional or generic:
        return "zh-cn"
    return None


def _is_bilingual(*values: str) -> bool:
    text = " ".join(values).casefold()
    return any(token in text for token in ("双语", "雙語", "中英", "简英", "簡英", "繁英")) or (
        ("english" in text or "英文" in text) and _zimuku_language(text) is not None
    )


def _row_format(row: Any, title: str) -> str | None:
    badges = " ".join(
        element.get_text(" ", strip=True) for element in row.select("span.label")
    ).casefold()
    values = f"{badges} {title.casefold()}"
    for extension in ("ass", "ssa", "srt", "vtt"):
        if re.search(rf"(?:^|[^a-z]){extension}(?:$|[^a-z])", values):
            return extension
    if badges:
        return None
    return "srt"


def _quality_score(row: Any) -> float:
    for element in row.select("[title]"):
        match = re.search(r"字幕质量\s*[:：]\s*(\d+(?:\.\d+)?)", str(element.get("title") or ""))
        if match:
            return float(match.group(1))
    return 0.0


def _download_count(row: Any) -> int:
    text = row.get_text(" ", strip=True)
    matches = re.findall(r"(\d+(?:\.\d+)?)\s*(万)?", text)
    if not matches:
        return 0
    value, unit = matches[-1]
    return int(float(value) * (10000 if unit else 1))


def _captcha_image(html: str) -> bytes | None:
    match = CAPTCHA_IMAGE_RE.search(html)
    if not match:
        return None
    try:
        return base64.b64decode(re.sub(r"\s+", "", match.group(1)), validate=True)
    except (ValueError, base64.binascii.Error) as error:
        raise ZimukuError("zimuku_captcha_image_invalid") from error


def _captcha_extension(image: bytes) -> str:
    if image.startswith(b"BM"):
        return ".bmp"
    if image.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if image.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    return ".img"


def _build_ocr_check_image() -> bytes:
    """Return a deterministic numeric OCR test vector generated for this project."""

    return base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAGQAAAAgAQAAAADMUOCDAAAAwElEQVR42q2QrQ7CMBSF"
        "T0uzjGQCiUD0DUDiqOR1kCCWJpDAIyEQZBPY8QYjIJCFkNAt3YpY96MQhOu+nHPu"
        "ubnEop0HRXe+kuWZiOBjBwAsv+tkFuZO00YBJSCr3PH6Hh3GVRElnElg7pyqPC5T"
        "IGwbjIBxORRCd9pNSuQ+a5zKbpJG6+kgwrMmJunFv3WvVtmqJsvbnVQAmZTOE8de"
        "MJ3YrbVWUYii/2LnSmI+44OFObmc5w+5WLOKyK8f/At9AG0kQZhblvscAAAAAElF"
        "TkSuQmCC"
    )


def _prepare_captcha_for_ocr(image: bytes) -> bytes:
    """Normalize small high-contrast captchas for MoviePilot-OCR's legacy model."""

    try:
        with Image.open(BytesIO(image)) as source:
            grayscale = ImageOps.grayscale(source)
            threshold = _otsu_threshold(grayscale.histogram())
            pixels = grayscale.load()
            edge_pixels = [pixels[x, 0] for x in range(grayscale.width)]
            edge_pixels.extend(
                pixels[x, grayscale.height - 1] for x in range(grayscale.width)
            )
            edge_pixels.extend(pixels[0, y] for y in range(grayscale.height))
            edge_pixels.extend(
                pixels[grayscale.width - 1, y] for y in range(grayscale.height)
            )
            edge_pixels.sort()
            background = edge_pixels[len(edge_pixels) // 2] if edge_pixels else 255
            dark_background = background <= threshold
            normalized = grayscale.point(
                lambda value: 0
                if (value > threshold if dark_background else value < threshold)
                else 255
            )
            normalized = normalized.resize(
                (max(1, normalized.width * 4), max(1, normalized.height * 4)),
                Image.Resampling.NEAREST,
            )
            normalized = ImageOps.expand(normalized, border=12, fill=255)
            output = BytesIO()
            normalized.save(output, format="PNG")
            return output.getvalue()
    except (OSError, ValueError):
        return image


def _otsu_threshold(histogram: list[int]) -> int:
    total = sum(histogram)
    if total <= 0:
        return 127
    weighted_sum = sum(index * count for index, count in enumerate(histogram))
    background_weight = 0
    background_sum = 0
    best_threshold = 127
    best_variance = -1.0
    for threshold, count in enumerate(histogram):
        background_weight += count
        if background_weight == 0:
            continue
        foreground_weight = total - background_weight
        if foreground_weight == 0:
            break
        background_sum += threshold * count
        background_mean = background_sum / background_weight
        foreground_mean = (weighted_sum - background_sum) / foreground_weight
        variance = background_weight * foreground_weight * (background_mean - foreground_mean) ** 2
        if variance > best_variance:
            best_variance = variance
            best_threshold = threshold
    return best_threshold


def _solver_name(solver: Any) -> str:
    return str(getattr(solver, "name", "") or solver.__class__.__name__)


def _materialize_download(
    content: bytes,
    filename: str,
    target_dir: Path,
) -> list[DownloadedSubtitleMember]:
    stream = BytesIO(content)
    if is_zipfile(stream):
        return _extract_zip(content, target_dir)
    if content.startswith(b"Rar!\x1a\x07") or content.startswith(b"7z\xbc\xaf\x27\x1c"):
        return _extract_external_archive(content, filename, target_dir)
    extension = Path(filename).suffix.lower().lstrip(".")
    if extension not in SUPPORTED_FORMATS:
        raise ZimukuError("zimuku_download_unsupported_format")
    safe_name = _safe_filename(filename)
    output = target_dir / f"zimuku-{safe_name}"
    output.write_bytes(content)
    return [DownloadedSubtitleMember(path=output, filename=safe_name)]


def _extract_zip(content: bytes, target_dir: Path) -> list[DownloadedSubtitleMember]:
    try:
        with ZipFile(BytesIO(content)) as archive:
            files = [
                info
                for info in archive.infolist()
                if not info.is_dir() and Path(info.filename).suffix.lower().lstrip(".") in SUPPORTED_FORMATS
            ]
            if len(files) > MAX_ARCHIVE_MEMBERS:
                raise ZimukuError("zimuku_archive_too_many_members")
            if sum(max(0, info.file_size) for info in files) > MAX_ARCHIVE_UNCOMPRESSED_BYTES:
                raise ZimukuError("zimuku_archive_too_large")
            members: list[DownloadedSubtitleMember] = []
            for index, info in enumerate(files, start=1):
                if not _safe_archive_member(info.filename):
                    raise ZimukuError("zimuku_unsafe_archive_member")
                payload = archive.read(info)
                if not payload:
                    continue
                safe_name = _safe_filename(info.filename)
                output = target_dir / f"zimuku-{index}-{safe_name}"
                output.write_bytes(payload)
                members.append(DownloadedSubtitleMember(path=output, filename=safe_name))
    except BadZipFile as error:
        raise ZimukuError("zimuku_archive_invalid") from error
    if not members:
        raise ZimukuError("zimuku_archive_has_no_supported_subtitle")
    return members


def _extract_external_archive(
    content: bytes,
    filename: str,
    target_dir: Path,
) -> list[DownloadedSubtitleMember]:
    executable = shutil.which("7zz") or shutil.which("7z")
    if not executable:
        raise ZimukuError("zimuku_archive_tool_unavailable")
    suffix = Path(filename).suffix or (".rar" if content.startswith(b"Rar!") else ".7z")
    with TemporaryDirectory(prefix="zimuku-archive-") as temporary:
        temporary_path = Path(temporary)
        archive_path = temporary_path / f"download{suffix}"
        archive_path.write_bytes(content)
        listed = _run_archive_tool([executable, "l", "-slt", str(archive_path)])
        entries = [
            (name, size)
            for name, size in _parse_archive_entries(listed.stdout, archive_path.name)
            if Path(name).suffix.lower().lstrip(".") in SUPPORTED_FORMATS
        ]
        if len(entries) > MAX_ARCHIVE_MEMBERS:
            raise ZimukuError("zimuku_archive_too_many_members")
        if sum(max(0, size) for _, size in entries) > MAX_ARCHIVE_UNCOMPRESSED_BYTES:
            raise ZimukuError("zimuku_archive_too_large")
        for name, _ in entries:
            if not _safe_archive_member(name):
                raise ZimukuError("zimuku_unsafe_archive_member")

        extract_root = temporary_path / "extracted"
        extract_root.mkdir()
        try:
            _run_archive_tool(
                [executable, "x", "-y", f"-o{extract_root}", str(archive_path)]
            )
        except ZimukuError:
            unar = shutil.which("unar") if content.startswith(b"Rar!") else None
            if not unar:
                raise
            shutil.rmtree(extract_root)
            extract_root.mkdir()
            _run_archive_tool(
                [unar, "-f", "-D", "-o", str(extract_root), str(archive_path)]
            )
        resolved_root = extract_root.resolve()
        members: list[DownloadedSubtitleMember] = []
        total = 0
        for index, (name, _) in enumerate(entries, start=1):
            extracted_path = extract_root / Path(name.replace("\\", "/"))
            if extracted_path.is_symlink():
                raise ZimukuError("zimuku_archive_extract_failed")
            try:
                resolved_path = extracted_path.resolve(strict=True)
                resolved_path.relative_to(resolved_root)
            except (FileNotFoundError, ValueError) as error:
                raise ZimukuError("zimuku_archive_extract_failed") from error
            if not resolved_path.is_file():
                raise ZimukuError("zimuku_archive_extract_failed")
            payload = resolved_path.read_bytes()
            if not payload:
                continue
            total += len(payload)
            if total > MAX_ARCHIVE_UNCOMPRESSED_BYTES:
                raise ZimukuError("zimuku_archive_too_large")
            safe_name = _safe_filename(name)
            output = target_dir / f"zimuku-{index}-{safe_name}"
            output.write_bytes(payload)
            members.append(DownloadedSubtitleMember(path=output, filename=safe_name))
    if not members:
        raise ZimukuError("zimuku_archive_has_no_supported_subtitle")
    return members


def _run_archive_tool(command: list[str], *, binary: bool = False) -> Any:
    try:
        return subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=not binary,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ZimukuError("zimuku_archive_extract_failed") from error


def _parse_archive_entries(output: str, archive_name: str) -> list[tuple[str, int]]:
    entries: list[tuple[str, int]] = []
    current_name = ""
    current_size = 0

    def append_current() -> None:
        if current_name and current_name != archive_name:
            entries.append((current_name, current_size))

    for line in output.splitlines():
        if line.startswith("Path = "):
            append_current()
            current_name = line[7:].strip()
            current_size = 0
        elif line.startswith("Size = "):
            try:
                current_size = int(line[7:].strip())
            except ValueError:
                current_size = 0
    append_current()
    return entries


def _safe_archive_member(value: str) -> bool:
    normalized = value.replace("\\", "/")
    path = Path(normalized)
    return not path.is_absolute() and ".." not in path.parts and not re.match(r"^[A-Za-z]:", normalized)


def _response_filename(response: Any, url: str) -> str:
    disposition = str((getattr(response, "headers", {}) or {}).get("Content-Disposition") or "")
    match = re.search(r"filename\*=UTF-8''([^;]+)", disposition, re.IGNORECASE)
    if match:
        return _safe_filename(unquote(match.group(1)))
    match = re.search(r"filename\s*=\s*[\"']?([^\"';]+)", disposition, re.IGNORECASE)
    if match:
        return _safe_filename(unquote(match.group(1).strip()))
    return _safe_filename(unquote(Path(urlparse(url).path).name) or "subtitle.zip")


def _safe_filename(value: str) -> str:
    name = Path(value.replace("\\", "/")).name.strip().replace("\x00", "")
    name = re.sub(r"[<>:\"/\\|?*]+", "_", name)
    return name[:240] or "subtitle.srt"


def _member_preference(filename: str, candidate: SubtitleCandidate) -> int:
    value = Path(filename).stem.casefold()
    score = 0
    bilingual = any(
        token in value
        for token in ("chseng", "chteng", "chs.eng", "cht.eng", "中英", "双语", "雙語")
    )
    simplified = any(token in value for token in ("chs", "gb", "简体", "簡體", "sc"))
    traditional = any(token in value for token in ("cht", "big5", "繁体", "繁體", "tc"))
    english = any(token in value for token in ("eng", "english"))
    if candidate.is_bilingual and bilingual:
        score += 12
    if candidate.language == "zh-hant" and traditional:
        score += 8
    if candidate.language != "zh-hant" and simplified:
        score += 8
    if simplified or traditional:
        score += 4
    if english and not (simplified or traditional or bilingual):
        score -= 12
    extension = Path(filename).suffix.lower()
    score += {".ass": 3, ".ssa": 2, ".srt": 1, ".vtt": 0}.get(extension, 0)
    return score


def _normalize_title(value: str) -> str:
    value = re.sub(r"\((?:19|20)\d{2}\)", " ", value)
    value = re.sub(r"\b(?:19|20)\d{2}\b", " ", value)
    value = re.sub(r"\bS\d{1,2}(?:E\d{1,3})?\b", " ", value, flags=re.IGNORECASE)
    return re.sub(r"[^a-z0-9\u3400-\u9fff]+", " ", value.casefold()).strip()


def _strip_episode_suffix(value: str) -> str:
    return re.sub(r"\s*(?:[-_. ]+)?s\d{1,2}e\d{1,3}\s*$", "", value, flags=re.IGNORECASE).strip()


def _release_years(value: str) -> Iterable[int]:
    return (int(match) for match in re.findall(r"(?<!\d)((?:19|20)\d{2})(?!\d)", value))


def _first_release_year(value: str) -> int | None:
    return next(iter(_release_years(value)), None)


def _string_to_hex(value: str) -> str:
    return "".join(format(ord(character), "x") for character in value)


def _anti_captcha_error(payload: dict[str, Any], fallback: str) -> str:
    code = str(payload.get("errorCode") or "").strip().lower()
    return f"zimuku_captcha_{code}" if code else fallback


def _raise_for_status(response: Any, error_code: str) -> None:
    try:
        response.raise_for_status()
    except httpx.TimeoutException as error:
        raise ZimukuError("provider_timeout") from error
    except Exception as error:
        raise ZimukuError(error_code) from error


def _safe_error(error: Exception) -> str:
    if isinstance(error, ZimukuError):
        return str(error)
    if isinstance(error, httpx.TimeoutException):
        return "provider_timeout"
    if isinstance(error, httpx.RequestError):
        return "provider_request_failed"
    return error.__class__.__name__


def _duration_ms(started_at: float) -> int:
    return max(0, int((perf_counter() - started_at) * 1000))
