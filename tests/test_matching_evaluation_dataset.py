from __future__ import annotations

import json
from pathlib import Path
import re

import pytest

from subtitle_sidecar.pipeline.orchestrator import (
    _episode_identity_from_path,
    _movie_identity_from_path,
)
from subtitle_sidecar.pipeline.scoring import candidate_mismatch_reason
from subtitle_sidecar.pipeline.validator import validate_subtitle_file
from subtitle_sidecar.providers.base import SubtitleCandidate


DATASET_PATH = Path(__file__).parent / "fixtures" / "matching_evaluation.json"
DATASET = json.loads(DATASET_PATH.read_text(encoding="utf-8"))
CASES = DATASET["cases"]


def _candidate(spec: dict[str, object]) -> SubtitleCandidate:
    provider = str(spec["provider"])
    raw_metadata: dict[str, object] = {}
    if provider == "zimuku":
        raw_metadata = {
            "zimuku_work_title": spec["work_title"],
            "zimuku_work_year": spec["work_year"],
        }
    return SubtitleCandidate(
        provider=provider,
        language="zh-cn",
        is_bilingual=True,
        format="srt",
        title=str(spec["title"]),
        source_url=f"https://example.invalid/{provider}/{spec['title']}",
        release_info=str(spec["release_info"]),
        confidence=0.8,
        raw_metadata=raw_metadata,
    )


def _mismatch(case: dict[str, object], spec: dict[str, object]) -> str | None:
    return candidate_mismatch_reason(
        _candidate(spec),
        season=case.get("season"),
        episode=case.get("episode"),
        year=case["year"],
        alternate_years=tuple(case["alternate_years"]),
        title=str(case["title"]),
        original_title=str(case["original_title"]),
    )


def _srt_time(seconds: int) -> str:
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},000"


def test_dataset_has_movies_episodes_difficulty_and_opaque_dialogue_references() -> None:
    assert DATASET["metadata_source"] == "IMDb"
    assert len(CASES) >= 12
    assert {case["media_type"] for case in CASES} == {"movie", "episode"}
    assert {case["difficulty"] for case in CASES} >= {"easy", "medium", "hard"}
    assert len({case["id"] for case in CASES}) == len(CASES)
    assert sum("dialogue_reference" in case for case in CASES) >= 10
    for case in CASES:
        assert re.fullmatch(r"tt\d{7,9}", case["imdb_id"])
        assert str(case["metadata_url"]).startswith("https://www.imdb.com/title/")
        reference = case.get("dialogue_reference")
        if reference is None:
            continue
        assert reference["source_urls"]
        assert all(
            re.fullmatch(r"[0-9a-f]{64}", fingerprint)
            for fingerprint in reference["fingerprints"]
        )


@pytest.mark.parametrize("case", CASES, ids=[case["id"] for case in CASES])
def test_dataset_path_identity(case: dict[str, object]) -> None:
    path = Path(str(case["path"]))
    if case["media_type"] == "movie":
        assert _movie_identity_from_path(path) == (case["path_title"], case["year"])
        return
    identity = _episode_identity_from_path(path)
    assert identity == (
        case["season"],
        case["episode"],
        case["path_title"],
        case["year"],
    )


@pytest.mark.parametrize("case", CASES, ids=[case["id"] for case in CASES])
def test_dataset_accepts_correct_identity_and_timeline(
    case: dict[str, object],
    tmp_path: Path,
) -> None:
    assert _mismatch(case, case["positive_candidate"]) is None
    subtitle = tmp_path / f"{case['id']}.srt"
    last_cue = int(case["last_cue_seconds"])
    subtitle.write_text(
        "1\n"
        f"{_srt_time(last_cue - 1)} --> {_srt_time(last_cue)}\n"
        "正确字幕\n",
        encoding="utf-8",
    )
    result = validate_subtitle_file(
        subtitle,
        video_duration_seconds=float(case["runtime_seconds"]),
    )
    assert result.is_valid is True


@pytest.mark.parametrize(
    ("case", "spec"),
    [
        (case, spec)
        for case in CASES
        for spec in case["negative_candidates"]
    ],
    ids=[
        f"{case['id']}-{index}"
        for case in CASES
        for index, _ in enumerate(case["negative_candidates"], start=1)
    ],
)
def test_dataset_rejects_known_decoys(
    case: dict[str, object],
    spec: dict[str, object],
) -> None:
    assert _mismatch(case, spec) == spec["expected_reason"]
