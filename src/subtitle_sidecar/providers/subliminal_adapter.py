from __future__ import annotations

from collections.abc import Callable
from importlib import import_module
from pathlib import Path
from time import perf_counter
from typing import Any

from dogpile.cache.exception import RegionNotConfigured
from subliminal.core import ProviderPool
from subliminal.exceptions import AuthenticationError
from subliminal.extensions import provider_manager

from subtitle_sidecar.providers.base import (
    DownloadedSubtitle,
    ProviderSearchReport,
    SubtitleCandidate,
    SubtitleSearchRequest,
)

LANGUAGE_ALIASES = {
    "chi": "zh-cn",
    "chs": "zh-cn",
    "cht": "zh-hant",
    "zho": "zh-cn",
    "zh": "zh-cn",
    "zh-cn": "zh-cn",
    "zh-hans": "zh-cn",
    "zh-sg": "zh-cn",
    "zh-tw": "zh-hant",
    "zh-hant": "zh-hant",
    "zh-us": "zh-cn",
    "zht": "zh-hant",
}

BILINGUAL_MARKERS = ("bilingual", "dual", "双语", "简英", "繁英")

SIMPLIFIED_LANGUAGE_ALIASES = {
    "chi",
    "chs",
    "zho",
    "zh",
    "zh-cn",
    "zh-hans",
    "zh-sg",
    "zh-us",
}
TRADITIONAL_LANGUAGE_ALIASES = {
    "cht",
    "zh-tw",
    "zh-hant",
    "zht",
}


def _supported_provider_languages(provider: str, plugin: Any, languages: set[Any]) -> set[Any]:
    supported = set(plugin.check_languages(languages))
    if provider == "opensubtitlescom":
        # Subliminal 2.6's provider language set omits zho-CN even though its
        # converter and the live API both support the zh-cn code.
        supported.update(
            language
            for language in languages
            if getattr(language, "opensubtitlescom", None) == "zh-cn"
        )
    return supported


class ReportingProviderPool(ProviderPool):
    """ProviderPool variant that preserves compatibility checks but never swallows errors."""

    def list_subtitles_provider(self, provider: str, video: Any, languages: set[Any]):
        plugin = provider_manager[provider].plugin
        if not plugin.check(video):
            return []
        provider_languages = _supported_provider_languages(provider, plugin, languages)
        if not provider_languages:
            return []
        return self[provider].list_subtitles(video, provider_languages)


class SubliminalProvider:
    name = "subliminal"

    def __init__(
        self,
        client: Any | None = None,
        language_factory: Callable[[str], Any] | None = None,
        providers: list[str] | None = None,
        languages: list[str] | None = None,
        authentication: dict[str, dict[str, str]] | None = None,
        provider_manager_instance: Any | None = None,
    ) -> None:
        self._client = client
        self._uses_native_client = client is None
        self._language_factory = language_factory
        self._providers = list(providers or [])
        self._languages = list(languages or [])
        self._authentication = authentication or {}
        self._provider_manager = (
            provider_manager_instance if provider_manager_instance is not None else (
                None if client is not None else provider_manager
            )
        )
        self._reporter: Callable[[ProviderSearchReport], None] | None = None

    def search(self, request: SubtitleSearchRequest) -> list[SubtitleCandidate]:
        client = self._get_client()
        video = client.scan_video(str(request.video_path))
        languages = {
            client_language
            for language in (self._languages or request.fallback_languages)
            for client_language in self._to_client_languages(language)
        }
        candidates: list[SubtitleCandidate] = []
        for provider_name in self._providers:
            started_at = perf_counter()
            report_provider = f"subliminal:{provider_name}"
            search_context: dict[str, str | int] | None = None
            self._emit_report(ProviderSearchReport(provider=report_provider, status="started"))
            try:
                search_title, title_skip_reason = self._search_title_for_provider(request, provider_name)
                search_context = self._search_context(
                    request,
                    provider_name=provider_name,
                    search_title=search_title,
                )
                if title_skip_reason is not None:
                    self._emit_report(
                        ProviderSearchReport(
                            provider=report_provider,
                            status="skipped",
                            duration_ms=_duration_ms(started_at),
                            error=title_skip_reason,
                            reason=title_skip_reason,
                            search_context=search_context,
                        )
                    )
                    continue
                self._apply_request_metadata(video, request, search_title=search_title)
                missing_credentials = self._missing_credentials_reason(provider_name)
                if missing_credentials is not None:
                    self._emit_report(
                        ProviderSearchReport(
                            provider=report_provider,
                            status="skipped",
                            duration_ms=_duration_ms(started_at),
                            error=missing_credentials,
                            reason=missing_credentials,
                            search_context=search_context,
                        )
                    )
                    continue
                provider_languages, skip_reason = self._check_provider_support(
                    provider_name,
                    video,
                    languages,
                )
                if skip_reason is not None:
                    self._emit_report(
                        ProviderSearchReport(
                            provider=report_provider,
                            status="skipped",
                            duration_ms=_duration_ms(started_at),
                            error=skip_reason,
                            reason=skip_reason,
                            search_context=search_context,
                        )
                    )
                    continue
                subtitles_by_video = client.list_subtitles(
                    {video},
                    provider_languages,
                    providers=[provider_name],
                    provider_configs={provider_name: self._provider_config(provider_name)},
                    pool_class=ReportingProviderPool,
                )
                subtitles = self._subtitles_for_video(subtitles_by_video, video)
                provider_candidates = [
                    self._to_candidate(request, subtitle, report_provider)
                    for subtitle in subtitles
                ]
            except Exception as error:
                self._emit_report(
                    ProviderSearchReport(
                        provider=report_provider,
                        status="failed",
                        duration_ms=_duration_ms(started_at),
                        error=self._safe_error(error),
                        search_context=search_context,
                    )
                )
                continue
            candidates.extend(provider_candidates)
            self._emit_report(
                ProviderSearchReport(
                    provider=report_provider,
                    status="completed",
                    candidate_count=len(provider_candidates),
                    duration_ms=_duration_ms(started_at),
                    search_context=search_context,
                )
            )
        return candidates

    def set_reporter(self, reporter: Callable[[ProviderSearchReport], None]) -> None:
        self._reporter = reporter

    def _emit_report(self, report: ProviderSearchReport) -> None:
        if self._reporter is not None:
            self._reporter(report)

    def _provider_config(self, provider_name: str) -> dict[str, str]:
        credentials = self._authentication.get(provider_name, {})
        return {
            key: value
            for key, value in credentials.items()
            if key in {"username", "password", "apikey"} and value
        }

    def _missing_credentials_reason(self, provider_name: str) -> str | None:
        # Subliminal 2.6 authenticates OpenSubtitles.com searches through its
        # login endpoint even when an API key is configured.
        if self._client is not None or provider_name != "opensubtitlescom":
            return None
        credentials = self._authentication.get(provider_name, {})
        if credentials.get("username") and credentials.get("password"):
            return None
        return "missing_credentials"

    def _apply_request_metadata(
        self,
        video: Any,
        request: SubtitleSearchRequest,
        *,
        search_title: str,
    ) -> None:
        if request.media_type == "episode":
            setattr(video, "series", search_title)
            if request.season is not None:
                setattr(video, "season", request.season)
            if request.episode is not None:
                setattr(video, "episodes", [request.episode])
        else:
            setattr(video, "title", search_title)
        if request.year is not None:
            setattr(video, "year", request.year)
        if request.imdb_id:
            setattr(video, "imdb_id", request.imdb_id)

    def _search_title_for_provider(
        self,
        request: SubtitleSearchRequest,
        provider_name: str,
    ) -> tuple[str, str | None]:
        title = request.title.strip()
        if provider_name != "opensubtitlescom":
            return title, None
        alternative = (request.original_title or "").strip()
        if _search_token_length(alternative) >= 3:
            return alternative, None
        if _search_token_length(title) >= 3:
            return title, None
        # OpenSubtitles.com rejects two-character query values with HTTP 400.
        return title, "query_too_short"

    def _search_context(
        self,
        request: SubtitleSearchRequest,
        *,
        provider_name: str,
        search_title: str,
    ) -> dict[str, str | int]:
        original_title = (request.original_title or "").strip()
        title_source = (
            "original_title"
            if provider_name == "opensubtitlescom" and original_title == search_title
            else "title"
        )
        context: dict[str, str | int] = {
            "title": search_title,
            "title_source": title_source,
            "file_name": request.video_path.name,
            "media_type": request.media_type,
        }
        if request.year is not None:
            context["year"] = request.year
        if request.imdb_id:
            context["imdb_id"] = request.imdb_id
        if request.season is not None:
            context["season"] = request.season
        if request.episode is not None:
            context["episode"] = request.episode
        return context

    def _check_provider_support(
        self,
        provider_name: str,
        video: Any,
        languages: set[Any],
    ) -> tuple[set[Any], str | None]:
        if self._provider_manager is None:
            return languages, None
        plugin = self._provider_manager[provider_name].plugin
        if not plugin.check(video):
            return set(), "invalid_video"
        provider_languages = _supported_provider_languages(provider_name, plugin, languages)
        if not provider_languages:
            return set(), "unsupported_language"
        return provider_languages, None

    def _safe_error(self, error: Exception) -> str:
        message = str(error).strip() or error.__class__.__name__
        for credentials in self._authentication.values():
            for secret in (credentials.get("password"), credentials.get("apikey")):
                if secret:
                    message = message.replace(secret, "[redacted]")
        return message

    def download(self, candidate: SubtitleCandidate, target_dir: Path) -> DownloadedSubtitle:
        subtitle = candidate.raw_metadata.get("subtitle")
        if subtitle is None:
            raise ValueError("candidate is missing source subtitle metadata")
        internal_provider = str(candidate.raw_metadata.get("internal_provider") or "").strip()
        if not internal_provider:
            raise ValueError("candidate is missing internal provider metadata")
        provider_config = self._provider_config(internal_provider)
        if self._uses_native_client:
            self._download_with_strict_pool(subtitle, internal_provider, provider_config)
        else:
            self._get_client().download_subtitles(
                [subtitle],
                providers=[internal_provider],
                provider_configs={internal_provider: provider_config},
            )
        content = getattr(subtitle, "content", None)
        if content is None:
            raise ValueError(f"{internal_provider}_download_empty")

        target_dir.mkdir(parents=True, exist_ok=True)
        path = target_dir / f"downloaded.{candidate.format or self._format_for(subtitle)}"
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(str(content), encoding="utf-8")
        return DownloadedSubtitle(candidate=candidate, path=path)

    def _download_with_strict_pool(
        self,
        subtitle: Any,
        provider_name: str,
        provider_config: dict[str, str],
    ) -> None:
        """Download without ProviderPool swallowing authentication and API errors."""
        _ensure_subliminal_cache()
        with ReportingProviderPool(
            providers=[provider_name],
            provider_configs={provider_name: provider_config},
        ) as pool:
            provider = pool[provider_name]
            try:
                provider.download_subtitle(subtitle)
            except AuthenticationError:
                if provider_name != "opensubtitlescom":
                    raise
                reset_token = getattr(provider, "reset_token", None)
                if callable(reset_token):
                    reset_token()
                try:
                    del provider.token
                except AttributeError:
                    pass
                provider.download_subtitle(subtitle)

    def _get_client(self) -> Any:
        if self._client is None:
            module = import_module("subliminal")
            self._client = module
        return self._client

    def _to_client_languages(self, language: str) -> set[Any]:
        normalized = str(language).strip().lower().replace("_", "-")
        if normalized in SIMPLIFIED_LANGUAGE_ALIASES:
            # ``zh-US`` is converted by Babelfish to OpenSubtitles.com's
            # legacy ``ze`` code, which that API rejects. ``zh-CN`` produces
            # the documented ``zh-cn`` code while plain ``zh`` remains useful
            # to the older OpenSubtitles provider.
            ietf_languages = ("zh", "zh-CN")
        elif normalized in TRADITIONAL_LANGUAGE_ALIASES:
            ietf_languages = ("zh-Hant", "zh-TW")
        else:
            ietf_languages = (language,)
        if self._language_factory is not None:
            return {self._language_factory(value) for value in ietf_languages}

        babelfish = import_module("babelfish")
        return {babelfish.Language.fromietf(value) for value in ietf_languages}

    def _subtitles_for_video(self, subtitles_by_video: Any, video: Any) -> list[Any]:
        if isinstance(subtitles_by_video, dict):
            return list(subtitles_by_video.get(video, []))
        return list(subtitles_by_video)

    def _to_candidate(
        self,
        request: SubtitleSearchRequest,
        subtitle: Any,
        provider_name: str,
    ) -> SubtitleCandidate:
        metadata = self._metadata_for(subtitle)
        title = self._title_for(subtitle, request.title)
        source_url = _optional_text(getattr(subtitle, "page_link", ""))
        release_info = _optional_text(getattr(subtitle, "release_info", "")) or title
        return SubtitleCandidate(
            provider=provider_name,
            language=self._normalize_language(getattr(subtitle, "language", "")),
            is_bilingual=self._is_bilingual(title, metadata),
            format=self._format_for(subtitle),
            title=title,
            source_url=source_url,
            release_info=release_info,
            confidence=self._confidence_for(subtitle),
            raw_metadata={
                "subtitle": subtitle,
                "metadata": metadata,
                "internal_provider": provider_name.removeprefix("subliminal:"),
            },
        )

    def _metadata_for(self, subtitle: Any) -> dict[str, Any]:
        metadata = getattr(subtitle, "metadata", {})
        if isinstance(metadata, dict):
            return metadata
        return {}

    def _title_for(self, subtitle: Any, fallback: str) -> str:
        for attribute in ("title", "release_info", "page_link"):
            value = getattr(subtitle, attribute, "")
            if value:
                return str(value)
        content = getattr(subtitle, "content", "")
        if isinstance(content, str) and content:
            return content
        return fallback

    def _format_for(self, subtitle: Any) -> str:
        for attribute in ("subtitle_format", "format"):
            value = getattr(subtitle, attribute, "")
            if value:
                return str(value).lower().lstrip(".")
        candidate_path = str(getattr(subtitle, "page_link", ""))
        suffix = Path(candidate_path).suffix.lower().lstrip(".")
        return suffix or "srt"

    def _confidence_for(self, subtitle: Any) -> float:
        score = getattr(subtitle, "score", None)
        if isinstance(score, (int, float)):
            return float(score)
        return 1.0

    def _normalize_language(self, language: Any) -> str:
        normalized = str(language).strip().lower().replace("_", "-")
        return LANGUAGE_ALIASES.get(normalized, normalized)

    def _is_bilingual(self, title: str, metadata: dict[str, Any]) -> bool:
        haystacks = [title.lower()]
        haystacks.extend(str(value).lower() for value in metadata.values())
        return any(marker in haystack for haystack in haystacks for marker in BILINGUAL_MARKERS)


def _duration_ms(started_at: float) -> int:
    return int((perf_counter() - started_at) * 1000)


def _optional_text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _ensure_subliminal_cache() -> None:
    """Configure Subliminal's otherwise-unconfigured token cache on library use."""
    cache_module = import_module("subliminal.cache")
    try:
        cache_module.region.get("subtitle-sidecar:cache-probe")
    except RegionNotConfigured:
        cache_module.region.configure("dogpile.cache.memory")


def _search_token_length(value: str) -> int:
    return sum(character.isalnum() for character in value)
