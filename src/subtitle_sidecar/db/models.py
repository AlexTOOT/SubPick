from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(100))
    raw_payload_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(50), default="queued")
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

    video_tasks: Mapped[list[VideoTask]] = relationship(
        back_populates="job",
        cascade="all, delete-orphan",
    )


class VideoTask(Base):
    __tablename__ = "video_tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"))
    video_path_original: Mapped[str] = mapped_column(Text)
    video_path_resolved: Mapped[str | None] = mapped_column(Text, nullable=True)
    media_server_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    season: Mapped[int | None] = mapped_column(Integer, nullable=True)
    episode: Mapped[int | None] = mapped_column(Integer, nullable=True)
    result_subtitle_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(50), default="queued")
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

    job: Mapped[Job] = relationship(back_populates="video_tasks")
    candidates: Mapped[list[SubtitleCandidateRecord]] = relationship(
        back_populates="video_task",
        cascade="all, delete-orphan",
    )
    artifacts: Mapped[list[SubtitleArtifact]] = relationship(
        back_populates="video_task",
        cascade="all, delete-orphan",
    )
    events: Mapped[list[TaskEvent]] = relationship(
        back_populates="video_task",
        cascade="all, delete-orphan",
        order_by="TaskEvent.id",
    )


class MediaProbeCache(Base):
    __tablename__ = "media_probe_cache"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    video_path: Mapped[str] = mapped_column(Text, unique=True)
    file_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    mtime: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    probe_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    has_chinese_subtitle: Mapped[bool] = mapped_column(Boolean, default=False)
    has_bilingual_subtitle: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )


class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )


class JellyfinMediaItem(Base):
    __tablename__ = "jellyfin_media_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    jellyfin_item_id: Mapped[str] = mapped_column(String(255), unique=True)
    library_id: Mapped[str] = mapped_column(String(255))
    library_name: Mapped[str] = mapped_column(Text)
    item_type: Mapped[str] = mapped_column(String(50))
    name: Mapped[str] = mapped_column(Text)
    original_title: Mapped[str | None] = mapped_column(Text, nullable=True)
    series_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    series_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    season: Mapped[int | None] = mapped_column(Integer, nullable=True)
    episode: Mapped[int | None] = mapped_column(Integer, nullable=True)
    path: Mapped[str] = mapped_column(Text)
    provider_ids_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    production_locations_json: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    primary_image_tag: Mapped[str | None] = mapped_column(Text, nullable=True)
    subtitle_status: Mapped[str] = mapped_column(String(50), default="unknown")
    has_external_chinese_subtitle: Mapped[bool] = mapped_column(Boolean, default=False)
    has_embedded_chinese_subtitle: Mapped[bool] = mapped_column(Boolean, default=False)
    has_bilingual_subtitle: Mapped[bool] = mapped_column(Boolean, default=False)
    ignored: Mapped[bool] = mapped_column(Boolean, default=False)
    jellyfin_date_created: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    last_scanned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )


class SubtitleCandidateRecord(Base):
    __tablename__ = "subtitle_candidates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    video_task_id: Mapped[int] = mapped_column(ForeignKey("video_tasks.id"))
    provider: Mapped[str] = mapped_column(String(100))
    language: Mapped[str] = mapped_column(String(50))
    is_bilingual: Mapped[bool] = mapped_column(Boolean, default=False)
    format: Mapped[str] = mapped_column(String(20))
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    title: Mapped[str] = mapped_column(Text)
    release_info: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_metadata_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    download_status: Mapped[str] = mapped_column(String(50), default="queued")
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    last_attempt_status: Mapped[str | None] = mapped_column(String(50), nullable=True)
    last_error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    video_task: Mapped[VideoTask] = relationship(back_populates="candidates")


class SubtitleArtifact(Base):
    __tablename__ = "subtitle_artifacts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    video_task_id: Mapped[int] = mapped_column(ForeignKey("video_tasks.id"))
    candidate_id: Mapped[int | None] = mapped_column(ForeignKey("subtitle_candidates.id"), nullable=True)
    kind: Mapped[str] = mapped_column(String(50))
    path: Mapped[str] = mapped_column(Text)
    is_synced: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    video_task: Mapped[VideoTask] = relationship(back_populates="artifacts")


class TaskEvent(Base):
    __tablename__ = "task_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    video_task_id: Mapped[int] = mapped_column(ForeignKey("video_tasks.id"))
    stage: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(50))
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    details_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    video_task: Mapped[VideoTask] = relationship(back_populates="events")


class SystemEvent(Base):
    __tablename__ = "system_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    category: Mapped[str] = mapped_column(String(50))
    level: Mapped[str] = mapped_column(String(20), default="INFO")
    event: Mapped[str] = mapped_column(String(100))
    message: Mapped[str] = mapped_column(Text)
    task_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    details_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
