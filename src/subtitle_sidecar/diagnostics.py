from __future__ import annotations

import os
from pathlib import Path
import shutil
from typing import Any

from subtitle_sidecar import (
    ADAPTER_VERSIONS,
    DATABASE_SCHEMA_VERSION,
    RUNTIME_METADATA_SETTING_KEY,
    __version__,
)
from subtitle_sidecar.config import (
    AppSettings,
    merge_assrt_provider_settings,
    merge_subdl_provider_settings,
    merge_subliminal_provider_settings,
    merge_zimuku_provider_settings,
)
from subtitle_sidecar.db.repository import Repository
from subtitle_sidecar.db.session import session_scope


JELLYFIN_SETTING_KEY = "jellyfin"


def build_diagnostics(
    settings: AppSettings,
    task_queue: Any,
    engine: Any,
    provider_scheduler: Any | None = None,
) -> dict[str, Any]:
    """Build a local-only health snapshot without invoking external services."""
    data_dir = _path_check(Path(settings.data_dir))
    database = _path_check(Path(settings.data_dir) / "subtitle-sidecar.sqlite3")
    tools = [
        _tool_check("ffprobe", settings.probe.ffprobe_path),
        _tool_check("mkvmerge", settings.probe.mkvmerge_path),
        _tool_check("ffsubsync", "ffsubsync"),
    ]
    jellyfin_configured = _jellyfin_configured(settings, engine)
    runtime_metadata = _runtime_metadata(engine)
    database_schema_version = int(runtime_metadata.get("database_schema_version", 0) or 0)
    compatibility_status = (
        "upgrade_required"
        if database_schema_version > DATABASE_SCHEMA_VERSION
        else "ok"
    )
    checks = [
        _check("data_dir", data_dir["status"]),
        _check("database", database["status"]),
        *[_check(f"tool:{item['name']}", item["status"]) for item in tools],
    ]
    overall_status = "degraded" if any(item["status"] == "degraded" for item in checks) else "ok"

    cooldowns = _provider_cooldowns(provider_scheduler)
    return {
        "version": __version__,
        "components": {
            "sidecar": __version__,
            **{f"{name}_adapter": version for name, version in ADAPTER_VERSIONS.items()},
            "subliminal": _package_version("subliminal"),
            "ffsubsync": _package_version("ffsubsync"),
        },
        "compatibility": {
            "status": compatibility_status,
            "config_version": settings.config_version,
            "database_schema_version": database_schema_version,
            "supported_database_schema_version": DATABASE_SCHEMA_VERSION,
        },
        "overall_status": overall_status,
        "queue": {
            "active_task_id": getattr(task_queue, "_active_task_id", None)
            or getattr(task_queue, "_active_preflight_task_id", None),
            "queued_count": _queued_count(task_queue),
            "search_interval_seconds": float(getattr(task_queue, "interval_seconds", 0.0)),
            "provider_cooldowns": cooldowns,
            "next_provider_ready_seconds": min(cooldowns.values(), default=0.0),
        },
        "providers": {
            "subliminal": _subliminal_diagnostic(settings, engine),
            "assrt": _assrt_diagnostic(settings, engine),
            "subdl": _subdl_diagnostic(settings, engine),
            "zimuku": _zimuku_diagnostic(settings, engine),
        },
        "jellyfin": {"configured": jellyfin_configured},
        "tools": tools,
        "data_dir": data_dir,
        "database": database,
        "logging": {
            "retention_days": settings.logging.retention_days,
            "max_task_events": settings.logging.max_task_events,
        },
        "checks": checks,
    }


def _provider_cooldowns(provider_scheduler: Any | None) -> dict[str, float]:
    snapshot = getattr(provider_scheduler, "snapshot", None)
    if not callable(snapshot):
        return {}
    try:
        values = snapshot()
    except Exception:
        return {}
    return {
        str(name): round(max(0.0, float(item.get("remaining_seconds", 0.0))), 1)
        for name, item in values.items()
        if isinstance(item, dict)
    }


def _path_check(path: Path) -> dict[str, Any]:
    try:
        exists = path.exists()
        readable = exists and os.access(path, os.R_OK)
        writable = exists and os.access(path, os.W_OK)
    except OSError:
        exists = readable = writable = False
    return {
        "path": str(path),
        "exists": exists,
        "readable": readable,
        "writable": writable,
        "status": "ok" if exists and readable and writable else "degraded",
    }


def _tool_check(name: str, executable: str) -> dict[str, Any]:
    try:
        available = shutil.which(executable) is not None
    except (OSError, ValueError):
        available = False
    return {
        "name": name,
        "executable": executable,
        "available": available,
        "status": "ok" if available else "degraded",
    }


def _queued_count(task_queue: Any) -> int:
    try:
        return int(task_queue._queue.qsize()) + int(task_queue._preflight_queue.qsize())
    except (AttributeError, NotImplementedError):
        return 0


def _jellyfin_configured(settings: AppSettings, engine: Any) -> bool:
    config = settings.jellyfin.model_dump()
    try:
        with session_scope(engine) as session:
            stored = Repository(session).get_setting(JELLYFIN_SETTING_KEY) or {}
        config.update({key: value for key, value in stored.items() if value})
    except Exception:
        return False
    return bool(config.get("server_url") and config.get("api_key"))


def _runtime_metadata(engine: Any) -> dict[str, Any]:
    try:
        with session_scope(engine) as session:
            value = Repository(session).get_setting(RUNTIME_METADATA_SETTING_KEY)
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def _subliminal_diagnostic(settings: AppSettings, engine: Any) -> dict[str, Any]:
    stored = None
    try:
        with session_scope(engine) as session:
            stored = Repository(session).get_setting("subliminal")
    except Exception:
        stored = None
    config = merge_subliminal_provider_settings(settings.providers.subliminal, stored)
    enabled = config.enabled
    return {"enabled": enabled, "status": "ok" if enabled else "disabled"}


def _assrt_diagnostic(settings: AppSettings, engine: Any) -> dict[str, Any]:
    stored = None
    try:
        with session_scope(engine) as session:
            stored = Repository(session).get_setting("assrt")
    except Exception:
        stored = None
    config = merge_assrt_provider_settings(settings.providers.assrt, stored)
    if not config.enabled:
        status = "disabled"
    elif not config.token:
        status = "unconfigured"
    else:
        status = "configured"
    return {"enabled": config.enabled, "status": status}


def _subdl_diagnostic(settings: AppSettings, engine: Any) -> dict[str, Any]:
    stored = None
    try:
        with session_scope(engine) as session:
            stored = Repository(session).get_setting("subdl")
    except Exception:
        stored = None
    config = merge_subdl_provider_settings(settings.providers.subdl, stored)
    if not config.enabled:
        status = "disabled"
    elif not config.api_key:
        status = "unconfigured"
    else:
        status = "configured"
    return {"enabled": config.enabled, "status": status}


def _zimuku_diagnostic(settings: AppSettings, engine: Any) -> dict[str, Any]:
    stored = None
    try:
        with session_scope(engine) as session:
            stored = Repository(session).get_setting("zimuku")
    except Exception:
        stored = None
    config = merge_zimuku_provider_settings(settings.providers.zimuku, stored)
    if not config.enabled:
        status = "disabled"
    elif not config.moviepilot_ocr_url and not config.anti_captcha_api_key:
        status = "unconfigured"
    else:
        status = "configured"
    return {"enabled": config.enabled, "status": status}


def _check(name: str, status: str) -> dict[str, str]:
    return {"name": name, "status": status}


def _package_version(name: str) -> str | None:
    try:
        from importlib.metadata import version
        return version(name)
    except Exception:
        return None
