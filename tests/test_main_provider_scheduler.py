from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

from subtitle_sidecar.config import AppSettings
from subtitle_sidecar.main import (
    _build_default_job_processor,
    _provider_interval_from_requests_per_minute,
    create_app,
)
from subtitle_sidecar.providers.scheduler import ProviderSearchScheduler


def test_create_app_uses_zero_global_queue_interval_and_builds_scheduler(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path, job_processor=lambda task_id: None)

    assert app.state.task_queue.interval_seconds == 0.0
    assert isinstance(app.state.provider_scheduler, ProviderSearchScheduler)


def test_create_app_keeps_custom_job_processor_while_creating_scheduler(tmp_path: Path) -> None:
    def custom_processor(task_id: int) -> None:
        return None

    app = create_app(data_dir=tmp_path, job_processor=custom_processor)

    assert app.state.job_processor is custom_processor
    assert isinstance(app.state.provider_scheduler, ProviderSearchScheduler)


def test_default_job_processor_reuses_shared_scheduler_across_calls(monkeypatch, tmp_path: Path) -> None:
    settings = AppSettings(data_dir=tmp_path)
    scheduler = ProviderSearchScheduler({"assrt": 12.0})
    seen_schedulers: list[ProviderSearchScheduler] = []

    class FakeRepository:
        def __init__(self, session) -> None:
            self.session = session

        def get_setting(self, key: str):
            return None

    class FakeResolver:
        def __init__(self, paths) -> None:
            self.paths = paths

    class FakeProvider:
        name = "assrt"

        def search(self, request):
            return []

        def download(self, candidate, target_dir):
            raise NotImplementedError

    class FakeRegistry:
        def __init__(self, providers, *, scheduler=None) -> None:
            seen_schedulers.append(scheduler)
            self.providers = providers
            self.scheduler = scheduler

    class FakeOrchestrator:
        def __init__(self, settings, repo, resolver, registry) -> None:
            self.registry = registry

        def process_video_task(self, task_id: int) -> None:
            return None

    @contextmanager
    def fake_session_scope(engine):
        yield object()

    monkeypatch.setattr("subtitle_sidecar.main.session_scope", fake_session_scope)
    monkeypatch.setattr("subtitle_sidecar.main.Repository", FakeRepository)
    monkeypatch.setattr("subtitle_sidecar.main.MediaResolver", FakeResolver)
    monkeypatch.setattr("subtitle_sidecar.main.ProviderRegistry", FakeRegistry)
    monkeypatch.setattr("subtitle_sidecar.main.SubtitleOrchestrator", FakeOrchestrator)
    monkeypatch.setattr("subtitle_sidecar.main.build_enabled_adapters", lambda *args, **kwargs: [FakeProvider()])

    processor = _build_default_job_processor(settings, engine=object(), provider_scheduler=scheduler)
    processor(1)
    processor(2)

    assert seen_schedulers == [scheduler, scheduler]


def test_provider_interval_uses_requests_per_minute_or_fallback() -> None:
    assert _provider_interval_from_requests_per_minute(5, 12.0) == 12.0
    assert _provider_interval_from_requests_per_minute(20, 3.0) == 3.0
    assert _provider_interval_from_requests_per_minute(0, 8.0) == 8.0
    assert _provider_interval_from_requests_per_minute(-1, 60.0) == 60.0


def test_default_job_processor_updates_scheduler_from_persisted_provider_settings(
    monkeypatch,
    tmp_path: Path,
) -> None:
    settings = AppSettings(data_dir=tmp_path)
    updated_intervals: list[dict[str, float]] = []

    class SpyScheduler:
        def update_intervals(self, intervals: dict[str, float]) -> None:
            updated_intervals.append(dict(intervals))

    class FakeRepository:
        def __init__(self, session) -> None:
            self.session = session

        def get_setting(self, key: str):
            if key == "assrt":
                return {"enabled": True, "token": "x", "requests_per_minute": 4}
            if key == "subdl":
                return {"enabled": True, "api_key": "y", "requests_per_minute": 30}
            return None

    class FakeResolver:
        def __init__(self, paths) -> None:
            self.paths = paths

    class FakeProvider:
        name = "assrt"

        def search(self, request):
            return []

        def download(self, candidate, target_dir):
            raise NotImplementedError

    class FakeRegistry:
        def __init__(self, providers, *, scheduler=None) -> None:
            self.providers = providers
            self.scheduler = scheduler

    class FakeOrchestrator:
        def __init__(self, settings, repo, resolver, registry) -> None:
            self.registry = registry

        def process_video_task(self, task_id: int) -> None:
            return None

    @contextmanager
    def fake_session_scope(engine):
        yield object()

    monkeypatch.setattr("subtitle_sidecar.main.session_scope", fake_session_scope)
    monkeypatch.setattr("subtitle_sidecar.main.Repository", FakeRepository)
    monkeypatch.setattr("subtitle_sidecar.main.MediaResolver", FakeResolver)
    monkeypatch.setattr("subtitle_sidecar.main.ProviderRegistry", FakeRegistry)
    monkeypatch.setattr("subtitle_sidecar.main.SubtitleOrchestrator", FakeOrchestrator)
    monkeypatch.setattr("subtitle_sidecar.main.build_enabled_adapters", lambda *args, **kwargs: [FakeProvider()])
    monkeypatch.setattr(
        "subtitle_sidecar.main.build_recommended_provider_intervals",
        lambda order=None: {
            "subliminal": 60.0,
            "assrt": 12.0,
            "subdl": 3.0,
            "zimuku": 8.0,
        },
    )

    processor = _build_default_job_processor(
        settings,
        engine=object(),
        provider_scheduler=SpyScheduler(),
    )
    processor(1)

    assert updated_intervals == [
        {
            "subliminal": 60.0,
            "assrt": 15.0,
            "subdl": 2.0,
            "zimuku": 8.0,
        }
    ]
