from pathlib import Path

from subtitle_sidecar.pipeline.naming import build_subtitle_path


def test_builds_jellyfin_friendly_name_for_movie(tmp_path: Path) -> None:
    video = tmp_path / "Movie.Name.2024.mkv"

    subtitle = build_subtitle_path(video, language="zh-cn", extension="srt", default=True)

    assert subtitle == tmp_path / "Movie.Name.2024.zh-cn.default.srt"


def test_omits_default_suffix_when_not_requested(tmp_path: Path) -> None:
    video = tmp_path / "Series.S01E02.mkv"

    subtitle = build_subtitle_path(video, language="zh-hant", extension=".ass", default=False)

    assert subtitle == tmp_path / "Series.S01E02.zh-hant.ass"


def test_never_uses_compound_bilingual_extension_in_v1(tmp_path: Path) -> None:
    video = tmp_path / "Movie.Name.2024.mkv"

    subtitle = build_subtitle_path(video, language="zh-cn.en", extension="srt", default=False)

    assert subtitle == tmp_path / "Movie.Name.2024.zh-cn.srt"
