from pathlib import Path
from dataclasses import replace

import pytest

from subtitle_sidecar.pipeline.bundle_cache import (
    EpisodeBundleCache,
    episode_from_filename,
    select_episode_member,
)
from subtitle_sidecar.providers.base import (
    DownloadedSubtitle,
    DownloadedSubtitleMember,
    SubtitleCandidate,
    SubtitleSearchRequest,
)


def _request(episode: int) -> SubtitleSearchRequest:
    return SubtitleSearchRequest(
        video_path=Path(f"/media/Show/S01E{episode:02d}.mkv"),
        title="测试剧",
        year=2026,
        media_type="episode",
        season=1,
        episode=episode,
        preferred="bilingual",
        fallback_languages=["zh-cn"],
        tmdb_id="12345",
    )


def _candidate() -> SubtitleCandidate:
    return SubtitleCandidate(
        provider="assrt",
        language="zh-cn",
        is_bilingual=False,
        format="srt",
        title="测试剧 第一季",
        source_url="https://assrt.net/xml/sub/123/123456.xml",
        release_info="WEB-DL",
        confidence=0.8,
    )


def test_selects_exact_episode_member_and_reuses_cached_bundle(tmp_path: Path) -> None:
    first = tmp_path / "Show.S01E01.zh.srt"
    second = tmp_path / "Show.S01E02.zh.srt"
    first.write_text("episode one", encoding="utf-8")
    second.write_text("episode two", encoding="utf-8")
    downloaded = DownloadedSubtitle(
        candidate=_candidate(),
        path=first,
        members=(
            DownloadedSubtitleMember(path=first, filename=first.name),
            DownloadedSubtitleMember(path=second, filename=second.name),
        ),
    )
    cache = EpisodeBundleCache(tmp_path / "data")

    selected = select_episode_member(downloaded, season=1, episode=2)
    first_request = replace(_request(1), tmdb_id="episode-tmdb-1", series_id="series-id")
    second_request = replace(_request(2), tmdb_id="episode-tmdb-2", series_id="series-id")
    stored = cache.store(first_request, selected, source_task_id=88)
    reused = cache.find(second_request)

    assert selected.path == second
    assert stored == 2
    assert reused is not None
    assert reused.source_task_id == 88
    materialized = cache.materialize(reused.candidate, tmp_path / "materialized")
    assert materialized.path.read_text(encoding="utf-8") == "episode two"


def test_rejects_multi_file_bundle_without_target_episode(tmp_path: Path) -> None:
    first = tmp_path / "Show.S01E01.zh.srt"
    first.write_text("episode one", encoding="utf-8")
    downloaded = DownloadedSubtitle(
        candidate=_candidate(),
        path=first,
        members=(
            DownloadedSubtitleMember(path=first, filename=first.name),
            DownloadedSubtitleMember(path=tmp_path / "Show.S01E03.zh.srt", filename="Show.S01E03.zh.srt"),
        ),
    )

    with pytest.raises(ValueError, match="bundle_missing_target_episode"):
        select_episode_member(downloaded, season=1, episode=2)


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("[Yousei-raws] Yojouhan Shinwa Taikei 01 [BDrip 1920x1080].ass", 1),
        ("[VCB-Studio] The Tatami Galaxy [02][Ma10p_1080p].ass", 2),
        ("Show.S01E03.1080p.ass", 3),
        ("Movie.2024.1920x1080.ass", None),
    ],
)
def test_extracts_common_bare_episode_tokens_without_matching_release_numbers(
    filename: str,
    expected: int | None,
) -> None:
    assert episode_from_filename(filename, season=1) == expected


def test_caches_only_preferred_variant_for_each_episode(tmp_path: Path) -> None:
    simplified = tmp_path / "Show.S01E01.简体.ass"
    traditional = tmp_path / "Show.S01E01.繁体.ass"
    second = tmp_path / "Show.S01E02.繁体.ass"
    simplified.write_text("simplified", encoding="utf-8")
    traditional.write_text("traditional", encoding="utf-8")
    second.write_text("episode two", encoding="utf-8")
    downloaded = DownloadedSubtitle(
        candidate=replace(_candidate(), language="zh-hant"),
        path=simplified,
        members=(
            DownloadedSubtitleMember(path=simplified, filename=simplified.name),
            DownloadedSubtitleMember(path=traditional, filename=traditional.name),
            DownloadedSubtitleMember(path=second, filename=second.name),
        ),
    )
    cache = EpisodeBundleCache(tmp_path / "data")

    stored = cache.store(_request(1), downloaded, source_task_id=88)
    cached = cache.find(_request(1))

    assert stored == 2
    assert cached is not None
    materialized = cache.materialize(cached.candidate, tmp_path / "materialized")
    assert materialized.path.read_text(encoding="utf-8") == "traditional"


def test_prefers_matching_script_when_bundle_has_two_variants_for_one_episode(tmp_path: Path) -> None:
    simplified = tmp_path / "Show.S01E04.简体中英.ass"
    traditional = tmp_path / "Show.S01E04.繁体中英.ass"
    simplified.write_text("simplified", encoding="utf-8")
    traditional.write_text("traditional", encoding="utf-8")
    candidate = replace(_candidate(), language="zh-hant")
    downloaded = DownloadedSubtitle(
        candidate=candidate,
        path=simplified,
        members=(
            DownloadedSubtitleMember(path=simplified, filename=simplified.name),
            DownloadedSubtitleMember(path=traditional, filename=traditional.name),
        ),
    )

    selected = select_episode_member(downloaded, season=1, episode=4)

    assert selected.path == traditional
