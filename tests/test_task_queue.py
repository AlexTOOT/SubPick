from __future__ import annotations

import asyncio

from subtitle_sidecar.db.repository import JobCreate, Repository
from subtitle_sidecar.db.session import create_sqlite_engine, create_tables, session_scope
from subtitle_sidecar.queue import InProcessTaskQueue


def test_task_queue_serializes_tasks_and_honors_configured_interval(tmp_path) -> None:
    engine = create_sqlite_engine(f"sqlite:///{tmp_path / 'queue.sqlite3'}")
    create_tables(engine)
    processed_task_ids: list[int] = []
    sleep_calls: list[float] = []

    def process_task(task_id: int) -> None:
        processed_task_ids.append(task_id)

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    async def run_queue() -> None:
        queue = InProcessTaskQueue(
            engine=engine,
            job_processor=process_task,
            search_interval_seconds=7.5,
            sleep=fake_sleep,
        )
        await queue.start()
        queue.enqueue(1)
        queue.enqueue(2)
        await queue.wait_until_idle_async(timeout=1)
        await queue.stop()

    asyncio.run(run_queue())

    assert processed_task_ids == [1, 2]
    assert sleep_calls == [7.5]


def test_task_queue_recovers_queued_tasks_and_interrupts_active_tasks(tmp_path) -> None:
    engine = create_sqlite_engine(f"sqlite:///{tmp_path / 'queue.sqlite3'}")
    create_tables(engine)
    processed_task_ids: list[int] = []

    with session_scope(engine) as session:
        repo = Repository(session)
        queued_job = repo.create_job(
            JobCreate(
                source="moviepilot-csf",
                raw_payload={"physical_video_file_full_path": "/media/queued.mkv"},
                video_path_original="/media/queued.mkv",
            )
        )
        active_job = repo.create_job(
            JobCreate(
                source="moviepilot-csf",
                raw_payload={"physical_video_file_full_path": "/media/active.mkv"},
                video_path_original="/media/active.mkv",
            )
        )
        queued_task_id = queued_job.video_tasks[0].id
        active_task_id = active_job.video_tasks[0].id
        repo.update_video_task_status(active_task_id, "searching")

    def process_task(task_id: int) -> None:
        processed_task_ids.append(task_id)

    async def fake_sleep(seconds: float) -> None:
        return None

    async def run_queue() -> None:
        queue = InProcessTaskQueue(
            engine=engine,
            job_processor=process_task,
            search_interval_seconds=0,
            sleep=fake_sleep,
        )
        await queue.start()
        await queue.wait_until_idle_async(timeout=1)
        await queue.stop()

    asyncio.run(run_queue())

    assert processed_task_ids == [queued_task_id]

    with session_scope(engine) as session:
        repo = Repository(session)
        active_task = repo.get_video_task(active_task_id)

    assert active_task is not None
    assert active_task.status == "interrupted"
    assert active_task.error_message == "interrupted_by_restart"
    assert [(event.stage, event.status, event.error_code) for event in active_task.events] == [
        ("queue", "interrupted", "interrupted_by_restart")
    ]
