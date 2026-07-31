from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
from time import monotonic, sleep as blocking_sleep

from sqlalchemy import Engine

from subtitle_sidecar.db.repository import Repository
from subtitle_sidecar.db.session import session_scope
from subtitle_sidecar.pipeline.status import TASK_FAILED, TASK_QUEUED


Processor = Callable[[int], None]
PreflightProcessor = Callable[[int], bool]
Sleep = Callable[[float], Awaitable[None]]
CacheProbe = Callable[[int], bool]


class TaskQueue:
    def __init__(
        self,
        *,
        engine: Engine,
        processor: Processor,
        interval_seconds: float,
        cache_probe: CacheProbe | None = None,
        preflight_processor: PreflightProcessor | None = None,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self.engine = engine
        self.processor = processor
        self.interval_seconds = max(0.0, float(interval_seconds))
        self.cache_probe = cache_probe
        self.preflight_processor = preflight_processor
        self.sleep = sleep
        self._queue: asyncio.PriorityQueue[tuple[int, int, int | None]] = asyncio.PriorityQueue()
        self._preflight_queue: asyncio.Queue[int | None] = asyncio.Queue()
        self._queued_task_ids: set[int] = set()
        self._sequence = 0
        self._queue_changed = asyncio.Event()
        self._network_cooldown_required = False
        self._worker_task: asyncio.Task[None] | None = None
        self._preflight_worker_task: asyncio.Task[None] | None = None
        self._stopping = False
        self._active_task_id: int | None = None
        self._active_preflight_task_id: int | None = None

    async def start(self, *, recover: bool = True) -> None:
        if self._worker_task is not None:
            return
        self._stopping = False
        self._worker_task = asyncio.create_task(self._run(), name="subtitle-sidecar-task-queue")
        if self.preflight_processor is not None:
            self._preflight_worker_task = asyncio.create_task(
                self._run_preflight(),
                name="subtitle-sidecar-preflight-queue",
            )
        if recover:
            await self.recover()

    async def stop(self) -> None:
        self._stopping = True
        await self.join()
        if self._preflight_worker_task is not None:
            self._preflight_queue.put_nowait(None)
            await self._preflight_worker_task
            self._preflight_worker_task = None
        if self._worker_task is None:
            return
        self._put(None, priority=2)
        with suppress(asyncio.CancelledError):
            await self._worker_task
        self._worker_task = None

    async def join(self) -> None:
        while True:
            await self._preflight_queue.join()
            await self._queue.join()
            if (
                self._preflight_queue.empty()
                and self._queue.empty()
                and self._active_task_id is None
                and self._active_preflight_task_id is None
            ):
                return

    async def wait_until_idle_async(self, timeout: float | None = None) -> None:
        if timeout is None:
            await self.join()
            return
        await asyncio.wait_for(self.join(), timeout=timeout)

    def wait_until_idle(self, timeout: float = 30.0) -> bool:
        deadline = monotonic() + timeout
        while monotonic() < deadline:
            if (
                self._preflight_queue.empty()
                and self._queue.empty()
                and self._active_task_id is None
                and self._active_preflight_task_id is None
            ):
                return True
            blocking_sleep(0.01)
        return False

    def enqueue(self, task_id: int) -> None:
        if task_id in self._queued_task_ids:
            return
        self._queued_task_ids.add(task_id)
        if self.preflight_processor is not None:
            self._preflight_queue.put_nowait(task_id)
        else:
            self._put(task_id, priority=0 if self._is_cache_hit(task_id) else 1)
            self._queue_changed.set()

    async def recover(self) -> None:
        with session_scope(self.engine) as session:
            repo = Repository(session)
            interrupted_task_ids = repo.mark_active_tasks_interrupted()
            for task_id in interrupted_task_ids:
                repo.record_task_event(
                    video_task_id=task_id,
                    stage="queue",
                    status="interrupted",
                    message="task was interrupted by service restart",
                    error_code="interrupted_by_restart",
                )
            queued_task_ids = repo.list_video_task_ids_by_status([TASK_QUEUED])
            repo.record_system_event(
                category="queue",
                event="queue_recovered",
                level="WARNING" if interrupted_task_ids else "INFO",
                message=(
                    f"队列恢复完成：重新排队 {len(queued_task_ids)} 个任务，"
                    f"中断 {len(interrupted_task_ids)} 个任务"
                ),
                details={
                    "queued_count": len(queued_task_ids),
                    "interrupted_count": len(interrupted_task_ids),
                },
            )

        for task_id in queued_task_ids:
            self.enqueue(task_id)

    async def _run(self) -> None:
        while True:
            _priority, _sequence, task_id = await self._queue.get()
            if task_id is None:
                self._queue.task_done()
                return
            self._queued_task_ids.discard(task_id)
            is_cache_hit = self._is_cache_hit(task_id)
            if not is_cache_hit and self._network_cooldown_required:
                should_process = await self._wait_for_network_slot(task_id)
                if not should_process:
                    self._queue.task_done()
                    continue
                self._network_cooldown_required = False
            await self._process_one(task_id)
            self._queue.task_done()
            if not is_cache_hit and self.interval_seconds > 0:
                self._network_cooldown_required = True

    async def _run_preflight(self) -> None:
        while True:
            task_id = await self._preflight_queue.get()
            if task_id is None:
                self._preflight_queue.task_done()
                return
            self._active_preflight_task_id = task_id
            try:
                needs_network = await asyncio.to_thread(self.preflight_processor, task_id)
            except Exception as exc:
                self._record_processor_failure(task_id, exc)
                self._queued_task_ids.discard(task_id)
            else:
                if needs_network:
                    self._put(task_id, priority=0 if self._is_cache_hit(task_id) else 1)
                    self._queue_changed.set()
                else:
                    self._queued_task_ids.discard(task_id)
            finally:
                self._active_preflight_task_id = None
                self._preflight_queue.task_done()

    async def _wait_for_network_slot(self, task_id: int) -> bool:
        """Wait once for rate limiting, unless a higher-priority cached task arrives."""
        if self.interval_seconds <= 0:
            return True
        if self.sleep is not asyncio.sleep:
            await self.sleep(self.interval_seconds)
            return True
        self._queue_changed.clear()
        try:
            await asyncio.wait_for(self._queue_changed.wait(), timeout=self.interval_seconds)
        except TimeoutError:
            return True
        self._queued_task_ids.add(task_id)
        self._put(task_id, priority=1)
        return False

    def _put(self, task_id: int | None, *, priority: int) -> None:
        self._sequence += 1
        self._queue.put_nowait((priority, self._sequence, task_id))

    def _is_cache_hit(self, task_id: int) -> bool:
        if self.cache_probe is None:
            return False
        try:
            return bool(self.cache_probe(task_id))
        except Exception:
            return False

    async def _process_one(self, task_id: int) -> None:
        started_at = monotonic()
        self._active_task_id = task_id
        try:
            await asyncio.to_thread(self.processor, task_id)
        except Exception as exc:
            self._record_processor_failure(task_id, exc, started_at=started_at)
            return
        finally:
            self._active_task_id = None

    def _record_processor_failure(
        self,
        task_id: int,
        exc: Exception,
        *,
        started_at: float | None = None,
    ) -> None:
        message = str(exc).strip() or exc.__class__.__name__
        details = {}
        if started_at is not None:
            details["duration_ms"] = int((monotonic() - started_at) * 1000)
        with session_scope(self.engine) as session:
            repo = Repository(session)
            repo.update_video_task_status(task_id, TASK_FAILED, message)
            repo.record_task_event(
                video_task_id=task_id,
                stage="queue",
                status="failed",
                message=message,
                error_code=message,
                details=details,
            )


class InProcessTaskQueue(TaskQueue):
    def __init__(
        self,
        *,
        engine: Engine,
        job_processor: Processor,
        search_interval_seconds: float,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        super().__init__(
            engine=engine,
            processor=job_processor,
            interval_seconds=search_interval_seconds,
            sleep=sleep,
        )
