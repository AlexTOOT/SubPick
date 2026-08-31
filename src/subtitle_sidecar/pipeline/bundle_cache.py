from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
import re
import shutil
from time import time
from typing import Any

from subtitle_sidecar.providers.base import (
    DownloadedSubtitle,
    DownloadedSubtitleMember,
    SubtitleCandidate,
    SubtitleSearchRequest,
)


CACHE_VERSION = 2
# Season packs are immutable downloaded content.  By default they remain reusable
# as long as the cached member still exists and passes the normal downstream checks.
CACHE_TTL_SECONDS: int | None = None
SUPPORTED_EXTENSIONS = {".srt", ".ass", ".ssa", ".vtt", ".sub"}


@dataclass(frozen=True)
class CachedSubtitle:
    candidate: SubtitleCandidate
    source_task_id: int | None


class EpisodeBundleCache:
    """Small filesystem cache for exact episode members from multi-file subtitle packs."""

    def __init__(self, data_dir: Path, *, ttl_seconds: int | None = CACHE_TTL_SECONDS) -> None:
        self.root = Path(data_dir) / "subtitle-bundles"
        self.ttl_seconds = ttl_seconds

    def find(self, request: SubtitleSearchRequest) -> CachedSubtitle | None:
        if request.season is None or request.episode is None:
            return None
        manifest_path = self._manifest_path(request)
        manifest = self._load_manifest(manifest_path)
        if manifest is None:
            return None
        now = time()
        entries = manifest.get("entries") or []
        for entry in reversed(entries):
            if not isinstance(entry, dict) or entry.get("episode") != request.episode:
                continue
            if (
                self.ttl_seconds is not None
                and now - float(entry.get("cached_at") or 0) > self.ttl_seconds
            ):
                continue
            relative_path = entry.get("path")
            if not isinstance(relative_path, str):
                continue
            path = self.root / relative_path
            if not path.is_file():
                continue
            source = entry.get("source") or {}
            if not isinstance(source, dict):
                continue
            candidate = SubtitleCandidate(
                provider=str(source.get("provider") or "bundle-cache"),
                language=str(source.get("language") or "zh-cn"),
                is_bilingual=bool(source.get("is_bilingual")),
                format=path.suffix.lstrip(".") or "srt",
                title=str(source.get("title") or path.name),
                source_url=str(source.get("source_url") or ""),
                release_info=str(source.get("release_info") or ""),
                confidence=float(source.get("confidence") or 0.75),
                raw_metadata={
                    "bundle_cache_path": str(path),
                    "bundle_reused": True,
                    "bundle_source_task_id": entry.get("source_task_id"),
                    "bundle_member_name": path.name,
                },
            )
            return CachedSubtitle(
                candidate=candidate, source_task_id=_as_int(entry.get("source_task_id"))
            )
        return None

    def store(
        self,
        request: SubtitleSearchRequest,
        downloaded: DownloadedSubtitle,
        *,
        source_task_id: int,
    ) -> int:
        if request.season is None or request.episode is None or len(downloaded.files) < 2:
            return 0
        members_by_episode: dict[int, list[DownloadedSubtitleMember]] = {}
        for member in downloaded.files:
            episode = episode_from_filename(member.filename, request.season)
            if episode is not None:
                members_by_episode.setdefault(episode, []).append(member)
        if not members_by_episode:
            return 0
        matches = {
            episode: _preferred_episode_member(members, downloaded.candidate.language)
            for episode, members in members_by_episode.items()
        }

        manifest_path = self._manifest_path(request)
        manifest = self._load_manifest(manifest_path) or {"version": CACHE_VERSION, "entries": []}
        source_key = sha256(
            f"{downloaded.candidate.provider}|{downloaded.candidate.source_url}|{source_task_id}".encode()
        ).hexdigest()[:16]
        destination_dir = manifest_path.parent / source_key
        destination_dir.mkdir(parents=True, exist_ok=True)
        source = {
            "provider": downloaded.candidate.provider,
            "language": downloaded.candidate.language,
            "is_bilingual": downloaded.candidate.is_bilingual,
            "title": downloaded.candidate.title,
            "source_url": downloaded.candidate.source_url,
            "release_info": downloaded.candidate.release_info,
            "confidence": downloaded.candidate.confidence,
        }
        entries = [entry for entry in manifest.get("entries", []) if isinstance(entry, dict)]
        stored = 0
        for episode, member in sorted(matches.items()):
            if not member.path.is_file():
                continue
            destination = destination_dir / _safe_filename(member.filename)
            shutil.copy2(member.path, destination)
            relative_path = str(destination.relative_to(self.root)).replace("\\", "/")
            entries = [entry for entry in entries if entry.get("episode") != episode]
            entries.append(
                {
                    "episode": episode,
                    "path": relative_path,
                    "cached_at": time(),
                    "source_task_id": source_task_id,
                    "source": source,
                }
            )
            stored += 1
        manifest["version"] = CACHE_VERSION
        manifest["series_identity"] = _series_identity(request)
        manifest["season"] = request.season
        manifest["entries"] = entries
        self._write_manifest(manifest_path, manifest)
        return stored

    def materialize(self, candidate: SubtitleCandidate, target_dir: Path) -> DownloadedSubtitle:
        value = candidate.raw_metadata.get("bundle_cache_path")
        source = Path(str(value or ""))
        if not source.is_file() or source.suffix.lower() not in SUPPORTED_EXTENSIONS:
            raise FileNotFoundError("bundle_cache_member_missing")
        target_dir.mkdir(parents=True, exist_ok=True)
        destination = target_dir / f"cached-{_safe_filename(source.name)}"
        shutil.copy2(source, destination)
        member = DownloadedSubtitleMember(path=destination, filename=source.name)
        return DownloadedSubtitle(candidate=candidate, path=destination, members=(member,))

    def _manifest_path(self, request: SubtitleSearchRequest) -> Path:
        # Jellyfin assigns TMDb IDs to individual episodes.  A season bundle
        # must therefore be keyed by its series identifier first; otherwise
        # every episode receives an isolated cache manifest.
        identity = _series_identity(request)
        digest = sha256(f"{identity}|s{request.season}".encode()).hexdigest()[:20]
        return self.root / digest / "manifest.json"

    def _load_manifest(self, path: Path) -> dict[str, Any] | None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return payload if isinstance(payload, dict) else None

    def _write_manifest(self, path: Path, manifest: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(manifest, ensure_ascii=False, sort_keys=True), encoding="utf-8"
        )
        temporary.replace(path)


def select_episode_member(
    downloaded: DownloadedSubtitle,
    *,
    season: int | None,
    episode: int | None,
) -> DownloadedSubtitle:
    if season is None or episode is None or len(downloaded.files) == 1:
        return downloaded
    matches = [
        member
        for member in downloaded.files
        if episode_from_filename(member.filename, season) == episode
    ]
    if not matches:
        raise ValueError("bundle_missing_target_episode")
    selected = _preferred_episode_member(matches, downloaded.candidate.language)
    return DownloadedSubtitle(
        candidate=downloaded.candidate, path=selected.path, members=downloaded.files
    )


def episode_from_filename(filename: str, season: int | None = None) -> int | None:
    text = Path(filename).stem.casefold()
    patterns = [
        r"s(?P<season>\d{1,2})[ ._-]*e(?P<episode>\d{1,3})",
        r"(?P<season>\d{1,2})x(?P<episode>\d{1,3})",
        r"第\s*(?P<episode>\d{1,3})\s*[集话]",
        # Common in season packs: "Show 01 [1080p]" or "[Show][01].ass".
        # Strict token boundaries avoid matching 1920, 1080p, years, or S01.
        r"(?<![a-z0-9])(?P<episode>0?[1-9]|[12]\d|3[0-9])(?=$|[\s._\-\]\)])",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match is None:
            continue
        matched_season = _as_int(match.groupdict().get("season"))
        if season is not None and matched_season is not None and matched_season != season:
            continue
        return _as_int(match.group("episode"))
    return None


def _normalize_title(value: str) -> str:
    return "".join(character for character in value.casefold() if character.isalnum()) or "unknown"


def _series_identity(request: SubtitleSearchRequest) -> str:
    return request.series_id or request.tmdb_id or _normalize_title(request.title)


def _safe_filename(value: str) -> str:
    name = Path(value).name
    return name if name and name != "." else "subtitle.srt"


def _preferred_episode_member(
    members: list[DownloadedSubtitleMember],
    language: str,
) -> DownloadedSubtitleMember:
    """Prefer the matching Chinese variant when a pack has both scripts for one episode."""
    preferred_markers = (
        ("繁体", "繁中", "traditional", "cht")
        if str(language).casefold() in {"zh-hant", "zh-tw"}
        else ("简体", "简中", "simplified", "chs")
    )
    for member in members:
        filename = member.filename.casefold()
        if any(marker in filename for marker in preferred_markers):
            return member
    return members[0]


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
