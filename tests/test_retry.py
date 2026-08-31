from __future__ import annotations

import pytest

from subtitle_sidecar.retry import classify_retry_error, retry_decision


@pytest.mark.parametrize(
    ("status", "error_code", "category"),
    [
        ("interrupted", "interrupted_by_restart", "interrupted"),
        ("failed", "provider_request_timeout", "provider_network"),
        ("failed", "download_failed", "download_place"),
        ("failed", "nfo_not_found", "identity"),
        ("failed", "timeline_too_short", "timeline_quality"),
        ("failed", "missing_chinese", "timeline_quality"),
        ("failed", "episode_content_duplicate", "timeline_quality"),
        ("failed", "no_candidate_found", "no_candidate"),
    ],
)
def test_retry_error_classification(status: str, error_code: str, category: str) -> None:
    assert classify_retry_error(status, error_code) == category


def test_retry_delays_are_chain_counted_and_do_not_reset_when_error_changes() -> None:
    first = retry_decision(
        status="failed",
        error_code="provider_request_timeout",
        completed_auto_retries=0,
        jitter=lambda value: value,
    )
    changed_error = retry_decision(
        status="failed",
        error_code="timeline_too_short",
        completed_auto_retries=1,
        jitter=lambda value: value,
    )
    exhausted = retry_decision(
        status="failed",
        error_code="no_candidate_found",
        completed_auto_retries=3,
        jitter=lambda value: value,
    )

    assert first is not None
    assert (first.attempt, first.delay_seconds) == (1, 60)
    assert changed_error is not None
    assert (changed_error.attempt, changed_error.delay_seconds) == (2, 6 * 60 * 60)
    assert exhausted is None


def test_retry_jitter_is_injectable() -> None:
    decision = retry_decision(
        status="failed",
        error_code="no_candidate_found",
        completed_auto_retries=0,
        jitter=lambda value: value * 0.9,
    )

    assert decision is not None
    assert decision.delay_seconds == 6 * 60 * 60 * 0.9
