from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import String, delete, func, or_, select
from sqlalchemy.orm import Session, selectinload

from subtitle_sidecar.observability import emit_structured_log
from subtitle_sidecar.db.models import (
    AppSetting,
    JellyfinMediaItem,
    Job,
    SubtitleArtifact,
    SubtitleCandidateRecord,
    SystemEvent,
    TaskEvent,
    VideoTask,
)
from subtitle_sidecar.pipeline.status import (
    ACTIVE_TASK_STATUSES,
    TASK_COMPLETED,
    TASK_FAILED,
    TASK_INTERRUPTED,
    TASK_SKIPPED_EMBEDDED_SUBTITLE,
    TASK_SKIPPED_EXISTING_SUBTITLE,
    TERMINAL_TASK_STATUSES,
    summarize_job_status,
)


@dataclass(frozen=True)
class JobCreate:
    source: str
    raw_payload: dict[str, Any]
    video_path_original: str
    media_server_id: str | None = None


@dataclass(frozen=True)
class JellyfinMediaItemData:
    jellyfin_item_id: str
    library_id: str
    library_name: str
    item_type: str
    name: str
    path: str
    original_title: str | None = None
    series_id: str | None = None
    series_name: str | None = None
    year: int | None = None
    season: int | None = None
    episode: int | None = None
    provider_ids: dict[str, Any] | None = None
    production_locations: list[str] | None = None
    primary_image_tag: str | None = None
    subtitle_status: str = "unknown"
    has_external_chinese_subtitle: bool = False
    has_embedded_chinese_subtitle: bool = False
    has_bilingual_subtitle: bool = False
    jellyfin_date_created: datetime | None = None
    last_scanned_at: datetime | None = None


@dataclass(frozen=True)
class JellyfinUpsertResult:
    item: JellyfinMediaItem
    status: str


class Repository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create_job(self, data: JobCreate) -> Job:
        job = Job(
            source=data.source,
            raw_payload_json=data.raw_payload,
            status="queued",
        )
        job.video_tasks.append(
            VideoTask(
                video_path_original=data.video_path_original,
                media_server_id=data.media_server_id,
                status="queued",
            )
        )
        self.session.add(job)
        self.session.flush()
        return job

    def get_setting(self, key: str) -> dict[str, Any] | None:
        setting = self.session.get(AppSetting, key)
        if setting is None:
            return None
        return dict(setting.value_json)

    def set_setting(self, key: str, value: dict[str, Any]) -> AppSetting:
        setting = self.session.get(AppSetting, key)
        if setting is None:
            setting = AppSetting(key=key, value_json=_json_safe(value))
            self.session.add(setting)
        else:
            setting.value_json = _json_safe(value)
        self.session.flush()
        return setting

    def get_job(self, job_id: int) -> Job | None:
        statement = (
            select(Job)
            .options(selectinload(Job.video_tasks))
            .where(Job.id == job_id)
        )
        return self.session.scalar(statement)

    def list_jobs(
        self, limit: int = 100, offset: int = 0, search: str = "", status: str = "all"
    ) -> list[Job]:
        statement = select(Job).join(VideoTask).options(selectinload(Job.video_tasks))
        statement = self._filter_jobs(statement, search, status)
        statement = statement.order_by(Job.id.desc()).offset(offset).limit(limit)
        return list(self.session.scalars(statement))

    def count_jobs(self, search: str = "", status: str = "all") -> int:
        statement = self._filter_jobs(select(func.count()).select_from(Job).join(VideoTask), search, status)
        return int(self.session.scalar(statement) or 0)

    @staticmethod
    def _filter_jobs(statement, search: str, status: str):
        if search.strip():
            like = f"%{search.strip()}%"
            statement = statement.where(
                or_(
                    VideoTask.video_path_original.ilike(like),
                    VideoTask.title.ilike(like),
                    func.cast(VideoTask.id, String).ilike(like),
                )
            )
        if status == "failed":
            statement = statement.where(VideoTask.status.in_(["failed", "interrupted"]))
        elif status == "completed":
            statement = statement.where(VideoTask.status.in_(["completed", "skipped_existing_subtitle", "skipped_embedded_subtitle"]))
        elif status == "active":
            statement = statement.where(VideoTask.status.not_in(["failed", "interrupted", "completed", "skipped_existing_subtitle", "skipped_embedded_subtitle"]))
        return statement

    def get_video_task(self, task_id: int) -> VideoTask | None:
        statement = (
            select(VideoTask)
            .options(
                selectinload(VideoTask.job),
                selectinload(VideoTask.candidates),
                selectinload(VideoTask.artifacts),
                selectinload(VideoTask.events),
            )
            .where(VideoTask.id == task_id)
        )
        return self.session.scalar(statement)

    def list_placed_candidates_for_task(
        self,
        task_id: int,
    ) -> list[SubtitleCandidateRecord]:
        statement = (
            select(SubtitleCandidateRecord)
            .join(
                SubtitleArtifact,
                SubtitleArtifact.candidate_id == SubtitleCandidateRecord.id,
            )
            .join(VideoTask, VideoTask.id == SubtitleArtifact.video_task_id)
            .where(
                VideoTask.id == task_id,
                VideoTask.status == "completed",
                SubtitleArtifact.kind == "placed",
                SubtitleArtifact.candidate_id.is_not(None),
            )
            .order_by(SubtitleArtifact.id.asc())
        )
        return list(self.session.scalars(statement).unique())

    def get_retry_parent_task_id(self, task_id: int) -> int | None:
        raw_payload = self.session.scalar(
            select(Job.raw_payload_json)
            .join(VideoTask, VideoTask.job_id == Job.id)
            .where(VideoTask.id == task_id)
        )
        if not isinstance(raw_payload, dict):
            return None
        value = raw_payload.get("retry_of_task_id")
        if isinstance(value, bool):
            return None
        try:
            parent_task_id = int(value)
        except (TypeError, ValueError):
            return None
        return parent_task_id if parent_task_id > 0 else None

    def get_jellyfin_media_item(self, jellyfin_item_id: str | None) -> JellyfinMediaItem | None:
        if not jellyfin_item_id:
            return None
        return self.session.scalar(
            select(JellyfinMediaItem).where(
                JellyfinMediaItem.jellyfin_item_id == jellyfin_item_id
            )
        )

    def get_jellyfin_media_item_by_path(self, path: str | None) -> JellyfinMediaItem | None:
        if not path:
            return None
        return self.session.scalar(
            select(JellyfinMediaItem).where(JellyfinMediaItem.path == path).limit(1)
        )

    def mark_jellyfin_media_item_has_chinese_subtitle(
        self,
        jellyfin_item_id: str | None,
        *,
        path: str | None = None,
    ) -> JellyfinMediaItem | None:
        item = self._resolve_jellyfin_media_item(jellyfin_item_id, path=path)
        if item is None:
            return None
        item.subtitle_status = "has_chinese"
        item.has_external_chinese_subtitle = True
        self.session.flush()
        return item

    def update_jellyfin_media_item_subtitle_state(
        self,
        jellyfin_item_id: str | None,
        *,
        path: str | None = None,
        subtitle_status: str,
        has_external_chinese_subtitle: bool,
        has_embedded_chinese_subtitle: bool,
        has_bilingual_subtitle: bool,
    ) -> JellyfinMediaItem | None:
        item = self._resolve_jellyfin_media_item(jellyfin_item_id, path=path)
        if item is None:
            return None
        item.subtitle_status = subtitle_status
        item.has_external_chinese_subtitle = has_external_chinese_subtitle
        item.has_embedded_chinese_subtitle = has_embedded_chinese_subtitle
        item.has_bilingual_subtitle = has_bilingual_subtitle
        self.session.flush()
        return item

    def list_video_tasks(self, limit: int = 100) -> list[VideoTask]:
        statement = (
            select(VideoTask)
            .options(
                selectinload(VideoTask.job),
                selectinload(VideoTask.candidates),
                selectinload(VideoTask.artifacts),
                selectinload(VideoTask.events),
            )
            .order_by(VideoTask.id.desc())
            .limit(limit)
        )
        return list(self.session.scalars(statement))

    def upsert_jellyfin_media_item(self, data: JellyfinMediaItemData) -> JellyfinMediaItem:
        return self.upsert_jellyfin_media_item_with_status(data).item

    def upsert_jellyfin_media_item_with_status(
        self,
        data: JellyfinMediaItemData,
    ) -> JellyfinUpsertResult:
        statement = select(JellyfinMediaItem).where(
            JellyfinMediaItem.jellyfin_item_id == data.jellyfin_item_id
        )
        item = self.session.scalar(statement)
        status = "unchanged"
        if item is None:
            item = JellyfinMediaItem(jellyfin_item_id=data.jellyfin_item_id)
            self.session.add(item)
            status = "created"
        elif any(
            getattr(item, field) != value
            for field, value in _jellyfin_item_values(data).items()
        ):
            status = "updated"

        for field, value in _jellyfin_item_values(data).items():
            setattr(item, field, value)
        item.last_scanned_at = data.last_scanned_at or datetime.now(timezone.utc)
        self.session.flush()
        return JellyfinUpsertResult(item=item, status=status)

    def delete_jellyfin_media_items_missing_from_library(
        self,
        library_id: str,
        jellyfin_item_ids: set[str],
    ) -> list[str]:
        statement = select(JellyfinMediaItem).where(JellyfinMediaItem.library_id == library_id)
        removed = [
            item
            for item in self.session.scalars(statement)
            if item.jellyfin_item_id not in jellyfin_item_ids
        ]
        removed_ids = [item.jellyfin_item_id for item in removed]
        for item in removed:
            self.session.delete(item)
        self.session.flush()
        return removed_ids

    def list_jellyfin_media_items(
        self,
        library_id: str,
        *,
        limit: int = 500,
    ) -> list[JellyfinMediaItem]:
        statement = (
            select(JellyfinMediaItem)
            .where(JellyfinMediaItem.library_id == library_id)
            .order_by(JellyfinMediaItem.name.asc(), JellyfinMediaItem.id.asc())
            .limit(limit)
        )
        return list(self.session.scalars(statement))

    def list_all_jellyfin_media_items(self, *, limit: int = 10000) -> list[JellyfinMediaItem]:
        statement = (
            select(JellyfinMediaItem)
            .order_by(
                JellyfinMediaItem.jellyfin_date_created.desc(),
                JellyfinMediaItem.id.desc(),
            )
            .limit(limit)
        )
        return list(self.session.scalars(statement))

    def get_jellyfin_media_items_by_ids(
        self,
        item_ids: list[str],
    ) -> list[JellyfinMediaItem]:
        if not item_ids:
            return []
        statement = select(JellyfinMediaItem).where(
            JellyfinMediaItem.jellyfin_item_id.in_(item_ids)
        )
        items_by_id = {
            item.jellyfin_item_id: item
            for item in self.session.scalars(statement)
        }
        return [items_by_id[item_id] for item_id in item_ids if item_id in items_by_id]

    def set_jellyfin_media_item_ignored(
        self,
        jellyfin_item_id: str,
        *,
        ignored: bool,
    ) -> JellyfinMediaItem | None:
        item = self.get_jellyfin_media_item(jellyfin_item_id)
        if item is None:
            return None
        if item.item_type.casefold() not in {"movie", "series"}:
            raise ValueError("only Movie and Series items can be ignored")
        item.ignored = ignored
        self.session.flush()
        return item

    def update_video_task_status(
        self,
        task_id: int,
        status: str,
        error_message: str | None = None,
    ) -> VideoTask:
        task = self.session.get(VideoTask, task_id)
        if task is None:
            raise ValueError(f"video task {task_id} not found")
        previous_status = task.status
        task.status = status
        task.error_message = error_message
        self._refresh_job_status(task.job_id)
        self.session.flush()
        self._record_task_lifecycle_transition(
            task,
            previous_status=previous_status,
            status=status,
            error_message=error_message,
        )
        return task

    def set_video_task_resolved_path(self, task_id: int, resolved_path: str) -> VideoTask:
        task = self.session.get(VideoTask, task_id)
        if task is None:
            raise ValueError(f"video task {task_id} not found")
        task.video_path_resolved = resolved_path
        self.session.flush()
        return task

    def list_video_task_ids_by_status(
        self,
        statuses: set[str] | list[str] | tuple[str, ...],
        limit: int | None = None,
    ) -> list[int]:
        statement = (
            select(VideoTask.id)
            .where(VideoTask.status.in_(list(statuses)))
            .order_by(VideoTask.id.asc())
        )
        if limit is not None:
            statement = statement.limit(limit)
        return list(self.session.scalars(statement))

    def list_all_video_task_ids(self) -> list[int]:
        return list(self.session.scalars(select(VideoTask.id).order_by(VideoTask.id.asc())))

    def mark_active_tasks_interrupted(
        self,
        *,
        reason: str = "interrupted_by_restart",
    ) -> list[int]:
        task_ids = self.list_video_task_ids_by_status(ACTIVE_TASK_STATUSES)
        for task_id in task_ids:
            self.update_video_task_status(task_id, TASK_INTERRUPTED, reason)
        return task_ids

    def delete_video_task(self, task_id: int) -> bool:
        task = self.get_video_task(task_id)
        if task is None:
            return False

        job_id = task.job_id
        self.session.delete(task)
        self.session.flush()

        remaining_task_id = self.session.scalar(
            select(VideoTask.id)
            .where(VideoTask.job_id == job_id)
            .limit(1)
        )
        if remaining_task_id is None:
            job = self.session.get(Job, job_id)
            if job is not None:
                self.session.delete(job)
                self.session.flush()
        else:
            self._refresh_job_status(job_id)
        return True

    def delete_all_video_tasks(self) -> int:
        jobs = list(self.session.scalars(select(Job)))
        count = sum(len(job.video_tasks) for job in jobs)
        for job in jobs:
            self.session.delete(job)
        self.session.flush()
        return count

    def record_candidate(
        self,
        *,
        video_task_id: int,
        provider: str,
        language: str,
        is_bilingual: bool,
        format: str,
        title: str,
        score: float | None,
        release_info: str | None,
        source_url: str | None,
        raw_metadata: dict[str, Any] | None = None,
    ) -> SubtitleCandidateRecord:
        candidate = SubtitleCandidateRecord(
            video_task_id=video_task_id,
            provider=provider,
            language=language,
            is_bilingual=is_bilingual,
            format=format,
            title=title,
            score=score,
            release_info=release_info,
            source_url=source_url,
            raw_metadata_json=_json_safe(raw_metadata),
            download_status="queued",
            attempt_count=0,
            last_attempt_status=None,
            last_error_message=None,
        )
        self.session.add(candidate)
        self.session.flush()
        return candidate

    def update_candidate_attempt(
        self,
        *,
        candidate_id: int,
        status: str,
        error_message: str | None = None,
        attempts: int | None = None,
        increment: bool = False,
    ) -> SubtitleCandidateRecord:
        candidate = self.session.get(SubtitleCandidateRecord, candidate_id)
        if candidate is None:
            raise ValueError(f"subtitle candidate {candidate_id} not found")

        if attempts is not None:
            candidate.attempt_count = attempts
        elif increment:
            candidate.attempt_count += 1

        candidate.last_attempt_status = status
        candidate.last_error_message = error_message
        candidate.download_status = status
        self.session.flush()
        return candidate

    def merge_candidate_metadata(
        self,
        candidate_id: int,
        metadata: dict[str, Any],
    ) -> SubtitleCandidateRecord:
        candidate = self.session.get(SubtitleCandidateRecord, candidate_id)
        if candidate is None:
            raise ValueError(f"subtitle candidate {candidate_id} not found")
        candidate.raw_metadata_json = _json_safe(
            {
                **(candidate.raw_metadata_json or {}),
                **metadata,
            }
        )
        self.session.flush()
        return candidate

    def record_artifact(
        self,
        *,
        video_task_id: int,
        kind: str,
        path: str,
        candidate_id: int | None = None,
        is_synced: bool = False,
    ) -> SubtitleArtifact:
        artifact = SubtitleArtifact(
            video_task_id=video_task_id,
            candidate_id=candidate_id,
            kind=kind,
            path=path,
            is_synced=is_synced,
        )
        self.session.add(artifact)
        self.session.flush()
        return artifact

    def record_task_event(
        self,
        video_task_id: int,
        stage: str,
        status: str,
        message: str | None = None,
        error_code: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> TaskEvent:
        event = TaskEvent(
            video_task_id=video_task_id,
            stage=stage,
            status=status,
            message=message,
            error_code=error_code,
            details_json=_json_safe(details),
        )
        self.session.add(event)
        self.session.flush()
        self.session.refresh(event)
        task = self.session.get(VideoTask, video_task_id)
        details_json = event.details_json if isinstance(event.details_json, dict) else {}
        emit_structured_log(
            event="task_event",
            job_id=task.job_id if task is not None else None,
            task_id=video_task_id,
            stage=stage,
            status=status,
            provider=details_json.get("provider"),
            candidate_id=details_json.get("candidate_id"),
            duration_ms=details_json.get("duration_ms"),
            error_code=error_code,
            message=message or f"{stage} {status}",
            details={"stage": stage, "status": status, **details_json},
        )
        return event

    def list_task_events(
        self,
        video_task_id: int,
        limit: int = 200,
    ) -> list[TaskEvent]:
        latest_event_ids = (
            select(TaskEvent.id)
            .where(TaskEvent.video_task_id == video_task_id)
            .order_by(TaskEvent.id.desc())
            .limit(limit)
            .subquery()
        )
        statement = (
            select(TaskEvent)
            .where(TaskEvent.id.in_(select(latest_event_ids.c.id)))
            .order_by(TaskEvent.id.asc())
        )
        return list(self.session.scalars(statement))

    def has_task_event(self, video_task_id: int, stage: str) -> bool:
        event_id = self.session.scalar(
            select(TaskEvent.id)
            .where(TaskEvent.video_task_id == video_task_id, TaskEvent.stage == stage)
            .limit(1)
        )
        return event_id is not None

    def list_task_events_after_id(self, event_id: int, limit: int = 100) -> list[TaskEvent]:
        statement = (
            select(TaskEvent)
            .where(TaskEvent.id > event_id)
            .order_by(TaskEvent.id.asc())
            .limit(limit)
        )
        return list(self.session.scalars(statement))

    def list_task_event_logs(
        self,
        *,
        after_id: int = 0,
        limit: int = 200,
        level: str | None = None,
        task_id: int | None = None,
        provider: str | None = None,
    ) -> list[tuple[TaskEvent, int | None]]:
        statement = (
            select(TaskEvent, VideoTask.job_id)
            .join(VideoTask, TaskEvent.video_task_id == VideoTask.id)
            .where(TaskEvent.id > after_id)
            .order_by(TaskEvent.id.desc() if after_id == 0 else TaskEvent.id.asc())
            .limit(max(limit * 10, 1000))
        )
        if task_id is not None:
            statement = statement.where(TaskEvent.video_task_id == task_id)

        rows: list[tuple[TaskEvent, int | None]] = []
        for event, job_id in self.session.execute(statement):
            details = event.details_json if isinstance(event.details_json, dict) else {}
            event_level = _task_event_log_level(event)
            if level is not None and event_level != level:
                continue
            if provider is not None and details.get("provider") != provider:
                continue
            rows.append((event, job_id))
            if len(rows) >= limit:
                break
        if after_id == 0:
            rows.reverse()
        return rows

    def record_system_event(
        self,
        *,
        category: str,
        event: str,
        message: str,
        level: str = "INFO",
        task_id: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> SystemEvent:
        system_event = SystemEvent(
            category=category,
            level=level.upper(),
            event=event,
            message=message,
            task_id=task_id,
            details_json=_json_safe(details),
        )
        self.session.add(system_event)
        self.session.flush()
        self.session.refresh(system_event)
        emit_structured_log(
            event="system_event",
            category=category,
            system_event=event,
            level=level.lower(),
            task_id=task_id,
            message=message,
            details=system_event.details_json,
        )
        return system_event

    def list_system_events(
        self,
        *,
        after_id: int = 0,
        limit: int = 200,
        level: str | None = None,
        category: str | None = None,
        task_id: int | None = None,
    ) -> list[SystemEvent]:
        statement = select(SystemEvent).where(SystemEvent.id > after_id)
        if level:
            statement = statement.where(SystemEvent.level == level.upper())
        if category:
            statement = statement.where(SystemEvent.category == category)
        if task_id is not None:
            statement = statement.where(SystemEvent.task_id == task_id)
        statement = statement.order_by(
            SystemEvent.id.desc() if after_id == 0 else SystemEvent.id.asc()
        ).limit(limit)
        events = list(self.session.scalars(statement))
        if after_id == 0:
            events.reverse()
        return events

    def prune_system_events(self, *, retention_days: int, max_entries: int) -> int:
        cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=retention_days)
        deleted = self.session.execute(
            delete(SystemEvent).where(SystemEvent.created_at < cutoff)
        ).rowcount or 0
        total = int(self.session.scalar(select(func.count()).select_from(SystemEvent)) or 0)
        overflow = total - max_entries
        if overflow > 0:
            oldest_ids = (
                select(SystemEvent.id)
                .order_by(SystemEvent.id.asc())
                .limit(overflow)
                .subquery()
            )
            deleted += self.session.execute(
                delete(SystemEvent).where(SystemEvent.id.in_(select(oldest_ids.c.id)))
            ).rowcount or 0
        return deleted

    def prune_task_events(self, *, retention_days: int, max_entries: int) -> int:
        cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=retention_days)
        deleted = self.session.execute(
            delete(TaskEvent).where(TaskEvent.created_at < cutoff)
        ).rowcount or 0
        total = int(self.session.scalar(select(func.count()).select_from(TaskEvent)) or 0)
        overflow = total - max_entries
        if overflow > 0:
            oldest_ids = (
                select(TaskEvent.id)
                .order_by(TaskEvent.id.asc())
                .limit(overflow)
                .subquery()
            )
            deleted += self.session.execute(
                delete(TaskEvent).where(TaskEvent.id.in_(select(oldest_ids.c.id)))
            ).rowcount or 0
        return deleted

    def _refresh_job_status(self, job_id: int) -> None:
        job = self.session.get(Job, job_id)
        if job is None:
            return
        statuses = list(
            self.session.scalars(
                select(VideoTask.status).where(VideoTask.job_id == job_id)
            )
        )
        job.status = summarize_job_status(statuses)

    def _record_task_lifecycle_transition(
        self,
        task: VideoTask,
        *,
        previous_status: str,
        status: str,
        error_message: str | None,
    ) -> None:
        if previous_status == status:
            return
        display_name = task.title or Path(task.video_path_original).name
        details = {
            "previous_status": previous_status,
            "status": status,
            "media": display_name,
        }
        if previous_status not in ACTIVE_TASK_STATUSES and status in ACTIVE_TASK_STATUSES:
            self.record_system_event(
                category="task",
                event="task_started",
                message=f"任务 #{task.id} 开始：{display_name}",
                task_id=task.id,
                details=details,
            )
            return
        if status not in TERMINAL_TASK_STATUSES:
            return
        if status == TASK_COMPLETED:
            event, level, summary = "task_completed", "INFO", "完成"
        elif status == TASK_FAILED:
            event, level, summary = "task_failed", "ERROR", "失败"
        elif status == TASK_INTERRUPTED:
            event, level, summary = "task_interrupted", "WARNING", "中断"
        elif status == TASK_SKIPPED_EXISTING_SUBTITLE:
            event, level, summary = "task_skipped", "INFO", "跳过（已有外挂字幕）"
        elif status == TASK_SKIPPED_EMBEDDED_SUBTITLE:
            event, level, summary = "task_skipped", "INFO", "跳过（已有内嵌字幕）"
        else:
            return
        suffix = f"：{error_message}" if error_message and level != "INFO" else ""
        self.record_system_event(
            category="task",
            event=event,
            message=f"任务 #{task.id} {summary}{suffix}",
            level=level,
            task_id=task.id,
            details={**details, "error": error_message},
        )

    def _resolve_jellyfin_media_item(
        self,
        jellyfin_item_id: str | None,
        *,
        path: str | None = None,
    ) -> JellyfinMediaItem | None:
        return self.get_jellyfin_media_item(jellyfin_item_id) or self.get_jellyfin_media_item_by_path(
            path
        )


def _jellyfin_item_values(data: JellyfinMediaItemData) -> dict[str, Any]:
    return {
        "library_id": data.library_id,
        "library_name": data.library_name,
        "item_type": data.item_type,
        "name": data.name,
        "original_title": data.original_title,
        "series_id": data.series_id,
        "series_name": data.series_name,
        "year": data.year,
        "season": data.season,
        "episode": data.episode,
        "path": data.path,
        "provider_ids_json": _json_safe(data.provider_ids),
        "production_locations_json": _json_safe(data.production_locations),
        "primary_image_tag": data.primary_image_tag,
        "subtitle_status": data.subtitle_status,
        "has_external_chinese_subtitle": data.has_external_chinese_subtitle,
        "has_embedded_chinese_subtitle": data.has_embedded_chinese_subtitle,
        "has_bilingual_subtitle": data.has_bilingual_subtitle,
        "jellyfin_date_created": data.jellyfin_date_created,
    }


def _task_event_log_level(event: TaskEvent) -> str:
    if event.error_code or event.status in {"failed", "error", "interrupted"}:
        return "error"
    if event.status in {"warning", "skipped"}:
        return "warning"
    return "info"


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return str(value)
