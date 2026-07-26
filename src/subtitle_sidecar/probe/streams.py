import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any


CHINESE_LANGUAGE_MARKERS = {"chi", "zho", "zh", "chs", "cht", "cn"}
CHINESE_TITLE_MARKERS = (
    "chinese",
    "中文",
    "简体",
    "繁体",
    "字幕",
    "中英",
    "简英",
    "繁英",
    "双语",
)
BILINGUAL_TITLE_MARKERS = ("bilingual", "双语", "中英", "简英", "繁英")
ENGLISH_TITLE_MARKERS = ("english", "eng", "英文")


@dataclass(frozen=True)
class EmbeddedSubtitleStream:
    language: str
    codec_name: str
    title: str
    is_default: bool
    is_forced: bool
    has_chinese: bool
    is_bilingual: bool


@dataclass(frozen=True)
class EmbeddedSubtitleResult:
    has_chinese: bool
    has_bilingual: bool
    streams: list[EmbeddedSubtitleStream]


def parse_subtitle_streams(payload: dict[str, Any]) -> EmbeddedSubtitleResult:
    parsed_streams: list[EmbeddedSubtitleStream] = []

    for stream in payload.get("streams", []):
        if stream.get("codec_type") != "subtitle":
            continue

        tags = stream.get("tags") or {}
        disposition = stream.get("disposition") or {}
        language = str(tags.get("language") or "").lower()
        title = str(tags.get("title") or "")
        has_chinese = _language_has_chinese(language) or _title_has_chinese(title)
        is_bilingual = has_chinese and _title_is_bilingual(title)
        parsed_streams.append(
            EmbeddedSubtitleStream(
                language=language,
                codec_name=str(stream.get("codec_name") or ""),
                title=title,
                is_default=bool(disposition.get("default")),
                is_forced=bool(disposition.get("forced")),
                has_chinese=has_chinese,
                is_bilingual=is_bilingual,
            )
        )

    return EmbeddedSubtitleResult(
        has_chinese=any(stream.has_chinese for stream in parsed_streams),
        has_bilingual=any(stream.is_bilingual for stream in parsed_streams),
        streams=parsed_streams,
    )


def probe_video_streams(
    video_path: Path,
    ffprobe_path: str = "ffprobe",
    runner: Callable[..., Any] = subprocess.run,
) -> EmbeddedSubtitleResult:
    command = [ffprobe_path, "-v", "quiet", "-print_format", "json", "-show_streams", str(video_path)]
    completed = runner(command, check=False, capture_output=True, text=True)

    if getattr(completed, "returncode", 1) != 0:
        return EmbeddedSubtitleResult(has_chinese=False, has_bilingual=False, streams=[])

    try:
        payload = json.loads(getattr(completed, "stdout", ""))
    except json.JSONDecodeError:
        return EmbeddedSubtitleResult(has_chinese=False, has_bilingual=False, streams=[])

    return parse_subtitle_streams(payload)


def probe_video_duration_seconds(
    video_path: Path,
    ffprobe_path: str = "ffprobe",
    runner: Callable[..., Any] = subprocess.run,
) -> float | None:
    """Return the container duration when ffprobe can provide a sane value."""
    command = [
        ffprobe_path,
        "-v",
        "quiet",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(video_path),
    ]
    try:
        completed = runner(command, check=False, capture_output=True, text=True)
    except OSError:
        return None
    if getattr(completed, "returncode", 1) != 0:
        return None
    try:
        value = float(str(getattr(completed, "stdout", "")).strip())
    except ValueError:
        return None
    return value if value > 0 else None


def _language_has_chinese(language: str) -> bool:
    normalized = language.strip().lower()
    return normalized in CHINESE_LANGUAGE_MARKERS or normalized.startswith("zh")


def _title_has_chinese(title: str) -> bool:
    lowered = title.lower()
    if any(marker in lowered for marker in CHINESE_TITLE_MARKERS):
        return True
    return any("\u4e00" <= character <= "\u9fff" for character in title)


def _title_is_bilingual(title: str) -> bool:
    lowered = title.lower()
    if any(marker in lowered for marker in BILINGUAL_TITLE_MARKERS):
        return True
    return _title_has_chinese(title) and any(marker in lowered for marker in ENGLISH_TITLE_MARKERS)
