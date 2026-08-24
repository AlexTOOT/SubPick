from pathlib import Path

import pytest

from subtitle_sidecar.pipeline.orchestrator import safe_place_subtitle
from subtitle_sidecar.pipeline.validator import validate_subtitle_file


def test_validates_srt_with_chinese_content(tmp_path: Path) -> None:
    subtitle = tmp_path / "Movie.zh-cn.srt"
    subtitle.write_text("1\n00:00:01,000 --> 00:00:02,000\n你好\n", encoding="utf-8")

    result = validate_subtitle_file(subtitle)

    assert result.is_valid is True
    assert result.has_chinese is True
    assert result.encoding == "utf-8-sig"
    assert result.cue_count == 1
    assert result.duration_seconds == 2.0


def test_accepts_common_gb18030_subtitle_encoding(tmp_path: Path) -> None:
    subtitle = tmp_path / "Movie.zh-cn.srt"
    subtitle.write_bytes("1\n00:00:01,000 --> 00:00:02,000\n你好\n".encode("gb18030"))

    result = validate_subtitle_file(subtitle)

    assert result.is_valid is True
    assert result.encoding == "gb18030"


def test_accepts_utf16_subtitle_with_bom(tmp_path: Path) -> None:
    subtitle = tmp_path / "Movie.zh-cn.ass"
    subtitle.write_text(
        "[Script Info]\n[Events]\n"
        "Dialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,你好\n",
        encoding="utf-16",
    )

    result = validate_subtitle_file(subtitle)

    assert result.is_valid is True
    assert result.has_chinese is True
    assert result.encoding == "utf-16"


def test_rejects_subtitle_that_runs_far_beyond_video_duration(tmp_path: Path) -> None:
    subtitle = tmp_path / "Movie.zh-cn.srt"
    subtitle.write_text("1\n02:00:01,000 --> 02:00:02,000\n你好\n", encoding="utf-8")

    result = validate_subtitle_file(subtitle, video_duration_seconds=3600)

    assert result.is_valid is False
    assert result.reason == "timeline_exceeds_video"
    assert result.duration_seconds == 7202.0


def test_rejects_subtitle_that_is_far_shorter_than_feature_video(tmp_path: Path) -> None:
    subtitle = tmp_path / "Movie.zh-cn.srt"
    subtitle.write_text("1\n00:19:58,000 --> 00:19:59,000\n你好\n", encoding="utf-8")

    result = validate_subtitle_file(subtitle, video_duration_seconds=2 * 60 * 60 + 6 * 60)

    assert result.is_valid is False
    assert result.reason == "timeline_too_short"
    assert result.duration_seconds == 1199.0


def test_accepts_subtitle_that_ends_before_feature_credits(tmp_path: Path) -> None:
    subtitle = tmp_path / "Movie.zh-cn.srt"
    subtitle.write_text("1\n01:39:58,000 --> 01:39:59,000\n你好\n", encoding="utf-8")

    result = validate_subtitle_file(
        subtitle,
        video_duration_seconds=2 * 60 * 60 + 6 * 60,
    )

    assert result.is_valid is True
    assert result.duration_seconds == 5999.0


def test_rejects_empty_subtitle(tmp_path: Path) -> None:
    subtitle = tmp_path / "Movie.zh-cn.srt"
    subtitle.write_text("", encoding="utf-8")

    result = validate_subtitle_file(subtitle)

    assert result.is_valid is False


def test_rejects_unsupported_subtitle_extension_even_with_utf8_text(tmp_path: Path) -> None:
    # Current validator scope only guarantees UTF-8 text subtitles for supported sidecar formats.
    subtitle = tmp_path / "Movie.zh-cn.txt"
    subtitle.write_text("你好\n", encoding="utf-8")

    result = validate_subtitle_file(subtitle)

    assert result.is_valid is False
    assert result.reason == "unsupported_extension"


def test_accepts_utf8_ass_with_events_section_and_chinese_content(tmp_path: Path) -> None:
    subtitle = tmp_path / "Movie.zh-cn.ass"
    subtitle.write_text(
        "[Script Info]\n"
        "Title: Movie\n"
        "[Events]\n"
        "Dialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,你好\n",
        encoding="utf-8",
    )

    result = validate_subtitle_file(subtitle)

    assert result.is_valid is True
    assert result.has_chinese is True


def test_rejects_ass_without_events_section(tmp_path: Path) -> None:
    subtitle = tmp_path / "Movie.zh-cn.ssa"
    subtitle.write_text(
        "[Script Info]\n"
        "Title: Movie\n"
        "Dialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,你好\n",
        encoding="utf-8",
    )

    result = validate_subtitle_file(subtitle)

    assert result.is_valid is False
    assert result.has_chinese is True
    assert result.reason == "missing_events_section"


def test_safe_place_subtitle_refuses_to_overwrite_by_default(tmp_path: Path) -> None:
    source = tmp_path / "incoming.srt"
    source.write_text("1\n00:00:01,000 --> 00:00:02,000\n你好\n", encoding="utf-8")
    destination = tmp_path / "library" / "Movie.zh-cn.srt"
    destination.parent.mkdir()
    destination.write_text("existing subtitle", encoding="utf-8")

    with pytest.raises(FileExistsError):
        safe_place_subtitle(source, destination)

    assert destination.read_text(encoding="utf-8") == "existing subtitle"
    assert source.exists()


def test_safe_place_subtitle_keeps_backup_when_overwriting(tmp_path: Path) -> None:
    source = tmp_path / "incoming.srt"
    source.write_text("1\n00:00:01,000 --> 00:00:02,000\n你好\n", encoding="utf-8")
    destination = tmp_path / "library" / "Movie.zh-cn.srt"
    destination.parent.mkdir()
    destination.write_text("old subtitle", encoding="utf-8")

    placed_path = safe_place_subtitle(source, destination, overwrite=True, keep_backup=True)

    assert placed_path == destination
    assert destination.read_text(encoding="utf-8") == "1\n00:00:01,000 --> 00:00:02,000\n你好\n"
    assert destination.with_suffix(".srt.bak").read_text(encoding="utf-8") == "old subtitle"
    assert source.exists() is False
