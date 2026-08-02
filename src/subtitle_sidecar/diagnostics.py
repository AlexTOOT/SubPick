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
    config_file = _path_check(
        Path(settings.runtime_config_path)
        if settings.runtime_config_path is not None
        else Path(settings.data_dir) / "config.yaml"
    )
    data_dir = _path_check(Path(settings.data_dir))
    cache_dir = _path_check(Path(settings.cache_dir))
    media_dir = _path_check(Path("/media"))
    database = _path_check(Path(settings.data_dir) / "subtitle-sidecar.sqlite3")
    tools = [
        _tool_check("ffprobe", settings.probe.ffprobe_path),
        _tool_check("mkvmerge", settings.probe.mkvmerge_path),
        _tool_check("ffsubsync", "ffsubsync"),
    ]
    jellyfin_configured = _jellyfin_configured(settings, engine)
    runtime_metadata = _runtime_metadata(engine)
    jellyfin = {
        "configured": jellyfin_configured,
        "connected": runtime_metadata.get("jellyfin_last_check_status") == "ok",
        "last_checked_at": runtime_metadata.get("jellyfin_last_checked_at"),
    }
    moviepilot = _moviepilot_diagnostic(settings, runtime_metadata)
    database_schema_version = int(runtime_metadata.get("database_schema_version", 0) or 0)
    compatibility_status = (
        "upgrade_required"
        if database_schema_version > DATABASE_SCHEMA_VERSION
        else "ok"
    )
    checks = [
        _check("config_file", config_file["status"]),
        _check("data_dir", data_dir["status"]),
        _check("cache_dir", cache_dir["status"]),
        _check("media_dir", media_dir["status"]),
        _check("database", database["status"]),
        *[_check(f"tool:{item['name']}", item["status"]) for item in tools],
    ]
    cooldowns = _provider_cooldowns(provider_scheduler)
    providers = {
        "subliminal": _subliminal_diagnostic(settings, engine),
        "assrt": _assrt_diagnostic(settings, engine, runtime_metadata),
        "subdl": _subdl_diagnostic(settings, engine, runtime_metadata),
        "zimuku": _zimuku_diagnostic(settings, engine, runtime_metadata),
    }
    provider_failed = any(
        item.get("enabled") and item.get("status") in {"failed", "error", "unavailable"}
        for item in providers.values()
    )
    overall_status = (
        "degraded"
        if provider_failed or any(item["status"] == "degraded" for item in checks)
        else "ok"
    )
    setup = _setup_status(
        config_file=config_file,
        data_dir=data_dir,
        cache_dir=cache_dir,
        media_dir=media_dir,
        database=database,
        moviepilot=moviepilot,
        jellyfin=jellyfin,
        providers=providers,
        runtime_metadata=runtime_metadata,
    )
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
        "providers": providers,
        "jellyfin": jellyfin,
        "moviepilot": moviepilot,
        "setup": setup,
        "tools": tools,
        "config_file": config_file,
        "data_dir": data_dir,
        "cache_dir": cache_dir,
        "media_dir": media_dir,
        "database": database,
        "logging": {
            "retention_days": settings.logging.retention_days,
            "max_task_events": settings.logging.max_task_events,
        },
        "checks": checks,
    }


def _moviepilot_diagnostic(
    settings: AppSettings,
    runtime_metadata: dict[str, Any],
) -> dict[str, Any]:
    last_callback_at = runtime_metadata.get("moviepilot_last_callback_at")
    return {
        "token_configured": bool(settings.server.token),
        "connected": bool(last_callback_at),
        "last_callback_at": str(last_callback_at) if last_callback_at else None,
        "last_received_path": runtime_metadata.get("moviepilot_last_received_path"),
    }


def _setup_status(
    *,
    config_file: dict[str, Any],
    data_dir: dict[str, Any],
    cache_dir: dict[str, Any],
    media_dir: dict[str, Any],
    database: dict[str, Any],
    moviepilot: dict[str, Any],
    jellyfin: dict[str, Any],
    providers: dict[str, dict[str, Any]],
    runtime_metadata: dict[str, Any],
) -> dict[str, Any]:
    storage_ready = all(
        item["status"] == "ok"
        for item in (config_file, data_dir, cache_dir, database)
    )
    provider_ready = any(
        item.get("enabled") and item.get("status") == "ok"
        for item in providers.values()
    )
    steps = [
        _setup_step(
            "jellyfin",
            "Jellyfin",
            bool(jellyfin.get("connected")),
            "settings",
            "jellyfin",
        ),
        _setup_step("provider", "字幕来源", provider_ready, "settings", "providers"),
        {
            "id": "moviepilot",
            "label": "MoviePilot",
            "status": (
                "ready"
                if moviepilot["connected"]
                else "waiting"
                if moviepilot["token_configured"]
                else "missing"
            ),
            "target_view": "settings",
            "target_section": "system",
            "help": (
                "需要成功接收到一次 MoviePilot 的鉴权调用后才会显示已连接。"
                if moviepilot["token_configured"] and not moviepilot["connected"]
                else None
            ),
        },
    ]
    notifications: list[dict[str, Any]] = []
    if not storage_ready:
        notifications.append(
            _notification(
                "storage-unavailable",
                "error",
                "运行目录不可用",
                "请检查拾幕主目录是否可写。",
                "settings",
                "health",
            )
        )
    if media_dir["status"] != "ok":
        notifications.append(
            _notification(
                "media-unavailable",
                "error",
                "媒体目录不可用",
                "请确认 Compose 中已把 MoviePilot 的完整媒体目录挂载到 /media，并允许写入字幕。",
                "settings",
                "paths",
            )
        )
    path_issue = runtime_metadata.get("moviepilot_path_issue")
    if isinstance(path_issue, dict) and path_issue.get("received_path"):
        notifications.append(
            _notification(
                "moviepilot-path",
                "error",
                "MoviePilot 路径无法访问",
                f"最近收到的路径无法在容器中找到：{path_issue['received_path']}",
                "settings",
                "paths",
            )
        )
    if moviepilot["token_configured"] and not moviepilot["connected"]:
        notifications.append(
            _notification(
                "moviepilot-waiting",
                "info",
                "MoviePilot 等待验证",
                "保存 Token 后，需要成功接收到一次 MoviePilot 调用才能确认连接。",
                "settings",
                "system",
            )
        )
    if not jellyfin.get("configured"):
        notifications.append(
            _notification(
                "jellyfin-missing",
                "warning",
                "尚未连接 Jellyfin",
                "连接后才能浏览媒体库、海报和字幕状态。",
                "settings",
                "jellyfin",
            )
        )
    elif not jellyfin.get("connected"):
        notifications.append(
            _notification(
                "jellyfin-unverified",
                "info",
                "Jellyfin 等待连接测试",
                "配置已保存，请执行一次连接测试确认地址、API Key 与用户可用。",
                "settings",
                "jellyfin",
            )
        )
    if not provider_ready:
        notifications.append(
            _notification(
                "provider-missing",
                "warning",
                "尚无可用字幕来源",
                "请至少启用并配置一个 Provider。",
                "settings",
                "providers",
            )
        )
    if (
        providers.get("zimuku", {}).get("enabled")
        and runtime_metadata.get("zimuku_ocr_last_check_status") != "ok"
    ):
        notifications.append(
            _notification(
                "ocr-unverified",
                "warning",
                "Zimuku OCR 尚未通过实图测试",
                "请在 Zimuku 设置中执行 OCR 实图检查。",
                "settings",
                "providers",
            )
        )
    return {
        "completed": all(step["status"] == "ready" for step in steps),
        "dismissed": bool(runtime_metadata.get("setup_wizard_dismissed")),
        "steps": steps,
        "notifications": notifications,
    }


def _setup_step(
    step_id: str,
    label: str,
    ready: bool,
    target_view: str,
    target_section: str | None = None,
) -> dict[str, Any]:
    return {
        "id": step_id,
        "label": label,
        "status": "ready" if ready else "missing",
        "target_view": target_view,
        "target_section": target_section,
        "help": None,
    }


def _notification(
    notification_id: str,
    level: str,
    title: str,
    message: str,
    target_view: str,
    target_section: str | None = None,
) -> dict[str, Any]:
    return {
        "id": notification_id,
        "level": level,
        "title": title,
        "message": message,
        "target_view": target_view,
        "target_section": target_section,
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
    return {
        "enabled": enabled,
        "status": "ok" if enabled else "disabled",
        "last_checked_at": None,
    }


def _assrt_diagnostic(
    settings: AppSettings,
    engine: Any,
    runtime_metadata: dict[str, Any],
) -> dict[str, Any]:
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
        status = _stored_provider_status(runtime_metadata, "assrt")
    return {
        "enabled": config.enabled,
        "status": status,
        "last_checked_at": runtime_metadata.get("assrt_last_checked_at"),
    }


def _subdl_diagnostic(
    settings: AppSettings,
    engine: Any,
    runtime_metadata: dict[str, Any],
) -> dict[str, Any]:
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
        status = _stored_provider_status(runtime_metadata, "subdl")
    return {
        "enabled": config.enabled,
        "status": status,
        "last_checked_at": runtime_metadata.get("subdl_last_checked_at"),
    }


def _zimuku_diagnostic(
    settings: AppSettings,
    engine: Any,
    runtime_metadata: dict[str, Any],
) -> dict[str, Any]:
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
        status = _stored_provider_status(runtime_metadata, "zimuku")
    return {
        "enabled": config.enabled,
        "status": status,
        "last_checked_at": runtime_metadata.get("zimuku_last_checked_at")
        or runtime_metadata.get("zimuku_ocr_last_checked_at"),
    }


def _stored_provider_status(runtime_metadata: dict[str, Any], name: str) -> str:
    value = runtime_metadata.get(f"{name}_last_check_status")
    if name == "zimuku" and value is None:
        value = runtime_metadata.get("zimuku_ocr_last_check_status")
    if value == "ok":
        return "ok"
    if value in {"failed", "error"}:
        return "failed"
    return "unverified"


def _check(name: str, status: str) -> dict[str, str]:
    return {"name": name, "status": status}


def _package_version(name: str) -> str | None:
    try:
        from importlib.metadata import version
        return version(name)
    except Exception:
        return None
