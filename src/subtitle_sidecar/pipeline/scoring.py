from __future__ import annotations

from difflib import SequenceMatcher
from pathlib import Path
import re

from subtitle_sidecar.media.identity import analyze_release_years
from subtitle_sidecar.providers.base import SubtitleCandidate

BILINGUAL_BONUS = 6
SIMPLIFIED_BONUS = 2
TRADITIONAL_BONUS = 1
CONFIDENCE_MULTIPLIER = 10
PROVIDER_QUALITY_MULTIPLIER = 100
PROVIDER_QUALITY_WINDOW = 0.05
RELEASE_MATCH_BONUS = 25
RELEASE_MISMATCH_PENALTY = 12
EPISODE_MATCH_BONUS = 40
SEASON_PACK_MATCH_BONUS = 42
EPISODE_MISMATCH_PENALTY = 40
FEATURE_RUNTIME_SECONDS = 75 * 60
EPISODE_YEAR_MISMATCH_YEARS = 4


def score_candidate(
    candidate: SubtitleCandidate,
    *,
    video_path: Path | None = None,
    season: int | None = None,
    episode: int | None = None,
    provider_quality_reference: float | None = None,
) -> float:
    return candidate_score_breakdown(
        candidate,
        video_path=video_path,
        season=season,
        episode=episode,
        provider_quality_reference=provider_quality_reference,
    )["total_score"]


def candidate_score_breakdown(
    candidate: SubtitleCandidate,
    *,
    video_path: Path | None = None,
    season: int | None = None,
    episode: int | None = None,
    provider_quality_reference: float | None = None,
) -> dict[str, float | bool | None]:
    """Return the transparent scoring signals used to rank one adapter batch."""
    language = _normalize_language(candidate.language)
    provider_quality = _normalized_provider_quality(candidate.provider_quality)
    provider_quality_score = (
        provider_quality * PROVIDER_QUALITY_MULTIPLIER
        if provider_quality is not None
        else 0.0
    )
    bilingual_bonus_applied = candidate.is_bilingual and _quality_is_comparable(
        provider_quality,
        provider_quality_reference,
    )
    bilingual_score = float(BILINGUAL_BONUS if bilingual_bonus_applied else 0)
    language_score = 0.0
    if language == "zh-cn":
        language_score = float(SIMPLIFIED_BONUS)
    elif language == "zh-hant":
        language_score = float(TRADITIONAL_BONUS)
    confidence_score = candidate.confidence * CONFIDENCE_MULTIPLIER
    release_score = _release_match_score(candidate, video_path) if video_path is not None else 0.0
    episode_score = (
        _episode_match_score(candidate, season, episode)
        if season is not None and episode is not None
        else 0.0
    )
    total_score = (
        provider_quality_score
        + bilingual_score
        + language_score
        + confidence_score
        + release_score
        + episode_score
    )
    return {
        "provider_quality": provider_quality,
        "provider_quality_reference": provider_quality_reference,
        "provider_quality_score": provider_quality_score,
        "bilingual_bonus_applied": bilingual_bonus_applied,
        "bilingual_score": bilingual_score,
        "language_score": language_score,
        "confidence_score": confidence_score,
        "release_score": release_score,
        "episode_score": episode_score,
        "total_score": total_score,
    }


def sort_candidates(
    candidates: list[SubtitleCandidate],
    *,
    video_path: Path | None = None,
    season: int | None = None,
    episode: int | None = None,
) -> list[SubtitleCandidate]:
    quality_references = _provider_quality_references(candidates)
    return sorted(
        candidates,
        key=lambda candidate: (
            -score_candidate(
                candidate,
                video_path=video_path,
                season=season,
                episode=episode,
                provider_quality_reference=quality_references.get(_adapter_name(candidate)),
            ),
            _normalize_language(candidate.language),
            candidate.provider.casefold(),
            candidate.release_info.casefold(),
            candidate.title.casefold(),
            candidate.source_url,
        ),
    )


def provider_quality_reference(
    candidates: list[SubtitleCandidate],
    candidate: SubtitleCandidate,
) -> float | None:
    """Return the best native quality reported by the candidate's adapter."""
    return _provider_quality_references(candidates).get(_adapter_name(candidate))


def _provider_quality_references(
    candidates: list[SubtitleCandidate],
) -> dict[str, float]:
    references: dict[str, float] = {}
    for candidate in candidates:
        quality = _normalized_provider_quality(candidate.provider_quality)
        if quality is None:
            continue
        adapter = _adapter_name(candidate)
        references[adapter] = max(references.get(adapter, 0.0), quality)
    return references


def _adapter_name(candidate: SubtitleCandidate) -> str:
    return candidate.provider.split(":", 1)[0].casefold()


def _normalized_provider_quality(value: float | None) -> float | None:
    if value is None:
        return None
    return min(max(float(value), 0.0), 1.0)


def _quality_is_comparable(
    quality: float | None,
    reference: float | None,
) -> bool:
    if quality is None or reference is None:
        return True
    return quality >= reference - PROVIDER_QUALITY_WINDOW


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
    alternate_years: tuple[int, ...] = (),
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
    # Only compare stable work-level metadata. Release labels and archive
    # filenames are intentionally excluded because they are often generic.
    stable_title = _stable_candidate_title(candidate)
    if stable_title is not None and not _candidate_title_matches(
        stable_title,
        title,
        original_title,
    ):
        return "title_mismatch"
    expected_years = tuple(
        dict.fromkeys(
            expected_year
            for expected_year in (year, *alternate_years)
            if expected_year is not None
        )
    )
    stable_years = _stable_candidate_years(candidate)
    if stable_years and expected_years and all(
        abs(candidate_year - expected_year) > 1
        for candidate_year in stable_years
        for expected_year in expected_years
    ):
        return "year_mismatch"
    if season is not None or episode is not None:
        candidate_text = f"{candidate.release_info} {candidate.title}".casefold()
        seasons, episodes = _episode_markers(candidate_text)
        if not seasons and not episodes:
            candidate_years = _release_years(candidate_text)
            if (
                expected_years
                and candidate_years
                and all(
                    all(
                        abs(candidate_year - expected_year) >= EPISODE_YEAR_MISMATCH_YEARS
                        for expected_year in expected_years
                    )
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
    year_evidence = [
        analyze_release_years(
            (candidate.title, candidate.release_info),
            expected_year=expected_year,
            expected_titles=(title, original_title),
        )
        for expected_year in expected_years
    ]
    if year_evidence and all(evidence.has_conflict for evidence in year_evidence):
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
    expected_values = [str(value).strip() for value in (title, original_title) if str(value or "").strip()]
    if len(candidate_identity) < 3 or not expected_values:
        return True
    for expected_value in expected_values:
        expected = _title_identity(expected_value)
        if not expected:
            continue
        if candidate_identity == expected:
            return True
        if candidate_identity.startswith(expected) and re.fullmatch(
            r"(?:19|20)\d{2}", candidate_identity[len(expected) :]
        ):
            return True
        if _english_title_matches(candidate_title, expected_value):
            return True
        if _chinese_title_matches(candidate_title, expected_value):
            return True
        if len(expected) >= 3 and SequenceMatcher(a=candidate_identity, b=expected).ratio() >= 0.72:
            return True
    return False


def _english_title_matches(actual: str, expected: str) -> bool:
    ignored = {
        "a",
        "an",
        "and",
        "for",
        "in",
        "of",
        "on",
        "the",
        "to",
        "ass",
        "bluray",
        "complete",
        "dl",
        "eng",
        "hdtv",
        "remux",
        "srt",
        "ssa",
        "sub",
        "subtitle",
        "subtitles",
        "web",
        "zh",
        "uk",
        "us",
        "usa",
    }

    def tokens(value: str) -> set[str]:
        value = re.sub(
            r"\b(?:season\s*\d{1,2}|episode\s*\d{1,3}|s\d{1,2}(?:e\d{1,3})?)\b",
            " ",
            value,
            flags=re.IGNORECASE,
        )
        return {
            token
            for token in re.findall(r"[a-z0-9]+", value.casefold())
            if token not in ignored
            and not re.fullmatch(r"(?:19|20)\d{2}", token)
            and not re.fullmatch(r"(?:s\d{1,2}(?:e\d{1,3})?|e\d{1,3}|\d{3,4}p)", token)
        }

    actual_tokens = tokens(actual)
    expected_tokens = tokens(expected)
    if not actual_tokens or not expected_tokens or not expected_tokens <= actual_tokens:
        return False
    allowed_extras = max(0, len(expected_tokens) // 3)
    return len(actual_tokens - expected_tokens) <= allowed_extras


def _chinese_title_matches(actual: str, expected: str) -> bool:
    def identity(value: str) -> str:
        value = re.sub(
            r"第\s*[一二三四五六七八九十百零两\d]+\s*(?:季|集)",
            "",
            value,
        )
        value = re.sub(r"(?:美|英|日|韩|法|德|台|港)版", "", value)
        chinese = "".join(re.findall(r"[\u4e00-\u9fff]+", value))
        return re.sub(r"(?:简繁|简体|繁体|中英|双语|中文)?字幕$", "", chinese)

    actual_identity = identity(actual)
    expected_identity = identity(expected)
    return bool(expected_identity) and actual_identity == expected_identity


def _stable_candidate_title(candidate: SubtitleCandidate) -> str | None:
    provider = candidate.provider.split(":", 1)[0].casefold()
    if provider == "assrt":
        return candidate.title
    if provider == "zimuku":
        value = str(candidate.raw_metadata.get("zimuku_work_title") or "").strip()
        return value or None
    if provider == "subdl":
        titles = candidate.raw_metadata.get("subdl_work_titles")
        if isinstance(titles, list):
            value = " ".join(str(title).strip() for title in titles if str(title).strip())
            return value or None
    return None


def _stable_candidate_years(candidate: SubtitleCandidate) -> set[int]:
    provider = candidate.provider.split(":", 1)[0].casefold()
    if provider == "zimuku":
        value = candidate.raw_metadata.get("zimuku_work_year")
        return {value} if isinstance(value, int) else set()
    if provider == "subdl":
        values = candidate.raw_metadata.get("subdl_work_years")
        if isinstance(values, list):
            return {value for value in values if isinstance(value, int)}
    return set()


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
    detected_seasons, detected_episodes = _episode_markers(text)
    if season in detected_seasons and episode in detected_episodes:
        return EPISODE_MATCH_BONUS
    if season in detected_seasons and not detected_episodes:
        return SEASON_PACK_MATCH_BONUS
    return -EPISODE_MISMATCH_PENALTY if detected_episodes else 0.0


def _episode_markers(text: str) -> tuple[set[int], set[int]]:
    seasons: set[int] = set()
    episodes: set[int] = set()
    for match in re.finditer(
        r"s(?P<season>\d{1,2})[ ._-]*e(?P<start>\d{1,3})[ ._-]*-[ ._-]*e?(?P<end>\d{1,3})",
        text,
    ):
        seasons.add(int(match.group("season")))
        start = int(match.group("start"))
        end = int(match.group("end"))
        if start <= end and end - start <= 100:
            episodes.update(range(start, end + 1))
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
