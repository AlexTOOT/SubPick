from __future__ import annotations

from difflib import SequenceMatcher
from pathlib import Path
import re

from subtitle_sidecar.media.identity import analyze_release_years
from subtitle_sidecar.providers.base import SubtitleCandidate

BILINGUAL_BONUS = 100
SIMPLIFIED_BONUS = 70
TRADITIONAL_BONUS = 50
CONFIDENCE_MULTIPLIER = 10
RELEASE_MATCH_BONUS = 25
RELEASE_MISMATCH_PENALTY = 12
EPISODE_MATCH_BONUS = 40
EPISODE_MISMATCH_PENALTY = 40
FEATURE_RUNTIME_SECONDS = 75 * 60
EPISODE_YEAR_MISMATCH_YEARS = 4


def score_candidate(
    candidate: SubtitleCandidate,
    *,
    video_path: Path | None = None,
    season: int | None = None,
    episode: int | None = None,
) -> float:
    score = 0.0
    language = _normalize_language(candidate.language)

    if candidate.is_bilingual:
        score += BILINGUAL_BONUS

    if language == "zh-cn":
        score += SIMPLIFIED_BONUS
    elif language == "zh-hant":
        score += TRADITIONAL_BONUS

    score += candidate.confidence * CONFIDENCE_MULTIPLIER
    if video_path is not None:
        score += _release_match_score(candidate, video_path)
    if season is not None and episode is not None:
        score += _episode_match_score(candidate, season, episode)
    return score


def sort_candidates(
    candidates: list[SubtitleCandidate],
    *,
    video_path: Path | None = None,
    season: int | None = None,
    episode: int | None = None,
) -> list[SubtitleCandidate]:
    return sorted(
        candidates,
        key=lambda candidate: (
            -score_candidate(candidate, video_path=video_path, season=season, episode=episode),
            _normalize_language(candidate.language),
            candidate.provider.casefold(),
            candidate.release_info.casefold(),
            candidate.title.casefold(),
            candidate.source_url,
        ),
    )


def episode_mismatch_reason(
    candidate: SubtitleCandidate,
    *,
    season: int | None,
    episode: int | None,
) -> str | None:
    """Reject only candidates that explicitly identify a different episode."""
    if season is None or episode is None:
        return None
    text = f"{candidate.release_info} {candidate.title}".casefold()
    detected_seasons, detected_episodes = _episode_markers(text)
    if detected_seasons and season not in detected_seasons:
        return "season_mismatch"
    if detected_episodes and episode not in detected_episodes:
        return "episode_mismatch"
    return None


def candidate_mismatch_reason(
    candidate: SubtitleCandidate,
    *,
    season: int | None,
    episode: int | None,
    year: int | None,
    title: str | None = None,
    original_title: str | None = None,
) -> str | None:
    """Return a conservative rejection reason for an explicitly wrong candidate.

    Episode tasks retain the exact season/episode guard and reject candidates
    that explicitly look like a feature film. For movies, reject only an
    explicitly episodic candidate or a title with a clearly different release
    year. One-year release differences remain eligible.
    """
    episode_reason = episode_mismatch_reason(candidate, season=season, episode=episode)
    if episode_reason is not None:
        return episode_reason
    # ASSRT exposes a stable work title in its search metadata. Other
    # providers frequently expose release labels or generic filenames, so
    # using them as a hard title filter would reject valid candidates.
    if candidate.provider.split(":", 1)[0] == "assrt" and not _candidate_title_matches(
        candidate.title,
        title,
        original_title,
    ):
        return "title_mismatch"
    if season is not None or episode is not None:
        candidate_text = f"{candidate.release_info} {candidate.title}".casefold()
        seasons, episodes = _episode_markers(candidate_text)
        if not seasons and not episodes:
            candidate_years = _release_years(candidate_text)
            if (
                year is not None
                and candidate_years
                and all(
                    abs(candidate_year - year) >= EPISODE_YEAR_MISMATCH_YEARS
                    for candidate_year in candidate_years
                )
            ):
                return "episode_feature_mismatch"
            runtime_seconds = _feature_runtime_seconds(candidate_text)
            if runtime_seconds is not None and runtime_seconds >= FEATURE_RUNTIME_SECONDS:
                return "episode_feature_mismatch"
        return None

    candidate_text = f"{candidate.release_info} {candidate.title}".casefold()
    seasons, episodes = _episode_markers(candidate_text)
    if seasons or episodes or re.search(r"\b(?:s\d{1,2}|season[ ._-]*\d{1,2})\b", candidate_text):
        return "movie_episode_mismatch"

    if year is None:
        return None
    year_evidence = analyze_release_years(
        (candidate.title, candidate.release_info),
        expected_year=year,
        expected_titles=(title, original_title),
    )
    if year_evidence.has_conflict:
        return "year_mismatch"
    return None


def _release_years(value: str) -> set[int]:
    return {int(match.group(0)) for match in re.finditer(r"(?<!\d)(?:19|20)\d{2}(?!\d)", value)}


def _feature_runtime_seconds(value: str) -> int | None:
    matches = re.finditer(
        r"(?<!\d)(?P<hours>\d{1,2})[:\uff1a](?P<minutes>\d{2})[:\uff1a](?P<seconds>\d{2})(?!\d)",
        value,
    )
    durations = [
        int(match.group("hours")) * 3600
        + int(match.group("minutes")) * 60
        + int(match.group("seconds"))
        for match in matches
        if int(match.group("minutes")) < 60 and int(match.group("seconds")) < 60
    ]
    return max(durations, default=None)


def _candidate_title_matches(
    candidate_title: str,
    title: str | None,
    original_title: str | None,
) -> bool:
    """Keep candidates whose title plausibly identifies the requested work.

    This is intentionally a rejection guard, not a fuzzy selector.  A missing
    or very short title remains eligible.  Otherwise, accept direct containment
    and close normalized matches against either the display or original title.
    """
    candidate_identity = _title_identity(candidate_title)
    expected_identities = {
        identity
        for value in (title, original_title)
        if (identity := _title_identity(value))
    }
    if len(candidate_identity) < 3 or not expected_identities:
        return True
    for expected in expected_identities:
        if len(expected) < 3:
            continue
        if candidate_identity in expected or expected in candidate_identity:
            return True
        if SequenceMatcher(a=candidate_identity, b=expected).ratio() >= 0.55:
            return True
    return False


def _title_identity(value: str | None) -> str:
    return "".join(re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]+", str(value or "").casefold()))


def _release_match_score(candidate: SubtitleCandidate, video_path: Path) -> float:
    video_markers = _release_markers(video_path.stem)
    candidate_markers = _release_markers(f"{candidate.release_info} {candidate.title}")
    if not video_markers or not candidate_markers:
        return 0.0
    shared = video_markers & candidate_markers
    if shared:
        return RELEASE_MATCH_BONUS + min(len(shared), 3) * 5
    video_resolution = next((marker for marker in video_markers if marker.endswith("p")), None)
    candidate_resolution = next((marker for marker in candidate_markers if marker.endswith("p")), None)
    if video_resolution and candidate_resolution and video_resolution != candidate_resolution:
        return -RELEASE_MISMATCH_PENALTY
    return 0.0


def _episode_match_score(candidate: SubtitleCandidate, season: int, episode: int) -> float:
    text = f"{candidate.release_info} {candidate.title}".casefold()
    expected = f"s{season:02d}e{episode:02d}"
    _seasons, detected_episodes = _episode_markers(text)
    episode_codes = set(re.findall(r"s\d{1,2}e\d{1,2}", text))
    if expected in episode_codes:
        return EPISODE_MATCH_BONUS
    return -EPISODE_MISMATCH_PENALTY if detected_episodes else 0.0


def _episode_markers(text: str) -> tuple[set[int], set[int]]:
    seasons: set[int] = set()
    episodes: set[int] = set()
    for match in re.finditer(r"s(?P<season>\d{1,2})[ ._-]*e(?P<episode>\d{1,3})", text):
        seasons.add(int(match.group("season")))
        episodes.add(int(match.group("episode")))
    for match in re.finditer(r"(?P<season>\d{1,2})x(?P<episode>\d{1,3})", text):
        seasons.add(int(match.group("season")))
        episodes.add(int(match.group("episode")))
    for match in re.finditer(
        r"\b(?:s|season[ ._-]*)(?P<season>\d{1,2})(?![ ._-]*e\d)",
        text,
    ):
        seasons.add(int(match.group("season")))
    for match in re.finditer(r"第\s*(?P<season>[一二三四五六七八九十\d]+)\s*季", text):
        value = _parse_episode_number(match.group("season"))
        if value is not None:
            seasons.add(value)
    for match in re.finditer(r"第\s*(?P<episode>[一二三四五六七八九十\d]+)\s*[集话]", text):
        value = _parse_episode_number(match.group("episode"))
        if value is not None:
            episodes.add(value)
    return seasons, episodes


def _parse_episode_number(value: str) -> int | None:
    if value.isdecimal():
        return int(value)
    numerals = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    if value == "十":
        return 10
    if "十" in value:
        before, _, after = value.partition("十")
        tens = numerals.get(before, 1 if not before else 0)
        ones = numerals.get(after, 0)
        return tens * 10 + ones if tens else None
    return numerals.get(value)


def _release_markers(value: str) -> set[str]:
    normalized = value.casefold().replace(" ", ".")
    return set(
        re.findall(r"\b(?:2160p|1080p|720p|480p|web[.-]?dl|bluray|bdrip|webrip|hdtv|remux)\b", normalized)
    )


def _normalize_language(language: str) -> str:
    normalized = language.strip().lower().replace("_", "-")
    if "." in normalized:
        normalized = normalized.split(".", 1)[0]
    aliases = {
        "chi": "zh-cn",
        "chs": "zh-cn",
        "zho": "zh-cn",
        "zh": "zh-cn",
        "zh-hans": "zh-cn",
        "zh-sg": "zh-cn",
        "cht": "zh-hant",
        "zh-tw": "zh-hant",
    }
    return aliases.get(normalized, normalized)
