from pathlib import Path
import subprocess

from subtitle_sidecar.sync.ffsubsync import (
    build_ffsubsync_command,
    parse_audio_reference_streams,
    probe_audio_reference_streams,
    sync_subtitle,
)


def no_audio_streams(_video_path: Path, *, ffprobe_path: str) -> tuple[object, ...]:
    assert ffprobe_path
    return ()


def test_builds_ffsubsync_command(tmp_path: Path) -> None:
    video = tmp_path / "Movie.mkv"
    subtitle = tmp_path / "Movie.srt"
    output = tmp_path / "Movie.synced.srt"

    command = build_ffsubsync_command(video, subtitle, output, executable="ffsubsync")

    assert command == [
        "ffsubsync",
        str(video),
        "-i",
        str(subtitle),
        "-o",
        str(output),
        "--skip-intro-outro",
        "--skip-sync-on-low-quality",
    ]


def test_builds_ffsubsync_command_with_explicit_audio_stream(tmp_path: Path) -> None:
    command = build_ffsubsync_command(
        tmp_path / "Movie.mkv",
        tmp_path / "Movie.srt",
        tmp_path / "Movie.synced.srt",
        reference_stream="0:a:1",
    )

    assert command[-4:] == [
        "--reference-stream",
        "0:a:1",
        "--skip-intro-outro",
        "--skip-sync-on-low-quality",
    ]


def test_audio_stream_parser_ignores_undecodable_stream_and_deprioritizes_commentary() -> None:
    streams = parse_audio_reference_streams(
        {
            "streams": [
                {
                    "index": 1,
                    "codec_type": "audio",
                    "channels": 6,
                    "disposition": {"default": 1},
                },
                {
                    "index": 2,
                    "codec_type": "audio",
                    "codec_name": "aac",
                    "channels": 2,
                    "tags": {"title": "Director Commentary"},
                    "disposition": {"default": 1},
                },
                {
                    "index": 3,
                    "codec_type": "audio",
                    "codec_name": "eac3",
                    "channels": 6,
                    "tags": {"language": "chi"},
                    "disposition": {"default": 0},
                },
            ]
        }
    )

    assert [stream.reference_spec for stream in streams] == ["0:a:2", "0:a:1"]
    assert streams[0].stream_index == 3


def test_probe_audio_reference_streams_uses_audio_only_ffprobe_json(tmp_path: Path) -> None:
    video = tmp_path / "Movie.mp4"
    observed: list[str] = []

    def fake_runner(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        observed.extend(command)
        return subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout=(
                '{"streams": [{"index": 2, "codec_type": "audio", '
                '"codec_name": "aac", "channels": 2, "disposition": {"default": 1}}]}'
            ),
            stderr="",
        )

    streams = probe_audio_reference_streams(
        video,
        ffprobe_path="custom-ffprobe",
        runner=fake_runner,
    )

    assert observed[0] == "custom-ffprobe"
    assert observed[observed.index("-select_streams") + 1] == "a"
    assert observed[-1] == str(video)
    assert [stream.reference_spec for stream in streams] == ["0:a:0"]


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

    result = sync_subtitle(
        video,
        subtitle,
        output,
        executable="ffsubsync",
        timeout_seconds=42,
        audio_stream_prober=no_audio_streams,
    )

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

    result = sync_subtitle(
        video,
        subtitle,
        output,
        executable="custom-ffsubsync",
        timeout_seconds=7,
        audio_stream_prober=no_audio_streams,
    )

    assert result.success is False
    assert observed == {
        "command": [
            "custom-ffsubsync",
            str(video),
            "-i",
            str(subtitle),
            "-o",
            str(output),
            "--skip-intro-outro",
            "--skip-sync-on-low-quality",
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

    result = sync_subtitle(video, subtitle, output, audio_stream_prober=no_audio_streams)

    assert result.success is False
    assert result.reason == "low_quality_alignment"
    assert result.score == -24976.575


def test_sync_subtitle_retries_next_audio_stream_when_speech_is_not_detected(
    tmp_path: Path,
    monkeypatch,
) -> None:
    video = tmp_path / "Movie.mp4"
    subtitle = tmp_path / "Movie.srt"
    output = tmp_path / "Movie.synced.srt"
    streams = parse_audio_reference_streams(
        {
            "streams": [
                {
                    "index": 1,
                    "codec_type": "audio",
                    "codec_name": "truehd",
                    "channels": 6,
                    "disposition": {"default": 1},
                },
                {
                    "index": 2,
                    "codec_type": "audio",
                    "codec_name": "aac",
                    "channels": 2,
                    "disposition": {"default": 0},
                },
            ]
        }
    )
    commands: list[list[str]] = []

    def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        if "0:a:0" in command:
            return subprocess.CompletedProcess(
                args=command,
                returncode=1,
                stdout="",
                stderr="ValueError: Unable to detect speech.",
            )
        output.write_text("synced subtitle", encoding="utf-8")
        return subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout="INFO score: 42.5",
            stderr="",
        )

    monkeypatch.setattr("subtitle_sidecar.sync.ffsubsync.subprocess.run", fake_run)

    result = sync_subtitle(
        video,
        subtitle,
        output,
        audio_stream_prober=lambda *_args, **_kwargs: streams,
    )

    assert result.success is True
    assert result.reference_stream == "0:a:1"
    assert result.attempted_reference_streams == ("0:a:0", "0:a:1")
    assert result.returncode == 0
    assert result.score == 42.5
    assert [command[command.index("--reference-stream") + 1] for command in commands] == [
        "0:a:0",
        "0:a:1",
    ]
