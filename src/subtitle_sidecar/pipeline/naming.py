from __future__ import annotations

from pathlib import Path


def build_subtitle_path(
    video_path: Path,
    language: str,
    extension: str,
    default: bool,
) -> Path:
    normalized_language = _normalize_language(language)
    normalized_extension = extension.lower().lstrip(".")
    suffixes = [normalized_language]
    if default:
        suffixes.append("default")
    suffix = ".".join(suffixes)
    return video_path.with_name(f"{video_path.stem}.{suffix}.{normalized_extension}")


def _normalize_language(language: str) -> str:
    normalized = language.strip().lower().replace("_", "-")
    if "." in normalized:
        normalized = normalized.split(".", 1)[0]
    return normalized
