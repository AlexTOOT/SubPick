from pathlib import Path
import subprocess

from subtitle_sidecar.sync.ffsubsync import build_ffsubsync_command, sync_subtitle


def test_builds_ffsubsync_command(tmp_path: Path) -> None:
    video = tmp_path / "Movie.mkv"
    subtitle = tmp_path / "Movie.srt"
    output = tmp_path / "Movie.synced.srt"

    command = build_ffsubsync_command(video, subtitle, output, executable="ffsubsync")

    assert command == [
        "ffsubsync", str(video), "-i", str(subtitle), "-o", str(output),
        "--skip-intro-outro", "--skip-sync-on-low-quality",
    ]


def test_sync_subtitle_captures_process_output_and_preserves_input(
    tmp_path: Path,
    monkeypatch,
) -> None:
    video = tmp_path / "Movie.mkv"
    subtitle = tmp_path / "Movie.srt"
    output = tmp_path / "Movie.synced.srt"
    subtitle.write_text("1\n00:00:01,000 --> 00:00:02,000\nhello\n", encoding="utf-8")

    def fake_run(
        command: list[str],
        *,
        check: bool,
        capture_output: bool,
        text: bool,
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        output.write_text("synced subtitle", encoding="utf-8")
        return subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout="sync ok",
            stderr="minor warning",
        )

    monkeypatch.setattr("subtitle_sidecar.sync.ffsubsync.subprocess.run", fake_run)

    result = sync_subtitle(video, subtitle, output, executable="ffsubsync", timeout_seconds=42)

    assert result.success is True
    assert result.output_path == output
    assert result.stdout == "sync ok"
    assert result.stderr == "minor warning"
    assert subtitle.read_text(encoding="utf-8") == "1\n00:00:01,000 --> 00:00:02,000\nhello\n"


def test_sync_subtitle_passes_configured_timeout_to_subprocess(
    tmp_path: Path,
    monkeypatch,
) -> None:
    video = tmp_path / "Movie.mkv"
    subtitle = tmp_path / "Movie.srt"
    output = tmp_path / "Movie.synced.srt"
    observed: dict[str, object] = {}

    def fake_run(
        command: list[str],
        *,
        check: bool,
        capture_output: bool,
        text: bool,
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        observed["command"] = command
        observed["timeout"] = timeout
        return subprocess.CompletedProcess(
            args=command,
            returncode=1,
            stdout="",
            stderr="failed",
        )

    monkeypatch.setattr("subtitle_sidecar.sync.ffsubsync.subprocess.run", fake_run)

    result = sync_subtitle(video, subtitle, output, executable="custom-ffsubsync", timeout_seconds=7)

    assert result.success is False
    assert observed == {
        "command": [
            "custom-ffsubsync", str(video), "-i", str(subtitle), "-o", str(output),
            "--skip-intro-outro", "--skip-sync-on-low-quality",
        ],
        "timeout": 7,
    }


def test_sync_subtitle_rejects_low_quality_alignment_even_when_output_exists(
    tmp_path: Path,
    monkeypatch,
) -> None:
    video = tmp_path / "Episode.mkv"
    subtitle = tmp_path / "Feature.ssa"
    output = tmp_path / "Feature.synced.ssa"

    def fake_run(*args, **kwargs) -> subprocess.CompletedProcess[str]:
        output.write_text("unchanged subtitle", encoding="utf-8")
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout="",
            stderr=(
                "INFO score: -24976.575\n"
                "WARNING low-quality alignment (score -24976.6 < 0.0); "
                "leaving subtitles unmodified"
            ),
        )

    monkeypatch.setattr("subtitle_sidecar.sync.ffsubsync.subprocess.run", fake_run)

    result = sync_subtitle(video, subtitle, output)

    assert result.success is False
    assert result.reason == "low_quality_alignment"
    assert result.score == -24976.575
