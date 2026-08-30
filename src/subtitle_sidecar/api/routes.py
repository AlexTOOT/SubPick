from __future__ import annotations

import asyncio
import secrets
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated
from urllib.parse import quote

import httpx
import yaml
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from starlette.responses import StreamingResponse

from subtitle_sidecar import RUNTIME_METADATA_SETTING_KEY
from subtitle_sidecar.api.schemas import (
    AddJobRequest,
    AddJobResponse,
    BatchDeleteTaskResultResponse,
    BatchDeleteTasksRequest,
    BatchDeleteTasksResponse,
    BatchRetryTaskResultResponse,
    BatchRetryTasksResponse,
    BatchTaskRequest,
    JobListItemResponse,
    JobResponse,
    JellyfinCreateTaskResultResponse,
    JellyfinCreateTasksRequest,
    JellyfinCreateTasksResponse,
    JellyfinLibrariesResponse,
    JellyfinLibraryTreeResponse,
    JellyfinLibraryResponse,
    JellyfinBatchIgnoreRequest,
    JellyfinBatchIgnoreResponse,
    JellyfinIgnoreResponse,
    JellyfinMediaItemResponse,
    JellyfinMediaItemsResponse,
    JellyfinRecentMediaItemsResponse,
    JellyfinRecentMediaResponse,
    JellyfinScanResponse,
    JellyfinSettingsRequest,
    JellyfinSettingsResponse,
    JellyfinConnectionCheckResponse,
    GitHubSettingsRequest,
    GitHubSettingsResponse,
    HealthCheckRunRequest,
    PathMappingTestRequest,
    PathMappingTestResponse,
    PathMappingResponse,
    PathSettingsRequest,
    PathSettingsResponse,
    ServerSettingsRequest,
    ServerSettingsResponse,
    SetupWizardStateRequest,
    SetupWizardStateResponse,
    RetryTaskResponse,
    DiagnosticsResponse,
    SubtitleArtifactResponse,
    SubtitleCandidateResponse,
    StructuredLogsResponse,
    TaskEventResponse,
    VideoTaskDetailResponse,
    VideoTaskSummaryResponse,
    AssrtProviderSettingsRequest,
    AssrtProviderSettingsResponse,
    AssrtQuotaResponse,
    SubdlProviderSettingsRequest,
    SubdlProviderSettingsResponse,
    SubdlUsageResponse,
    ZimukuCaptchaBalanceResponse,
    LogProvidersResponse,
    ProviderAdapterResponse,
    ProviderCapabilitiesResponse,
    ProviderOrderRequest,
    ProviderOrderResponse,
    ZimukuOcrCheckResponse,
    ZimukuProviderSettingsRequest,
    ZimukuProviderSettingsResponse,
    SubliminalProviderAuthenticationResponse,
    SubliminalProviderSettingsRequest,
    SubliminalProviderSettingsResponse,
    SubliminalUpdateCheckResponse,
    DependencyUpdateChecksResponse,
)
from subtitle_sidecar.config import (
    AppSettings,
    AssrtProviderSettings,
    PathMapping,
    PathsSettings,
    SubdlProviderSettings,
    merge_assrt_provider_settings,
    merge_subdl_provider_settings,
    merge_subliminal_provider_settings,
    merge_zimuku_provider_settings,
)
from subtitle_sidecar.db.models import (
    Job,
    SubtitleArtifact,
    SubtitleCandidateRecord,
    SystemEvent,
    TaskEvent,
    VideoTask,
)
from subtitle_sidecar.db.repository import JellyfinMediaItemData, JobCreate, Repository
from subtitle_sidecar.db.session import session_scope
from subtitle_sidecar.jellyfin.client import JellyfinClient
from subtitle_sidecar.jellyfin.subtitle_status import SubtitleStatus, detect_subtitle_status
from subtitle_sidecar.media.subtitles import SUBTITLE_EXTENSIONS
from subtitle_sidecar.media.resolver import MediaResolver
from subtitle_sidecar.pipeline.status import TASK_COMPLETED
from subtitle_sidecar.diagnostics import build_diagnostics
from subtitle_sidecar.observability import emit_structured_log
from subtitle_sidecar.providers.assrt_adapter import AssrtProvider
from subtitle_sidecar.providers.adapters import discover_adapter_factories
from subtitle_sidecar.providers.subdl_adapter import SubdlProvider
from subtitle_sidecar.providers.zimuku_adapter import MoviePilotOcrSolver, ZimukuProvider
from subtitle_sidecar.providers.subliminal_update import check_ffsubsync_update, check_subliminal_update


JELLYFIN_SETTING_KEY = "jellyfin"
SUBLIMINAL_SETTING_KEY = "subliminal"
ASSRT_SETTING_KEY = "assrt"
SUBDL_SETTING_KEY = "subdl"
ZIMUKU_SETTING_KEY = "zimuku"
GITHUB_SETTING_KEY = "github"
SERVER_SETTING_KEY = "server"
PATHS_SETTING_KEY = "paths"
PROVIDER_ORDER_SETTING_KEY = "provider_order"
BACKUP_SETTING_KEYS = (
    JELLYFIN_SETTING_KEY,
    SUBLIMINAL_SETTING_KEY,
    ASSRT_SETTING_KEY,
    SUBDL_SETTING_KEY,
    ZIMUKU_SETTING_KEY,
    GITHUB_SETTING_KEY,
    SERVER_SETTING_KEY,
    PATHS_SETTING_KEY,
    PROVIDER_ORDER_SETTING_KEY,
)


def _require_bearer_token(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    token = request.app.state.settings.server.token
    if token == "":
        return
    if authorization is None:
        raise HTTPException(status_code=401, detail="Unauthorized")
    scheme, _, value = authorization.partition(" ")
    if scheme != "Bearer" or not secrets.compare_digest(value, token):
        raise HTTPException(status_code=401, detail="Unauthorized")


def create_api_router() -> APIRouter:
    router = APIRouter(prefix="/api/v1")

    @router.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @router.get(
        "/logs",
        response_model=StructuredLogsResponse,
        response_model_exclude_none=True,
    )
    def list_logs(
        request: Request,
        after_id: int = Query(default=0, ge=0),
        limit: int = Query(default=200, ge=1, le=500),
        level: str | None = None,
        task_id: int | None = Query(default=None, ge=1),
        category: str | None = None,
    ) -> StructuredLogsResponse:
        with session_scope(request.app.state.engine) as session:
            events = Repository(session).list_system_events(
                after_id=after_id,
                limit=limit,
                level=level,
                task_id=task_id,
                category=category,
            )
        entries = [_to_system_event_log(event) for event in events]
        next_after_id = entries[-1]["id"] if entries else after_id
        return StructuredLogsResponse(entries=entries, next_after_id=next_after_id)

    @router.get("/logs/providers", response_model=LogProvidersResponse)
    def list_log_providers(request: Request) -> LogProvidersResponse:
        """Return configured top-level providers plus names found in retained logs."""
        providers: set[str] = set()
        with session_scope(request.app.state.engine) as session:
            repo = Repository(session)
            configs = (
                ("subliminal", _load_subliminal_config(request, repo)),
                ("assrt", _load_assrt_config(request, repo)),
                ("subdl", _load_subdl_config(request, repo)),
                ("zimuku", _load_zimuku_config(request, repo)),
            )
            providers.update(name for name, config in configs if config.enabled)
            for event, _job_id in repo.list_task_event_logs(limit=500):
                details = event.details_json if isinstance(event.details_json, dict) else {}
                provider = details.get("provider")
                if isinstance(provider, str) and provider.strip():
                    providers.add(provider.strip())
        return LogProvidersResponse(providers=sorted(providers, key=str.casefold))

    @router.get("/events", include_in_schema=False)
    async def stream_events(
        request: Request,
        after_id: int = Query(default=0, ge=0),
        after_log_id: int = Query(default=0, ge=0),
    ) -> StreamingResponse:
        try:
            last_event_id = max(after_id, int(request.headers.get("last-event-id", "0")))
        except ValueError:
            last_event_id = after_id
        last_log_id = after_log_id

        async def event_stream():
            nonlocal last_event_id, last_log_id
            while not await request.is_disconnected():
                with session_scope(request.app.state.engine) as session:
                    repo = Repository(session)
                    events = repo.list_task_events_after_id(last_event_id)
                    system_events = repo.list_system_events(after_id=last_log_id, limit=100)
                if events:
                    for event in events:
                        last_event_id = event.id
                        payload = _to_task_event(event).model_dump(mode="json")
                        yield (
                            f"id: {event.id}\n"
                            "event: task_event\n"
                            f"data: {json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}\n\n"
                        )
                for system_event in system_events:
                    last_log_id = system_event.id
                    entry = _to_system_event_log(system_event)
                    yield (
                        "event: system_event\n"
                        f"data: {json.dumps(entry, ensure_ascii=False, separators=(',', ':'))}\n\n"
                    )
                if not events and not system_events:
                    yield ": keepalive\n\n"
                await asyncio.sleep(1)

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @router.get("/diagnostics", response_model=DiagnosticsResponse)
    def diagnostics(request: Request) -> DiagnosticsResponse:
        return DiagnosticsResponse(
            **build_diagnostics(
                request.app.state.settings,
                request.app.state.task_queue,
                request.app.state.engine,
                getattr(request.app.state, "provider_scheduler", None),
            )
        )

    @router.put("/setup/wizard", response_model=SetupWizardStateResponse)
    def save_setup_wizard_state(
        request: Request,
        payload: SetupWizardStateRequest,
    ) -> SetupWizardStateResponse:
        with session_scope(request.app.state.engine) as session:
            repo = Repository(session)
            runtime_metadata = repo.get_setting(RUNTIME_METADATA_SETTING_KEY) or {}
            runtime_metadata["setup_wizard_dismissed"] = payload.dismissed
            repo.set_setting(RUNTIME_METADATA_SETTING_KEY, runtime_metadata)
            _record_config_event(
                repo,
                "首次设置向导已跳过" if payload.dismissed else "首次设置向导已重新启用",
                details={"dismissed": payload.dismissed},
            )
        return SetupWizardStateResponse(dismissed=payload.dismissed)

    @router.post("/diagnostics/health-runs", status_code=204)
    def record_health_run(request: Request, payload: HealthCheckRunRequest) -> Response:
        counts = {
            status: sum(1 for item in payload.checks if item.status == status)
            for status in ("ok", "warning", "error", "skipped")
        }
        if counts["error"]:
            level, summary = "ERROR", "健康检查完成，存在错误"
        elif counts["warning"]:
            level, summary = "WARNING", "健康检查完成，存在警告"
        else:
            level, summary = "INFO", "健康检查完成"
        with session_scope(request.app.state.engine) as session:
            Repository(session).record_system_event(
                category="health",
                event="health_check_completed",
                level=level,
                message=(
                    f"{summary}：正常 {counts['ok']}，警告 {counts['warning']}，"
                    f"错误 {counts['error']}，未启用 {counts['skipped']}"
                ),
                details={"counts": counts, "checks": [item.model_dump() for item in payload.checks]},
            )
        return Response(status_code=204)

    @router.get("/diagnostics/export")
    def export_diagnostics(request: Request) -> Response:
        payload = build_diagnostics(
            request.app.state.settings,
            request.app.state.task_queue,
            request.app.state.engine,
            getattr(request.app.state, "provider_scheduler", None),
        )
        return Response(
            content=json.dumps(payload, ensure_ascii=False, indent=2, default=str),
            media_type="application/json",
            headers={"Content-Disposition": "attachment; filename=subtitle-sidecar-diagnostics.json"},
        )

    @router.get("/settings/export")
    def export_settings(request: Request) -> Response:
        with session_scope(request.app.state.engine) as session:
            repo = Repository(session)
            stored = {
                key: value
                for key in BACKUP_SETTING_KEYS
                if (value := repo.get_setting(key)) is not None
            }
        config_yaml = ""
        config_path = request.app.state.settings.runtime_config_path
        if config_path is not None and config_path.is_file():
            config_yaml = config_path.read_text(encoding="utf-8")
        payload = {
            "format": "subpick-settings-v1",
            "exported_at": datetime.now(UTC).isoformat(),
            "config_yaml": config_yaml,
            "settings": stored,
        }
        return Response(
            content=json.dumps(payload, ensure_ascii=False, indent=2, default=str),
            media_type="application/json",
            headers={"Content-Disposition": "attachment; filename=subpick-settings.json"},
        )

    @router.put("/settings/import")
    def import_settings(request: Request, payload: dict) -> dict[str, bool]:
        if payload.get("format") != "subpick-settings-v1":
            raise HTTPException(status_code=422, detail="不支持的配置备份格式")
        stored = payload.get("settings")
        if not isinstance(stored, dict):
            raise HTTPException(status_code=422, detail="配置备份缺少 settings")
        config_yaml = payload.get("config_yaml")
        restart_required = False
        imported_paths: PathsSettings | None = None
        paths = stored.get(PATHS_SETTING_KEY)
        if isinstance(paths, dict):
            try:
                imported_paths = PathsSettings.model_validate(paths)
            except Exception as error:
                raise HTTPException(status_code=422, detail=f"paths 配置无效：{error}") from error
        if isinstance(config_yaml, str) and config_yaml.strip():
            try:
                parsed = yaml.safe_load(config_yaml) or {}
                if not isinstance(parsed, dict):
                    raise ValueError("top-level value must be an object")
                AppSettings(**parsed)
            except Exception as error:
                raise HTTPException(
                    status_code=422,
                    detail=f"config.yaml 无效：{error}",
                ) from error
            config_path = request.app.state.settings.runtime_config_path
            if config_path is not None:
                config_path.parent.mkdir(parents=True, exist_ok=True)
                temporary_path = config_path.with_suffix(".yaml.tmp")
                temporary_path.write_text(config_yaml, encoding="utf-8")
                temporary_path.replace(config_path)
                restart_required = True
        with session_scope(request.app.state.engine) as session:
            repo = Repository(session)
            for key in BACKUP_SETTING_KEYS:
                value = stored.get(key)
                if isinstance(value, dict):
                    repo.set_setting(key, value)
            _record_config_event(
                repo,
                "配置导入",
                details={"restart_required": restart_required},
            )
        server = stored.get(SERVER_SETTING_KEY)
        if isinstance(server, dict) and "token" in server:
            request.app.state.settings.server.token = str(server.get("token") or "")
        if imported_paths is not None:
            request.app.state.settings.paths = imported_paths
            with session_scope(request.app.state.engine) as session:
                repo = Repository(session)
                runtime_metadata = repo.get_setting(RUNTIME_METADATA_SETTING_KEY) or {}
                _refresh_moviepilot_path_issue(runtime_metadata, request.app.state.settings.paths)
                repo.set_setting(RUNTIME_METADATA_SETTING_KEY, runtime_metadata)
        return {"imported": True, "restart_required": restart_required}

    @router.post(
        "/diagnostics/dependency-updates",
        response_model=DependencyUpdateChecksResponse,
    )
    def check_dependency_updates(request: Request) -> DependencyUpdateChecksResponse:
        with session_scope(request.app.state.engine) as session:
            github = _load_github_config(request, Repository(session))
        client = getattr(request.app.state, "subliminal_update_client", None)
        kwargs = {"github_token": github["api_key"]}
        if client is not None:
            kwargs["client"] = client
        return DependencyUpdateChecksResponse(
            subliminal=SubliminalUpdateCheckResponse(**check_subliminal_update(**kwargs)),
            ffsubsync=SubliminalUpdateCheckResponse(**check_ffsubsync_update(**kwargs)),
        )

    @router.post(
        "/add-job",
        response_model=AddJobResponse,
        dependencies=[Depends(_require_bearer_token)],
    )
    def add_job(
        request: Request,
        payload: AddJobRequest,
    ) -> AddJobResponse:
        with session_scope(request.app.state.engine) as session:
            repo = Repository(session)
            runtime_metadata = repo.get_setting(RUNTIME_METADATA_SETTING_KEY) or {}
            received_path = payload.physical_video_file_full_path
            resolved = MediaResolver(request.app.state.settings.paths).resolve(received_path)
            runtime_metadata.update(
                {
                    "moviepilot_last_callback_at": datetime.now(UTC).isoformat(),
                    "moviepilot_last_received_path": received_path,
                }
            )
            if resolved.resolved_path is None:
                runtime_metadata["moviepilot_path_issue"] = {
                    "received_path": received_path,
                    "detected_at": datetime.now(UTC).isoformat(),
                }
            else:
                runtime_metadata.pop("moviepilot_path_issue", None)
            repo.set_setting(RUNTIME_METADATA_SETTING_KEY, runtime_metadata)
            job = repo.create_job(
                JobCreate(
                    source="moviepilot-csf",
                    raw_payload=payload.model_dump(exclude_none=True),
                    video_path_original=payload.physical_video_file_full_path,
                    media_server_id=payload.media_server_inside_video_id or None,
                )
            )
            task_id = job.video_tasks[0].id
        _enqueue_task(request, task_id)
        return AddJobResponse(job_id=job.id, status=job.status)

    @router.get(
        "/jobs",
        response_model=list[JobListItemResponse],
    )
    def list_jobs(
        request: Request,
        response: Response,
        limit: int = Query(default=100, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
        search: str = Query(default="", max_length=300),
        status: str = Query(default="all", pattern="^(all|failed|active|completed)$"),
    ) -> list[JobListItemResponse]:
        with session_scope(request.app.state.engine) as session:
            repo = Repository(session)
            total = repo.count_jobs(search=search, status=status)
            jobs = repo.list_jobs(limit=limit, offset=offset, search=search, status=status)
        response.headers["X-Total-Count"] = str(total)
        return [_to_job_list_item(job) for job in jobs]

    @router.get(
        "/jobs/{job_id}",
        response_model=JobResponse,
    )
    def get_job(request: Request, job_id: int) -> JobResponse:
        with session_scope(request.app.state.engine) as session:
            repo = Repository(session)
            job = repo.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found")
        return JobResponse(job_id=job.id, status=job.status)

    @router.get(
        "/jellyfin/settings",
        response_model=JellyfinSettingsResponse,
    )
    def get_jellyfin_settings(request: Request) -> JellyfinSettingsResponse:
        with session_scope(request.app.state.engine) as session:
            repo = Repository(session)
            config = _load_jellyfin_config(request, repo)
        return _to_jellyfin_settings_response(config)

    @router.put(
        "/jellyfin/settings",
        response_model=JellyfinSettingsResponse,
    )
    def save_jellyfin_settings(
        request: Request,
        payload: JellyfinSettingsRequest,
    ) -> JellyfinSettingsResponse:
        with session_scope(request.app.state.engine) as session:
            repo = Repository(session)
            existing = _load_jellyfin_config(request, repo)
            server_url = payload.server_url.strip().rstrip("/")
            api_key_changed = payload.api_key is not None
            config = {
                "server_url": server_url,
                "api_key": (
                    existing["api_key"]
                    if payload.api_key is None
                    else payload.api_key.strip()
                ),
                "user_id": (
                    payload.user_id.strip()
                    or (
                        existing["user_id"]
                        if server_url == existing["server_url"] and not api_key_changed
                        else ""
                    )
                ),
            }
            repo.set_setting(JELLYFIN_SETTING_KEY, config)
            runtime_metadata = repo.get_setting(RUNTIME_METADATA_SETTING_KEY) or {}
            runtime_metadata.pop("jellyfin_last_check_status", None)
            runtime_metadata.pop("jellyfin_last_checked_at", None)
            repo.set_setting(RUNTIME_METADATA_SETTING_KEY, runtime_metadata)
            _record_config_event(
                repo,
                "Jellyfin 配置已保存",
                details={"server_url": config["server_url"], "api_key_configured": bool(config["api_key"])},
            )
        return _to_jellyfin_settings_response(config)

    @router.post(
        "/jellyfin/check",
        response_model=JellyfinConnectionCheckResponse,
    )
    def check_jellyfin_connection(request: Request) -> JellyfinConnectionCheckResponse:
        with session_scope(request.app.state.engine) as session:
            repo = Repository(session)
            config = _require_jellyfin_config(request, repo)
        client = _jellyfin_client(request, config)
        try:
            libraries = client.list_libraries()
        except Exception as error:
            with session_scope(request.app.state.engine) as session:
                repo = Repository(session)
                metadata = repo.get_setting(RUNTIME_METADATA_SETTING_KEY) or {}
                metadata.update(
                    {
                        "jellyfin_last_check_status": "failed",
                        "jellyfin_last_checked_at": datetime.now(UTC).isoformat(),
                    }
                )
                repo.set_setting(RUNTIME_METADATA_SETTING_KEY, metadata)
            raise HTTPException(status_code=502, detail="Jellyfin 连接测试失败") from error
        with session_scope(request.app.state.engine) as session:
            repo = Repository(session)
            resolved_user_id = str(getattr(client, "user_id", "") or "")
            if resolved_user_id and resolved_user_id != config["user_id"]:
                repo.set_setting(
                    JELLYFIN_SETTING_KEY,
                    {**config, "user_id": resolved_user_id},
                )
            metadata = repo.get_setting(RUNTIME_METADATA_SETTING_KEY) or {}
            metadata.update(
                {
                    "jellyfin_last_check_status": "ok",
                    "jellyfin_last_checked_at": datetime.now(UTC).isoformat(),
                }
            )
            repo.set_setting(RUNTIME_METADATA_SETTING_KEY, metadata)
        return JellyfinConnectionCheckResponse(
            connected=True,
            library_count=len(libraries),
        )

    @router.get(
        "/github/settings",
        response_model=GitHubSettingsResponse,
    )
    def get_github_settings(request: Request) -> GitHubSettingsResponse:
        with session_scope(request.app.state.engine) as session:
            config = _load_github_config(request, Repository(session))
        return GitHubSettingsResponse(api_key_configured=bool(config["api_key"]))

    @router.put(
        "/github/settings",
        response_model=GitHubSettingsResponse,
    )
    def save_github_settings(
        request: Request,
        payload: GitHubSettingsRequest,
    ) -> GitHubSettingsResponse:
        with session_scope(request.app.state.engine) as session:
            repo = Repository(session)
            config = _load_github_config(request, repo)
            if payload.api_key is not None:
                config["api_key"] = payload.api_key.strip()
            repo.set_setting(GITHUB_SETTING_KEY, config)
            _record_config_event(
                repo,
                "GitHub 更新检查配置已保存",
                details={"api_key_configured": bool(config["api_key"])},
            )
        return GitHubSettingsResponse(api_key_configured=bool(config["api_key"]))

    @router.get(
        "/server/settings",
        response_model=ServerSettingsResponse,
    )
    def get_server_settings(request: Request) -> ServerSettingsResponse:
        return ServerSettingsResponse(token=request.app.state.settings.server.token)

    @router.put(
        "/server/settings",
        response_model=ServerSettingsResponse,
    )
    def save_server_settings(
        request: Request,
        payload: ServerSettingsRequest,
    ) -> ServerSettingsResponse:
        token = payload.token.strip()
        previous_token = request.app.state.settings.server.token
        with session_scope(request.app.state.engine) as session:
            repo = Repository(session)
            repo.set_setting(SERVER_SETTING_KEY, {"token": token})
            if token != previous_token:
                runtime_metadata = repo.get_setting(RUNTIME_METADATA_SETTING_KEY) or {}
                runtime_metadata.pop("moviepilot_last_callback_at", None)
                runtime_metadata.pop("moviepilot_last_received_path", None)
                repo.set_setting(RUNTIME_METADATA_SETTING_KEY, runtime_metadata)
            _record_config_event(
                repo,
                "MoviePilot 通信配置已保存",
                details={"token_configured": bool(token)},
            )
        request.app.state.settings.server.token = token
        return ServerSettingsResponse(token=token)

    @router.get("/paths/settings", response_model=PathSettingsResponse)
    def get_path_settings(request: Request) -> PathSettingsResponse:
        with session_scope(request.app.state.engine) as session:
            repo = Repository(session)
            mappings = _load_paths_config(request, repo)
            runtime_metadata = repo.get_setting(RUNTIME_METADATA_SETTING_KEY) or {}
        return _path_settings_response(mappings, runtime_metadata)

    @router.put("/paths/settings", response_model=PathSettingsResponse)
    def save_path_settings(
        request: Request,
        payload: PathSettingsRequest,
    ) -> PathSettingsResponse:
        mappings = _paths_from_request(payload)
        _validate_path_mappings(mappings)
        request.app.state.settings.paths = mappings
        with session_scope(request.app.state.engine) as session:
            repo = Repository(session)
            repo.set_setting(PATHS_SETTING_KEY, mappings.model_dump(by_alias=True))
            runtime_metadata = repo.get_setting(RUNTIME_METADATA_SETTING_KEY) or {}
            _refresh_moviepilot_path_issue(runtime_metadata, mappings)
            repo.set_setting(RUNTIME_METADATA_SETTING_KEY, runtime_metadata)
            _record_config_event(
                repo,
                "目录映射已保存",
                details={"mapping_count": len(mappings.mappings)},
            )
        return _path_settings_response(mappings, runtime_metadata)

    @router.post("/paths/check", response_model=PathMappingTestResponse)
    def test_path_settings(
        request: Request,
        payload: PathMappingTestRequest,
    ) -> PathMappingTestResponse:
        mappings = _paths_from_request(payload)
        _validate_path_mappings(mappings)
        with session_scope(request.app.state.engine) as session:
            runtime_metadata = Repository(session).get_setting(RUNTIME_METADATA_SETTING_KEY) or {}
        sample_path = payload.sample_path.strip() or str(
            runtime_metadata.get("moviepilot_last_received_path") or ""
        )
        if not sample_path:
            raise HTTPException(
                status_code=422,
                detail="没有可测试的路径，请先接收一次 MoviePilot 回调或填写 sample_path",
            )
        result = MediaResolver(mappings).resolve(sample_path)
        return PathMappingTestResponse(
            original_path=result.original_path,
            resolved_path=str(result.resolved_path) if result.resolved_path is not None else None,
            strategy=result.strategy,
            exists=result.resolved_path is not None,
        )

    @router.get(
        "/providers/subliminal/settings",
        response_model=SubliminalProviderSettingsResponse,
    )
    def get_subliminal_settings(request: Request) -> SubliminalProviderSettingsResponse:
        with session_scope(request.app.state.engine) as session:
            config = _load_subliminal_config(request, Repository(session))
        return _to_subliminal_settings_response(config)

    @router.get("/providers/order", response_model=ProviderOrderResponse)
    def get_provider_order(request: Request) -> ProviderOrderResponse:
        with session_scope(request.app.state.engine) as session:
            repo = Repository(session)
            return _provider_order_response(request, repo)

    @router.put("/providers/order", response_model=ProviderOrderResponse)
    def save_provider_order(
        request: Request,
        payload: ProviderOrderRequest,
    ) -> ProviderOrderResponse:
        factories = discover_adapter_factories()
        requested = [name.strip() for name in payload.order]
        duplicates = sorted({name for name in requested if requested.count(name) > 1})
        if duplicates:
            raise HTTPException(
                status_code=400,
                detail=f"Duplicate provider names: {', '.join(duplicates)}",
            )
        unknown = sorted(name for name in requested if name not in factories)
        if unknown:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown provider names: {', '.join(unknown)}",
            )
        order = requested + [name for name in factories if name not in requested]
        with session_scope(request.app.state.engine) as session:
            repo = Repository(session)
            repo.set_setting(PROVIDER_ORDER_SETTING_KEY, {"order": order})
            _record_config_event(repo, "Provider 搜索顺序已保存", details={"order": order})
            return _provider_order_response(request, repo, factories=factories)

    @router.put(
        "/providers/subliminal/settings",
        response_model=SubliminalProviderSettingsResponse,
    )
    def save_subliminal_settings(
        request: Request,
        payload: SubliminalProviderSettingsRequest,
    ) -> SubliminalProviderSettingsResponse:
        requested_providers = [
            provider.strip() for provider in payload.providers if provider.strip()
        ]
        unsupported = sorted(
            provider for provider in requested_providers if provider != "opensubtitlescom"
        )
        if unsupported:
            raise HTTPException(
                status_code=422,
                detail=f"Subliminal 不再支持这些字幕源：{'、'.join(unsupported)}",
            )
        with session_scope(request.app.state.engine) as session:
            repo = Repository(session)
            existing = _load_subliminal_config(request, repo)
            authentication = {
                provider: credentials.model_dump()
                for provider, credentials in existing.authentication.items()
            }
            for provider, credentials in payload.authentication.items():
                current = dict(authentication.get(provider) or {})
                current["username"] = credentials.username.strip()
                if credentials.password is not None:
                    current["password"] = credentials.password
                if credentials.apikey is not None:
                    current["apikey"] = credentials.apikey
                authentication[provider] = current
            saved = {
                "enabled": payload.enabled,
                "providers": requested_providers,
                "languages": [language.strip() for language in payload.languages if language.strip()],
                "authentication": authentication,
            }
            if saved["enabled"] and not saved["providers"]:
                raise HTTPException(status_code=422, detail="启用 Subliminal 时至少选择一个字幕源")
            if saved["enabled"] and "opensubtitlescom" in saved["providers"]:
                credentials = saved["authentication"].get("opensubtitlescom") or {}
                missing = [
                    label
                    for key, label in (
                        ("username", "用户名"),
                        ("password", "密码"),
                        ("apikey", "API Key"),
                    )
                    if not str(credentials.get(key) or "").strip()
                ]
                if missing:
                    raise HTTPException(
                        status_code=422,
                        detail=f"启用 OpenSubtitles.com 前请填写：{'、'.join(missing)}",
                    )
            repo.set_setting(SUBLIMINAL_SETTING_KEY, saved)
            _record_config_event(
                repo,
                "Subliminal 配置已保存",
                details={"enabled": payload.enabled, "providers": saved["providers"]},
            )
            config = _load_subliminal_config(request, repo)
        return _to_subliminal_settings_response(config)

    @router.get(
        "/providers/subliminal/update-check",
        response_model=SubliminalUpdateCheckResponse,
    )
    def get_subliminal_update_check(request: Request) -> SubliminalUpdateCheckResponse:
        with session_scope(request.app.state.engine) as session:
            github = _load_github_config(request, Repository(session))
        client = getattr(request.app.state, "subliminal_update_client", None)
        kwargs = {"github_token": github["api_key"]}
        if client is not None:
            kwargs["client"] = client
        return SubliminalUpdateCheckResponse(**check_subliminal_update(**kwargs))

    @router.get(
        "/providers/assrt/settings",
        response_model=AssrtProviderSettingsResponse,
    )
    def get_assrt_settings(request: Request) -> AssrtProviderSettingsResponse:
        with session_scope(request.app.state.engine) as session:
            config = _load_assrt_config(request, Repository(session))
        return _to_assrt_settings_response(config)

    @router.put(
        "/providers/assrt/settings",
        response_model=AssrtProviderSettingsResponse,
    )
    def save_assrt_settings(
        request: Request,
        payload: AssrtProviderSettingsRequest,
    ) -> AssrtProviderSettingsResponse:
        with session_scope(request.app.state.engine) as session:
            repo = Repository(session)
            existing = _load_assrt_config(request, repo)
            saved = {
                "enabled": payload.enabled,
                "token": existing.token if payload.token is None else payload.token.strip(),
                "timeout_seconds": payload.timeout_seconds,
                "requests_per_minute": payload.requests_per_minute,
            }
        config = AssrtProviderSettings(**saved)
        if config.enabled:
            if not config.token:
                raise HTTPException(status_code=422, detail="启用 ASSRT 前请填写 API Key")
            provider_factory = getattr(request.app.state, "assrt_provider_factory", None)
            provider = provider_factory(config) if provider_factory else AssrtProvider(
                token=config.token,
                timeout_seconds=config.timeout_seconds,
                requests_per_minute=config.requests_per_minute,
            )
            try:
                provider.quota()
            except Exception as error:
                raise HTTPException(status_code=502, detail="ASSRT API Key 验证失败，配置未保存") from error
        with session_scope(request.app.state.engine) as session:
            repo = Repository(session)
            repo.set_setting(ASSRT_SETTING_KEY, saved)
            if config.enabled:
                _set_runtime_health(repo, "assrt", "ok")
            else:
                _clear_runtime_health(repo, "assrt")
            _record_config_event(
                repo,
                "ASSRT 配置已保存",
                details={"enabled": payload.enabled, "token_configured": bool(saved["token"])},
            )
            config = _load_assrt_config(request, repo)
        return _to_assrt_settings_response(config)

    @router.post("/providers/assrt/quota", response_model=AssrtQuotaResponse)
    def get_assrt_quota(request: Request) -> AssrtQuotaResponse:
        with session_scope(request.app.state.engine) as session:
            config = _load_assrt_config(request, Repository(session))
        if not config.token:
            raise HTTPException(status_code=400, detail="ASSRT API Key is not configured")
        provider_factory = getattr(request.app.state, "assrt_provider_factory", None)
        provider = provider_factory(config) if provider_factory else AssrtProvider(
            token=config.token,
            timeout_seconds=config.timeout_seconds,
            requests_per_minute=config.requests_per_minute,
        )
        try:
            response = AssrtQuotaResponse(quota=provider.quota())
        except Exception as error:
            _record_runtime_health(request, "assrt", "failed")
            raise HTTPException(status_code=502, detail="ASSRT quota check failed") from error
        _record_runtime_health(request, "assrt", "ok")
        return response

    @router.get(
        "/providers/subdl/settings",
        response_model=SubdlProviderSettingsResponse,
    )
    def get_subdl_settings(request: Request) -> SubdlProviderSettingsResponse:
        with session_scope(request.app.state.engine) as session:
            config = _load_subdl_config(request, Repository(session))
        return _to_subdl_settings_response(config)

    @router.put(
        "/providers/subdl/settings",
        response_model=SubdlProviderSettingsResponse,
    )
    def save_subdl_settings(
        request: Request,
        payload: SubdlProviderSettingsRequest,
    ) -> SubdlProviderSettingsResponse:
        with session_scope(request.app.state.engine) as session:
            repo = Repository(session)
            existing = _load_subdl_config(request, repo)
            saved = {
                "enabled": payload.enabled,
                "api_key": existing.api_key if payload.api_key is None else payload.api_key.strip(),
                "timeout_seconds": payload.timeout_seconds,
                "requests_per_minute": payload.requests_per_minute,
                "use_api_key_for_downloads": payload.use_api_key_for_downloads,
            }
        config = SubdlProviderSettings(**saved)
        if config.enabled:
            if not config.api_key:
                raise HTTPException(status_code=422, detail="启用 SubDL 前请填写 API Key")
            provider_factory = getattr(request.app.state, "subdl_provider_factory", None)
            provider = provider_factory(config) if provider_factory else SubdlProvider(
                api_key=config.api_key,
                timeout_seconds=config.timeout_seconds,
                requests_per_minute=config.requests_per_minute,
                use_api_key_for_downloads=config.use_api_key_for_downloads,
            )
            try:
                provider.usage()
            except Exception as error:
                raise HTTPException(status_code=502, detail="SubDL API Key 验证失败，配置未保存") from error
        with session_scope(request.app.state.engine) as session:
            repo = Repository(session)
            repo.set_setting(SUBDL_SETTING_KEY, saved)
            if config.enabled:
                _set_runtime_health(repo, "subdl", "ok")
            else:
                _clear_runtime_health(repo, "subdl")
            _record_config_event(
                repo,
                "SubDL 配置已保存",
                details={"enabled": payload.enabled, "api_key_configured": bool(saved["api_key"])},
            )
            config = _load_subdl_config(request, repo)
        return _to_subdl_settings_response(config)

    @router.post("/providers/subdl/usage", response_model=SubdlUsageResponse)
    def get_subdl_usage(request: Request) -> SubdlUsageResponse:
        with session_scope(request.app.state.engine) as session:
            config = _load_subdl_config(request, Repository(session))
        if not config.api_key:
            raise HTTPException(status_code=400, detail="SubDL API Key is not configured")
        provider_factory = getattr(request.app.state, "subdl_provider_factory", None)
        provider = provider_factory(config) if provider_factory else SubdlProvider(
            api_key=config.api_key,
            timeout_seconds=config.timeout_seconds,
            requests_per_minute=config.requests_per_minute,
            use_api_key_for_downloads=config.use_api_key_for_downloads,
        )
        try:
            response = SubdlUsageResponse(**_subdl_usage_response(provider.usage()))
        except Exception as error:
            _record_runtime_health(request, "subdl", "failed")
            raise HTTPException(status_code=502, detail="SubDL usage check failed") from error
        _record_runtime_health(request, "subdl", "ok")
        return response

    @router.get(
        "/providers/zimuku/settings",
        response_model=ZimukuProviderSettingsResponse,
    )
    def get_zimuku_settings(request: Request) -> ZimukuProviderSettingsResponse:
        with session_scope(request.app.state.engine) as session:
            config = _load_zimuku_config(request, Repository(session))
        return _to_zimuku_settings_response(config, request.app.state.settings.data_dir)

    @router.put(
        "/providers/zimuku/settings",
        response_model=ZimukuProviderSettingsResponse,
    )
    def save_zimuku_settings(
        request: Request,
        payload: ZimukuProviderSettingsRequest,
    ) -> ZimukuProviderSettingsResponse:
        with session_scope(request.app.state.engine) as session:
            repo = Repository(session)
            existing = _load_zimuku_config(request, repo)
            saved = {
                "enabled": payload.enabled,
                "anti_captcha_api_key": (
                    existing.anti_captcha_api_key
                    if payload.anti_captcha_api_key is None
                    else payload.anti_captcha_api_key.strip()
                ),
                "moviepilot_ocr_url": payload.moviepilot_ocr_url.strip().rstrip("/"),
                "captcha_debug_capture": payload.captcha_debug_capture,
                "base_url": payload.base_url.strip().rstrip("/"),
                "timeout_seconds": payload.timeout_seconds,
                "request_delay_seconds": payload.request_delay_seconds,
            }
            repo.set_setting(ZIMUKU_SETTING_KEY, saved)
            runtime_metadata = repo.get_setting(RUNTIME_METADATA_SETTING_KEY) or {}
            runtime_metadata.pop("zimuku_ocr_last_check_status", None)
            runtime_metadata.pop("zimuku_ocr_last_checked_at", None)
            runtime_metadata.pop("zimuku_last_check_status", None)
            runtime_metadata.pop("zimuku_last_checked_at", None)
            repo.set_setting(RUNTIME_METADATA_SETTING_KEY, runtime_metadata)
            _record_config_event(
                repo,
                "Zimuku 配置已保存",
                details={
                    "enabled": payload.enabled,
                    "ocr_configured": bool(saved["moviepilot_ocr_url"]),
                    "anti_captcha_configured": bool(saved["anti_captcha_api_key"]),
                },
            )
            config = _load_zimuku_config(request, repo)
        return _to_zimuku_settings_response(config, request.app.state.settings.data_dir)

    @router.post(
        "/providers/zimuku/ocr-check",
        response_model=ZimukuOcrCheckResponse,
    )
    def check_zimuku_ocr(request: Request) -> ZimukuOcrCheckResponse:
        with session_scope(request.app.state.engine) as session:
            config = _load_zimuku_config(request, Repository(session))
        if not config.moviepilot_ocr_url:
            _record_runtime_health(request, "zimuku_ocr", "failed")
            emit_structured_log(
                event="provider_diagnostic",
                provider="zimuku",
                stage="ocr_check",
                status="failed",
                error_code="ocr_not_configured",
                message="OCR 实图识别检查失败：未配置 MoviePilot OCR 地址",
            )
            raise HTTPException(status_code=400, detail="MoviePilot OCR URL is not configured")
        solver_factory = getattr(request.app.state, "zimuku_ocr_solver_factory", None)
        solver = solver_factory(config) if solver_factory else MoviePilotOcrSolver(
            base_url=config.moviepilot_ocr_url,
            timeout_seconds=config.timeout_seconds,
        )
        expected = MoviePilotOcrSolver.CHECK_EXPECTED_ANSWER
        emit_structured_log(
            event="provider_diagnostic",
            provider="zimuku",
            stage="ocr_check",
            status="started",
            message=f"OCR 实图识别检查开始：测试图片预期答案 {expected}",
            base_url=config.moviepilot_ocr_url,
            expected_answer=expected,
            test_image="embedded_deterministic_captcha",
        )
        try:
            duration_ms = solver.check_available()
        except Exception as error:
            _record_runtime_health(request, "zimuku_ocr", "failed")
            emit_structured_log(
                event="provider_diagnostic",
                provider="zimuku",
                stage="ocr_check",
                status="failed",
                error_code=type(error).__name__,
                message=f"OCR 实图识别请求失败：{error}",
                base_url=config.moviepilot_ocr_url,
                expected_answer=expected,
            )
            raise HTTPException(
                status_code=502,
                detail=f"MoviePilot OCR POST check failed: {error}",
            ) from error
        answer = str(getattr(solver, "last_check_answer", "")).strip()
        if answer != expected:
            _record_runtime_health(request, "zimuku_ocr", "failed")
            emit_structured_log(
                event="provider_diagnostic",
                provider="zimuku",
                stage="ocr_check",
                status="failed",
                error_code="ocr_answer_mismatch",
                message=f"OCR 实图识别错误：识别为 {answer or '<empty>'}，预期 {expected}",
                base_url=config.moviepilot_ocr_url,
                duration_ms=duration_ms,
                recognized_answer=answer or "<empty>",
                expected_answer=expected,
            )
            raise HTTPException(
                status_code=502,
                detail=(
                    "MoviePilot OCR recognition failed: "
                    f"expected {expected}, got {answer or '<empty>'}"
                ),
            )
        emit_structured_log(
            event="provider_diagnostic",
            provider="zimuku",
            stage="ocr_check",
            status="completed",
            message=f"OCR 实图识别成功：识别结果 {answer}，预期 {expected}，耗时 {duration_ms} ms",
            base_url=config.moviepilot_ocr_url,
            duration_ms=duration_ms,
            recognized_answer=answer,
            expected_answer=expected,
        )
        _record_runtime_health(request, "zimuku_ocr", "ok")
        return ZimukuOcrCheckResponse(
            status="available",
            duration_ms=duration_ms,
            base_url=config.moviepilot_ocr_url,
            recognized_answer=answer,
            expected_answer=expected,
        )

    @router.post(
        "/providers/zimuku/captcha-balance",
        response_model=ZimukuCaptchaBalanceResponse,
    )
    def get_zimuku_captcha_balance(request: Request) -> ZimukuCaptchaBalanceResponse:
        with session_scope(request.app.state.engine) as session:
            config = _load_zimuku_config(request, Repository(session))
        if not config.anti_captcha_api_key:
            raise HTTPException(status_code=400, detail="Anti-Captcha API Key is not configured")
        provider_factory = getattr(request.app.state, "zimuku_provider_factory", None)
        provider = provider_factory(config) if provider_factory else ZimukuProvider(
            anti_captcha_api_key=config.anti_captcha_api_key,
            base_url=config.base_url,
            timeout_seconds=config.timeout_seconds,
            request_delay_seconds=config.request_delay_seconds,
        )
        try:
            response = ZimukuCaptchaBalanceResponse(balance=provider.captcha_balance())
        except Exception as error:
            _record_runtime_health(request, "zimuku_captcha", "failed")
            raise HTTPException(status_code=502, detail="Anti-Captcha balance check failed") from error
        _record_runtime_health(request, "zimuku_captcha", "ok")
        return response

    @router.get(
        "/jellyfin/libraries",
        response_model=JellyfinLibrariesResponse,
    )
    def list_jellyfin_libraries(request: Request) -> JellyfinLibrariesResponse:
        with session_scope(request.app.state.engine) as session:
            repo = Repository(session)
            config = _require_jellyfin_config(request, repo)
        libraries = _jellyfin_client(request, config).list_libraries()
        return JellyfinLibrariesResponse(
            libraries=[JellyfinLibraryResponse(**library) for library in libraries]
        )

    @router.get(
        "/jellyfin/recent",
        response_model=JellyfinRecentMediaItemsResponse,
    )
    def list_recent_jellyfin_media(
        request: Request,
        limit: int = Query(default=8, ge=1, le=50),
    ) -> JellyfinRecentMediaItemsResponse:
        with session_scope(request.app.state.engine) as session:
            items_by_library: dict[str, list] = {}
            for item in Repository(session).list_all_jellyfin_media_items():
                items_by_library.setdefault(item.library_id, []).append(item)

            recent: list[JellyfinRecentMediaResponse] = []
            for library_id, library_items in items_by_library.items():
                tree = _to_jellyfin_library_tree(library_id, library_items)
                for item_type, cards in (("Movie", tree.movies), ("Series", tree.series)):
                    recent.extend(
                        JellyfinRecentMediaResponse(
                            id=card.id,
                            library_id=library_id,
                            library_name=tree.library_name,
                            item_type=item_type,
                            name=card.name,
                            year=card.year,
                            status=card.status,
                            has_external_chinese_subtitle=card.has_external_chinese_subtitle,
                            has_embedded_chinese_subtitle=card.has_embedded_chinese_subtitle,
                            image_url=card.image_url,
                            ignored=card.ignored,
                            date_created=card.date_created,
                        )
                        for card in cards
                    )

        recent.sort(
            key=lambda item: item.date_created or datetime.min.replace(tzinfo=UTC),
            reverse=True,
        )
        return JellyfinRecentMediaItemsResponse(items=recent[:limit])

    @router.post(
        "/jellyfin/libraries/{library_id}/scan",
        response_model=JellyfinScanResponse,
    )
    def scan_jellyfin_library(request: Request, library_id: str) -> JellyfinScanResponse:
        with session_scope(request.app.state.engine) as session:
            repo = Repository(session)
            config = _require_jellyfin_config(request, repo)
        client = _jellyfin_client(request, config)
        library = _find_jellyfin_library(client, library_id)
        items = client.list_library_items(library_id)
        resolver = MediaResolver(request.app.state.settings.paths)
        stats = {"created": 0, "updated": 0, "unchanged": 0}
        scanned_item_ids = {str(item["id"]) for item in items if item.get("id")}

        with session_scope(request.app.state.engine) as session:
            repo = Repository(session)
            for item in items:
                item_id = str(item.get("id") or "")
                if not item_id:
                    continue
                path = str(item.get("path") or "")
                if item.get("type") in {"Movie", "Episode"} and path:
                    resolved = resolver.resolve(path)
                    resolved_path = getattr(resolved, "resolved_path", None) or Path(path)
                    subtitle_status = detect_subtitle_status(
                        Path(resolved_path),
                        item.get("media_streams") or [],
                    )
                else:
                    subtitle_status = SubtitleStatus(
                        status="unknown",
                        has_external_chinese=False,
                        has_embedded_chinese=False,
                        has_bilingual=False,
                    )
                result = repo.upsert_jellyfin_media_item_with_status(
                    JellyfinMediaItemData(
                        jellyfin_item_id=item_id,
                        library_id=library_id,
                        library_name=library["name"],
                        item_type=item.get("type") or "",
                        name=item.get("name") or "",
                        original_title=item.get("original_title"),
                        series_id=item.get("series_id"),
                        series_name=item.get("series_name"),
                        year=item.get("year"),
                        season=item.get("season"),
                        episode=item.get("episode"),
                        path=path,
                        provider_ids=item.get("provider_ids"),
                        production_locations=item.get("production_locations"),
                        primary_image_tag=item.get("primary_image_tag"),
                        subtitle_status=subtitle_status.status,
                        has_external_chinese_subtitle=subtitle_status.has_external_chinese,
                        has_embedded_chinese_subtitle=subtitle_status.has_embedded_chinese,
                        has_bilingual_subtitle=subtitle_status.has_bilingual,
                        jellyfin_date_created=item.get("date_created"),
                    )
                )
                stats[result.status] += 1
            removed_ids = repo.delete_jellyfin_media_items_missing_from_library(
                library_id,
                scanned_item_ids,
            )

        _purge_jellyfin_image_cache(request.app.state.settings.cache_dir, removed_ids)

        return JellyfinScanResponse(
            library_id=library_id,
            library_name=library["name"],
            scanned_count=len(scanned_item_ids),
            removed=len(removed_ids),
            **stats,
        )

    @router.get(
        "/jellyfin/libraries/{library_id}/items",
        response_model=JellyfinMediaItemsResponse,
    )
    def list_jellyfin_media_items(
        request: Request,
        library_id: str,
        limit: int = Query(default=500, ge=1, le=2000),
    ) -> JellyfinMediaItemsResponse:
        with session_scope(request.app.state.engine) as session:
            repo = Repository(session)
            items = repo.list_jellyfin_media_items(library_id, limit=limit)
        return JellyfinMediaItemsResponse(
            items=[_to_jellyfin_media_item(item) for item in items]
        )

    @router.get(
        "/jellyfin/libraries/{library_id}/tree",
        response_model=JellyfinLibraryTreeResponse,
    )
    def get_jellyfin_library_tree(
        request: Request,
        library_id: str,
    ) -> JellyfinLibraryTreeResponse:
        with session_scope(request.app.state.engine) as session:
            repo = Repository(session)
            items = repo.list_jellyfin_media_items(library_id, limit=2000)
        return _to_jellyfin_library_tree(library_id, items)

    @router.post(
        "/jellyfin/items/{item_id}/ignore",
        response_model=JellyfinIgnoreResponse,
    )
    def ignore_jellyfin_media_item(request: Request, item_id: str) -> JellyfinIgnoreResponse:
        return _set_jellyfin_media_item_ignored(request, item_id, ignored=True)

    @router.post(
        "/jellyfin/items/{item_id}/unignore",
        response_model=JellyfinIgnoreResponse,
    )
    def unignore_jellyfin_media_item(request: Request, item_id: str) -> JellyfinIgnoreResponse:
        return _set_jellyfin_media_item_ignored(request, item_id, ignored=False)

    @router.post(
        "/jellyfin/items/batch-ignore",
        response_model=JellyfinBatchIgnoreResponse,
    )
    def batch_ignore_jellyfin_media_items(
        request: Request,
        payload: JellyfinBatchIgnoreRequest,
    ) -> JellyfinBatchIgnoreResponse:
        item_ids = list(dict.fromkeys(payload.item_ids))
        if not item_ids:
            raise HTTPException(status_code=400, detail="at least one item_id is required")

        with session_scope(request.app.state.engine) as session:
            repo = Repository(session)
            items = repo.get_jellyfin_media_items_by_ids(item_ids)
            items_by_id = {item.jellyfin_item_id: item for item in items}
            missing_ids = [item_id for item_id in item_ids if item_id not in items_by_id]
            if missing_ids:
                raise HTTPException(
                    status_code=404,
                    detail=f"Jellyfin items not found: {', '.join(missing_ids)}",
                )
            invalid_ids = [
                item_id
                for item_id in item_ids
                if items_by_id[item_id].item_type.casefold() not in {"movie", "series"}
            ]
            if invalid_ids:
                raise HTTPException(
                    status_code=400,
                    detail="only Movie and Series items can be ignored",
                )
            updated = [
                repo.set_jellyfin_media_item_ignored(item_id, ignored=payload.ignored)
                for item_id in item_ids
            ]

        return JellyfinBatchIgnoreResponse(
            items=[
                JellyfinIgnoreResponse(
                    item_id=item.jellyfin_item_id,
                    item_type=item.item_type,
                    ignored=item.ignored,
                )
                for item in updated
                if item is not None
            ]
        )

    @router.get("/jellyfin/items/{item_id}/primary-image")
    def get_jellyfin_primary_image(request: Request, item_id: str) -> Response:
        with session_scope(request.app.state.engine) as session:
            repo = Repository(session)
            config = _require_jellyfin_config(request, repo)
            items = repo.get_jellyfin_media_items_by_ids([item_id])

        if not items:
            raise HTTPException(status_code=404, detail="Jellyfin item not found")
        item = items[0]
        if not item.primary_image_tag:
            raise HTTPException(status_code=404, detail="Jellyfin item has no primary image")

        headers = _jellyfin_image_headers(item.primary_image_tag)
        if _etag_matches(request.headers.get("if-none-match"), headers["ETag"]):
            return Response(status_code=304, headers=headers)

        cached = _read_jellyfin_image_cache(
            request.app.state.settings.cache_dir,
            item_id,
            item.primary_image_tag,
        )
        if cached is not None:
            content, content_type = cached
            return Response(content=content, media_type=content_type, headers=headers)

        try:
            content, content_type = _jellyfin_client(request, config).get_primary_image(item_id)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                raise HTTPException(status_code=404, detail="Jellyfin image not found") from exc
            raise HTTPException(status_code=502, detail="Jellyfin image request failed") from exc
        _write_jellyfin_image_cache(
            request.app.state.settings.cache_dir,
            item_id,
            item.primary_image_tag,
            content,
            content_type,
        )
        return Response(content=content, media_type=content_type, headers=headers)

    @router.post(
        "/jellyfin/tasks",
        response_model=JellyfinCreateTasksResponse,
    )
    def create_tasks_from_jellyfin_items(
        request: Request,
        payload: JellyfinCreateTasksRequest,
    ) -> JellyfinCreateTasksResponse:
        task_ids_to_enqueue: list[int] = []
        results_by_id: dict[str, JellyfinCreateTaskResultResponse] = {}
        item_ids = list(dict.fromkeys(payload.item_ids))
        with session_scope(request.app.state.engine) as session:
            repo = Repository(session)
            items = repo.get_jellyfin_media_items_by_ids(item_ids)
            items_by_id = {item.jellyfin_item_id: item for item in items}
            valid_items = []
            for item_id in item_ids:
                item = items_by_id.get(item_id)
                if item is None:
                    results_by_id[item_id] = JellyfinCreateTaskResultResponse(
                        item_id=item_id,
                        ok=False,
                        job_id=None,
                        task_id=None,
                        status="not_found",
                        error="Jellyfin item not found; scan the library first",
                    )
                    continue

                valid_items.append(item)

            jobs = repo.create_jobs(
                [
                    JobCreate(
                        source="jellyfin-manual",
                        raw_payload={
                            "jellyfin_item_id": item.jellyfin_item_id,
                            "physical_video_file_full_path": item.path,
                            "library_id": item.library_id,
                        },
                        video_path_original=item.path,
                        media_server_id=item.jellyfin_item_id,
                    )
                    for item in valid_items
                ]
            )
            for item, job in zip(valid_items, jobs, strict=True):
                task = job.video_tasks[0]
                task_ids_to_enqueue.append(task.id)
                results_by_id[item.jellyfin_item_id] = JellyfinCreateTaskResultResponse(
                    item_id=item.jellyfin_item_id,
                    ok=True,
                    job_id=job.id,
                    task_id=task.id,
                    status=task.status,
                    error=None,
                )

        for task_id in task_ids_to_enqueue:
            _enqueue_task(request, task_id)
        return JellyfinCreateTasksResponse(results=[results_by_id[item_id] for item_id in item_ids])

    @router.post(
        "/tasks/batch-retry",
        response_model=BatchRetryTasksResponse,
    )
    def batch_retry_tasks(request: Request, payload: BatchTaskRequest) -> BatchRetryTasksResponse:
        results: list[BatchRetryTaskResultResponse] = []
        for task_id in payload.task_ids:
            with session_scope(request.app.state.engine) as session:
                repo = Repository(session)
                original_task = repo.get_video_task(task_id)
                if original_task is None:
                    results.append(
                        BatchRetryTaskResultResponse(
                            task_id=task_id,
                            ok=False,
                            job_id=None,
                            new_task_id=None,
                            status="not_found",
                            error="Task not found",
                        )
                    )
                    continue
                retry_job_id, retry_task_id, retry_status = _create_retry_task(repo, original_task)
            _enqueue_task(request, retry_task_id)
            results.append(
                BatchRetryTaskResultResponse(
                    task_id=task_id,
                    ok=True,
                    job_id=retry_job_id,
                    new_task_id=retry_task_id,
                    status=retry_status,
                    error=None,
                )
            )
        return BatchRetryTasksResponse(results=results)

    @router.post(
        "/tasks/batch-delete",
        response_model=BatchDeleteTasksResponse,
    )
    def batch_delete_tasks(
        request: Request,
        payload: BatchDeleteTasksRequest,
    ) -> BatchDeleteTasksResponse:
        results: list[BatchDeleteTaskResultResponse] = []
        for task_id in payload.task_ids:
            with session_scope(request.app.state.engine) as session:
                repo = Repository(session)
                deleted, subtitle_deleted = _delete_task(
                    repo,
                    task_id,
                    delete_subtitles=payload.delete_subtitles,
                )
            results.append(
                BatchDeleteTaskResultResponse(
                    task_id=task_id,
                    ok=deleted,
                    deleted=deleted,
                    subtitle_deleted=subtitle_deleted,
                    error=None if deleted else "Task not found",
                )
            )
        return BatchDeleteTasksResponse(results=results)

    @router.delete("/tasks")
    def delete_all_tasks(
        request: Request,
        delete_subtitles: bool = Query(default=False),
    ) -> dict[str, int]:
        with session_scope(request.app.state.engine) as session:
            repo = Repository(session)
            task_ids = repo.list_all_video_task_ids()
            deleted = 0
            subtitles_deleted = 0
            for task_id in task_ids:
                task_deleted, subtitle_deleted = _delete_task(
                    repo,
                    task_id,
                    delete_subtitles=delete_subtitles,
                )
                deleted += int(task_deleted)
                subtitles_deleted += int(subtitle_deleted)
        return {"deleted": deleted, "subtitles_deleted": subtitles_deleted}

    @router.get(
        "/tasks/{task_id}",
        response_model=VideoTaskDetailResponse,
    )
    def get_task_detail(request: Request, task_id: int) -> VideoTaskDetailResponse:
        with session_scope(request.app.state.engine) as session:
            repo = Repository(session)
            task = repo.get_video_task(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found")
        return _to_task_detail(task)

    @router.get(
        "/tasks/{task_id}/events",
        response_model=list[TaskEventResponse],
    )
    def list_task_events(
        request: Request,
        task_id: int,
        limit: int = Query(default=200, ge=1, le=500),
    ) -> list[TaskEventResponse]:
        with session_scope(request.app.state.engine) as session:
            repo = Repository(session)
            task = repo.get_video_task(task_id)
            if task is None:
                raise HTTPException(status_code=404, detail="Task not found")
            events = repo.list_task_events(task_id, limit=limit)
        return [_to_task_event(event) for event in events]

    @router.post(
        "/tasks/{task_id}/retry",
        response_model=RetryTaskResponse,
    )
    def retry_task(
        request: Request,
        task_id: int,
    ) -> RetryTaskResponse:
        with session_scope(request.app.state.engine) as session:
            repo = Repository(session)
            original_task = repo.get_video_task(task_id)
            if original_task is None:
                raise HTTPException(status_code=404, detail="Task not found")

            retry_job_id, retry_task_id, retry_status = _create_retry_task(repo, original_task)

        _enqueue_task(request, retry_task_id)
        return RetryTaskResponse(
            job_id=retry_job_id,
            task_id=retry_task_id,
            status=retry_status,
        )

    @router.delete(
        "/tasks/{task_id}",
        status_code=204,
    )
    def delete_task(
        request: Request,
        task_id: int,
        delete_subtitle: bool = Query(default=False),
    ) -> Response:
        with session_scope(request.app.state.engine) as session:
            repo = Repository(session)
            deleted, _subtitle_deleted = _delete_task(
                repo,
                task_id,
                delete_subtitles=delete_subtitle,
            )
            if not deleted:
                raise HTTPException(status_code=404, detail="Task not found")
        return Response(status_code=204)

    return router


def _enqueue_task(request: Request, task_id: int) -> None:
    request.app.state.enqueue_task(task_id)


def _create_retry_task(repo: Repository, original_task: VideoTask) -> tuple[int, int, str]:
    raw_payload = dict(original_task.job.raw_payload_json)
    raw_payload["retry_of_task_id"] = original_task.id
    retry_job = repo.create_job(
        JobCreate(
            source="manual-retry",
            raw_payload=raw_payload,
            video_path_original=original_task.video_path_original,
            media_server_id=original_task.media_server_id,
        )
    )
    retry_task = retry_job.video_tasks[0]
    retry_task.title = original_task.title
    retry_task.year = original_task.year
    retry_task.season = original_task.season
    retry_task.episode = original_task.episode
    return retry_job.id, retry_task.id, retry_task.status


def _delete_task(
    repo: Repository,
    task_id: int,
    *,
    delete_subtitles: bool,
) -> tuple[bool, bool]:
    task = repo.get_video_task(task_id)
    if task is None:
        return False, False

    subtitle_deleted = False
    if delete_subtitles and task.status == TASK_COMPLETED:
        video_path = Path(task.video_path_resolved or task.video_path_original)
        placed_paths = {
            Path(artifact.path)
            for artifact in task.artifacts
            if artifact.kind == "placed" and artifact.path
        }
        if not placed_paths and task.result_subtitle_path:
            placed_paths.add(Path(task.result_subtitle_path))
        for subtitle_path in placed_paths:
            if not _is_task_owned_subtitle(subtitle_path, video_path):
                continue
            existed = subtitle_path.is_file()
            try:
                subtitle_path.unlink(missing_ok=True)
            except OSError as error:
                raise HTTPException(
                    status_code=409,
                    detail=f"Unable to delete subtitle file: {type(error).__name__}",
                ) from error
            subtitle_deleted = subtitle_deleted or existed

    return repo.delete_video_task(task_id), subtitle_deleted


def _is_task_owned_subtitle(subtitle_path: Path, video_path: Path) -> bool:
    return (
        subtitle_path.suffix.casefold() in SUBTITLE_EXTENSIONS
        and subtitle_path.parent == video_path.parent
        and subtitle_path.name.startswith(f"{video_path.stem}.")
    )


def _paths_from_request(payload: PathSettingsRequest) -> PathsSettings:
    return PathsSettings(
        mappings=[
            PathMapping(from_path=item.from_path.strip(), to_path=item.to_path.strip())
            for item in payload.mappings
        ]
    )


def _validate_path_mappings(settings: PathsSettings) -> None:
    for mapping in settings.mappings:
        if not mapping.from_path or not mapping.to_path:
            raise HTTPException(
                status_code=422,
                detail="路径映射的来源路径和目标路径不能为空",
            )


def _load_paths_config(request: Request, repo: Repository) -> PathsSettings:
    defaults = request.app.state.settings.paths
    stored = repo.get_setting(PATHS_SETTING_KEY)
    if not isinstance(stored, dict):
        return defaults
    try:
        return PathsSettings.model_validate(stored)
    except Exception:
        return defaults


def _refresh_moviepilot_path_issue(
    runtime_metadata: dict,
    settings: PathsSettings,
) -> None:
    received_path = str(runtime_metadata.get("moviepilot_last_received_path") or "")
    if not received_path:
        return
    resolved = MediaResolver(settings).resolve(received_path)
    if resolved.resolved_path is None:
        issue = runtime_metadata.get("moviepilot_path_issue")
        runtime_metadata["moviepilot_path_issue"] = {
            "received_path": received_path,
            "detected_at": (
                str(issue.get("detected_at"))
                if isinstance(issue, dict) and issue.get("detected_at")
                else datetime.now(UTC).isoformat()
            ),
        }
    else:
        runtime_metadata.pop("moviepilot_path_issue", None)


def _path_settings_response(
    settings: PathsSettings,
    runtime_metadata: dict,
) -> PathSettingsResponse:
    return PathSettingsResponse(
        mappings=[
            PathMappingResponse(from_path=mapping.from_path, to_path=mapping.to_path)
            for mapping in settings.mappings
        ],
        latest_moviepilot_path=(
            str(runtime_metadata.get("moviepilot_last_received_path"))
            if runtime_metadata.get("moviepilot_last_received_path")
            else None
        ),
        path_issue=(
            runtime_metadata.get("moviepilot_path_issue")
            if isinstance(runtime_metadata.get("moviepilot_path_issue"), dict)
            else None
        ),
        needs_attention=bool(runtime_metadata.get("moviepilot_path_issue")),
    )


def _load_jellyfin_config(request: Request, repo: Repository) -> dict[str, str]:
    defaults = request.app.state.settings.jellyfin.model_dump()
    stored = repo.get_setting(JELLYFIN_SETTING_KEY) or {}
    return {
        "server_url": str(stored.get("server_url") or defaults.get("server_url") or ""),
        "api_key": str(stored.get("api_key") or defaults.get("api_key") or ""),
        "user_id": str(stored.get("user_id") or defaults.get("user_id") or ""),
    }


def _load_github_config(request: Request, repo: Repository) -> dict[str, str]:
    defaults = request.app.state.settings.github.model_dump()
    stored = repo.get_setting(GITHUB_SETTING_KEY) or {}
    return {"api_key": str(stored.get("api_key") or defaults.get("api_key") or "")}


def _load_subliminal_config(request: Request, repo: Repository):
    return merge_subliminal_provider_settings(
        request.app.state.settings.providers.subliminal,
        repo.get_setting(SUBLIMINAL_SETTING_KEY),
    )


def _load_assrt_config(request: Request, repo: Repository):
    return merge_assrt_provider_settings(
        request.app.state.settings.providers.assrt,
        repo.get_setting(ASSRT_SETTING_KEY),
    )


def _load_subdl_config(request: Request, repo: Repository):
    return merge_subdl_provider_settings(
        request.app.state.settings.providers.subdl,
        repo.get_setting(SUBDL_SETTING_KEY),
    )


def _load_zimuku_config(request: Request, repo: Repository):
    return merge_zimuku_provider_settings(
        request.app.state.settings.providers.zimuku,
        repo.get_setting(ZIMUKU_SETTING_KEY),
    )


def _provider_order_response(
    request: Request,
    repo: Repository,
    *,
    factories=None,
) -> ProviderOrderResponse:
    discovered = factories or discover_adapter_factories()
    stored = repo.get_setting(PROVIDER_ORDER_SETTING_KEY) or {}
    configured = stored.get("order")
    requested = configured if isinstance(configured, list) else request.app.state.settings.providers.order
    order = []
    for name in requested:
        normalized = str(name)
        if normalized in discovered and normalized not in order:
            order.append(normalized)
    order.extend(name for name in discovered if name not in order)

    built_in_enabled = {
        "subliminal": _load_subliminal_config(request, repo).enabled,
        "assrt": _load_assrt_config(request, repo).enabled,
        "subdl": _load_subdl_config(request, repo).enabled,
        "zimuku": _load_zimuku_config(request, repo).enabled,
    }
    external_settings = request.app.state.settings.providers.adapters
    adapters = []
    for name in order:
        metadata = discovered[name].metadata
        configured_external = external_settings.get(name) or {}
        enabled = built_in_enabled.get(name, bool(configured_external.get("enabled")))
        adapters.append(
            ProviderAdapterResponse(
                name=metadata.name,
                display_name=metadata.display_name,
                version=metadata.version,
                enabled=enabled,
                capabilities=ProviderCapabilitiesResponse(
                    media_scopes=list(metadata.media_scopes),
                    lookup_keys=list(metadata.lookup_keys),
                    transport=metadata.transport,
                    requires_auth=metadata.requires_auth,
                    requires_captcha=metadata.requires_captcha,
                    supports_archives=metadata.supports_archives,
                    recommended_interval_seconds=metadata.recommended_interval_seconds,
                    stable_candidate_identity=metadata.stable_candidate_identity,
                ),
            )
        )
    return ProviderOrderResponse(order=order, adapters=adapters)


def _to_subliminal_settings_response(config) -> SubliminalProviderSettingsResponse:
    return SubliminalProviderSettingsResponse(
        enabled=config.enabled,
        providers=config.providers,
        languages=config.languages,
        authentication={
            provider: SubliminalProviderAuthenticationResponse(
                username=credentials.username,
                password_configured=bool(credentials.password),
                apikey_configured=bool(credentials.apikey),
            )
            for provider, credentials in config.authentication.items()
        },
    )


def _to_assrt_settings_response(config) -> AssrtProviderSettingsResponse:
    if not config.enabled:
        status = "disabled"
    elif not config.token:
        status = "unconfigured"
    else:
        status = "configured"
    return AssrtProviderSettingsResponse(
        enabled=config.enabled,
        token_configured=bool(config.token),
        timeout_seconds=config.timeout_seconds,
        requests_per_minute=config.requests_per_minute,
        status=status,
    )


def _to_subdl_settings_response(config) -> SubdlProviderSettingsResponse:
    if not config.enabled:
        status = "disabled"
    elif not config.api_key:
        status = "unconfigured"
    else:
        status = "configured"
    return SubdlProviderSettingsResponse(
        enabled=config.enabled,
        api_key_configured=bool(config.api_key),
        timeout_seconds=config.timeout_seconds,
        requests_per_minute=config.requests_per_minute,
        use_api_key_for_downloads=config.use_api_key_for_downloads,
        status=status,
    )


def _to_zimuku_settings_response(config, data_dir: Path) -> ZimukuProviderSettingsResponse:
    if not config.enabled:
        status = "disabled"
    elif not config.moviepilot_ocr_url and not config.anti_captcha_api_key:
        status = "unconfigured"
    else:
        status = "configured"
    return ZimukuProviderSettingsResponse(
        enabled=config.enabled,
        anti_captcha_api_key_configured=bool(config.anti_captcha_api_key),
        moviepilot_ocr_url=config.moviepilot_ocr_url,
        moviepilot_ocr_configured=bool(config.moviepilot_ocr_url),
        captcha_debug_capture=config.captcha_debug_capture,
        captcha_debug_directory=str(data_dir / "diagnostics" / "captcha" / "zimuku"),
        base_url=config.base_url,
        timeout_seconds=config.timeout_seconds,
        request_delay_seconds=config.request_delay_seconds,
        status=status,
    )


def _subdl_usage_response(payload: dict) -> dict:
    plan = payload.get("plan") or {}
    usage = payload.get("usage") or {}
    search = usage.get("search") or {}
    downloads = usage.get("downloads") or {}
    return {
        "plan_name": str(plan.get("name") or "Unknown"),
        "is_pro": bool(plan.get("is_pro")),
        "search_remaining": _optional_int(search.get("remaining")),
        "search_limit": _optional_int(search.get("limit")),
        "download_remaining": _optional_int(downloads.get("remaining")),
        "download_limit": _optional_int(downloads.get("limit")),
        "reset_at": str(search.get("reset_at") or downloads.get("reset_at") or "") or None,
    }


def _optional_int(value):
    return value if isinstance(value, int) else None


def _require_jellyfin_config(request: Request, repo: Repository) -> dict[str, str]:
    config = _load_jellyfin_config(request, repo)
    if not config["server_url"] or not config["api_key"]:
        raise HTTPException(status_code=400, detail="Jellyfin is not configured")
    return config


def _to_jellyfin_settings_response(config: dict[str, str]) -> JellyfinSettingsResponse:
    return JellyfinSettingsResponse(
        server_url=config["server_url"],
        user_id=config["user_id"],
        configured=bool(config["server_url"] and config["api_key"]),
        api_key_configured=bool(config["api_key"]),
    )


def _jellyfin_client(request: Request, config: dict[str, str]):
    factory = getattr(request.app.state, "jellyfin_client_factory", None)
    if factory is not None:
        return factory(config)
    return JellyfinClient(
        server_url=config["server_url"],
        api_key=config["api_key"],
        user_id=config["user_id"],
    )


def _record_runtime_health(request: Request, name: str, status: str) -> None:
    with session_scope(request.app.state.engine) as session:
        repo = Repository(session)
        _set_runtime_health(repo, name, status)
        repo.record_system_event(
            category="provider",
            event="provider_health_checked",
            level="ERROR" if status == "failed" else "INFO",
            message=f"Provider {name} 检查：{'不可用' if status == 'failed' else '可用'}",
            details={"provider": name, "status": status},
        )


def _set_runtime_health(repo: Repository, name: str, status: str) -> None:
    metadata = repo.get_setting(RUNTIME_METADATA_SETTING_KEY) or {}
    checked_at = datetime.now(UTC).isoformat()
    names = {name}
    if name in {"zimuku_ocr", "zimuku_captcha"}:
        names.add("zimuku")
    for health_name in names:
        metadata.update(
            {
                f"{health_name}_last_check_status": status,
                f"{health_name}_last_checked_at": checked_at,
            }
        )
    repo.set_setting(RUNTIME_METADATA_SETTING_KEY, metadata)


def _record_config_event(
    repo: Repository,
    message: str,
    *,
    details: dict | None = None,
) -> None:
    repo.record_system_event(
        category="configuration",
        event="configuration_changed",
        message=message,
        details=details,
    )


def _clear_runtime_health(repo: Repository, name: str) -> None:
    metadata = repo.get_setting(RUNTIME_METADATA_SETTING_KEY) or {}
    metadata.pop(f"{name}_last_check_status", None)
    metadata.pop(f"{name}_last_checked_at", None)
    repo.set_setting(RUNTIME_METADATA_SETTING_KEY, metadata)


def _find_jellyfin_library(client, library_id: str) -> dict[str, str]:
    for library in client.list_libraries():
        if library["id"] == library_id:
            return library
    raise HTTPException(status_code=404, detail="Jellyfin library not found")


def _to_jellyfin_library_tree(library_id: str, items) -> JellyfinLibraryTreeResponse:
    item_list = list(items)
    library_name = item_list[0].library_name if item_list else ""
    collection_type = (
        "tvshows"
        if any(item.item_type == "Episode" for item in item_list)
        else "movies"
    )
    movie_items = [item for item in item_list if item.item_type == "Movie"]
    series_items = [item for item in item_list if item.item_type == "Series"]
    episode_items = [
        item
        for item in item_list
        if item.item_type == "Episode"
    ]
    return JellyfinLibraryTreeResponse(
        library_id=library_id,
        library_name=library_name,
        collection_type=collection_type,
        movies=[_to_jellyfin_tree_media_card(item) for item in _sort_movie_items(movie_items)],
        series=_to_jellyfin_series_tree(series_items, episode_items),
    )


def _sort_movie_items(items) -> list:
    return sorted(items, key=lambda item: (item.name or "", item.year or 0, item.jellyfin_item_id))


def _to_jellyfin_series_tree(series_items, episode_items) -> list[dict]:
    series_by_id = {item.jellyfin_item_id: item for item in series_items}
    series_groups: dict[str, list] = {item.jellyfin_item_id: [] for item in series_items}
    for item in episode_items:
        key = item.series_id or item.series_name or item.name or "Unknown Series"
        series_groups.setdefault(key, []).append(item)

    series_payload = []
    for series_key in sorted(
        series_groups,
        key=lambda key: (series_by_id.get(key).name if key in series_by_id else key),
    ):
        grouped_episodes = series_groups[series_key]
        series_item = series_by_id.get(series_key)
        fallback = grouped_episodes[0] if grouped_episodes else None
        series_name = (
            series_item.name if series_item is not None else (fallback.series_name if fallback else series_key)
        )
        season_groups: dict[int | None, list] = {}
        for item in grouped_episodes:
            season_groups.setdefault(item.season, []).append(item)

        seasons = []
        for season_number in sorted(
            season_groups,
            key=lambda value: (-1 if value is None else value),
        ):
            season_items = sorted(
                season_groups[season_number],
                key=lambda item: (-1 if item.episode is None else item.episode, item.name or ""),
            )
            seasons.append(
                {
                    "season": season_number,
                    "status": _rollup_subtitle_status(
                        [item.subtitle_status for item in season_items]
                    ),
                    "has_external_chinese_subtitle": any(
                        item.has_external_chinese_subtitle for item in season_items
                    ),
                    "has_embedded_chinese_subtitle": any(
                        item.has_embedded_chinese_subtitle for item in season_items
                    ),
                    "episodes": [_to_jellyfin_tree_episode(item) for item in season_items],
                }
            )

        series_payload.append(
            {
                "id": series_item.jellyfin_item_id if series_item is not None else str(series_key),
                "name": series_name,
                "year": series_item.year if series_item is not None else None,
                "status": (
                    "ignored"
                    if series_item is not None and series_item.ignored
                    else _rollup_subtitle_status(
                        [item.subtitle_status for item in grouped_episodes]
                    )
                ),
                "has_external_chinese_subtitle": any(
                    item.has_external_chinese_subtitle for item in grouped_episodes
                ),
                "has_embedded_chinese_subtitle": any(
                    item.has_embedded_chinese_subtitle for item in grouped_episodes
                ),
                "production_locations": _merge_production_locations(
                    ([series_item] if series_item is not None else []) + grouped_episodes
                ),
                "primary_image_tag": series_item.primary_image_tag if series_item is not None else None,
                "image_url": _jellyfin_image_url(series_item) if series_item is not None else None,
                "ignored": bool(series_item is not None and series_item.ignored),
                "date_created": (
                    _as_utc(series_item.jellyfin_date_created)
                    if series_item is not None
                    else max(
                        (
                            _as_utc(item.jellyfin_date_created)
                            for item in grouped_episodes
                            if item.jellyfin_date_created is not None
                        ),
                        default=None,
                    )
                ),
                "seasons": seasons,
            }
        )
    return series_payload


def _rollup_subtitle_status(statuses: list[str]) -> str:
    normalized = [status or "unknown" for status in statuses]
    if not normalized:
        return "unknown"
    unique_statuses = set(normalized)
    if unique_statuses == {"has_chinese"}:
        return "has_chinese"
    if unique_statuses == {"missing"}:
        return "missing"
    if unique_statuses == {"unknown"}:
        return "unknown"
    return "partial"


def _to_jellyfin_tree_media_card(item) -> dict:
    return {
        "id": item.jellyfin_item_id,
        "name": item.name,
        "year": item.year,
        "status": "ignored" if item.ignored else item.subtitle_status,
        "has_external_chinese_subtitle": item.has_external_chinese_subtitle,
        "has_embedded_chinese_subtitle": item.has_embedded_chinese_subtitle,
        "production_locations": list(item.production_locations_json or []),
        "path": item.path,
        "primary_image_tag": item.primary_image_tag,
        "image_url": _jellyfin_image_url(item),
        "ignored": item.ignored,
        "date_created": _as_utc(item.jellyfin_date_created),
    }


def _to_jellyfin_tree_episode(item) -> dict:
    payload = _to_jellyfin_tree_media_card(item)
    payload["season"] = item.season
    payload["episode"] = item.episode
    return payload


def _merge_production_locations(items) -> list[str]:
    locations: list[str] = []
    seen: set[str] = set()
    for item in items:
        for location in item.production_locations_json or []:
            value = str(location).strip()
            key = value.casefold()
            if value and key not in seen:
                seen.add(key)
                locations.append(value)
    return locations


def _jellyfin_image_url(item) -> str | None:
    if not item.primary_image_tag:
        return None
    return f"/api/v1/jellyfin/items/{item.jellyfin_item_id}/primary-image"


def _jellyfin_image_headers(image_tag: str) -> dict[str, str]:
    return {
        "ETag": f'"{image_tag.replace(chr(34), "")}"',
        "Cache-Control": "public, max-age=31536000, immutable",
    }


def _etag_matches(if_none_match: str | None, etag: str) -> bool:
    if not if_none_match:
        return False
    return etag in {value.strip() for value in if_none_match.split(",")}


def _jellyfin_image_cache_paths(cache_root: Path, item_id: str, image_tag: str) -> tuple[Path, Path]:
    cache_dir = cache_root / "jellyfin-images"
    key = f"{quote(item_id, safe='')}+{quote(image_tag, safe='')}"
    return cache_dir / f"{key}.image", cache_dir / f"{key}.json"


def _read_jellyfin_image_cache(
    cache_dir: Path,
    item_id: str,
    image_tag: str,
) -> tuple[bytes, str] | None:
    image_path, metadata_path = _jellyfin_image_cache_paths(cache_dir, item_id, image_tag)
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        content_type = str(metadata.get("content_type") or "application/octet-stream")
        return image_path.read_bytes(), content_type
    except (OSError, ValueError, TypeError):
        return None


def _write_jellyfin_image_cache(
    cache_dir: Path,
    item_id: str,
    image_tag: str,
    content: bytes,
    content_type: str,
) -> None:
    image_path, metadata_path = _jellyfin_image_cache_paths(cache_dir, item_id, image_tag)
    image_path.parent.mkdir(parents=True, exist_ok=True)
    image_path.write_bytes(content)
    metadata_path.write_text(
        json.dumps({"content_type": content_type}, separators=(",", ":")),
        encoding="utf-8",
    )


def _purge_jellyfin_image_cache(cache_root: Path, item_ids: list[str]) -> None:
    cache_dir = cache_root / "jellyfin-images"
    for item_id in item_ids:
        for path in cache_dir.glob(f"{quote(item_id, safe='')}+*"):
            try:
                path.unlink()
            except OSError:
                continue


def _to_jellyfin_media_item(item) -> JellyfinMediaItemResponse:
    return JellyfinMediaItemResponse(
        jellyfin_item_id=item.jellyfin_item_id,
        library_id=item.library_id,
        library_name=item.library_name,
        item_type=item.item_type,
        name=item.name,
        original_title=item.original_title,
        series_id=item.series_id,
        series_name=item.series_name,
        year=item.year,
        season=item.season,
        episode=item.episode,
        path=item.path,
        provider_ids=item.provider_ids_json,
        production_locations=list(item.production_locations_json or []),
        primary_image_tag=item.primary_image_tag,
        subtitle_status=item.subtitle_status,
        has_external_chinese_subtitle=item.has_external_chinese_subtitle,
        has_embedded_chinese_subtitle=item.has_embedded_chinese_subtitle,
        has_bilingual_subtitle=item.has_bilingual_subtitle,
        ignored=item.ignored,
        last_scanned_at=_as_utc(item.last_scanned_at),
    )


def _set_jellyfin_media_item_ignored(
    request: Request,
    item_id: str,
    *,
    ignored: bool,
) -> JellyfinIgnoreResponse:
    with session_scope(request.app.state.engine) as session:
        repo = Repository(session)
        try:
            item = repo.set_jellyfin_media_item_ignored(item_id, ignored=ignored)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if item is None:
            raise HTTPException(status_code=404, detail="Jellyfin item not found")
        return JellyfinIgnoreResponse(
            item_id=item.jellyfin_item_id,
            item_type=item.item_type,
            ignored=item.ignored,
        )


def _to_system_event_log(event: SystemEvent) -> dict:
    return {
        "id": event.id,
        "ts": _as_utc(event.created_at).isoformat(),
        "level": event.level.lower(),
        "event": event.event,
        "category": event.category,
        "task_id": event.task_id,
        "message": event.message,
    }


def _to_job_list_item(job: Job) -> JobListItemResponse:
    return JobListItemResponse(
        job_id=job.id,
        status=job.status,
        created_at=_as_utc(job.created_at),
        updated_at=_as_utc(job.updated_at),
        video_tasks=[_to_task_summary(task) for task in job.video_tasks],
    )


def _to_task_summary(task: VideoTask) -> VideoTaskSummaryResponse:
    return VideoTaskSummaryResponse(
        id=task.id,
        job_id=task.job_id,
        status=task.status,
        video_path_original=task.video_path_original,
        result_subtitle_path=task.result_subtitle_path,
        created_at=_as_utc(task.created_at),
        updated_at=_as_utc(task.updated_at),
    )


def _to_task_detail(task: VideoTask) -> VideoTaskDetailResponse:
    return VideoTaskDetailResponse(
        id=task.id,
        job_id=task.job_id,
        status=task.status,
        error_message=task.error_message,
        video_path_original=task.video_path_original,
        video_path_resolved=task.video_path_resolved,
        result_subtitle_path=task.result_subtitle_path,
        created_at=_as_utc(task.created_at),
        updated_at=_as_utc(task.updated_at),
        candidates=[_to_candidate(candidate) for candidate in task.candidates],
        artifacts=[_to_artifact(artifact) for artifact in task.artifacts],
        events=[_to_task_event(event) for event in task.events],
    )


def _to_candidate(candidate: SubtitleCandidateRecord) -> SubtitleCandidateResponse:
    return SubtitleCandidateResponse(
        id=candidate.id,
        provider=candidate.provider,
        language=candidate.language,
        is_bilingual=candidate.is_bilingual,
        format=candidate.format,
        score=candidate.score,
        title=candidate.title,
        release_info=candidate.release_info,
        source_url=candidate.source_url,
        download_status=candidate.download_status,
        attempt_count=candidate.attempt_count,
        last_attempt_status=candidate.last_attempt_status,
        last_error_message=candidate.last_error_message,
        raw_metadata=candidate.raw_metadata_json,
        created_at=_as_utc(candidate.created_at),
    )


def _to_artifact(artifact: SubtitleArtifact) -> SubtitleArtifactResponse:
    return SubtitleArtifactResponse(
        id=artifact.id,
        candidate_id=artifact.candidate_id,
        kind=artifact.kind,
        path=artifact.path,
        is_synced=artifact.is_synced,
        created_at=_as_utc(artifact.created_at),
    )


def _to_task_event(event: TaskEvent) -> TaskEventResponse:
    return TaskEventResponse(
        id=event.id,
        video_task_id=event.video_task_id,
        stage=event.stage,
        status=event.status,
        message=event.message,
        error_code=event.error_code,
        details=event.details_json,
        created_at=_as_utc(event.created_at),
    )


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
