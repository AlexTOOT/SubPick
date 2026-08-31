from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from time import monotonic, sleep as blocking_sleep

from sqlalchemy import Engine

from subtitle_sidecar.db.repository import Repository
from subtitle_sidecar.db.session import session_scope
from subtitle_sidecar.media.nfo import NfoIdentityPending
from subtitle_sidecar.pipeline.status import (
    SUCCESS_TASK_STATUSES,
    TASK_FAILED,
    TASK_INTERRUPTED,
    TASK_QUEUED,
    TASK_RETRY_WAIT,
)
from subtitle_sidecar.retry import classify_retry_error, default_retry_jitter, retry_decision


Processor = Callable[[int], None]
PreflightProcessor = Callable[[int], bool]
Sleep = Callable[[float], Awaitable[None]]
CacheProbe = Callable[[int], bool]
Clock = Callable[[], datetime]
RetryJitter = Callable[[float], float]


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
        clock: Clock = lambda: datetime.now(timezone.utc),
        retry_jitter: RetryJitter = default_retry_jitter,
        retry_poll_seconds: float = 30.0,
    ) -> None:
        self.engine = engine
        self.processor = processor
        self.interval_seconds = max(0.0, float(interval_seconds))
        self.cache_probe = cache_probe
        self.preflight_processor = preflight_processor
        self.sleep = sleep
        self.clock = clock
        self.retry_jitter = retry_jitter
        self.retry_poll_seconds = max(1.0, float(retry_poll_seconds))
        self._queue: asyncio.PriorityQueue[tuple[int, int, int | None]] = asyncio.PriorityQueue()
        self._preflight_queue: asyncio.Queue[int | None] = asyncio.Queue()
        self._queued_task_ids: set[int] = set()
        self._sequence = 0
        self._queue_changed = asyncio.Event()
        self._network_cooldown_required = False
        self._worker_task: asyncio.Task[None] | None = None
        self._preflight_worker_task: asyncio.Task[None] | None = None
        self._retry_worker_task: asyncio.Task[None] | None = None
        self._retry_changed = asyncio.Event()
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
        self._retry_worker_task = asyncio.create_task(
            self._run_retry_wait(),
            name="subtitle-sidecar-retry-wait",
        )

    async def stop(self) -> None:
        self._stopping = True
        self._retry_changed.set()
        if self._retry_worker_task is not None:
            await self._retry_worker_task
            self._retry_worker_task = None
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
                task = repo.get_video_task(task_id)
                if task is not None:
                    _create_auto_retry_child(
                        repo,
                        task,
                        now=self.clock(),
                        jitter=self.retry_jitter,
                    )
            due_retry_ids = repo.list_due_retry_task_ids(self.clock())
            for task_id in due_retry_ids:
                repo.activate_retry_task(task_id)
            queued_task_ids = repo.list_video_task_ids_by_status([TASK_QUEUED])
            retry_wait_count = len(repo.list_retry_wait_task_ids())
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
                    "retry_wait_count": retry_wait_count,
                },
            )

        prioritized_task_ids = sorted(
            queued_task_ids,
            key=lambda task_id: 0 if self._is_cache_hit(task_id) else 1,
        )
        for task_id in prioritized_task_ids:
            self.enqueue(task_id)

    async def _run(self) -> None:
        while True:
            _priority, _sequence, task_id = await self._queue.get()
            if task_id is None:
                self._queue.task_done()
                return
            self._queued_task_ids.discard(task_id)
            if not self._is_task_queued(task_id):
                self._queue.task_done()
                continue
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
                if not self._is_task_queued(task_id):
                    self._queued_task_ids.discard(task_id)
                    continue
                needs_network = await asyncio.to_thread(self.preflight_processor, task_id)
            except NfoIdentityPending as pending:
                await self.sleep(pending.retry_after_seconds)
                if self._stopping:
                    self._queued_task_ids.discard(task_id)
                else:
                    self._preflight_queue.put_nowait(task_id)
            except Exception as exc:
                self._record_processor_failure(task_id, exc)
                self._queued_task_ids.discard(task_id)
            else:
                if needs_network:
                    self._put(task_id, priority=0 if self._is_cache_hit(task_id) else 1)
                    self._queue_changed.set()
                else:
                    self._queued_task_ids.discard(task_id)
                    self._schedule_retry_after_terminal(task_id)
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

    def _is_task_queued(self, task_id: int) -> bool:
        with session_scope(self.engine) as session:
            task = Repository(session).get_video_task(task_id)
            return task is not None and task.status == TASK_QUEUED

    async def _process_one(self, task_id: int) -> None:
        started_at = monotonic()
        self._active_task_id = task_id
        try:
            await asyncio.to_thread(self.processor, task_id)
            self._schedule_retry_after_terminal(task_id)
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
        self._schedule_retry_after_terminal(task_id)

    def _schedule_retry_after_terminal(self, task_id: int) -> None:
        child_id: int | None = None
        child_status: str | None = None
        with session_scope(self.engine) as session:
            repo = Repository(session)
            task = repo.get_video_task(task_id)
            if task is None:
                return
            if task.status in SUCCESS_TASK_STATUSES:
                canceled_ids = repo.cancel_pending_auto_retries_for_path(
                    task.video_path_original,
                    alternate_path=task.video_path_resolved,
                    completed_task_id=task.id,
                )
                for canceled_task_id in canceled_ids:
                    repo.record_task_event(
                        video_task_id=canceled_task_id,
                        stage="retry_wait",
                        status="skipped",
                        message=f"同一路径任务 #{task.id} 已成功，取消本次自动重试",
                        error_code="retry_superseded_by_success",
                        details={"completed_task_id": task.id},
                    )
                return
            if task.status not in {TASK_FAILED, TASK_INTERRUPTED}:
                return
            child = _create_auto_retry_child(
                repo,
                task,
                now=self.clock(),
                jitter=self.retry_jitter,
            )
            if child is not None:
                child_id = child.id
                child_status = child.status
        if child_id is None:
            return
        if child_status == TASK_QUEUED:
            self.enqueue(child_id)
        else:
            self._retry_changed.set()

    async def _run_retry_wait(self) -> None:
        while True:
            self._retry_changed.clear()
            if self._stopping:
                return
            due_ids: list[int] = []
            timeout = self.retry_poll_seconds
            with session_scope(self.engine) as session:
                repo = Repository(session)
                due_ids = repo.list_due_retry_task_ids(self.clock())
                for task_id in due_ids:
                    repo.activate_retry_task(task_id)
                next_retry_at = repo.next_retry_at()
                if next_retry_at is not None:
                    delay = _seconds_until(next_retry_at, self.clock())
                    timeout = max(0.05, min(timeout, delay))
            for task_id in due_ids:
                self.enqueue(task_id)
            if self._stopping:
                return
            try:
                await asyncio.wait_for(self._retry_changed.wait(), timeout=timeout)
            except TimeoutError:
                pass


def _create_auto_retry_child(
    repo: Repository,
    task,
    *,
    now: datetime,
    jitter: RetryJitter,
):
    if repo.has_task_event(task.id, "retry_schedule"):
        return None
    existing = repo.find_in_flight_task_for_path(
        task.video_path_original,
        alternate_path=task.video_path_resolved,
        exclude_task_id=task.id,
    )
    if existing is not None:
        repo.record_task_event(
            video_task_id=task.id,
            stage="retry_schedule",
            status="skipped",
            message=f"同一路径已有活动任务 #{existing.id}，不重复创建自动重试",
            error_code="retry_task_already_active",
            details={"existing_task_id": existing.id},
        )
        return None
    decision = retry_decision(
        status=task.status,
        error_code=task.error_message,
        completed_auto_retries=task.auto_retry_count,
        jitter=jitter,
    )
    if decision is None:
        if classify_retry_error(task.status, task.error_message) is None:
            return None
        repo.record_task_event(
            video_task_id=task.id,
            stage="retry_schedule",
            status="completed",
            message="该错误不再自动重试",
            error_code="auto_retry_not_scheduled",
            details={
                "error": task.error_message,
                "completed_auto_retries": task.auto_retry_count,
            },
        )
        return None
    retry_at = _aware_utc(now) + timedelta(seconds=decision.delay_seconds)
    child_status = TASK_QUEUED if decision.delay_seconds <= 0 else TASK_RETRY_WAIT
    child = repo.create_retry_child(
        task,
        source="auto-retry",
        status=child_status,
        retry_at=retry_at,
        auto_retry_count=decision.attempt,
        retry_category=decision.category,
    )
    details = {
        "retry_task_id": child.id,
        "retry_at": retry_at.isoformat(),
        "retry_category": decision.category,
        "auto_retry_count": decision.attempt,
        "delay_seconds": round(decision.delay_seconds, 3),
    }
    repo.record_task_event(
        video_task_id=task.id,
        stage="retry_schedule",
        status="completed",
        message=f"已创建自动重试任务 #{child.id}",
        details=details,
    )
    repo.record_task_event(
        video_task_id=child.id,
        stage="retry_wait",
        status="pending" if child_status == TASK_RETRY_WAIT else "completed",
        message=(
            f"自动重试将在 {retry_at.isoformat()} 执行"
            if child_status == TASK_RETRY_WAIT
            else "自动重试已立即排队"
        ),
        details={**details, "retry_of_task_id": task.id},
    )
    return child


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _seconds_until(value: datetime, now: datetime) -> float:
    return max(0.0, (_aware_utc(value) - _aware_utc(now)).total_seconds())


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
