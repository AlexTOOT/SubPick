from __future__ import annotations

from contextlib import asynccontextmanager
import hashlib
from pathlib import Path
from typing import Callable

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response

from subtitle_sidecar import DATABASE_SCHEMA_VERSION, RUNTIME_METADATA_SETTING_KEY, __version__
from subtitle_sidecar.api import create_api_router
from subtitle_sidecar.config import (
    AppSettings,
    load_settings,
    merge_assrt_provider_settings,
    merge_subdl_provider_settings,
    merge_subliminal_provider_settings,
    merge_zimuku_provider_settings,
)
from subtitle_sidecar.db.repository import Repository
from subtitle_sidecar.db.session import create_sqlite_engine, create_tables, session_scope
from subtitle_sidecar.media.resolver import MediaResolver
from subtitle_sidecar.pipeline.orchestrator import SubtitleOrchestrator
from subtitle_sidecar.providers.adapters import (
    build_enabled_adapters,
    build_recommended_provider_intervals,
)
from subtitle_sidecar.providers.registry import ProviderRegistry
from subtitle_sidecar.providers.scheduler import ProviderSearchScheduler
from subtitle_sidecar.providers.negative_cache import ProviderNegativeCache
from subtitle_sidecar.queue import TaskQueue


WEB_V2_DIR = Path(__file__).parent / "web_v2"
PROVIDER_ORDER_SETTING_KEY = "provider_order"
SERVER_SETTING_KEY = "server"


def _web_asset_version(web_dir: Path) -> str:
    digest = hashlib.sha256()
    for filename in ("index.html", "styles.css", "app.js"):
        digest.update((web_dir / filename).read_bytes())
    return digest.hexdigest()[:12]


def _no_store(response: Response) -> Response:
    response.headers["Cache-Control"] = "no-store"
    return response


def _render_web_index(web_dir: Path, asset_prefix: str) -> HTMLResponse:
    asset_version = _web_asset_version(web_dir)
    html = (web_dir / "index.html").read_text(encoding="utf-8")
    html = html.replace(
        f'href="{asset_prefix}/styles.css"',
        f'href="{asset_prefix}/styles.css?v={asset_version}"',
    ).replace(
        f'src="{asset_prefix}/app.js"',
        f'src="{asset_prefix}/app.js?v={asset_version}"',
    )
    return _no_store(HTMLResponse(html, media_type="text/html"))


class NoStoreStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope) -> Response:
        response = await super().get_response(path, scope)
        return _no_store(response)


def _build_default_job_processor(
    settings: AppSettings,
    engine,
    provider_scheduler: ProviderSearchScheduler,
) -> Callable[[int], None]:
    negative_cache = ProviderNegativeCache()

    def process_video_task(task_id: int) -> None:
        with session_scope(engine) as session:
            repo = Repository(session)
            resolver = MediaResolver(settings.paths)
            recommended_intervals = build_recommended_provider_intervals(
                order=settings.providers.order
            )
            subliminal_settings = merge_subliminal_provider_settings(
                settings.providers.subliminal,
                repo.get_setting("subliminal"),
            )
            assrt_settings = merge_assrt_provider_settings(
                settings.providers.assrt,
                repo.get_setting("assrt"),
            )
            subdl_settings = merge_subdl_provider_settings(
                settings.providers.subdl,
                repo.get_setting("subdl"),
            )
            zimuku_settings = merge_zimuku_provider_settings(
                settings.providers.zimuku,
                repo.get_setting("zimuku"),
            )
            adapter_settings = dict(settings.providers.adapters)
            adapter_settings.update(
                {
                    "subliminal": {
                        **subliminal_settings.model_dump(exclude={"authentication"}),
                        "authentication": {
                            provider: credentials.model_dump()
                            for provider, credentials in subliminal_settings.authentication.items()
                        },
                    },
                    "assrt": assrt_settings.model_dump(),
                    "subdl": subdl_settings.model_dump(),
                    "zimuku": {
                        **zimuku_settings.model_dump(),
                        "captcha_debug_dir": str(
                            settings.data_dir / "diagnostics" / "captcha" / "zimuku"
                        ),
                    },
                }
            )
            adapter_settings["assrt"]["_negative_cache"] = negative_cache
            provider_scheduler.update_intervals(
                {
                    "subliminal": recommended_intervals.get("subliminal", 0.0),
                    "zimuku": recommended_intervals.get("zimuku", 0.0),
                    "assrt": _provider_interval_from_requests_per_minute(
                        assrt_settings.requests_per_minute,
                        recommended_intervals.get("assrt", 0.0),
                    ),
                    "subdl": _provider_interval_from_requests_per_minute(
                        subdl_settings.requests_per_minute,
                        recommended_intervals.get("subdl", 0.0),
                    ),
                }
            )
            provider_instances = build_enabled_adapters(
                adapter_settings,
                order=_provider_order(repo.get_setting(PROVIDER_ORDER_SETTING_KEY), settings),
            )
            registry = ProviderRegistry(provider_instances, scheduler=provider_scheduler)
            orchestrator = SubtitleOrchestrator(settings, repo, resolver, registry)
            orchestrator.process_video_task(task_id)

    return process_video_task


def _provider_order(stored: dict | None, settings: AppSettings) -> list[str]:
    if isinstance(stored, dict):
        order = stored.get("order")
        if isinstance(order, list):
            return [str(name) for name in order]
    return list(settings.providers.order)


def _provider_interval_from_requests_per_minute(
    requests_per_minute: int,
    fallback_seconds: float,
) -> float:
    if requests_per_minute > 0:
        return 60.0 / float(requests_per_minute)
    return fallback_seconds


def _build_bundle_cache_probe(settings: AppSettings, engine) -> Callable[[int], bool]:
    def has_cached_bundle(task_id: int) -> bool:
        with session_scope(engine) as session:
            orchestrator = SubtitleOrchestrator(
                settings,
                Repository(session),
                MediaResolver(settings.paths),
                ProviderRegistry([]),
            )
            return orchestrator.has_cached_bundle(task_id)

    return has_cached_bundle


def _build_local_preflight_processor(settings: AppSettings, engine) -> Callable[[int], bool]:
    def preflight_video_task(task_id: int) -> bool:
        with session_scope(engine) as session:
            orchestrator = SubtitleOrchestrator(
                settings,
                Repository(session),
                MediaResolver(settings.paths),
                ProviderRegistry([]),
            )
            return orchestrator.preflight_video_task(task_id)

    return preflight_video_task


def create_app(
    data_dir: Path | None = None,
    token: str | None = None,
    config_path: Path | None = None,
    job_processor: Callable[[int], None] | None = None,
) -> FastAPI:
    settings = load_settings(
        config_path=config_path,
        data_dir=data_dir,
        token=token,
    )
    settings.data_dir.mkdir(parents=True, exist_ok=True)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        create_tables(app.state.engine)
        with session_scope(app.state.engine) as session:
            repo = Repository(session)
            stored_server = repo.get_setting(SERVER_SETTING_KEY) or {}
            if "token" in stored_server:
                app.state.settings.server.token = str(stored_server.get("token") or "")
            existing = repo.get_setting(RUNTIME_METADATA_SETTING_KEY) or {}
            repo.set_setting(
                RUNTIME_METADATA_SETTING_KEY,
                {
                    **existing,
                    "database_schema_version": max(
                        int(existing.get("database_schema_version", 0) or 0),
                        DATABASE_SCHEMA_VERSION,
                    ),
                    "config_version": app.state.settings.config_version,
                    "app_version": __version__,
                },
            )
            repo.prune_task_events(
                retention_days=app.state.settings.logging.retention_days,
                max_entries=app.state.settings.logging.max_task_events,
            )
        await app.state.task_queue.start(
            recover=app.state.settings.queue.recover_interrupted_tasks
        )
        try:
            yield
        finally:
            await app.state.task_queue.stop()

    app = FastAPI(title="SubPick", lifespan=lifespan)
    app.state.settings = settings
    app.state.engine = create_sqlite_engine(settings.database_url)
    app.state.provider_scheduler = ProviderSearchScheduler(
        build_recommended_provider_intervals(order=settings.providers.order)
    )
    app.state.job_processor = job_processor or _build_default_job_processor(
        settings,
        app.state.engine,
        app.state.provider_scheduler,
    )
    app.state.task_queue = TaskQueue(
        engine=app.state.engine,
        processor=app.state.job_processor,
        interval_seconds=0,
        cache_probe=_build_bundle_cache_probe(settings, app.state.engine),
        preflight_processor=_build_local_preflight_processor(settings, app.state.engine),
    )
    app.state.enqueue_task = app.state.task_queue.enqueue
    app.include_router(create_api_router())
    app.mount("/web", NoStoreStaticFiles(directory=WEB_V2_DIR), name="web")

    @app.get("/", include_in_schema=False)
    def web_index() -> HTMLResponse:
        return _render_web_index(WEB_V2_DIR, "/web")

    @app.get("/v2", include_in_schema=False)
    @app.get("/v2/", include_in_schema=False)
    def web_v2_index() -> HTMLResponse:
        return _render_web_index(WEB_V2_DIR, "/web")

    return app
