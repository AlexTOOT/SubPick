from pathlib import Path

from subtitle_sidecar.media.subtitles import detect_external_subtitles


def test_detects_bilingual_external_subtitle(tmp_path: Path) -> None:
    video = tmp_path / "Movie.mkv"
    video.write_bytes(b"fake")
    subtitle = tmp_path / "Movie.zh-cn.default.srt"
    subtitle.write_text(
        "1\n00:00:01,000 --> 00:00:02,000\nhello 中文\n",
        encoding="utf-8",
    )

    result = detect_external_subtitles(video)

    assert result.has_chinese is True
    assert result.has_bilingual is True
    assert result.matches[0].path == subtitle


def test_detects_chinese_marker_from_filename_without_full_file_read(tmp_path: Path) -> None:
    video = tmp_path / "Series.mkv"
    video.write_bytes(b"fake")
    subtitle = tmp_path / "Series.chinese.srt"
    subtitle.write_text("hello only english " + ("x" * 70000), encoding="utf-8")

    result = detect_external_subtitles(video)

    assert result.has_chinese is True
    assert result.has_bilingual is False
    assert result.matches[0].path == subtitle
