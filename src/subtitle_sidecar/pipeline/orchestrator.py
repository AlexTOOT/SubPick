from __future__ import annotations

import math
from pathlib import Path
import re
import shutil
import tempfile
from typing import Any

from subtitle_sidecar.jellyfin.client import JellyfinClient
from subtitle_sidecar.jellyfin.subtitle_status import detect_subtitle_status
from subtitle_sidecar.media.identity import analyze_release_years
from subtitle_sidecar.media.subtitles import detect_external_subtitles
from subtitle_sidecar.pipeline.bundle_cache import EpisodeBundleCache, select_episode_member
from subtitle_sidecar.pipeline.candidate_identity import (
    candidate_identity,
    subtitle_content_identity,
)
from subtitle_sidecar.pipeline.naming import build_subtitle_path
from subtitle_sidecar.pipeline.scoring import (
    candidate_score_breakdown,
    candidate_mismatch_reason,
    provider_quality_reference,
    score_candidate,
    sort_candidates,
)
from subtitle_sidecar.pipeline.status import (
    TASK_CHECKING_EMBEDDED,
    TASK_CHECKING_EXISTING,
    TASK_COMPLETED,
    TASK_DOWNLOADING,
    TASK_FAILED,
    TASK_PLACING,
    TASK_QUEUED,
    TASK_RESOLVING,
    TASK_SEARCHING,
    TASK_SKIPPED_EMBEDDED_SUBTITLE,
    TASK_SKIPPED_EXISTING_SUBTITLE,
    TASK_SYNCING,
    TASK_VALIDATING,
)
from subtitle_sidecar.pipeline.validator import validate_subtitle_file
from subtitle_sidecar.probe.streams import probe_video_streams
from subtitle_sidecar.providers.base import DownloadedSubtitle, SubtitleCandidate, SubtitleSearchRequest
from subtitle_sidecar.sync.ffsubsync import sync_subtitle


def safe_place_subtitle(
    source: Path,
    destination: Path,
    overwrite: bool = False,
    keep_backup: bool = True,
) -> Path:
    source_path = Path(source)
    destination_path = Path(destination)
    destination_path.parent.mkdir(parents=True, exist_ok=True)

    backup_path = destination_path.with_suffix(f"{destination_path.suffix}.bak")
    if destination_path.exists():
        if not overwrite:
            raise FileExistsError(f"destination already exists: {destination_path}")
        if keep_backup:
            if backup_path.exists():
                backup_path.unlink()
            destination_path.replace(backup_path)
        else:
            destination_path.unlink()

    temp_target = _temporary_target(destination_path)
    try:
        shutil.copy2(source_path, temp_target)
        temp_target.replace(destination_path)
    finally:
        if temp_target.exists():
            temp_target.unlink()

    source_path.unlink()
    return destination_path


def _temporary_target(destination: Path) -> Path:
    temp_name = next(tempfile._get_candidate_names())
    return destination.with_name(f"{destination.name}.{temp_name}.tmp")


class SubtitleOrchestrator:
    def __init__(
        self,
        settings: Any,
        repository: Any,
        resolver: Any,
        provider_registry: Any,
        embedded_subtitle_detector: Any | None = None,
        subtitle_syncer: Any | None = None,
        bundle_cache: EpisodeBundleCache | None = None,
        jellyfin_client_factory: Any | None = None,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.resolver = resolver
        self.provider_registry = provider_registry
        self.embedded_subtitle_detector = embedded_subtitle_detector or probe_video_streams
        self.subtitle_syncer = subtitle_syncer or sync_subtitle
        self.bundle_cache = bundle_cache or EpisodeBundleCache(Path(settings.cache_dir))
        self.jellyfin_client_factory = jellyfin_client_factory
        self._retry_content_identities: dict[int, dict[str, int]] = {}

    def preflight_video_task(self, task_id: int) -> bool:
        """Run local-only checks before a task occupies the provider search queue."""
        task = self.repository.get_video_task(task_id)
        if task is None:
            raise ValueError(f"video task {task_id} not found")

        self._record_task_source(task)
        self._set_task_stage(task_id, TASK_RESOLVING)
        resolved = self.resolver.resolve(task.video_path_original)
        resolved_path = getattr(resolved, "resolved_path", None)
        if resolved_path is None:
            self._fail_task(task_id, "video_not_found", stage="resolving")
            return False
        self._set_resolved_path(task, resolved_path)
        resolve_details = self._record_resolved_path(task_id, task, resolved)
        self._enrich_task_identity_from_path(task, Path(resolved_path))

        supplemental_search = getattr(getattr(task, "job", None), "source", "") in {
            "manual-retry",
            "jellyfin-manual",
        }
        self._set_task_stage(task_id, TASK_CHECKING_EXISTING)
        existing, external_details = self._inspect_external_subtitles(task_id, resolved_path)
        if existing.has_chinese and not supplemental_search:
            self.repository.update_video_task_status(task_id, TASK_SKIPPED_EXISTING_SUBTITLE)
            self._record_task_event(
                task_id,
                "preflight",
                "completed",
                message=(
                    "本地检查完成：路径可用；"
                    f"外挂字幕 {external_details['subtitle_count']} 个，"
                    f"其中中文 {external_details['chinese_count']} 个；"
                    "已满足需求，未执行内封检查，跳过 Provider 搜索"
                ),
                details={
                    "path": resolve_details,
                    "external_subtitles": external_details,
                    "embedded_subtitles": {"status": "not_run"},
                },
            )
            return False

        self._set_task_stage(task_id, TASK_CHECKING_EMBEDDED)
        embedded = self._probe_embedded_subtitles(task_id, resolved_path)
        embedded_details = self._embedded_subtitle_details(embedded)
        if embedded is not None:
            if bool(getattr(embedded, "has_chinese", False)) and not supplemental_search:
                self.repository.update_video_task_status(task_id, TASK_SKIPPED_EMBEDDED_SUBTITLE)
                self._record_task_event(
                    task_id,
                    "preflight",
                    "completed",
                    message=(
                        "本地检查完成：路径可用；"
                        f"外挂字幕 {external_details['subtitle_count']} 个，"
                        f"中文 {external_details['chinese_count']} 个；"
                        f"内封字幕流 {embedded_details['subtitle_stream_count']} 条，"
                        f"中文 {embedded_details['chinese_count']} 条；"
                        "已满足需求，跳过 Provider 搜索"
                    ),
                    details={
                        "path": resolve_details,
                        "external_subtitles": external_details,
                        "embedded_subtitles": embedded_details,
                    },
                )
                return False

        self.repository.update_video_task_status(task_id, TASK_QUEUED)
        embedded_summary = (
            "内封检查失败但任务可继续"
            if embedded is None
            else (
                f"内封字幕流 {embedded_details['subtitle_stream_count']} 条，"
                f"中文 {embedded_details['chinese_count']} 条"
            )
        )
        self._record_task_event(
            task_id,
            "preflight",
            "completed",
            message=(
                "本地检查完成：路径可用；"
                f"外挂字幕 {external_details['subtitle_count']} 个，"
                f"中文 {external_details['chinese_count']} 个；"
                f"{embedded_summary}；等待 Provider 搜索槽位"
            ),
            details={
                "path": resolve_details,
                "external_subtitles": external_details,
                "embedded_subtitles": embedded_details,
            },
        )
        return True

    def process_video_task(self, task_id: int) -> None:
        task = self.repository.get_video_task(task_id)
        if task is None:
            raise ValueError(f"video task {task_id} not found")

        self._record_task_source(task)
        supplemental_search = getattr(getattr(task, "job", None), "source", "") in {
            "manual-retry",
            "jellyfin-manual",
        }
        preflight_completed = bool(getattr(task, "video_path_resolved", None))
        if preflight_completed:
            resolved_path = Path(task.video_path_resolved)
            existing_matches = (
                tuple(detect_external_subtitles(resolved_path).matches)
                if supplemental_search
                else ()
            )
        else:
            self._set_task_stage(task_id, TASK_RESOLVING)
            resolved = self.resolver.resolve(task.video_path_original)
            resolved_path = getattr(resolved, "resolved_path", None)
            if resolved_path is None:
                self._fail_task(
                    task_id,
                    "video_not_found",
                    stage="resolving",
                    message=f"路径解析失败：{task.video_path_original}，未找到可用文件",
                    details={
                        "original_path": task.video_path_original,
                        "resolved_path": None,
                        "strategy": getattr(resolved, "strategy", "not_found"),
                    },
                )
                return
            self._set_resolved_path(task, resolved_path)
            self._record_resolved_path(task_id, task, resolved)

            self._set_task_stage(task_id, TASK_CHECKING_EXISTING)
            existing, _external_details = self._inspect_external_subtitles(
                task_id, resolved_path
            )
            existing_matches = tuple(existing.matches)
            if existing.has_chinese and not supplemental_search:
                self.repository.update_video_task_status(
                    task_id,
                    TASK_SKIPPED_EXISTING_SUBTITLE,
                )
                return

            self._set_task_stage(task_id, TASK_CHECKING_EMBEDDED)
            embedded = self._probe_embedded_subtitles(task_id, resolved_path)
            if (
                embedded is not None
                and bool(getattr(embedded, "has_chinese", False))
                and not supplemental_search
            ):
                self.repository.update_video_task_status(
                    task_id,
                    TASK_SKIPPED_EMBEDDED_SUBTITLE,
                )
                return

        self._enrich_task_identity_from_path(task, Path(resolved_path))
        request = self._build_search_request(task, resolved_path)
        attempt_budget = self.settings.subtitles.max_candidate_attempts
        attempts_used = 0
        last_failure_reason: str | None = None

        cached = self.bundle_cache.find(request)
        if cached is not None:
            self._set_task_stage(task_id, TASK_SEARCHING)
            self._record_task_event(
                task_id,
                "bundle_reuse",
                "completed",
                message=(
                    f"字幕包复用：命中同季第 {request.episode} 集缓存，"
                    f"来源 {cached.candidate.provider}"
                ),
                details={
                    "provider": cached.candidate.provider,
                    "source_task_id": cached.source_task_id,
                    "episode": request.episode,
                },
            )
            succeeded, used, failure = self._process_candidate_batch(
                task=task,
                request=request,
                resolved_path=resolved_path,
                candidates=[cached.candidate],
                supplemental_search=supplemental_search,
                existing_matches=existing_matches,
                attempts_used=attempts_used,
                attempt_budget=attempt_budget,
            )
            attempts_used += used
            last_failure_reason = failure or last_failure_reason
            if succeeded:
                self._finish_task(
                    task_id,
                    TASK_COMPLETED,
                    "task",
                    "subtitle task completed",
                )
                return

        provider_candidate_count = 0
        if attempts_used < attempt_budget:
            self._set_task_stage(task_id, TASK_SEARCHING)
            search_details = self._search_details(request)
            self._record_task_event(
                task_id,
                "searching",
                "started",
                message=self._search_started_message(search_details),
                details=search_details,
            )
            set_reporter = getattr(self.provider_registry, "set_reporter", None)
            if callable(set_reporter):
                set_reporter(
                    lambda report: self._record_provider_search_report(task_id, report)
                )
            search_batches = getattr(self.provider_registry, "search_batches", None)
            if callable(search_batches):
                batches = iter(
                    search_batches(
                        request,
                        on_wait=lambda provider, wait_seconds, _ready_at: (
                            self._record_provider_wait(task_id, provider, wait_seconds)
                        ),
                    )
                )
            else:
                self._release_database_lock()
                batches = iter([self.provider_registry.search(request)])

            while attempts_used < attempt_budget:
                self._release_database_lock()
                try:
                    batch = next(batches)
                except StopIteration:
                    break
                provider_candidate_count += len(batch)
                succeeded, used, failure = self._process_candidate_batch(
                    task=task,
                    request=request,
                    resolved_path=resolved_path,
                    candidates=batch,
                    supplemental_search=supplemental_search,
                    existing_matches=existing_matches,
                    attempts_used=attempts_used,
                    attempt_budget=attempt_budget,
                )
                attempts_used += used
                last_failure_reason = failure or last_failure_reason
                if succeeded:
                    self._record_search_summary(task_id, provider_candidate_count)
                    self._finish_task(
                        task_id,
                        TASK_COMPLETED,
                        "task",
                        "subtitle task completed",
                    )
                    return

            self._record_search_summary(task_id, provider_candidate_count)

        self._fail_task(
            task_id,
            last_failure_reason or self._search_failure_reason(),
            stage="task",
        )

    def _process_candidate_batch(
        self,
        *,
        task: Any,
        request: SubtitleSearchRequest,
        resolved_path: Path,
        candidates: list[SubtitleCandidate],
        supplemental_search: bool,
        existing_matches: tuple[Any, ...],
        attempts_used: int,
        attempt_budget: int,
    ) -> tuple[bool, int, str | None]:
        task_id = task.id
        self._record_candidate_results(task_id, candidates)
        compatible_candidates = []
        mismatch_counts: dict[str, int] = {}
        reason_labels = {
            "title_mismatch": "标题不匹配",
            "season_mismatch": "季不匹配",
            "episode_mismatch": "集不匹配",
            "episode_feature_mismatch": "疑似电影字幕",
            "movie_episode_mismatch": "疑似剧集字幕",
            "year_mismatch": "年份不匹配",
        }
        for candidate in candidates:
            mismatch_reason = candidate_mismatch_reason(
                candidate,
                season=task.season,
                episode=task.episode,
                year=request.year,
                title=request.title,
                original_title=request.original_title,
            )
            if mismatch_reason is None:
                compatible_candidates.append(candidate)
                continue
            mismatch_counts[mismatch_reason] = mismatch_counts.get(mismatch_reason, 0) + 1
            details = {
                "reason": mismatch_reason,
                "reason_label": reason_labels.get(mismatch_reason, mismatch_reason),
                "provider": candidate.provider,
                "title": candidate.title,
                "release_info": candidate.release_info,
                "source_url": candidate.source_url,
                "expected_title": request.title,
                "expected_original_title": request.original_title,
                "expected_year": request.year,
            }
            if mismatch_reason == "year_mismatch":
                evidence = analyze_release_years(
                    (candidate.title, candidate.release_info),
                    expected_year=request.year,
                    expected_titles=(request.title, request.original_title),
                )
                details["candidate_years"] = sorted(evidence.years)
            self._record_task_event(
                task_id,
                "candidate_filter",
                "skipped",
                message=(
                    f"候选预检：拒绝 {candidate.provider}「{candidate.title or '未命名字幕'}」；"
                    f"{details['reason_label']}；发布信息 {candidate.release_info or '无'}；"
                    f"链接 {candidate.source_url or '无'}"
                ),
                error_code=mismatch_reason,
                details=details,
            )
        if mismatch_counts:
            summary = "，".join(
                f"{reason_labels.get(reason, reason)} {count} 条"
                for reason, count in sorted(mismatch_counts.items())
            )
            self._record_task_event(
                task_id,
                "candidate_filter",
                "completed",
                message=f"候选预检：跳过明确不匹配的候选（{summary}）",
                details={"mismatch_counts": mismatch_counts},
            )
        compatible_candidates = self._exclude_previous_retry_candidates(
            task,
            compatible_candidates,
        )
        all_ranked_candidates = sort_candidates(
            compatible_candidates,
            video_path=resolved_path,
            season=task.season,
            episode=task.episode,
        )
        remaining_attempts = max(0, attempt_budget - attempts_used)
        self._record_candidate_ranking(
            task_id,
            all_ranked_candidates,
            compatible_candidates=compatible_candidates,
            resolved_path=resolved_path,
            season=task.season,
            episode=task.episode,
            attempt_limit=remaining_attempts,
        )
        ranked_candidates = all_ranked_candidates[:remaining_attempts]
        last_failure_reason: str | None = None
        batch_attempts = 0

        for attempt_number, selected_candidate in enumerate(
            ranked_candidates,
            start=attempts_used + 1,
        ):
            batch_attempts += 1
            event_details = self._event_details(
                selected_candidate,
                attempt_number=attempt_number,
                max_attempts=attempt_budget,
            )
            self._record_task_event(
                task_id,
                "candidate_attempt",
                "started",
                message=self._candidate_message("开始尝试", event_details),
                details=event_details,
            )
            recorded_candidate = self.repository.record_candidate(
                video_task_id=task_id,
                provider=selected_candidate.provider,
                language=selected_candidate.language,
                is_bilingual=selected_candidate.is_bilingual,
                format=selected_candidate.format,
                title=selected_candidate.title,
                score=score_candidate(
                    selected_candidate,
                    video_path=resolved_path,
                    season=task.season,
                    episode=task.episode,
                    provider_quality_reference=provider_quality_reference(
                        compatible_candidates,
                        selected_candidate,
                    ),
                ),
                release_info=selected_candidate.release_info,
                source_url=selected_candidate.source_url,
                raw_metadata=selected_candidate.raw_metadata,
            )
            self._update_candidate_attempt(
                recorded_candidate.id,
                status="running",
                increment=True,
            )

            try:
                self._set_task_stage(task_id, TASK_DOWNLOADING)
                is_bundle_reuse = bool(selected_candidate.raw_metadata.get("bundle_reused"))
                download_stage = "bundle_materialization" if is_bundle_reuse else "candidate_download"
                download_message = "开始取用缓存" if is_bundle_reuse else "开始下载"
                self._record_task_event(
                    task_id,
                    download_stage,
                    "started",
                    message=self._candidate_message(download_message, event_details),
                    details=event_details,
                )
                self._release_database_lock()
                downloaded = self._download_candidate(selected_candidate, task_id)
                self._record_task_event(
                    task_id,
                    download_stage,
                    "completed",
                    message=self._candidate_message("缓存已准备" if is_bundle_reuse else "下载完成", event_details),
                    details=event_details,
                )
            except Exception as exc:
                last_failure_reason = self._exception_reason(exc, "download_failed")
                self._record_task_event(
                    task_id,
                    download_stage,
                    "failed",
                    message=last_failure_reason,
                    error_code=last_failure_reason,
                    details=event_details,
                )
                self._record_task_event(
                    task_id,
                    "candidate_attempt",
                    "failed",
                    message=last_failure_reason,
                    error_code=last_failure_reason,
                    details=event_details,
                )
                self._update_candidate_attempt(
                    recorded_candidate.id,
                    status="failed",
                    error_message=last_failure_reason,
                )
                continue

            self._set_task_stage(task_id, TASK_VALIDATING)
            self._record_task_event(
                task_id,
                "candidate_validation",
                "started",
                message=self._candidate_message("开始校验", event_details),
                details=event_details,
            )
            try:
                downloaded = select_episode_member(
                    downloaded,
                    season=task.season,
                    episode=task.episode,
                )
            except ValueError as exc:
                last_failure_reason = str(exc)
                self._record_task_event(
                    task_id,
                    "candidate_validation",
                    "failed",
                    message=last_failure_reason,
                    error_code=last_failure_reason,
                    details=event_details,
                )
                self._record_task_event(
                    task_id,
                    "candidate_attempt",
                    "failed",
                    message=last_failure_reason,
                    error_code=last_failure_reason,
                    details=event_details,
                )
                self._update_candidate_attempt(
                    recorded_candidate.id,
                    status="failed",
                    error_message=last_failure_reason,
                )
                continue
            content_identity = subtitle_content_identity(downloaded.path)
            merge_candidate_metadata = getattr(
                self.repository,
                "merge_candidate_metadata",
                None,
            )
            if callable(merge_candidate_metadata):
                merge_candidate_metadata(recorded_candidate.id, content_identity)
            duplicate_task_id = self._retry_content_duplicate_task_id(
                task,
                content_identity,
            )
            if duplicate_task_id is not None:
                last_failure_reason = "retry_candidate_content_duplicate"
                duplicate_details = {
                    **event_details,
                    **content_identity,
                    "excluded_from_task_id": duplicate_task_id,
                }
                self._record_task_event(
                    task_id,
                    "candidate_filter",
                    "skipped",
                    message=(
                        "重试候选排除：下载内容与此前已落库字幕相同，"
                        f"来源任务 #{duplicate_task_id}，继续尝试下一候选"
                    ),
                    error_code=last_failure_reason,
                    details=duplicate_details,
                )
                self._update_candidate_attempt(
                    recorded_candidate.id,
                    status="skipped",
                    error_message=last_failure_reason,
                )
                continue
            validation = validate_subtitle_file(downloaded.path)
            if not validation.is_valid:
                last_failure_reason = validation.reason or "invalid_subtitle"
                self._record_task_event(
                    task_id,
                    "candidate_validation",
                    "failed",
                    message=last_failure_reason,
                    error_code=last_failure_reason,
                    details=event_details,
                )
                self._record_task_event(
                    task_id,
                    "candidate_attempt",
                    "failed",
                    message=last_failure_reason,
                    error_code=last_failure_reason,
                    details=event_details,
                )
                self._update_candidate_attempt(
                    recorded_candidate.id,
                    status="failed",
                    error_message=last_failure_reason,
                )
                continue
            self._record_task_event(
                task_id,
                "candidate_validation",
                "completed",
                message=self._validation_message(event_details, validation),
                details={
                    **event_details,
                    "encoding": validation.encoding,
                    "cue_count": validation.cue_count,
                    "duration_seconds": validation.duration_seconds,
                },
            )

            subtitle_to_place = downloaded.path
            is_synced = False
            if self.settings.sync.enabled:
                self._set_task_stage(task_id, TASK_SYNCING)
                self._record_task_event(
                    task_id,
                    "candidate_sync",
                    "started",
                    message=self._candidate_message("开始对轴", event_details),
                    details=event_details,
                )
                self._release_database_lock()
                sync_result = self.subtitle_syncer(
                    resolved_path,
                    downloaded.path,
                    _synced_output_path(downloaded.path),
                )
                if sync_result.success:
                    synced_validation = validate_subtitle_file(sync_result.output_path)
                    if not synced_validation.is_valid:
                        last_failure_reason = synced_validation.reason or "invalid_synced_subtitle"
                        self._record_task_event(
                            task_id,
                            "candidate_sync",
                            "failed",
                            message=last_failure_reason,
                            error_code=last_failure_reason,
                            details=event_details,
                        )
                        self._record_task_event(
                            task_id,
                            "candidate_attempt",
                            "failed",
                            message=last_failure_reason,
                            error_code=last_failure_reason,
                            details=event_details,
                        )
                        self._update_candidate_attempt(
                            recorded_candidate.id,
                            status="failed",
                            error_message=last_failure_reason,
                        )
                        continue
                    subtitle_to_place = sync_result.output_path
                    is_synced = True
                    sync_score = getattr(sync_result, "score", None)
                    sync_details = {**event_details, "sync_score": sync_score}
                    sync_summary = (
                        f"对轴完成，语音活动匹配分 {sync_score:.1f}（不代表影片身份匹配）"
                        if isinstance(sync_score, (int, float))
                        else "对轴完成（不代表影片身份匹配）"
                    )
                    self._record_task_event(
                        task_id,
                        "candidate_sync",
                        "completed",
                        message=self._candidate_message(sync_summary, event_details),
                        details=sync_details,
                    )
                else:
                    last_failure_reason = getattr(sync_result, "reason", None) or "sync_failed"
                    sync_details = {
                        **event_details,
                        "sync_reason": last_failure_reason,
                        "sync_score": getattr(sync_result, "score", None),
                    }
                    sync_message = (
                        f"对轴拒绝：音频匹配质量过低（score {sync_details['sync_score']:.1f}）"
                        if last_failure_reason == "low_quality_alignment"
                        and isinstance(sync_details["sync_score"], (int, float))
                        else last_failure_reason
                    )
                    self._record_task_event(
                        task_id,
                        "candidate_sync",
                        "failed",
                        message=sync_message,
                        error_code=last_failure_reason,
                        details=sync_details,
                    )
                    if (
                        last_failure_reason == "low_quality_alignment"
                        or not self.settings.subtitles.save_unsynced_on_sync_failure
                    ):
                        self._record_task_event(
                            task_id,
                            "candidate_attempt",
                            "failed",
                            message=sync_message,
                            error_code=last_failure_reason,
                            details=sync_details,
                        )
                        self._update_candidate_attempt(
                            recorded_candidate.id,
                            status="failed",
                            error_message=last_failure_reason,
                        )
                        continue

            try:
                if not selected_candidate.raw_metadata.get("bundle_reused"):
                    cached_count = self.bundle_cache.store(
                        request,
                        downloaded,
                        source_task_id=task_id,
                    )
                    if cached_count:
                        self._record_task_event(
                            task_id,
                            "bundle_cache",
                            "completed",
                            message=f"字幕包缓存：缓存同季 {cached_count} 集的优选字幕文件",
                            details={**event_details, "cached_episode_count": cached_count},
                        )
                if supplemental_search and any(
                    _same_subtitle_content(subtitle_to_place, match.path) for match in existing_matches
                ):
                    last_failure_reason = "duplicate_existing_subtitle"
                    self._record_task_event(task_id, "candidate_placement", "skipped", message=last_failure_reason, details=event_details)
                    self._update_candidate_attempt(recorded_candidate.id, status="skipped", error_message=last_failure_reason)
                    continue
                self._set_task_stage(task_id, TASK_PLACING)
                self._record_task_event(
                    task_id,
                    "candidate_placement",
                    "started",
                    message=self._candidate_message("开始写入媒体库", event_details),
                    details=event_details,
                )
                placed_path = safe_place_subtitle(
                    subtitle_to_place,
                    _supplemental_subtitle_path(
                        resolved_path,
                        language=selected_candidate.language,
                        extension=selected_candidate.format,
                        supplemental=supplemental_search,
                    ),
                    overwrite=self.settings.subtitles.overwrite,
                    keep_backup=self.settings.sync.keep_backup,
                )
            except Exception as exc:
                last_failure_reason = self._exception_reason(exc, "placement_failed")
                self._record_task_event(
                    task_id,
                    "candidate_placement",
                    "failed",
                    message=last_failure_reason,
                    error_code=last_failure_reason,
                    details=event_details,
                )
                self._record_task_event(
                    task_id,
                    "candidate_attempt",
                    "failed",
                    message=last_failure_reason,
                    error_code=last_failure_reason,
                    details=event_details,
                )
                self._update_candidate_attempt(
                    recorded_candidate.id,
                    status="failed",
                    error_message=last_failure_reason,
                )
                continue

            self.repository.record_artifact(
                video_task_id=task_id,
                candidate_id=recorded_candidate.id,
                kind="placed",
                path=str(placed_path),
                is_synced=is_synced,
            )
            self._refresh_jellyfin_subtitle_state(task)
            self._record_task_event(
                task_id,
                "candidate_placement",
                "completed",
                message=self._candidate_message("已写入媒体库", event_details),
                details=event_details,
            )
            self._record_task_event(
                task_id,
                "candidate_attempt",
                "completed",
                message=self._candidate_message("尝试成功", event_details),
                details=event_details,
            )
            self._update_candidate_attempt(
                recorded_candidate.id,
                status=TASK_COMPLETED,
            )
            task.result_subtitle_path = str(placed_path)
            return True, batch_attempts, None

        return False, batch_attempts, last_failure_reason

    def _record_candidate_results(
        self,
        task_id: int,
        candidates: list[SubtitleCandidate],
    ) -> None:
        if not candidates:
            return
        provider = candidates[0].provider.split(":", 1)[0]
        self._record_task_event(
            task_id,
            "candidate_results",
            "completed",
            message=f"字幕源 {provider} 返回 {len(candidates)} 条候选",
            details={
                "provider": provider,
                "candidate_count": len(candidates),
                "candidates": [
                    self._candidate_snapshot(candidate, position=index)
                    for index, candidate in enumerate(candidates, start=1)
                ],
            },
        )

    def _record_candidate_ranking(
        self,
        task_id: int,
        candidates: list[SubtitleCandidate],
        *,
        compatible_candidates: list[SubtitleCandidate],
        resolved_path: Path,
        season: int | None,
        episode: int | None,
        attempt_limit: int,
    ) -> None:
        if not candidates:
            return
        provider = candidates[0].provider.split(":", 1)[0]
        snapshots: list[dict[str, Any]] = []
        for rank, candidate in enumerate(candidates, start=1):
            breakdown = candidate_score_breakdown(
                candidate,
                video_path=resolved_path,
                season=season,
                episode=episode,
                provider_quality_reference=provider_quality_reference(
                    compatible_candidates,
                    candidate,
                ),
            )
            snapshots.append(
                self._candidate_snapshot(
                    candidate,
                    rank=rank,
                    score_breakdown={
                        key: round(value, 4) if isinstance(value, float) else value
                        for key, value in breakdown.items()
                    },
                )
            )
        self._record_task_event(
            task_id,
            "candidate_ranking",
            "completed",
            message=(
                f"SubPick 排序完成：{len(candidates)} 条可用候选，"
                f"本轮最多尝试 {min(len(candidates), attempt_limit)} 条"
            ),
            details={
                "provider": provider,
                "candidate_count": len(candidates),
                "attempt_limit": attempt_limit,
                "candidates": snapshots,
            },
        )

    @staticmethod
    def _candidate_snapshot(
        candidate: SubtitleCandidate,
        *,
        position: int | None = None,
        rank: int | None = None,
        score_breakdown: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        snapshot: dict[str, Any] = {
            "provider": candidate.provider,
            "title": candidate.title,
            "source_url": candidate.source_url,
            "language": candidate.language,
            "is_bilingual": candidate.is_bilingual,
            "format": candidate.format,
            "release_info": candidate.release_info,
            "confidence": candidate.confidence,
            "provider_quality": candidate.provider_quality,
        }
        if position is not None:
            snapshot["position"] = position
        if rank is not None:
            snapshot["rank"] = rank
        if score_breakdown is not None:
            snapshot["score"] = score_breakdown["total_score"]
            snapshot["score_breakdown"] = score_breakdown
        for key in (
            "assrt_downloads",
            "assrt_views",
            "assrt_vote_score",
            "zimuku_downloads",
            "zimuku_quality",
        ):
            value = candidate.raw_metadata.get(key)
            if value is not None:
                snapshot[key] = value
        return snapshot

    def _exclude_previous_retry_candidates(
        self,
        task: Any,
        candidates: list[SubtitleCandidate],
    ) -> list[SubtitleCandidate]:
        raw_payload = getattr(getattr(task, "job", None), "raw_payload_json", None) or {}
        retry_of_task_id = _positive_int(raw_payload.get("retry_of_task_id"))
        if retry_of_task_id is None:
            return candidates
        list_candidates = getattr(self.repository, "list_placed_candidates_for_task", None)
        if not callable(list_candidates):
            return candidates

        previous_identities: dict[str, int] = {}
        previous_content_identities: dict[str, int] = {}
        ancestor_task_id: int | None = retry_of_task_id
        visited_task_ids = {task.id}
        get_retry_parent = getattr(self.repository, "get_retry_parent_task_id", None)
        for _ in range(50):
            if ancestor_task_id is None or ancestor_task_id in visited_task_ids:
                break
            visited_task_ids.add(ancestor_task_id)
            for previous in list_candidates(ancestor_task_id):
                metadata = getattr(previous, "raw_metadata_json", None) or {}
                identity = candidate_identity(
                    provider=getattr(previous, "provider", ""),
                    raw_metadata=metadata,
                    source_url=getattr(previous, "source_url", None),
                )
                if identity:
                    previous_identities.setdefault(identity, ancestor_task_id)
                for key in ("content_sha256", "text_fingerprint"):
                    value = metadata.get(key)
                    if isinstance(value, str) and value:
                        previous_content_identities.setdefault(
                            f"{key}:{value}",
                            ancestor_task_id,
                        )
            if not callable(get_retry_parent):
                break
            ancestor_task_id = _positive_int(get_retry_parent(ancestor_task_id))
        self._retry_content_identities[task.id] = previous_content_identities
        if not previous_identities:
            return candidates

        remaining: list[SubtitleCandidate] = []
        for candidate in candidates:
            identity = candidate_identity(
                provider=candidate.provider,
                raw_metadata=candidate.raw_metadata,
                source_url=candidate.source_url,
            )
            excluded_from_task_id = previous_identities.get(identity or "")
            if excluded_from_task_id is None:
                remaining.append(candidate)
                continue
            details = {
                "provider": candidate.provider,
                "title": candidate.title,
                "link": candidate.source_url,
                "source_url": candidate.source_url,
                "identity": identity,
                "retry_of_task_id": retry_of_task_id,
                "excluded_from_task_id": excluded_from_task_id,
            }
            self._record_task_event(
                task.id,
                "candidate_filter",
                "skipped",
                message=(
                    f"重试候选排除：跳过上次已落库字幕，来源 {candidate.provider}，"
                    f"{candidate.title or '未命名字幕'}，来源任务 #{excluded_from_task_id}，"
                    f"链接 {candidate.source_url or '无'}"
                ),
                error_code="retry_candidate_already_used",
                details=details,
            )
        return remaining

    def _retry_content_duplicate_task_id(
        self,
        task: Any,
        content_identity: dict[str, str],
    ) -> int | None:
        known = self._retry_content_identities.get(task.id, {})
        for key in ("content_sha256", "text_fingerprint"):
            value = content_identity.get(key)
            ancestor_task_id = known.get(f"{key}:{value}") if value else None
            if ancestor_task_id is not None:
                return ancestor_task_id
        return None

    def has_cached_bundle(self, task_id: int) -> bool:
        """Return whether a queued episode can complete its provider step from local cache."""
        task = self.repository.get_video_task(task_id)
        if task is None:
            return False
        resolved = self.resolver.resolve(task.video_path_original)
        resolved_path = getattr(resolved, "resolved_path", None)
        if resolved_path is None:
            return False
        request = self._build_search_request(task, Path(resolved_path))
        return self.bundle_cache.find(request) is not None

    def _build_search_request(self, task: Any, resolved_path: Path) -> SubtitleSearchRequest:
        media_item = self._jellyfin_media_item_for_task(task)
        is_episode = task.season is not None or task.episode is not None
        series_item = self._jellyfin_series_for_media_item(media_item) if is_episode else None
        identity_item = series_item or media_item
        raw_payload = getattr(getattr(task, "job", None), "raw_payload_json", None) or {}
        job_metadata = raw_payload.get("jellyfin_metadata") or {}
        provider_ids = (
            getattr(identity_item, "provider_ids_json", None)
            or job_metadata.get("provider_ids")
            or {}
        )
        original_title = (
            getattr(identity_item, "original_title", None)
            or job_metadata.get("original_title")
            or _english_title_from_path(resolved_path)
        )
        year = (
            task.year
            or getattr(identity_item, "year", None)
            or getattr(media_item, "year", None)
            or job_metadata.get("year")
        )
        title = task.title or resolved_path.stem
        if is_episode:
            title = (
                getattr(series_item, "name", None)
                or getattr(media_item, "series_name", None)
                or _normalized_search_title(title, is_episode=True)
                or title
            )
        return SubtitleSearchRequest(
            video_path=resolved_path,
            title=title,
            year=year,
            media_type="episode" if is_episode else "movie",
            season=task.season,
            episode=task.episode,
            preferred=self.settings.subtitles.preferred,
            fallback_languages=list(self.settings.subtitles.fallback),
            imdb_id=_provider_id(provider_ids, "imdb"),
            tmdb_id=_provider_id(provider_ids, "tmdb"),
            original_title=_normalized_search_title(
                original_title,
                is_episode=is_episode,
            ),
            series_id=(
                getattr(media_item, "series_id", None)
                or getattr(series_item, "jellyfin_item_id", None)
            ),
        )

    def _enrich_task_identity_from_path(self, task: Any, resolved_path: Path) -> None:
        episode_identity = _episode_identity_from_path(resolved_path)
        if episode_identity is None:
            return
        season, episode, series_title, year = episode_identity
        changed: dict[str, Any] = {}
        if task.season is None:
            task.season = season
            changed["season"] = season
        if task.episode is None:
            task.episode = episode
            changed["episode"] = episode
        if not str(task.title or "").strip():
            task.title = series_title
            changed["title"] = series_title
        if task.year is None and year is not None:
            task.year = year
            changed["year"] = year
        if not changed:
            return
        self._record_task_event(
            task.id,
            "metadata",
            "completed",
            message=(
                f"路径元数据：{task.title or '标题未知'}，"
                f"S{task.season:02d}E{task.episode:02d}，年份 {task.year or '未知'}"
            ),
            details={"source": "path", **changed},
        )

    def _jellyfin_series_for_media_item(self, media_item: Any | None) -> Any | None:
        series_id = getattr(media_item, "series_id", None)
        lookup = getattr(self.repository, "get_jellyfin_media_item", None)
        if not series_id or not callable(lookup):
            return None
        return lookup(series_id)

    def _jellyfin_media_item_for_task(self, task: Any) -> Any | None:
        lookup = getattr(self.repository, "get_jellyfin_media_item", None)
        item = None
        if callable(lookup):
            item = lookup(getattr(task, "media_server_id", None))
        if item is not None:
            return item
        lookup_by_path = getattr(self.repository, "get_jellyfin_media_item_by_path", None)
        if not callable(lookup_by_path):
            return None
        return lookup_by_path(
            getattr(task, "video_path_resolved", None) or getattr(task, "video_path_original", None)
        ) or lookup_by_path(getattr(task, "video_path_original", None))

    def _mark_jellyfin_subtitle_ready(
        self,
        jellyfin_item_id: str | None,
        *,
        path: str | None = None,
    ) -> Any | None:
        mark_ready = getattr(self.repository, "mark_jellyfin_media_item_has_chinese_subtitle", None)
        if callable(mark_ready):
            return mark_ready(jellyfin_item_id, path=path)
        return None

    def _refresh_jellyfin_subtitle_state(self, task: Any) -> None:
        media_item = self._jellyfin_media_item_for_task(task)
        item_id = getattr(task, "media_server_id", None) or getattr(media_item, "jellyfin_item_id", None)
        item_path = (
            getattr(media_item, "path", None)
            or getattr(task, "video_path_resolved", None)
            or getattr(task, "video_path_original", None)
        )
        media_item = self._mark_jellyfin_subtitle_ready(item_id, path=item_path) or media_item
        client = self._build_jellyfin_client()
        if client is None or not item_id:
            return
        try:
            client.refresh_item(item_id)
            metadata = client.get_item(item_id)
        except Exception:
            return
        status = detect_subtitle_status(
            Path(metadata.get("path") or item_path or task.video_path_original),
            list(metadata.get("media_streams") or []),
        )
        update_state = getattr(self.repository, "update_jellyfin_media_item_subtitle_state", None)
        if callable(update_state):
            update_state(
                item_id,
                path=metadata.get("path") or item_path,
                subtitle_status=status.status,
                has_external_chinese_subtitle=status.has_external_chinese,
                has_embedded_chinese_subtitle=status.has_embedded_chinese,
                has_bilingual_subtitle=status.has_bilingual,
            )

    def _build_jellyfin_client(self) -> JellyfinClient | None:
        config = self._jellyfin_config()
        if not config["server_url"] or not config["api_key"]:
            return None
        factory = self.jellyfin_client_factory or JellyfinClient
        return factory(
            server_url=config["server_url"],
            api_key=config["api_key"],
            user_id=config["user_id"],
        )

    def _jellyfin_config(self) -> dict[str, str]:
        defaults = getattr(getattr(self.settings, "jellyfin", None), "model_dump", None)
        config = {"server_url": "", "api_key": "", "user_id": ""}
        if callable(defaults):
            config.update(
                {
                    key: str(value or "")
                    for key, value in self.settings.jellyfin.model_dump().items()
                    if key in config
                }
            )
        stored = {}
        get_setting = getattr(self.repository, "get_setting", None)
        if callable(get_setting):
            stored = get_setting("jellyfin") or {}
        for key in config:
            if stored.get(key):
                config[key] = str(stored[key])
        return config

    def _download_candidate(self, candidate: SubtitleCandidate, task_id: int) -> DownloadedSubtitle:
        target_dir = Path(self.settings.data_dir) / "downloads" / str(task_id)
        if candidate.raw_metadata.get("bundle_reused"):
            return self.bundle_cache.materialize(candidate, target_dir)
        provider = self._find_provider(candidate.provider)
        return provider.download(candidate, target_dir)

    def _find_provider(self, provider_name: str) -> Any:
        for provider in getattr(self.provider_registry, "providers", []):
            if getattr(provider, "name", None) == provider_name.split(":", 1)[0]:
                return provider
        raise ValueError(f"provider {provider_name!r} not found")

    def _record_provider_search_report(self, task_id: int, report: Any) -> None:
        if report.status in {"started", "skipped"}:
            return
        details = {"provider": report.provider}
        if report.candidate_count is not None:
            details["candidate_count"] = report.candidate_count
        if report.duration_ms is not None:
            details["duration_ms"] = report.duration_ms
        if report.error:
            details["error"] = report.error
        if report.reason:
            details["search_strategy"] = report.reason
        search_context = dict(getattr(report, "search_context", None) or {})
        if search_context:
            details["search_context"] = search_context
        context_suffix = _format_provider_search_context(search_context)
        provider_name = str(report.provider).removeprefix("subliminal:")
        if report.status == "progress":
            if search_context.get("cache") == "12h_negative_hit":
                message = (
                    f"字幕来源 {provider_name}：检索键 {report.reason} 命中 12 小时空结果缓存，"
                    "跳过本次网络请求"
                )
            elif report.error:
                message = f"字幕来源 {provider_name}：检索键 {report.reason} 未识别影片，继续回退"
            else:
                message = f"字幕来源 {provider_name}：检索键 {report.reason} 返回 {report.candidate_count or 0} 条"
        elif report.status == "completed":
            strategy = f"，检索键 {report.reason}" if report.reason else ""
            message = (
                f"字幕来源 {provider_name}：返回 {report.candidate_count or 0} 条，"
                f"耗时 {report.duration_ms or 0} ms{strategy}{context_suffix}"
            )
        else:
            message = (
                f"字幕来源 {provider_name}：搜索失败，错误 {report.error or '未知错误'}，"
                f"耗时 {report.duration_ms or 0} ms{context_suffix}"
            )
        self._record_task_event(
            task_id,
            "provider_search",
            report.status,
            message=message,
            error_code="provider_search_failed" if report.status == "failed" else None,
            details=details,
        )

    def _record_provider_wait(
        self,
        task_id: int,
        provider: str,
        wait_seconds: float,
    ) -> None:
        rounded_wait_seconds = math.ceil(max(0.0, float(wait_seconds)))
        self._record_task_event(
            task_id,
            "provider_wait",
            "waiting",
            message=(
                f"Provider {provider} 冷却，约 {rounded_wait_seconds} 秒后获得搜索槽位"
            ),
            details={
                "provider": provider,
                "wait_seconds": rounded_wait_seconds,
            },
        )
        self._release_database_lock()

    def _record_search_summary(self, task_id: int, candidate_count: int) -> None:
        reports = list(getattr(self.provider_registry, "search_reports", []))
        terminal_reports = [report for report in reports if report.status != "started"]
        provider_success_count = sum(
            1 for report in terminal_reports if report.status == "completed"
        )
        provider_failure_count = sum(
            1 for report in terminal_reports if report.status == "failed"
        )
        provider_skipped_count = sum(
            1 for report in terminal_reports if report.status == "skipped"
        )
        details = {
            "candidate_count": candidate_count,
            "provider_success_count": provider_success_count,
            "provider_failure_count": provider_failure_count,
            "provider_skipped_count": provider_skipped_count,
        }
        self._record_task_event(
            task_id,
            "searching",
            "completed",
            message=(
                f"字幕搜索结束：候选 {candidate_count} 条，"
                f"来源成功 {provider_success_count} 个，失败 {provider_failure_count} 个，"
                f"跳过 {provider_skipped_count} 个"
            ),
            details=details,
        )

    def _search_failure_reason(self) -> str:
        reports = list(getattr(self.provider_registry, "search_reports", []))
        terminal_reports = [report for report in reports if report.status != "started"]
        attempted_reports = [
            report for report in terminal_reports if report.status in {"completed", "failed"}
        ]
        if attempted_reports and all(report.status == "failed" for report in attempted_reports):
            return "all_providers_failed"
        if any(report.status == "skipped" for report in terminal_reports) and not attempted_reports:
            return "no_compatible_provider"
        return "no_candidate_found"

    def _record_resolved_path(self, task_id: int, task: Any, resolved: Any) -> dict[str, Any]:
        resolved_path = Path(resolved.resolved_path)
        details = {
            "original_path": task.video_path_original,
            "resolved_path": str(resolved_path),
            "strategy": getattr(resolved, "strategy", "direct"),
        }
        self._record_task_event(
            task_id,
            "resolving",
            "completed",
            message=(
                f"路径解析：{task.video_path_original} -> {resolved_path}，"
                f"策略 {details['strategy']}"
            ),
            details=details,
        )
        return details

    def _inspect_external_subtitles(
        self,
        task_id: int,
        resolved_path: Path,
    ) -> tuple[Any, dict[str, int]]:
        existing = detect_external_subtitles(resolved_path)
        details = {
            "subtitle_count": len(existing.matches),
            "chinese_count": sum(1 for match in existing.matches if match.has_chinese),
            "bilingual_count": sum(1 for match in existing.matches if match.is_bilingual),
        }
        self._record_task_event(
            task_id,
            "checking_existing",
            "completed",
            message=(
                "外挂字幕检查："
                f"共 {details['subtitle_count']} 个，"
                f"中文 {details['chinese_count']} 个，"
                f"双语 {details['bilingual_count']} 个"
            ),
            details=details,
        )
        return existing, details

    @staticmethod
    def _embedded_subtitle_details(result: Any | None) -> dict[str, Any]:
        if result is None:
            return {"status": "failed"}
        streams = list(getattr(result, "streams", []) or [])
        return {
            "subtitle_stream_count": len(streams),
            "chinese_count": sum(
                1 for stream in streams if bool(getattr(stream, "has_chinese", False))
            ),
            "bilingual_count": sum(
                1 for stream in streams if bool(getattr(stream, "is_bilingual", False))
            ),
        }

    def _probe_embedded_subtitles(self, task_id: int, resolved_path: Path) -> Any | None:
        try:
            result = self.embedded_subtitle_detector(
                resolved_path,
                self.settings.probe.ffprobe_path,
            )
        except Exception as exc:
            reason = self._exception_reason(exc, "probe_failed")
            self._record_task_event(
                task_id,
                "checking_embedded",
                "warning",
                message=f"内封字幕检查失败：{reason}，将继续搜索",
                error_code="embedded_probe_failed",
                details={"error": reason, "continued": True},
            )
            return None
        details = self._embedded_subtitle_details(result)
        self._record_task_event(
            task_id,
            "checking_embedded",
            "completed",
            message=(
                "内封字幕检查："
                f"共 {details['subtitle_stream_count']} 条字幕流，"
                f"中文 {details['chinese_count']} 条，"
                f"双语 {details['bilingual_count']} 条"
            ),
            details=details,
        )
        return result

    def _record_task_event(
        self,
        task_id: int,
        stage: str,
        status: str,
        *,
        message: str | None = None,
        error_code: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        if not hasattr(self.repository, "record_task_event"):
            return
        self.repository.record_task_event(
            video_task_id=task_id,
            stage=stage,
            status=status,
            message=message,
            error_code=error_code,
            details=details,
        )

    def _record_task_source(self, task: Any) -> None:
        has_event = getattr(self.repository, "has_task_event", None)
        if callable(has_event) and has_event(task.id, "task_source"):
            return
        source = str(getattr(getattr(task, "job", None), "source", "") or "unknown")
        labels = {
            "moviepilot-csf": "MoviePilot 下发",
            "jellyfin-manual": "手动添加",
            "manual": "手动添加",
            "manual-retry": "手动重试",
            "test": "系统测试添加",
            "system-test": "系统测试添加",
        }
        label = labels.get(source, f"其他来源（{source}）")
        self._record_task_event(
            task.id,
            "task_source",
            "completed",
            message=f"任务来源：{label}",
            details={"source": source, "source_label": label},
        )

    def _release_database_lock(self) -> None:
        """Commit pending progress before a slow external operation holds the worker."""
        session = getattr(self.repository, "session", None)
        if session is not None:
            session.commit()

    def _set_task_stage(
        self,
        task_id: int,
        status: str,
    ) -> None:
        if hasattr(self.repository, "update_video_task_status"):
            self.repository.update_video_task_status(task_id, status)

    def _finish_task(
        self,
        task_id: int,
        status: str,
        stage: str,
        message: str,
    ) -> None:
        self.repository.update_video_task_status(task_id, status)
        self._record_task_event(task_id, stage, "completed", message=message)

    def _fail_task(
        self,
        task_id: int,
        error_code: str,
        *,
        stage: str,
        message: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.repository.update_video_task_status(task_id, TASK_FAILED, error_code)
        self._record_task_event(
            task_id,
            stage,
            "failed",
            message=message or error_code,
            error_code=error_code,
            details=details,
        )

    def _search_details(self, request: SubtitleSearchRequest) -> dict[str, Any]:
        episode_code = None
        if request.season is not None or request.episode is not None:
            season = "??" if request.season is None else f"{request.season:02d}"
            episode = "??" if request.episode is None else f"{request.episode:02d}"
            episode_code = f"S{season}E{episode}"
        details = {
            "title": request.title,
            "year": request.year,
            "season": request.season,
            "episode": request.episode,
            "episode_code": episode_code,
            "languages": list(request.fallback_languages),
        }
        for key, value in (
            ("original_title", request.original_title),
            ("imdb_id", request.imdb_id),
            ("tmdb_id", request.tmdb_id),
        ):
            if value:
                details[key] = value
        return details

    def _search_started_message(self, details: dict[str, Any]) -> str:
        media = details["title"]
        if details["year"] is not None:
            media += f" ({details['year']})"
        if details["episode_code"]:
            media += f" {details['episode_code']}"
        languages = ", ".join(details["languages"]) or "未指定"
        identifier_parts = []
        if details.get("imdb_id"):
            identifier_parts.append(f"IMDb {details['imdb_id']}")
        if details.get("tmdb_id"):
            identifier_parts.append(f"TMDb {details['tmdb_id']}")
        if details.get("original_title"):
            identifier_parts.append(f"原始名 {details['original_title']}")
        identifiers = f"，{'；'.join(identifier_parts)}" if identifier_parts else ""
        return f"字幕搜索开始：{media}{identifiers}，语言 {languages}"

    def _set_resolved_path(self, task: Any, resolved_path: Path) -> None:
        task.video_path_resolved = str(resolved_path)
        if hasattr(self.repository, "set_video_task_resolved_path"):
            self.repository.set_video_task_resolved_path(task.id, str(resolved_path))

    def _update_candidate_attempt(
        self,
        candidate_id: int,
        *,
        status: str,
        error_message: str | None = None,
        increment: bool = False,
    ) -> None:
        if not hasattr(self.repository, "update_candidate_attempt"):
            return
        self.repository.update_candidate_attempt(
            candidate_id=candidate_id,
            status=status,
            error_message=error_message,
            increment=increment,
        )

    def _event_details(
        self,
        candidate: SubtitleCandidate,
        *,
        attempt_number: int,
        max_attempts: int,
    ) -> dict[str, Any]:
        details = {
            "attempt_number": attempt_number,
            "max_attempts": max_attempts,
            "provider": candidate.provider,
            "language": candidate.language,
            "title": candidate.title,
            "source_url": candidate.source_url,
        }
        if candidate.provider_quality is not None:
            details["provider_quality"] = round(candidate.provider_quality, 4)
        for key in (
            "assrt_downloads",
            "assrt_views",
            "assrt_vote_score",
            "zimuku_downloads",
            "zimuku_quality",
        ):
            if candidate.raw_metadata.get(key) is not None:
                details[key] = candidate.raw_metadata[key]
        return details

    def _candidate_message(self, action: str, details: dict[str, Any]) -> str:
        provider = str(details.get("provider") or "unknown").removeprefix("subliminal:")
        return (
            f"候选 {details['attempt_number']}/{details['max_attempts']}：{action}，"
            f"来源 {provider}，{details.get('title') or '未命名字幕'}"
        )

    def _validation_message(self, details: dict[str, Any], validation: Any) -> str:
        encoding = getattr(validation, "encoding", None) or "未知编码"
        cue_count = int(getattr(validation, "cue_count", 0) or 0)
        duration_seconds = getattr(validation, "duration_seconds", None)
        duration = ""
        if duration_seconds is not None:
            duration = f"，末条 {round(float(duration_seconds))} 秒"
        return (
            f"候选 {details['attempt_number']}/{details['max_attempts']}：字幕文件结构检查通过，"
            f"{encoding}，{cue_count} 条时间轴{duration}"
        )

    def _exception_reason(self, exc: Exception, fallback: str) -> str:
        message = str(exc).strip()
        if message:
            return message
        return fallback


def _synced_output_path(subtitle_path: Path) -> Path:
    return subtitle_path.with_name(f"{subtitle_path.stem}.synced{subtitle_path.suffix}")


def _format_provider_search_context(context: dict[str, Any]) -> str:
    if not context:
        return ""
    parts: list[str] = []
    title = str(context.get("title") or "").strip()
    if title:
        title_label = "原始标题" if context.get("title_source") == "original_title" else "媒体标题"
        parts.append(f"{title_label}“{title}”")
    if context.get("year"):
        parts.append(f"年份 {context['year']}")
    if context.get("imdb_id"):
        parts.append(f"IMDb {context['imdb_id']}")
    strategy = str(context.get("strategy") or "").strip()
    if strategy:
        parts.append(f"策略 {strategy}")
    season = context.get("season")
    episode = context.get("episode")
    if isinstance(season, int) and isinstance(episode, int):
        parts.append(f"S{season:02d}E{episode:02d}")
    file_name = str(context.get("file_name") or "").strip()
    if file_name:
        parts.append(f"文件名“{file_name}”")
    return f"；检索依据：{'，'.join(parts)}" if parts else ""


def _provider_id(provider_ids: Any, name: str) -> str | None:
    if not isinstance(provider_ids, dict):
        return None
    normalized_name = name.casefold()
    for key, value in provider_ids.items():
        if str(key).casefold() != normalized_name:
            continue
        candidate = str(value or "").strip()
        return candidate or None
    return None


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _same_subtitle_content(first: Path, second: Path) -> bool:
    try:
        return first.read_bytes() == second.read_bytes()
    except OSError:
        return False


def _supplemental_subtitle_path(video_path: Path, language: str, extension: str, supplemental: bool) -> Path:
    if not supplemental:
        return build_subtitle_path(video_path, language=language, extension=extension, default=True)

    index = 1
    while True:
        base = video_path.with_name(
            f"{video_path.stem}.{language}.extra-{index}.{extension.lstrip('.')}"
        )
        if not base.exists():
            return base
        index += 1


def _english_title_from_path(path: Path) -> str | None:
    """Extract an English release title from a mixed-language media folder."""
    for value in (path.parent.name, path.stem):
        if not re.search(r"[\u4e00-\u9fff]", value):
            continue
        matches = re.findall(r"[A-Za-z][A-Za-z0-9 .,'&:!\-]*", value)
        for match in matches:
            title = re.sub(r"\s*\(?(?:19|20)\d{2}\)?\s*$", "", match)
            title = re.split(r"\s+-\s+(?:2160|1080|720|480)p\b", title, maxsplit=1)[0]
            title = title.strip(" .-_")
            if len(title) >= 3 and not re.fullmatch(r"(?:mkv|mp4|avi|web[- ]?dl)", title, re.I):
                return title
    return None


def _normalized_search_title(value: str | None, *, is_episode: bool) -> str | None:
    if not value:
        return None
    normalized = value.strip()
    if is_episode:
        normalized = re.sub(
            r"\s*[-_. ]+S\d{1,2}E\d{1,2}\b.*$",
            "",
            normalized,
            flags=re.IGNORECASE,
        ).strip(" .-_")
    return normalized or None


def _episode_identity_from_path(path: Path) -> tuple[int, int, str, int | None] | None:
    match = re.search(
        r"(?i)(?:\bS(?P<season>\d{1,2})[ ._-]*E(?P<episode>\d{1,3})\b|"
        r"\b(?P<season_x>\d{1,2})x(?P<episode_x>\d{1,3})\b)",
        path.stem,
    )
    if match is None:
        return None
    season = int(match.group("season") or match.group("season_x"))
    episode = int(match.group("episode") or match.group("episode_x"))
    season_directory = path.parent
    series_directory = (
        season_directory.parent
        if re.fullmatch(r"(?i)(?:season|第)\s*\d+\s*(?:季)?", season_directory.name)
        else None
    )
    series_source = series_directory.name if series_directory is not None else path.stem[: match.start()]
    year_match = re.search(r"(?<!\d)((?:19|20)\d{2})(?!\d)", series_source)
    year = int(year_match.group(1)) if year_match is not None else None
    title = re.sub(r"\s*\((?:19|20)\d{2}\)\s*$", "", series_source).strip(" .-_")
    if not title:
        title = path.stem[: match.start()].strip(" .-_")
    return season, episode, title, year
