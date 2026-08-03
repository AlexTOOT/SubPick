from pathlib import Path

from subtitle_sidecar.pipeline.scoring import (
    candidate_score_breakdown,
    candidate_mismatch_reason,
    episode_mismatch_reason,
    score_candidate,
    sort_candidates,
)
from subtitle_sidecar.providers.base import SubtitleCandidate


def candidate(
    language: str,
    bilingual: bool,
    release: str = "WEB-DL",
    provider_quality: float | None = None,
) -> SubtitleCandidate:
    return SubtitleCandidate(
        provider="fake",
        language=language,
        is_bilingual=bilingual,
        format="srt",
        title="Movie",
        source_url="https://example.invalid",
        release_info=release,
        confidence=0.5,
        provider_quality=provider_quality,
        raw_metadata={},
    )


def test_bilingual_scores_above_plain_chinese() -> None:
    assert score_candidate(candidate("zh-cn", True)) > score_candidate(candidate("zh-cn", False))


def test_simplified_scores_above_traditional_when_not_bilingual() -> None:
    assert score_candidate(candidate("zh-cn", False)) > score_candidate(candidate("zh-hant", False))


def test_provider_quality_beats_low_popularity_bilingual_candidate() -> None:
    popular_simplified = candidate("zh-cn", False, provider_quality=0.9)
    obscure_bilingual = candidate("zh-cn", True, provider_quality=0.35)

    assert sort_candidates([obscure_bilingual, popular_simplified]) == [
        popular_simplified,
        obscure_bilingual,
    ]


def test_bilingual_is_preferred_when_provider_quality_is_close() -> None:
    plain = candidate("zh-cn", False, provider_quality=0.82)
    bilingual = candidate("zh-cn", True, provider_quality=0.80)

    assert sort_candidates([plain, bilingual]) == [bilingual, plain]


def test_score_breakdown_explains_total_and_conditional_bilingual_bonus() -> None:
    subtitle = candidate("zh-cn", True, provider_quality=0.8)

    breakdown = candidate_score_breakdown(
        subtitle,
        provider_quality_reference=0.82,
    )

    assert breakdown["provider_quality_score"] == 80
    assert breakdown["bilingual_bonus_applied"] is True
    assert breakdown["bilingual_score"] == 6
    assert breakdown["language_score"] == 2
    assert breakdown["confidence_score"] == 5
    assert breakdown["total_score"] == 93

    low_quality = candidate("zh-cn", True, provider_quality=0.4)
    low_breakdown = candidate_score_breakdown(
        low_quality,
        provider_quality_reference=0.82,
    )
    assert low_breakdown["bilingual_bonus_applied"] is False
    assert low_breakdown["bilingual_score"] == 0


def test_confidence_affects_score_within_same_language_bucket() -> None:
    lower_confidence = candidate("zh-cn", False)
    higher_confidence = SubtitleCandidate(
        provider="fake",
        language="zh-cn",
        is_bilingual=False,
        format="srt",
        title="Movie",
        source_url="https://example.invalid/high",
        release_info="WEB-DL",
        confidence=0.9,
        raw_metadata={},
    )

    assert score_candidate(higher_confidence) > score_candidate(lower_confidence)


def test_sort_candidates_is_deterministic_for_equal_scores() -> None:
    first = SubtitleCandidate(
        provider="z-provider",
        language="zh-cn",
        is_bilingual=False,
        format="srt",
        title="Movie A",
        source_url="https://example.invalid/z",
        release_info="WEB-DL",
        confidence=0.5,
        raw_metadata={},
    )
    second = SubtitleCandidate(
        provider="a-provider",
        language="zh-CN",
        is_bilingual=False,
        format="srt",
        title="Movie B",
        source_url="https://example.invalid/a",
        release_info="WEB-DL",
        confidence=0.5,
        raw_metadata={},
    )

    ranked = sort_candidates([first, second])

    assert ranked == [second, first]


def test_prefers_candidate_with_matching_release_markers(tmp_path: Path) -> None:
    video = tmp_path / "Movie.2024.1080p.WEB-DL.mkv"
    matching = candidate("zh-cn", False, "Movie.2024.1080p.WEB-DL")
    mismatching = candidate("zh-cn", False, "Movie.2024.2160p.BluRay")

    ranked = sort_candidates([mismatching, matching], video_path=video)

    assert ranked == [matching, mismatching]


def test_prefers_matching_episode_over_wrong_episode() -> None:
    matching = candidate("zh-cn", False, "Show.S01E02.1080p.WEB-DL")
    wrong = candidate("zh-cn", False, "Show.S01E03.1080p.WEB-DL")

    ranked = sort_candidates([wrong, matching], season=1, episode=2)

    assert ranked == [matching, wrong]


def test_episode_mismatch_reason_rejects_explicit_other_season_or_episode() -> None:
    other_season = candidate("zh-cn", False, "Damages.S05E01 以法之名 第五季 第1集")
    other_episode = candidate("zh-cn", False, "Show.S01E03 Show 第一季 第3集")
    season_pack = candidate("zh-cn", False, "Show 第一季全集")

    assert episode_mismatch_reason(other_season, season=1, episode=10) == "season_mismatch"
    assert episode_mismatch_reason(other_episode, season=1, episode=10) == "episode_mismatch"
    assert episode_mismatch_reason(season_pack, season=1, episode=10) is None


def test_movie_candidate_mismatch_rejects_explicit_episode_or_distant_title_year() -> None:
    series_candidate = candidate("zh-cn", False, "Kingdom.S01E01")
    season_only_candidate = candidate("zh-cn", False, "My Sister S01")
    sequel_candidate = SubtitleCandidate(
        provider="fake",
        language="zh-cn",
        is_bilingual=False,
        format="srt",
        title="Ready or Not 2 (2026)",
        source_url="https://example.invalid/sequel",
        release_info="WEB-DL",
        confidence=0.5,
        raw_metadata={},
    )
    near_year_candidate = SubtitleCandidate(
        provider="fake",
        language="zh-cn",
        is_bilingual=False,
        format="srt",
        title="Movie (2020)",
        source_url="https://example.invalid/near-year",
        release_info="WEB-DL",
        confidence=0.5,
        raw_metadata={},
    )

    assert candidate_mismatch_reason(series_candidate, season=None, episode=None, year=2023) == "movie_episode_mismatch"
    assert candidate_mismatch_reason(season_only_candidate, season=None, episode=None, year=2021) == "movie_episode_mismatch"
    assert candidate_mismatch_reason(sequel_candidate, season=None, episode=None, year=2019) == "year_mismatch"
    assert candidate_mismatch_reason(near_year_candidate, season=None, episode=None, year=2019) is None


def test_movie_year_check_uses_release_info_and_ignores_numbers_in_title() -> None:
    wrong_remake = SubtitleCandidate(
        provider="assrt",
        language="zh-cn",
        is_bilingual=False,
        format="ssa",
        title="驯龙高手1",
        source_url="https://assrt.net/xml/sub/261/261912.xml",
        release_info="驯龙高手.How.To.Train.Your.Dragon.2010",
        confidence=0.8,
        raw_metadata={},
    )
    numbered_title = SubtitleCandidate(
        provider="assrt",
        language="zh-cn",
        is_bilingual=False,
        format="srt",
        title="Blade Runner 2049",
        source_url="https://example.invalid/blade-runner",
        release_info="Blade.Runner.2049.2017.2160p.BluRay.REMUX",
        confidence=0.8,
        raw_metadata={},
    )

    assert candidate_mismatch_reason(
        wrong_remake,
        season=None,
        episode=None,
        year=2025,
        title="新·驯龙高手",
        original_title="How to Train Your Dragon",
    ) == "year_mismatch"
    assert candidate_mismatch_reason(
        numbered_title,
        season=None,
        episode=None,
        year=2017,
        title="银翼杀手2049",
        original_title="Blade Runner 2049",
    ) is None


def test_candidate_mismatch_rejects_unrelated_title_but_accepts_original_title_match() -> None:
    unrelated = SubtitleCandidate(
        provider="assrt",
        language="zh-cn",
        is_bilingual=False,
        format="srt",
        title="Saint Seiya Soul of Gold",
        source_url="https://example.invalid/unrelated",
        release_info="S01E01",
        confidence=0.5,
        raw_metadata={},
    )
    original_title_match = SubtitleCandidate(
        provider="assrt",
        language="zh-cn",
        is_bilingual=False,
        format="srt",
        title="The First Gas Man S01E01",
        source_url="https://example.invalid/match",
        release_info="S01E01",
        confidence=0.5,
        raw_metadata={},
    )

    assert candidate_mismatch_reason(
        unrelated,
        season=1,
        episode=1,
        year=2026,
        title="Gas Man Number One",
        original_title="The First Gas Man",
    ) == "title_mismatch"
    assert candidate_mismatch_reason(
        original_title_match,
        season=1,
        episode=1,
        year=2026,
        title="Gas Man Number One",
        original_title="The First Gas Man",
    ) is None


def test_episode_candidate_rejects_same_title_feature_film_without_episode_markers() -> None:
    feature = SubtitleCandidate(
        provider="assrt",
        language="zh-cn",
        is_bilingual=True,
        format="ssa",
        title="[01:46:07] 2012: 泰迪熊",
        source_url="https://example.invalid/feature",
        release_info="不知道",
        confidence=0.65,
        raw_metadata={},
    )

    assert candidate_mismatch_reason(
        feature,
        season=2,
        episode=2,
        year=2026,
        title="泰迪熊",
        original_title="Ted",
    ) == "episode_feature_mismatch"


def test_episode_candidate_keeps_old_series_year_when_season_marker_matches() -> None:
    season_pack = candidate("zh-cn", False, "Long Running Show.1989.S35.1080p.WEB-DL")

    assert candidate_mismatch_reason(
        season_pack,
        season=35,
        episode=2,
        year=2024,
        title="Long Running Show",
        original_title="Long Running Show",
    ) is None


def test_episode_candidate_accepts_series_or_season_release_year() -> None:
    candidate_with_season_year = SubtitleCandidate(
        provider="assrt",
        language="zh-cn",
        is_bilingual=True,
        format="ass",
        title="泰迪熊",
        source_url="https://example.invalid/season-year",
        release_info="Ted.2026.WEB-DL",
        confidence=0.8,
        raw_metadata={},
    )

    assert candidate_mismatch_reason(
        candidate_with_season_year,
        season=2,
        episode=4,
        year=2024,
        alternate_years=(2026,),
        title="泰迪熊",
        original_title="Ted",
    ) is None
