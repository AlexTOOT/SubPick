import asyncio
from datetime import datetime, timedelta, timezone

from subtitle_sidecar.config import AppSettings
from subtitle_sidecar.db.repository import JobCreate, Repository
from subtitle_sidecar.db.session import create_sqlite_engine, create_tables, session_scope
from subtitle_sidecar.main import _build_bundle_cache_probe
from subtitle_sidecar.media.nfo import NfoIdentityPending
from subtitle_sidecar.queue import TaskQueue


def _create_task(engine, path: str) -> int:
    with session_scope(engine) as session:
        repo = Repository(session)
        job = repo.create_job(
            JobCreate(
                source="test",
                raw_payload={"physical_video_file_full_path": path},
                video_path_original=path,
            )
        )
        return job.video_tasks[0].id


def test_task_queue_processes_tasks_serially_and_waits_between_items(tmp_path):
    engine = create_sqlite_engine(f"sqlite:///{tmp_path / 'queue.sqlite3'}")
    create_tables(engine)
    first_task_id = _create_task(engine, "/media/A.mkv")
    second_task_id = _create_task(engine, "/media/B.mkv")

    calls: list[int] = []
    sleeps: list[float] = []
    running = 0
    max_running = 0

    def processor(task_id: int) -> None:
        nonlocal running, max_running
        running += 1
        max_running = max(max_running, running)
        calls.append(task_id)
        with session_scope(engine) as session:
            Repository(session).update_video_task_status(task_id, "completed")
        running -= 1

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    async def run_queue() -> None:
        queue = TaskQueue(
            engine=engine,
            processor=processor,
            interval_seconds=12.5,
            sleep=fake_sleep,
        )
        await queue.start(recover=False)
        queue.enqueue(first_task_id)
        queue.enqueue(second_task_id)
        await queue.join()
        await queue.stop()

    asyncio.run(run_queue())

    assert calls == [first_task_id, second_task_id]
    assert max_running == 1
    assert sleeps == [12.5]
    with session_scope(engine) as session:
        repo = Repository(session)
        assert repo.list_task_events(first_task_id) == []
        assert repo.list_task_events(second_task_id) == []


def test_task_queue_records_only_failed_event_when_processor_raises(tmp_path):
    engine = create_sqlite_engine(f"sqlite:///{tmp_path / 'queue.sqlite3'}")
    create_tables(engine)
    task_id = _create_task(engine, "/media/failed.mkv")

    def processor(processed_task_id: int) -> None:
        assert processed_task_id == task_id
        raise RuntimeError("processor failed")

    async def run_queue() -> None:
        queue = TaskQueue(
            engine=engine,
            processor=processor,
            interval_seconds=0,
        )
        await queue.start(recover=False)
        queue.enqueue(task_id)
        await queue.join()
        await queue.stop()

    asyncio.run(run_queue())

    with session_scope(engine) as session:
        repo = Repository(session)
        task = repo.get_video_task(task_id)
        events = repo.list_task_events(task_id)

    assert task is not None
    assert task.status == "failed"
    assert task.error_message == "processor failed"
    assert len(events) == 1
    assert events[0].stage == "queue"
    assert events[0].status == "failed"
    assert events[0].message == "processor failed"
    assert events[0].error_code == "processor failed"
    assert events[0].details_json["duration_ms"] >= 0


def test_task_queue_prioritizes_cached_subtitle_tasks_without_waiting(tmp_path):
    engine = create_sqlite_engine(f"sqlite:///{tmp_path / 'queue.sqlite3'}")
    create_tables(engine)
    network_task_id = _create_task(engine, "/media/network.mkv")
    cached_task_id = _create_task(engine, "/media/cached.mkv")
    processed: list[int] = []
    sleeps: list[float] = []

    def processor(task_id: int) -> None:
        processed.append(task_id)

    def cache_probe(task_id: int) -> bool:
        return task_id == cached_task_id

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    async def run_queue() -> None:
        queue = TaskQueue(
            engine=engine,
            processor=processor,
            interval_seconds=30,
            cache_probe=cache_probe,
            sleep=fake_sleep,
        )
        await queue.start(recover=False)
        queue.enqueue(network_task_id)
        queue.enqueue(cached_task_id)
        await queue.join()
        await queue.stop()

    asyncio.run(run_queue())

    assert processed == [cached_task_id, network_task_id]
    assert sleeps == []


def test_manual_retry_is_treated_as_high_priority_queue_work(tmp_path, monkeypatch):
    engine = create_sqlite_engine(f"sqlite:///{tmp_path / 'queue.sqlite3'}")
    create_tables(engine)
    with session_scope(engine) as session:
        repo = Repository(session)
        regular = repo.create_job(
            JobCreate(
                source="jellyfin-manual",
                raw_payload={"physical_video_file_full_path": "/media/regular.mkv"},
                video_path_original="/media/regular.mkv",
            )
        ).video_tasks[0]
        retry = repo.create_job(
            JobCreate(
                source="manual-retry",
                raw_payload={"physical_video_file_full_path": "/media/retry.mkv"},
                video_path_original="/media/retry.mkv",
            )
        ).video_tasks[0]
        regular_id = regular.id
        retry_id = retry.id

    monkeypatch.setattr(
        "subtitle_sidecar.pipeline.orchestrator.SubtitleOrchestrator.has_cached_bundle",
        lambda _self, _task_id: False,
    )
    priority_probe = _build_bundle_cache_probe(AppSettings(data_dir=tmp_path), engine)

    assert priority_probe(regular_id) is False
    assert priority_probe(retry_id) is True


def test_queue_recovery_preflights_high_priority_tasks_first(tmp_path):
    engine = create_sqlite_engine(f"sqlite:///{tmp_path / 'queue.sqlite3'}")
    create_tables(engine)
    regular_task_id = _create_task(engine, "/media/regular.mkv")
    priority_task_id = _create_task(engine, "/media/priority.mkv")
    preflighted: list[int] = []
    processed: list[int] = []

    def preflight(task_id: int) -> bool:
        preflighted.append(task_id)
        return True

    async def run_queue() -> None:
        queue = TaskQueue(
            engine=engine,
            processor=processed.append,
            preflight_processor=preflight,
            interval_seconds=0,
            cache_probe=lambda task_id: task_id == priority_task_id,
        )
        await queue.start(recover=True)
        await queue.join()
        await queue.stop()

    asyncio.run(run_queue())

    assert preflighted == [priority_task_id, regular_task_id]
    assert processed == [priority_task_id, regular_task_id]


def test_task_queue_preflights_local_checks_before_provider_queue(tmp_path):
    engine = create_sqlite_engine(f"sqlite:///{tmp_path / 'queue.sqlite3'}")
    create_tables(engine)
    local_task_id = _create_task(engine, "/media/local.mkv")
    provider_task_id = _create_task(engine, "/media/provider.mkv")
    preflighted: list[int] = []
    processed: list[int] = []

    def preflight(task_id: int) -> bool:
        preflighted.append(task_id)
        return task_id == provider_task_id

    def processor(task_id: int) -> None:
        processed.append(task_id)

    async def run_queue() -> None:
        queue = TaskQueue(
            engine=engine,
            processor=processor,
            preflight_processor=preflight,
            interval_seconds=30,
        )
        await queue.start(recover=False)
        queue.enqueue(local_task_id)
        queue.enqueue(provider_task_id)
        await queue.join()
        await queue.stop()

    asyncio.run(run_queue())

    assert preflighted == [local_task_id, provider_task_id]
    assert processed == [provider_task_id]


def test_task_queue_retries_preflight_while_moviepilot_nfo_is_pending(tmp_path):
    engine = create_sqlite_engine(f"sqlite:///{tmp_path / 'queue.sqlite3'}")
    create_tables(engine)
    task_id = _create_task(engine, "/media/pending-nfo.mkv")
    preflighted: list[int] = []
    processed: list[int] = []
    sleeps: list[float] = []

    def preflight(current_task_id: int) -> bool:
        preflighted.append(current_task_id)
        if len(preflighted) == 1:
            raise NfoIdentityPending("NFO is still being written", retry_after_seconds=0.5)
        return True

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    async def run_queue() -> None:
        queue = TaskQueue(
            engine=engine,
            processor=processed.append,
            preflight_processor=preflight,
            interval_seconds=0,
            sleep=fake_sleep,
        )
        await queue.start(recover=False)
        queue.enqueue(task_id)
        await queue.join()
        await queue.stop()

    asyncio.run(run_queue())

    assert preflighted == [task_id, task_id]
    assert processed == [task_id]
    assert sleeps == [0.5]


def test_task_queue_recovers_queued_tasks_and_marks_stale_running_interrupted(tmp_path):
    engine = create_sqlite_engine(f"sqlite:///{tmp_path / 'queue.sqlite3'}")
    create_tables(engine)
    queued_task_id = _create_task(engine, "/media/queued.mkv")
    running_task_id = _create_task(engine, "/media/running.mkv")

    with session_scope(engine) as session:
        repo = Repository(session)
        repo.update_video_task_status(running_task_id, "running")

    calls: list[int] = []

    def processor(task_id: int) -> None:
        calls.append(task_id)
        with session_scope(engine) as session:
            Repository(session).update_video_task_status(task_id, "completed")

    async def run_queue() -> None:
        queue = TaskQueue(
            engine=engine,
            processor=processor,
            interval_seconds=0,
        )
        await queue.start(recover=True)
        await queue.join()
        await queue.stop()

    asyncio.run(run_queue())

    with session_scope(engine) as session:
        repo = Repository(session)
        queued_task = repo.get_video_task(queued_task_id)
        running_task = repo.get_video_task(running_task_id)
        running_events = repo.list_task_events(running_task_id)

    assert calls[0] == queued_task_id
    assert len(calls) == 2
    assert queued_task is not None
    assert queued_task.status == "completed"
    assert running_task is not None
    assert running_task.status == "interrupted"
    assert running_task.error_message == "interrupted_by_restart"
    assert [(event.stage, event.status, event.error_code) for event in running_events] == [
        ("queue", "interrupted", "interrupted_by_restart"),
        ("retry_schedule", "completed", None),
    ]


def test_failed_task_persists_retry_wait_and_restart_recovers_when_due(tmp_path) -> None:
    engine = create_sqlite_engine(f"sqlite:///{tmp_path / 'queue.sqlite3'}")
    create_tables(engine)
    task_id = _create_task(engine, "/media/retry-later.mkv")
    now = datetime(2026, 8, 31, tzinfo=timezone.utc)

    def fail_with_provider_error(current_task_id: int) -> None:
        with session_scope(engine) as session:
            Repository(session).update_video_task_status(
                current_task_id,
                "failed",
                "provider_request_timeout",
            )

    async def schedule_retry() -> None:
        queue = TaskQueue(
            engine=engine,
            processor=fail_with_provider_error,
            interval_seconds=0,
            clock=lambda: now,
            retry_jitter=lambda value: value,
        )
        await queue.start(recover=False)
        queue.enqueue(task_id)
        await queue.join()
        await queue.stop()

    asyncio.run(schedule_retry())

    with session_scope(engine) as session:
        repo = Repository(session)
        retry_ids = repo.list_retry_wait_task_ids()
        assert len(retry_ids) == 1
        retry_task = repo.get_video_task(retry_ids[0])
        assert retry_task is not None
        assert retry_task.auto_retry_count == 1
        assert retry_task.retry_category == "provider_network"
        assert retry_task.retry_at == (now + timedelta(minutes=1)).replace(tzinfo=None)

    processed: list[int] = []

    def complete(current_task_id: int) -> None:
        processed.append(current_task_id)
        with session_scope(engine) as session:
            Repository(session).update_video_task_status(current_task_id, "completed")

    async def recover_due_retry() -> None:
        queue = TaskQueue(
            engine=engine,
            processor=complete,
            interval_seconds=0,
            clock=lambda: now + timedelta(minutes=2),
            retry_jitter=lambda value: value,
        )
        await queue.start(recover=True)
        await queue.join()
        await queue.stop()

    asyncio.run(recover_due_retry())

    assert processed == retry_ids
    with session_scope(engine) as session:
        assert Repository(session).get_video_task(retry_ids[0]).status == "completed"


def test_successful_task_cancels_pending_auto_retry_for_same_path(tmp_path) -> None:
    engine = create_sqlite_engine(f"sqlite:///{tmp_path / 'queue.sqlite3'}")
    create_tables(engine)
    path = "/media/already-fixed.mkv"
    successful_task_id = _create_task(engine, path)
    now = datetime(2026, 8, 31, tzinfo=timezone.utc)

    with session_scope(engine) as session:
        repo = Repository(session)
        failed = repo.create_job(
            JobCreate(
                source="test",
                raw_payload={"physical_video_file_full_path": path},
                video_path_original=path,
            )
        ).video_tasks[0]
        failed.status = "failed"
        failed.error_message = "no_candidate_found"
        pending = repo.create_retry_child(
            failed,
            source="auto-retry",
            status="retry_wait",
            retry_at=now + timedelta(hours=6),
            auto_retry_count=1,
            retry_category="no_candidate",
        )
        pending_task_id = pending.id

    processed: list[int] = []

    def complete(current_task_id: int) -> None:
        processed.append(current_task_id)
        with session_scope(engine) as session:
            Repository(session).update_video_task_status(current_task_id, "completed")

    async def run_queue() -> None:
        queue = TaskQueue(
            engine=engine,
            processor=complete,
            interval_seconds=0,
            clock=lambda: now,
        )
        await queue.start(recover=False)
        queue.enqueue(successful_task_id)
        await queue.join()
        await queue.stop()

    asyncio.run(run_queue())

    assert processed == [successful_task_id]
    with session_scope(engine) as session:
        repo = Repository(session)
        pending = repo.get_video_task(pending_task_id)
        assert pending is not None
        assert pending.status == "skipped_existing_subtitle"
        assert pending.retry_at is None
        assert repo.list_retry_wait_task_ids() == []
        assert repo.list_task_events(pending_task_id)[-1].error_code == (
            "retry_superseded_by_success"
        )


def test_queue_stop_does_not_wait_for_retry_poll_timeout(tmp_path) -> None:
    engine = create_sqlite_engine(f"sqlite:///{tmp_path / 'queue.sqlite3'}")
    create_tables(engine)

    async def run_queue() -> None:
        queue = TaskQueue(
            engine=engine,
            processor=lambda _task_id: None,
            interval_seconds=0,
            retry_poll_seconds=30,
        )
        await queue.start(recover=False)
        await asyncio.wait_for(queue.stop(), timeout=0.5)

    asyncio.run(run_queue())
