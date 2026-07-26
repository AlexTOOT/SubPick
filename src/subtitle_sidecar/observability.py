from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
from itertools import count
import json
from threading import Lock
from typing import Any


LOG_BUFFER_MAX_ENTRIES = 1000

_log_entries: deque[dict[str, Any]] = deque(maxlen=LOG_BUFFER_MAX_ENTRIES)
_log_id_counter = count(1)
_log_lock = Lock()
_SENSITIVE_FIELD_PARTS = ("token", "api_key", "apikey", "password", "secret", "authorization")


def emit_structured_log(**fields: Any) -> None:
    """Write one structured event to stdout and the in-process diagnostics buffer."""
    sanitized_fields = {
        key: _json_safe(value)
        for key, value in fields.items()
        if _should_include(key, value)
    }
    status = str(sanitized_fields.get("status") or "").lower()
    level = str(sanitized_fields.pop("level", "") or _default_level(sanitized_fields, status))
    event = str(sanitized_fields.pop("event", "log"))

    with _log_lock:
        payload = {
            "id": next(_log_id_counter),
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": level,
            "event": event,
            **sanitized_fields,
        }
        _log_entries.append(payload)
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), flush=True)


def list_structured_logs(
    *,
    after_id: int = 0,
    limit: int = 200,
    level: str | None = None,
    task_id: int | None = None,
    provider: str | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Return a stable, filtered snapshot of in-memory structured logs."""
    with _log_lock:
        entries = list(_log_entries)

    filtered = [
        entry
        for entry in entries
        if entry["id"] > after_id
        and (level is None or entry.get("level") == level)
        and (task_id is None or entry.get("task_id") == task_id)
        and (provider is None or entry.get("provider") == provider)
    ][:limit]
    next_after_id = filtered[-1]["id"] if filtered else after_id
    return filtered, next_after_id


def clear_log_buffer_for_tests() -> None:
    """Reset in-memory log state for deterministic tests."""
    global _log_id_counter
    with _log_lock:
        _log_entries.clear()
        _log_id_counter = count(1)


def _should_include(key: str, value: Any) -> bool:
    return value not in (None, "") and not _is_sensitive_key(key)


def _default_level(fields: dict[str, Any], status: str) -> str:
    if fields.get("error_code") or status in {"failed", "error", "interrupted"}:
        return "error"
    return "info"


def _is_sensitive_key(key: object) -> bool:
    normalized = str(key).lower().replace("-", "_")
    return any(part in normalized for part in _SENSITIVE_FIELD_PARTS)


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {
            str(key): _json_safe(item)
            for key, item in value.items()
            if not _is_sensitive_key(key)
        }
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return str(value)
