from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import subprocess


@dataclass(frozen=True)
class SyncResult:
    success: bool
    output_path: Path
    stdout: str
    stderr: str
    reason: str | None = None
    score: float | None = None


def build_ffsubsync_command(
    video_path: Path,
    subtitle_path: Path,
    output_path: Path,
    executable: str = "ffsubsync",
) -> list[str]:
    return [
        executable,
        str(video_path),
        "-i",
        str(subtitle_path),
        "-o",
        str(output_path),
        "--skip-intro-outro",
        "--skip-sync-on-low-quality",
    ]


def sync_subtitle(
    video_path: Path,
    subtitle_path: Path,
    output_path: Path,
    executable: str = "ffsubsync",
    timeout_seconds: int = 900,
) -> SyncResult:
    command = build_ffsubsync_command(video_path, subtitle_path, output_path, executable)
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
    )
    combined_output = f"{completed.stdout}\n{completed.stderr}"
    low_quality = "low-quality alignment" in combined_output.casefold()
    score_match = re.search(r"\bscore:\s*(-?\d+(?:\.\d+)?)", combined_output, flags=re.IGNORECASE)
    score = float(score_match.group(1)) if score_match is not None else None
    return SyncResult(
        success=completed.returncode == 0 and output_path.exists() and not low_quality,
        output_path=output_path,
        stdout=completed.stdout,
        stderr=completed.stderr,
        reason="low_quality_alignment" if low_quality else None,
        score=score,
    )
