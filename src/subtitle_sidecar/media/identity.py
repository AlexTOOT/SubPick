from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import re
from collections.abc import Iterable
from typing import Any, Literal


_YEAR_PATTERN = re.compile(r"(?<!\d)(?:18|19|20)\d{2}(?!\d|[pi])", re.IGNORECASE)


@dataclass(frozen=True)
class ReleaseYearEvidence:
    years: frozenset[int]
    matching_years: frozenset[int]

    @property
    def has_conflict(self) -> bool:
        return bool(self.years) and not self.matching_years


@dataclass(frozen=True)
class MediaIdentity:
    """Provider-facing media identity resolved from MoviePilot NFO files."""

    media_type: Literal["movie", "episode"]
    title: str
    original_title: str | None
    year: int
    season: int | None = None
    episode: int | None = None
    imdb_id: str | None = None
    tmdb_id: str | None = None
    tvdb_id: str | None = None
    episode_title: str | None = None
    alternate_years: tuple[int, ...] = ()
    nfo_paths: tuple[Path, ...] = ()

    @property
    def series_id(self) -> str | None:
        if self.media_type != "episode":
            return None
        for provider, value in (
            ("tmdb", self.tmdb_id),
            ("tvdb", self.tvdb_id),
            ("imdb", self.imdb_id),
        ):
            if value:
                return f"{provider}:{value}"
        return None

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "source": "nfo",
            "media_type": self.media_type,
            "title": self.title,
            "original_title": self.original_title,
            "year": self.year,
            "season": self.season,
            "episode": self.episode,
            "episode_title": self.episode_title,
            "provider_ids": {
                key: value
                for key, value in (
                    ("imdb", self.imdb_id),
                    ("tmdb", self.tmdb_id),
                    ("tvdb", self.tvdb_id),
                )
                if value
            },
            "series_id": self.series_id,
            "alternate_years": list(self.alternate_years),
            "nfo_paths": [str(path) for path in self.nfo_paths],
        }

    @classmethod
    def from_payload(cls, payload: Any) -> MediaIdentity | None:
        if not isinstance(payload, dict):
            return None
        if payload.get("schema_version") != 1 or payload.get("source") != "nfo":
            return None
        media_type = payload.get("media_type")
        title = payload.get("title")
        year = payload.get("year")
        if media_type not in {"movie", "episode"} or not isinstance(title, str):
            return None
        if isinstance(year, bool) or not isinstance(year, int):
            return None
        provider_ids = payload.get("provider_ids")
        provider_ids = provider_ids if isinstance(provider_ids, dict) else {}
        alternate_years = payload.get("alternate_years")
        nfo_paths = payload.get("nfo_paths")
        return cls(
            media_type=media_type,
            title=title,
            original_title=_optional_string(payload.get("original_title")),
            year=year,
            season=_optional_int(payload.get("season")),
            episode=_optional_int(payload.get("episode")),
            imdb_id=_optional_string(provider_ids.get("imdb")),
            tmdb_id=_optional_string(provider_ids.get("tmdb")),
            tvdb_id=_optional_string(provider_ids.get("tvdb")),
            episode_title=_optional_string(payload.get("episode_title")),
            alternate_years=tuple(
                value
                for value in (alternate_years if isinstance(alternate_years, list) else [])
                if isinstance(value, int) and not isinstance(value, bool)
            ),
            nfo_paths=tuple(
                Path(value)
                for value in (nfo_paths if isinstance(nfo_paths, list) else [])
                if isinstance(value, str) and value
            ),
        )


def _optional_string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def analyze_release_years(
    values: Iterable[str | None],
    *,
    expected_year: int | None,
    expected_titles: Iterable[str | None] = (),
    tolerance: int = 1,
    current_year: int | None = None,
) -> ReleaseYearEvidence:
    """Extract plausible release years while ignoring numbers that belong to the title."""
    if expected_year is None:
        return ReleaseYearEvidence(frozenset(), frozenset())

    now_year = current_year or datetime.now(timezone.utc).year
    upper_bound = max(now_year + 1, expected_year + tolerance)
    title_numbers = {
        int(match.group(0))
        for title in expected_titles
        for match in _YEAR_PATTERN.finditer(str(title or ""))
    }
    years = {
        year
        for value in values
        for match in _YEAR_PATTERN.finditer(str(value or ""))
        if (year := int(match.group(0))) <= upper_bound and year not in title_numbers
    }
    matching = {year for year in years if abs(year - expected_year) <= tolerance}
    return ReleaseYearEvidence(frozenset(years), frozenset(matching))
