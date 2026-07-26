from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from subtitle_sidecar.media.subtitles import detect_external_subtitles


CHINESE_LANGUAGE_CODES = {
    "chi",
    "zho",
    "zh",
    "chs",
    "cht",
    "zh-cn",
    "zh-hans",
    "zh-tw",
    "zh-hant",
}


@dataclass(frozen=True)
class SubtitleStatus:
    status: str
    has_external_chinese: bool
    has_embedded_chinese: bool
    has_bilingual: bool


def detect_subtitle_status(path: Path, media_streams: list[dict[str, Any]]) -> SubtitleStatus:
    if not path.exists():
        stream_flags = detect_stream_subtitle_flags(media_streams)
        return SubtitleStatus(
            status="has_chinese" if stream_flags.has_any_chinese else "path_missing",
            has_external_chinese=stream_flags.has_external_chinese,
            has_embedded_chinese=stream_flags.has_embedded_chinese,
            has_bilingual=stream_flags.has_bilingual,
        )

    external = detect_external_subtitles(path)
    stream_flags = detect_stream_subtitle_flags(media_streams)
    has_external = external.has_chinese or stream_flags.has_external_chinese
    has_embedded = stream_flags.has_embedded_chinese
    has_bilingual = external.has_bilingual or stream_flags.has_bilingual
    return SubtitleStatus(
        status="has_chinese" if has_external or has_embedded else "missing",
        has_external_chinese=has_external,
        has_embedded_chinese=has_embedded,
        has_bilingual=has_bilingual,
    )


@dataclass(frozen=True)
class StreamSubtitleFlags:
    has_external_chinese: bool
    has_embedded_chinese: bool
    has_bilingual: bool

    @property
    def has_any_chinese(self) -> bool:
        return self.has_external_chinese or self.has_embedded_chinese


def detect_stream_subtitle_flags(media_streams: list[dict[str, Any]]) -> StreamSubtitleFlags:
    has_external = False
    has_embedded = False
    has_bilingual = False
    for stream in media_streams:
        if str(stream.get("Type") or "").lower() != "subtitle":
            continue
        if not _is_chinese_stream(stream):
            continue
        if bool(stream.get("IsExternal")):
            has_external = True
        else:
            has_embedded = True
        if _is_bilingual_stream(stream):
            has_bilingual = True
    return StreamSubtitleFlags(
        has_external_chinese=has_external,
        has_embedded_chinese=has_embedded,
        has_bilingual=has_bilingual,
    )


def _is_chinese_stream(stream: dict[str, Any]) -> bool:
    language = str(stream.get("Language") or "").lower()
    if language in CHINESE_LANGUAGE_CODES:
        return True
    text = _stream_text(stream)
    return any(marker in text for marker in ("中文", "简体", "繁体", "chinese", "chi "))


def _is_bilingual_stream(stream: dict[str, Any]) -> bool:
    text = _stream_text(stream)
    return any(marker in text for marker in ("双语", "中英", "简英", "繁英", "bilingual"))


def _stream_text(stream: dict[str, Any]) -> str:
    return " ".join(
        str(stream.get(key) or "")
        for key in ("DisplayTitle", "Title", "Language")
    ).lower()
