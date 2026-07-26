from __future__ import annotations


TASK_QUEUED = "queued"
TASK_RUNNING = "running"
TASK_RESOLVING = "resolving"
TASK_CHECKING_EXISTING = "checking_existing"
TASK_CHECKING_EMBEDDED = "checking_embedded"
TASK_SEARCHING = "searching"
TASK_DOWNLOADING = "downloading"
TASK_VALIDATING = "validating"
TASK_SYNCING = "syncing"
TASK_PLACING = "placing"
TASK_COMPLETED = "completed"
TASK_FAILED = "failed"
TASK_INTERRUPTED = "interrupted"
TASK_SKIPPED_EXISTING_SUBTITLE = "skipped_existing_subtitle"
TASK_SKIPPED_EMBEDDED_SUBTITLE = "skipped_embedded_subtitle"

JOB_QUEUED = "queued"
JOB_RUNNING = "running"
JOB_COMPLETED = "completed"
JOB_FAILED = "failed"
JOB_PARTIAL = "partial"

ACTIVE_TASK_STATUSES = {
    TASK_RUNNING,
    TASK_RESOLVING,
    TASK_CHECKING_EXISTING,
    TASK_CHECKING_EMBEDDED,
    TASK_SEARCHING,
    TASK_DOWNLOADING,
    TASK_VALIDATING,
    TASK_SYNCING,
    TASK_PLACING,
}

SUCCESS_TASK_STATUSES = {
    TASK_COMPLETED,
    TASK_SKIPPED_EXISTING_SUBTITLE,
    TASK_SKIPPED_EMBEDDED_SUBTITLE,
}

FAILED_TASK_STATUSES = {
    TASK_FAILED,
    TASK_INTERRUPTED,
}

TERMINAL_TASK_STATUSES = SUCCESS_TASK_STATUSES | FAILED_TASK_STATUSES


def summarize_job_status(task_statuses: list[str]) -> str:
    if not task_statuses:
        return JOB_COMPLETED

    statuses = set(task_statuses)
    if statuses <= {TASK_QUEUED}:
        return JOB_QUEUED
    if statuses <= SUCCESS_TASK_STATUSES:
        return JOB_COMPLETED
    if statuses <= FAILED_TASK_STATUSES:
        return JOB_FAILED
    if any(status not in TERMINAL_TASK_STATUSES for status in statuses):
        return JOB_RUNNING
    return JOB_PARTIAL
