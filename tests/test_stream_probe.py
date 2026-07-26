import json
from pathlib import Path

from subtitle_sidecar.probe.streams import (
    parse_subtitle_streams,
    probe_video_duration_seconds,
    probe_video_streams,
)


def test_parse_chinese_subtitle_stream_from_ffprobe_json() -> None:
    payload = {
        "streams": [
            {
                "codec_type": "subtitle",
                "codec_name": "ass",
                "tags": {"language": "chi", "title": "简英双语"},
                "disposition": {"default": 1, "forced": 0},
            }
        ]
    }

    result = parse_subtitle_streams(payload)

    assert result.has_chinese is True
    assert result.has_bilingual is True
    assert result.streams[0].language == "chi"


def test_probe_video_streams_runs_ffprobe_and_parses_output(tmp_path: Path) -> None:
    video = tmp_path / "Movie.mkv"
    video.write_bytes(b"fake")
    captured: list[list[str]] = []

    def fake_runner(command: list[str], **_: object):
        captured.append(command)

        class CompletedProcess:
            returncode = 0
            stdout = json.dumps(
                {
                    "streams": [
                        {
                            "codec_type": "subtitle",
                            "codec_name": "subrip",
                            "tags": {"language": "zho", "title": "中文字幕"},
                            "disposition": {"default": 1, "forced": 0},
                        }
                    ]
                }
            )
            stderr = ""

        return CompletedProcess()

    result = probe_video_streams(video, ffprobe_path="ffprobe-bin", runner=fake_runner)

    assert captured == [
        ["ffprobe-bin", "-v", "quiet", "-print_format", "json", "-show_streams", str(video)]
    ]
    assert result.has_chinese is True
    assert result.streams[0].codec_name == "subrip"


def test_parse_stream_recognizes_english_chinese_markers() -> None:
    payload = {
        "streams": [
            {
                "codec_type": "subtitle",
                "codec_name": "ass",
                "tags": {"language": "und", "title": "chinese bilingual"},
                "disposition": {"default": 0, "forced": 0},
            }
        ]
    }

    result = parse_subtitle_streams(payload)

    assert result.has_chinese is True
    assert result.has_bilingual is True


def test_probe_video_duration_reads_positive_ffprobe_duration(tmp_path: Path) -> None:
    video = tmp_path / "Movie.mkv"
    video.write_bytes(b"fake")

    def fake_runner(command: list[str], **_: object):
        assert command[-1] == str(video)

        class CompletedProcess:
            returncode = 0
            stdout = "7234.125\n"

        return CompletedProcess()

    assert probe_video_duration_seconds(video, runner=fake_runner) == 7234.125
