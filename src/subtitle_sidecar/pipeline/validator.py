from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re

SUPPORTED_EXTENSIONS = {".srt", ".ass", ".ssa"}
CHINESE_CODEPOINT_RANGES = (
    (0x3400, 0x4DBF),
    (0x4E00, 0x9FFF),
    (0xF900, 0xFAFF),
)


@dataclass(frozen=True)
class ValidationResult:
    is_valid: bool
    has_chinese: bool
    reason: str | None = None
    encoding: str | None = None
    cue_count: int = 0
    duration_seconds: float | None = None


def validate_subtitle_file(
    path: Path,
    *,
    video_duration_seconds: float | None = None,
) -> ValidationResult:
    if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
        return ValidationResult(is_valid=False, has_chinese=False, reason="unsupported_extension")

    content, encoding = _read_subtitle_text(path)
    if content is None:
        return ValidationResult(is_valid=False, has_chinese=False, reason="decode_error")

    if not content.strip():
        return ValidationResult(is_valid=False, has_chinese=False, reason="empty_file")

    has_chinese = _contains_chinese(content)
    if not has_chinese:
        return ValidationResult(
            is_valid=False,
            has_chinese=False,
            reason="missing_chinese",
            encoding=encoding,
        )

    cue_count, duration_seconds = _subtitle_timing_metrics(path.suffix.lower(), content)
    if path.suffix.lower() == ".srt" and cue_count == 0:
        return ValidationResult(
            is_valid=False,
            has_chinese=True,
            reason="missing_timestamps",
            encoding=encoding,
        )

    if path.suffix.lower() in {".ass", ".ssa"} and "[events]" not in content.casefold():
        return ValidationResult(
            is_valid=False,
            has_chinese=True,
            reason="missing_events_section",
            encoding=encoding,
        )

    if path.suffix.lower() in {".ass", ".ssa"} and cue_count == 0:
        return ValidationResult(
            is_valid=False,
            has_chinese=True,
            reason="missing_dialogue_lines",
            encoding=encoding,
        )

    if duration_seconds is not None and video_duration_seconds is not None:
        allowed_overrun = max(300.0, video_duration_seconds * 0.08)
        if duration_seconds > video_duration_seconds + allowed_overrun:
            return ValidationResult(
                is_valid=False,
                has_chinese=True,
                reason="timeline_exceeds_video",
                encoding=encoding,
                cue_count=cue_count,
                duration_seconds=duration_seconds,
            )
        if video_duration_seconds >= 40 * 60 and duration_seconds < video_duration_seconds * 0.5:
            return ValidationResult(
                is_valid=False,
                has_chinese=True,
                reason="timeline_too_short",
                encoding=encoding,
                cue_count=cue_count,
                duration_seconds=duration_seconds,
            )

    return ValidationResult(
        is_valid=True,
        has_chinese=True,
        encoding=encoding,
        cue_count=cue_count,
        duration_seconds=duration_seconds,
    )


def _read_subtitle_text(path: Path) -> tuple[str | None, str | None]:
    try:
        payload = path.read_bytes()
    except OSError:
        return None, None
    encodings: list[str] = []
    if payload.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
        encodings.append("utf-32")
    elif payload.startswith((b"\xff\xfe", b"\xfe\xff")):
        encodings.append("utf-16")
    encodings.extend(("utf-8-sig", "utf-8", "gb18030", "big5"))
    for encoding in encodings:
        try:
            return payload.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    return None, None


def _subtitle_timing_metrics(extension: str, content: str) -> tuple[int, float | None]:
    if extension == ".srt":
        ranges = [
            (_parse_srt_time(match.group("start")), _parse_srt_time(match.group("end")))
            for match in re.finditer(
                r"(?P<start>\d{1,2}:\d{2}:\d{2}[,.]\d{1,3})\s*-->\s*"
                r"(?P<end>\d{1,2}:\d{2}:\d{2}[,.]\d{1,3})",
                content,
            )
        ]
    else:
        ranges = [
            (_parse_ass_time(match.group("start")), _parse_ass_time(match.group("end")))
            for match in re.finditer(
                r"^Dialogue:[^,]*,(?P<start>\d+:\d{2}:\d{2}[.]\d{1,2}),"
                r"(?P<end>\d+:\d{2}:\d{2}[.]\d{1,2}),",
                content,
                flags=re.MULTILINE | re.IGNORECASE,
            )
        ]
    valid_ranges = [(start, end) for start, end in ranges if start is not None and end is not None and end > start]
    return len(valid_ranges), max((end for _, end in valid_ranges), default=None)


def _parse_srt_time(value: str) -> float | None:
    match = re.fullmatch(r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})", value)
    if match is None:
        return None
    hours, minutes, seconds, milliseconds = (int(part) for part in match.groups())
    return hours * 3600 + minutes * 60 + seconds + milliseconds / (10 ** len(match.group(4)))


def _parse_ass_time(value: str) -> float | None:
    match = re.fullmatch(r"(\d+):(\d{2}):(\d{2})[.](\d{1,2})", value)
    if match is None:
        return None
    hours, minutes, seconds, centiseconds = (int(part) for part in match.groups())
    return hours * 3600 + minutes * 60 + seconds + centiseconds / (10 ** len(match.group(4)))


def _contains_chinese(content: str) -> bool:
    for character in content:
        codepoint = ord(character)
        for lower, upper in CHINESE_CODEPOINT_RANGES:
            if lower <= codepoint <= upper:
                return True
    return False
