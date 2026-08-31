from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import random


MAX_AUTO_RETRIES = 3

_RETRY_DELAYS: dict[str, tuple[float, ...]] = {
    "interrupted": (0.0, 5 * 60.0),
    "provider_network": (60.0, 5 * 60.0, 30 * 60.0),
    "download_place": (2 * 60.0, 10 * 60.0, 60 * 60.0),
    "identity": (60.0, 5 * 60.0, 30 * 60.0),
    "timeline_quality": (10 * 60.0, 6 * 60 * 60.0, 24 * 60 * 60.0),
    "no_candidate": (6 * 60 * 60.0, 24 * 60 * 60.0, 72 * 60 * 60.0),
}


@dataclass(frozen=True)
class RetryDecision:
    category: str
    attempt: int
    delay_seconds: float


def classify_retry_error(status: str, error_code: str | None) -> str | None:
    normalized = str(error_code or "").strip().casefold()
    if status == "interrupted" or normalized.startswith("interrupted"):
        return "interrupted"
    if normalized in {"no_candidate_found", "no_compatible_provider"}:
        return "no_candidate"
    if normalized in {
        "timeline_exceeds_video",
        "timeline_too_short",
        "low_quality_alignment",
        "speech_not_detected",
        "episode_content_duplicate",
        "retry_candidate_content_duplicate",
        "missing_chinese",
        "missing_timestamps",
        "missing_events_section",
        "missing_dialogue_lines",
        "empty_file",
        "decode_error",
        "unsupported_extension",
    } or "low_quality" in normalized:
        return "timeline_quality"
    if normalized.startswith("nfo_") or normalized in {
        "video_not_found",
        "media_identity_missing",
        "identity_unavailable",
    }:
        return "identity"
    if normalized in {"all_providers_failed", "provider_search_failed"} or any(
        marker in normalized
        for marker in (
            "provider_request",
            "network",
            "connection",
            "timeout",
            "http_error",
            "rate_limit",
        )
    ):
        return "provider_network"
    if any(
        marker in normalized
        for marker in (
            "download",
            "placement",
            "destination",
            "archive_extract",
            "sync_failed",
        )
    ):
        return "download_place"
    return None


def default_retry_jitter(delay_seconds: float) -> float:
    if delay_seconds <= 0:
        return 0.0
    return delay_seconds * random.uniform(0.9, 1.1)


def retry_decision(
    *,
    status: str,
    error_code: str | None,
    completed_auto_retries: int,
    jitter: Callable[[float], float] = default_retry_jitter,
) -> RetryDecision | None:
    category = classify_retry_error(status, error_code)
    if category is None:
        return None
    attempt = max(0, int(completed_auto_retries)) + 1
    delays = _RETRY_DELAYS[category]
    if attempt > min(MAX_AUTO_RETRIES, len(delays)):
        return None
    return RetryDecision(
        category=category,
        attempt=attempt,
        delay_seconds=max(0.0, float(jitter(delays[attempt - 1]))),
    )
