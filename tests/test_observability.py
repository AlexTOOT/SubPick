import json

from subtitle_sidecar.observability import (
    LOG_BUFFER_MAX_ENTRIES,
    clear_log_buffer_for_tests,
    emit_structured_log,
    list_structured_logs,
)


def setup_function() -> None:
    clear_log_buffer_for_tests()


def test_emit_structured_log_writes_stdout_and_redacted_buffer_entry(capsys) -> None:
    emit_structured_log(
        event="subtitle_download",
        task_id=42,
        provider="fake",
        status="failed",
        error_code="download_failed",
        message="provider returned an error",
        api_key="never-record-this",
        details={"token": "also-never-record-this", "attempt": 1},
    )

    stdout_entry = json.loads(capsys.readouterr().out)
    entries, next_after_id = list_structured_logs()

    assert stdout_entry == entries[0]
    assert entries[0]["id"] == 1
    assert entries[0]["level"] == "error"
    assert entries[0]["event"] == "subtitle_download"
    assert entries[0]["details"] == {"attempt": 1}
    assert "api_key" not in entries[0]
    assert "token" not in entries[0]["details"]
    assert next_after_id == 1


def test_structured_log_buffer_is_bounded_and_supports_cursor_filters(capsys) -> None:
    for index in range(LOG_BUFFER_MAX_ENTRIES + 2):
        emit_structured_log(
            event="task_event",
            task_id=1 if index % 2 else 2,
            provider="wanted" if index % 3 == 0 else "other",
            status="failed" if index % 5 == 0 else "completed",
        )
    capsys.readouterr()

    default_entries, _ = list_structured_logs()
    entries, _ = list_structured_logs(limit=LOG_BUFFER_MAX_ENTRIES)
    filtered, next_after_id = list_structured_logs(
        after_id=entries[0]["id"],
        limit=2,
        level="error",
        task_id=1,
        provider="wanted",
    )

    assert len(default_entries) == 200
    assert len(entries) == LOG_BUFFER_MAX_ENTRIES
    assert entries[0]["id"] == 3
    assert all(entry["id"] > entries[0]["id"] for entry in filtered)
    assert all(entry["level"] == "error" for entry in filtered)
    assert all(entry["task_id"] == 1 for entry in filtered)
    assert all(entry["provider"] == "wanted" for entry in filtered)
    assert next_after_id == filtered[-1]["id"]
