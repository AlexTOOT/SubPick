from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Mapping
from typing import Any, Protocol


PROVIDER_ADAPTER_API_VERSION = 1


@dataclass(frozen=True)
class SubtitleSearchRequest:
    video_path: Path
    title: str
    year: int | None
    media_type: str
    season: int | None
    episode: int | None
    preferred: str
    fallback_languages: list[str]
    imdb_id: str | None = None
    tmdb_id: str | None = None
    original_title: str | None = None
    series_id: str | None = None
    alternate_years: tuple[int, ...] = ()


@dataclass(frozen=True)
class SubtitleCandidate:
    provider: str
    language: str
    is_bilingual: bool
    format: str
    title: str
    source_url: str
    release_info: str
    confidence: float
    raw_metadata: dict[str, Any] = field(default_factory=dict)
    provider_quality: float | None = None


@dataclass(frozen=True)
class DownloadedSubtitleMember:
    path: Path
    filename: str


@dataclass(frozen=True)
class DownloadedSubtitle:
    candidate: SubtitleCandidate
    path: Path
    members: tuple[DownloadedSubtitleMember, ...] = ()

    @property
    def files(self) -> tuple[DownloadedSubtitleMember, ...]:
        """Return every usable member, including legacy single-file downloads."""
        return self.members or (DownloadedSubtitleMember(self.path, self.path.name),)


@dataclass(frozen=True)
class ProviderSearchReport:
    provider: str
    status: str
    candidate_count: int | None = None
    duration_ms: int | None = None
    error: str | None = None
    reason: str | None = None
    search_context: Mapping[str, str | int] | None = None


class SubtitleProvider(Protocol):
    name: str

    def search(self, request: SubtitleSearchRequest) -> list[SubtitleCandidate]:
        ...

    def download(self, candidate: SubtitleCandidate, target_dir: Path) -> DownloadedSubtitle:
        ...


@dataclass(frozen=True)
class ProviderAdapterMetadata:
    """Public metadata supplied by a bundled or externally installed adapter."""

    name: str
    display_name: str
    version: str
    homepage: str = ""
    attribution: str = ""
    media_scopes: tuple[str, ...] = ()
    lookup_keys: tuple[str, ...] = ()
    transport: str = ""
    requires_auth: bool = False
    requires_captcha: bool = False
    supports_archives: bool = False
    recommended_interval_seconds: float = 60.0
    stable_candidate_identity: bool = False


class ProviderAdapterFactory(Protocol):
    """Stable factory contract for adapters distributed outside the core project.

    External packages register a factory object in the
    ``subtitle_sidecar.providers`` Python entry-point group. The factory receives
    only its own persisted configuration mapping and returns a SubtitleProvider.
    """

    metadata: ProviderAdapterMetadata

    def create(self, settings: Mapping[str, Any]) -> SubtitleProvider:
        ...
