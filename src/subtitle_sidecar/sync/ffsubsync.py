from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
import json
import math
from pathlib import Path
import re
import subprocess
import time
from typing import Any


@dataclass(frozen=True)
class AudioReferenceStream:
    ordinal: int
    stream_index: int
    codec_name: str
    channels: int
    language: str
    title: str
    is_default: bool
    is_commentary: bool

    @property
    def reference_spec(self) -> str:
        return f"0:a:{self.ordinal}"


@dataclass(frozen=True)
class SyncResult:
    success: bool
    output_path: Path
    stdout: str
    stderr: str
    reason: str | None = None
    score: float | None = None
    returncode: int | None = None
    reference_stream: str | None = None
    attempted_reference_streams: tuple[str, ...] = ()


def parse_audio_reference_streams(payload: dict[str, Any]) -> tuple[AudioReferenceStream, ...]:
    streams: list[AudioReferenceStream] = []
    for ordinal, stream in enumerate(payload.get("streams") or []):
        if stream.get("codec_type") not in (None, "audio"):
            continue
        codec_name = str(stream.get("codec_name") or "").strip().lower()
        try:
            stream_index = int(stream.get("index"))
            channels = int(stream.get("channels") or 0)
        except (TypeError, ValueError):
            continue
        if not codec_name or channels <= 0:
            continue

        tags = stream.get("tags") or {}
        disposition = stream.get("disposition") or {}
        title = str(tags.get("title") or "")
        lowered_title = title.casefold()
        is_commentary = bool(
            disposition.get("comment")
            or disposition.get("descriptions")
            or disposition.get("visual_impaired")
            or disposition.get("hearing_impaired")
            or "commentary" in lowered_title
            or "director comment" in lowered_title
            or "解说" in title
            or "评论音轨" in title
        )
        streams.append(
            AudioReferenceStream(
                ordinal=ordinal,
                stream_index=stream_index,
                codec_name=codec_name,
                channels=channels,
                language=str(tags.get("language") or "").strip().lower(),
                title=title,
                is_default=bool(disposition.get("default")),
                is_commentary=is_commentary,
            )
        )

    return tuple(
        sorted(
            streams,
            key=lambda item: (
                item.is_commentary,
                not item.is_default,
                item.ordinal,
            ),
        )
    )


def probe_audio_reference_streams(
    video_path: Path,
    ffprobe_path: str = "ffprobe",
    runner: Callable[..., Any] = subprocess.run,
    timeout_seconds: int = 30,
) -> tuple[AudioReferenceStream, ...]:
    command = [
        ffprobe_path,
        "-v",
        "quiet",
        "-select_streams",
        "a",
        "-show_entries",
        (
            "stream=index,codec_type,codec_name,channels:"
            "stream_tags=language,title:"
            "stream_disposition=default,comment,descriptions,visual_impaired,hearing_impaired"
        ),
        "-of",
        "json",
        str(video_path),
    ]
    try:
        completed = runner(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.SubprocessError):
        return ()
    if getattr(completed, "returncode", 1) != 0:
        return ()
    try:
        payload = json.loads(str(getattr(completed, "stdout", "")))
    except (json.JSONDecodeError, TypeError):
        return ()
    if not isinstance(payload, dict):
        return ()
    return parse_audio_reference_streams(payload)


def build_ffsubsync_command(
    video_path: Path,
    subtitle_path: Path,
    output_path: Path,
    executable: str = "ffsubsync",
    reference_stream: str | None = None,
) -> list[str]:
    command = [
        executable,
        str(video_path),
        "-i",
        str(subtitle_path),
        "-o",
        str(output_path),
    ]
    if reference_stream:
        command.extend(["--reference-stream", reference_stream])
    command.extend(["--skip-intro-outro", "--skip-sync-on-low-quality"])
    return command


def sync_subtitle(
    video_path: Path,
    subtitle_path: Path,
    output_path: Path,
    executable: str = "ffsubsync",
    timeout_seconds: int = 900,
    ffprobe_path: str = "ffprobe",
    audio_stream_prober: Callable[..., Sequence[AudioReferenceStream]] = (
        probe_audio_reference_streams
    ),
    max_reference_streams: int = 3,
) -> SyncResult:
    started_at = time.monotonic()
    audio_streams = audio_stream_prober(video_path, ffprobe_path=ffprobe_path)
    references: list[str | None] = [
        stream.reference_spec for stream in audio_streams[:max_reference_streams]
    ] or [None]
    attempted: list[str] = []
    last_result: SyncResult | None = None

    for reference_stream in references:
        if output_path.exists():
            output_path.unlink()
        if reference_stream is not None:
            attempted.append(reference_stream)
        remaining_seconds = max(1, math.ceil(timeout_seconds - (time.monotonic() - started_at)))
        command = build_ffsubsync_command(
            video_path,
            subtitle_path,
            output_path,
            executable,
            reference_stream=reference_stream,
        )
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=remaining_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            return SyncResult(
                success=False,
                output_path=output_path,
                stdout=_process_text(exc.stdout),
                stderr=_process_text(exc.stderr),
                reason="sync_timeout",
                reference_stream=reference_stream,
                attempted_reference_streams=tuple(attempted),
            )
        except OSError as exc:
            return SyncResult(
                success=False,
                output_path=output_path,
                stdout="",
                stderr=str(exc),
                reason="ffsubsync_unavailable",
                reference_stream=reference_stream,
                attempted_reference_streams=tuple(attempted),
            )

        last_result = _result_from_process(
            completed,
            output_path=output_path,
            reference_stream=reference_stream,
            attempted_reference_streams=tuple(attempted),
        )
        if last_result.success:
            return last_result
        if last_result.reason not in {"speech_not_detected", "audio_stream_unavailable"}:
            return last_result

    if last_result is not None:
        return last_result
    return SyncResult(
        success=False,
        output_path=output_path,
        stdout="",
        stderr="No audio reference stream was attempted",
        reason="audio_stream_unavailable",
        attempted_reference_streams=tuple(attempted),
    )


def _result_from_process(
    completed: subprocess.CompletedProcess[str],
    *,
    output_path: Path,
    reference_stream: str | None,
    attempted_reference_streams: tuple[str, ...],
) -> SyncResult:
    stdout = _process_text(completed.stdout)
    stderr = _process_text(completed.stderr)
    combined_output = f"{stdout}\n{stderr}"
    lowered_output = combined_output.casefold()
    low_quality = "low-quality alignment" in lowered_output
    score_match = re.search(r"\bscore:\s*(-?\d+(?:\.\d+)?)", combined_output, flags=re.IGNORECASE)
    score = float(score_match.group(1)) if score_match is not None else None
    success = completed.returncode == 0 and output_path.exists() and not low_quality
    reason: str | None = None
    if low_quality:
        reason = "low_quality_alignment"
    elif "unable to detect speech" in lowered_output:
        reason = "speech_not_detected"
    elif any(
        marker in lowered_output
        for marker in (
            "matches no streams",
            "error while decoding",
            "could not find codec parameters",
            "invalid data found when processing input",
        )
    ):
        reason = "audio_stream_unavailable"
    elif completed.returncode != 0:
        reason = "ffsubsync_failed"
    elif not output_path.exists():
        reason = "sync_output_missing"

    return SyncResult(
        success=success,
        output_path=output_path,
        stdout=stdout,
        stderr=stderr,
        reason=reason,
        score=score,
        returncode=completed.returncode,
        reference_stream=reference_stream,
        attempted_reference_streams=attempted_reference_streams,
    )


def _process_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)
