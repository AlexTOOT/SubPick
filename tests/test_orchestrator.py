from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from subtitle_sidecar.config import AppSettings
from subtitle_sidecar.db.repository import JobCreate, Repository
from subtitle_sidecar.db.session import create_sqlite_engine, create_tables, session_scope
from subtitle_sidecar.pipeline.orchestrator import SubtitleOrchestrator
from subtitle_sidecar.probe.streams import EmbeddedSubtitleResult
from subtitle_sidecar.providers.base import (
    DownloadedSubtitle,
    DownloadedSubtitleMember,
    ProviderSearchReport,
    SubtitleCandidate,
)


class FakeRepository:
    def __init__(self, task: SimpleNamespace) -> None:
        self.task = task
        self.status_updates: list[tuple[int, str, str | None]] = []
        self.candidate_records: list[dict[str, Any]] = []
        self.candidate_attempt_updates: list[dict[str, Any]] = []
        self.artifact_records: list[dict[str, Any]] = []
        self.task_events: list[dict[str, Any]] = []
        self.settings: dict[str, dict[str, Any]] = {}
        self.jellyfin_item: SimpleNamespace | None = None
        self.jellyfin_mark_ready_calls: list[dict[str, Any]] = []
        self.jellyfin_state_updates: list[dict[str, Any]] = []
        self.placed_candidates_by_task: dict[int, list[SimpleNamespace]] = {}
        self.retry_parent_by_task: dict[int, int] = {}
        self.retry_parent_calls: list[int] = []

    def get_video_task(self, task_id: int) -> SimpleNamespace | None:
        if task_id != self.task.id:
            return None
        return self.task

    def list_placed_candidates_for_task(self, task_id: int) -> list[SimpleNamespace]:
        return list(self.placed_candidates_by_task.get(task_id, []))

    def get_retry_parent_task_id(self, task_id: int) -> int | None:
        self.retry_parent_calls.append(task_id)
        return self.retry_parent_by_task.get(task_id)

    def get_setting(self, key: str) -> dict[str, Any] | None:
        value = self.settings.get(key)
        if value is None:
            return None
        return dict(value)

    def get_jellyfin_media_item(self, jellyfin_item_id: str | None) -> SimpleNamespace | None:
        if self.jellyfin_item is None:
            return None
        if jellyfin_item_id and self.jellyfin_item.jellyfin_item_id == jellyfin_item_id:
            return self.jellyfin_item
        return None

    def get_jellyfin_media_item_by_path(self, path: str | None) -> SimpleNamespace | None:
        if self.jellyfin_item is None or not path:
            return None
        if self.jellyfin_item.path == path:
            return self.jellyfin_item
        return None

    def mark_jellyfin_media_item_has_chinese_subtitle(
        self,
        jellyfin_item_id: str | None,
        *,
        path: str | None = None,
    ) -> SimpleNamespace | None:
        self.jellyfin_mark_ready_calls.append({"jellyfin_item_id": jellyfin_item_id, "path": path})
        item = self.get_jellyfin_media_item(jellyfin_item_id) or self.get_jellyfin_media_item_by_path(path)
        if item is None:
            return None
        item.subtitle_status = "has_chinese"
        item.has_external_chinese_subtitle = True
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
    ) -> SimpleNamespace | None:
        self.jellyfin_state_updates.append(
            {
                "jellyfin_item_id": jellyfin_item_id,
                "path": path,
                "subtitle_status": subtitle_status,
                "has_external_chinese_subtitle": has_external_chinese_subtitle,
                "has_embedded_chinese_subtitle": has_embedded_chinese_subtitle,
                "has_bilingual_subtitle": has_bilingual_subtitle,
            }
        )
        item = self.get_jellyfin_media_item(jellyfin_item_id) or self.get_jellyfin_media_item_by_path(path)
        if item is None:
            return None
        item.subtitle_status = subtitle_status
        item.has_external_chinese_subtitle = has_external_chinese_subtitle
        item.has_embedded_chinese_subtitle = has_embedded_chinese_subtitle
        item.has_bilingual_subtitle = has_bilingual_subtitle
        return item

    def update_video_task_status(
        self,
        task_id: int,
        status: str,
        error_message: str | None = None,
    ) -> SimpleNamespace:
        self.status_updates.append((task_id, status, error_message))
        self.task.status = status
        self.task.error_message = error_message
        return self.task

    def record_candidate(self, **kwargs: Any) -> SimpleNamespace:
        self.candidate_records.append(kwargs)
        return SimpleNamespace(id=len(self.candidate_records), **kwargs)

    def record_artifact(self, **kwargs: Any) -> SimpleNamespace:
        self.artifact_records.append(kwargs)
        return SimpleNamespace(id=len(self.artifact_records), **kwargs)

    def update_candidate_attempt(
        self,
        *,
        candidate_id: int,
        status: str,
        error_message: str | None = None,
        attempts: int | None = None,
        increment: bool = False,
    ) -> SimpleNamespace:
        update = {
            "candidate_id": candidate_id,
            "status": status,
            "error_message": error_message,
            "attempts": attempts,
            "increment": increment,
        }
        self.candidate_attempt_updates.append(update)
        return SimpleNamespace(**update)

    def record_task_event(
        self,
        *,
        video_task_id: int,
        stage: str,
        status: str,
        message: str | None = None,
        error_code: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> SimpleNamespace:
        event = {
            "video_task_id": video_task_id,
            "stage": stage,
            "status": status,
            "message": message,
            "error_code": error_code,
            "details": details,
        }
        self.task_events.append(event)
        return SimpleNamespace(id=len(self.task_events), **event)

    def has_task_event(self, video_task_id: int, stage: str) -> bool:
        return any(
            event["video_task_id"] == video_task_id and event["stage"] == stage
            for event in self.task_events
        )


class FakeResolver:
    def __init__(self, resolved_path: Path | None, strategy: str | None = None) -> None:
        self.resolved_path = resolved_path
        self.strategy = strategy or ("direct" if resolved_path is not None else "not_found")

    def resolve(self, original_path: str) -> SimpleNamespace:
        return SimpleNamespace(
            original_path=original_path,
            resolved_path=self.resolved_path,
            strategy=self.strategy,
        )


@dataclass
class FakeProvider:
    name: str = "fake"
    downloaded_content: str = "1\n00:00:01,000 --> 00:00:02,000\n你好\n"
    download_calls: list[tuple[SubtitleCandidate, Path]] = field(default_factory=list)

    def download(self, candidate: SubtitleCandidate, target_dir: Path) -> DownloadedSubtitle:
        self.download_calls.append((candidate, target_dir))
        target_dir.mkdir(parents=True, exist_ok=True)
        path = target_dir / f"downloaded.{candidate.format}"
        path.write_text(self.downloaded_content, encoding="utf-8")
        return DownloadedSubtitle(candidate=candidate, path=path)


class FakeProviderRegistry:
    def __init__(
        self,
        providers: list[FakeProvider],
        candidates: list[SubtitleCandidate],
        search_reports: list[ProviderSearchReport] | None = None,
    ) -> None:
        self.providers = providers
        self._candidates = candidates
        self.requests: list[Any] = []
        self.search_reports = list(search_reports or [])
        self.reporter = None

    def set_reporter(self, reporter) -> None:
        self.reporter = reporter

    def search(self, request: Any) -> list[SubtitleCandidate]:
        self.requests.append(request)
        if self.reporter is not None:
            for report in self.search_reports:
                self.reporter(report)
        return list(self._candidates)


class BundleProvider(FakeProvider):
    def download(self, candidate: SubtitleCandidate, target_dir: Path) -> DownloadedSubtitle:
        self.download_calls.append((candidate, target_dir))
        target_dir.mkdir(parents=True, exist_ok=True)
        first = target_dir / "Shared.Show.S01E01.zh-cn.srt"
        second = target_dir / "Shared.Show.S01E02.zh-cn.srt"
        content = "1\n00:00:01,000 --> 00:00:02,000\n\u4e2d\u6587\u5b57\u5e55\n"
        first.write_text(content, encoding="utf-8")
        second.write_text(content, encoding="utf-8")
        return DownloadedSubtitle(
            candidate=candidate,
            path=first,
            members=(
                DownloadedSubtitleMember(path=first, filename=first.name),
                DownloadedSubtitleMember(path=second, filename=second.name),
            ),
        )


class FakeBundleCache:
    def __init__(self, candidate: SubtitleCandidate, *, fail_materialize: bool = False) -> None:
        self.candidate = candidate
        self.fail_materialize = fail_materialize
        self.find_calls = 0
        self.materialize_calls = 0
        self.store_calls = 0

    def find(self, request: Any) -> SimpleNamespace | None:
        self.find_calls += 1
        return SimpleNamespace(candidate=self.candidate, source_task_id=99)

    def materialize(self, candidate: SubtitleCandidate, target_dir: Path) -> DownloadedSubtitle:
        self.materialize_calls += 1
        if self.fail_materialize:
            raise FileNotFoundError("bundle_cache_member_missing")
        target_dir.mkdir(parents=True, exist_ok=True)
        path = target_dir / "cached.srt"
        path.write_text("1\n00:00:01,000 --> 00:00:02,000\n缓存字幕\n", encoding="utf-8")
        return DownloadedSubtitle(candidate=candidate, path=path)

    def store(self, *args: Any, **kwargs: Any) -> int:
        self.store_calls += 1
        return 0


class FakeEmbeddedDetector:
    def __init__(self, result: SimpleNamespace | None = None, *, should_raise: bool = False) -> None:
        self.result = result or SimpleNamespace(has_chinese=False, has_bilingual=False, streams=[])
        self.should_raise = should_raise
        self.calls: list[tuple[Path, str]] = []

    def __call__(self, video_path: Path, ffprobe_path: str) -> SimpleNamespace:
        self.calls.append((video_path, ffprobe_path))
        if self.should_raise:
            raise RuntimeError("probe failed")
        return self.result


class FakeSyncer:
    def __init__(
        self,
        synced_content: str = "",
        *,
        success: bool = True,
        reason: str | None = None,
        score: float | None = None,
    ) -> None:
        self.synced_content = synced_content
        self.success = success
        self.reason = reason
        self.score = score
        self.calls: list[tuple[Path, Path, Path]] = []

    def __call__(self, video_path: Path, subtitle_path: Path, output_path: Path) -> SimpleNamespace:
        self.calls.append((video_path, subtitle_path, output_path))
        if self.success:
            output_path.write_text(self.synced_content, encoding="utf-8")
        return SimpleNamespace(
            success=self.success,
            output_path=output_path,
            stdout="sync ok" if self.success else "",
            stderr="" if self.success else "sync failed",
            reason=self.reason,
            score=self.score,
        )


def build_settings(
    tmp_path: Path,
    *,
    sync_enabled: bool = False,
    save_unsynced_on_sync_failure: bool = False,
) -> AppSettings:
    return AppSettings(
        data_dir=tmp_path / "data",
        sync={"enabled": sync_enabled},
        subtitles={"save_unsynced_on_sync_failure": save_unsynced_on_sync_failure},
    )


def build_task(video_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        id=1,
        video_path_original=str(video_path),
        video_path_resolved=None,
        media_server_id=None,
        title="Movie Name",
        year=2024,
        season=None,
        episode=None,
        result_subtitle_path=None,
        status="queued",
        error_message=None,
        candidates=[],
        artifacts=[],
        job=SimpleNamespace(source="moviepilot-csf", raw_payload_json={}),
    )


def build_candidate() -> SubtitleCandidate:
    return SubtitleCandidate(
        provider="fake",
        language="zh-cn",
        is_bilingual=True,
        format="srt",
        title="Movie Name bilingual",
        source_url="https://example.invalid/subtitle.srt",
        release_info="WEB-DL",
        confidence=0.9,
        raw_metadata={},
    )


def test_episode_tasks_reuse_cached_members_from_a_season_bundle(tmp_path: Path) -> None:
    first_video = tmp_path / "Shared.Show.S01E01.mkv"
    second_video = tmp_path / "Shared.Show.S01E02.mkv"
    first_video.write_bytes(b"video")
    second_video.write_bytes(b"video")
    first_task = build_task(first_video)
    first_task.title = "Shared Show"
    first_task.season = 1
    first_task.episode = 1
    second_task = build_task(second_video)
    second_task.id = 2
    second_task.title = "Shared Show"
    second_task.season = 1
    second_task.episode = 2
    provider = BundleProvider()
    registry = FakeProviderRegistry([provider], [build_candidate()])
    settings = build_settings(tmp_path)
    first_repository = FakeRepository(first_task)
    SubtitleOrchestrator(
        settings=settings,
        repository=first_repository,
        resolver=FakeResolver(first_video),
        provider_registry=registry,
    ).process_video_task(first_task.id)

    second_repository = FakeRepository(second_task)
    SubtitleOrchestrator(
        settings=settings,
        repository=second_repository,
        resolver=FakeResolver(second_video),
        provider_registry=registry,
    ).process_video_task(second_task.id)

    assert len(registry.requests) == 1
    assert len(provider.download_calls) == 1
    assert first_repository.status_updates[-1] == (1, "completed", None)
    assert second_repository.status_updates[-1] == (2, "completed", None)
    assert any(event["stage"] == "bundle_cache" for event in first_repository.task_events)
    assert any(event["stage"] == "bundle_reuse" for event in second_repository.task_events)
    assert Path(second_task.result_subtitle_path).read_text(encoding="utf-8").endswith("\u4e2d\u6587\u5b57\u5e55\n")


def test_episode_task_skips_candidates_with_explicit_wrong_season(tmp_path: Path) -> None:
    video = tmp_path / "Show.S01E10.mkv"
    video.write_bytes(b"video")
    task = build_task(video)
    task.season = 1
    task.episode = 10
    provider = FakeProvider()
    wrong_candidate = replace(
        build_candidate(),
        title="Show 第五季 第1集",
        release_info="Other.Show.S05E01",
    )
    repository = FakeRepository(task)
    orchestrator = SubtitleOrchestrator(
        settings=build_settings(tmp_path),
        repository=repository,
        resolver=FakeResolver(video),
        provider_registry=FakeProviderRegistry([provider], [wrong_candidate]),
    )

    orchestrator.process_video_task(task.id)

    assert provider.download_calls == []
    assert task.error_message == "no_candidate_found"
    assert any(event["stage"] == "candidate_filter" for event in repository.task_events)


def test_movie_task_skips_explicit_series_candidate(tmp_path: Path) -> None:
    video = tmp_path / "Movie.2023.mkv"
    video.write_bytes(b"video")
    task = build_task(video)
    provider = FakeProvider()
    series_candidate = replace(build_candidate(), title="Kingdom S01E01", release_info="Kingdom.S01E01")
    repository = FakeRepository(task)
    orchestrator = SubtitleOrchestrator(
        settings=build_settings(tmp_path),
        repository=repository,
        resolver=FakeResolver(video),
        provider_registry=FakeProviderRegistry([provider], [series_candidate]),
    )

    orchestrator.process_video_task(task.id)

    assert provider.download_calls == []
    assert task.error_message == "no_candidate_found"
    assert any(event["stage"] == "candidate_filter" for event in repository.task_events)


def test_assrt_title_mismatch_records_its_own_filter_reason(tmp_path: Path) -> None:
    video = tmp_path / "Gas.Man.S01E01.mkv"
    video.write_bytes(b"video")
    task = build_task(video)
    task.title = "Gas Man"
    task.season = 1
    task.episode = 1
    unrelated = replace(
        build_candidate(),
        provider="assrt",
        title="Saint Seiya Soul of Gold",
        release_info="S01E01",
    )
    repository = FakeRepository(task)
    orchestrator = SubtitleOrchestrator(
        settings=build_settings(tmp_path),
        repository=repository,
        resolver=FakeResolver(video),
        provider_registry=FakeProviderRegistry([FakeProvider()], [unrelated]),
    )

    orchestrator.process_video_task(task.id)

    filters = [event for event in repository.task_events if event["stage"] == "candidate_filter"]
    assert filters[0]["status"] == "skipped"
    assert filters[0]["details"]["reason"] == "title_mismatch"
    assert filters[0]["details"]["source_url"] == unrelated.source_url
    assert unrelated.source_url in filters[0]["message"]
    assert filters[-1]["details"]["mismatch_counts"] == {"title_mismatch": 1}


class NonJsonSubtitle:
    def __repr__(self) -> str:
        return "OpenSubtitlesSubtitle(test)"


def test_existing_external_chinese_subtitle_skips_download(tmp_path: Path) -> None:
    video = tmp_path / "Movie.Name.2024.mkv"
    video.write_bytes(b"video")
    (tmp_path / "Movie.Name.2024.zh-cn.srt").write_text(
        "1\n00:00:01,000 --> 00:00:02,000\n你好\n",
        encoding="utf-8",
    )
    task = build_task(video)
    repository = FakeRepository(task)
    provider = FakeProvider()
    registry = FakeProviderRegistry([provider], [build_candidate()])
    orchestrator = SubtitleOrchestrator(
        settings=build_settings(tmp_path),
        repository=repository,
        resolver=FakeResolver(video),
        provider_registry=registry,
    )

    orchestrator.process_video_task(task.id)

    assert repository.status_updates[-1] == (task.id, "skipped_existing_subtitle", None)
    assert provider.download_calls == []
    assert repository.candidate_records == []
    assert repository.artifact_records == []
    event = next(
        event for event in repository.task_events if event["stage"] == "checking_existing"
    )
    assert event["status"] == "completed"
    assert event["details"] == {
        "subtitle_count": 1,
        "chinese_count": 1,
        "bilingual_count": 0,
    }
    assert event["message"] == "外挂字幕检查：共 1 个，中文 1 个，双语 0 个"


def test_manual_retry_downloads_supplemental_subtitle_when_chinese_exists(tmp_path: Path) -> None:
    video = tmp_path / "Movie.Name.2024.mkv"
    video.write_bytes(b"video")
    (tmp_path / "Movie.Name.2024.zh-cn.default.srt").write_text(
        "1\n00:00:01,000 --> 00:00:02,000\n已有字幕\n",
        encoding="utf-8",
    )
    task = build_task(video)
    task.job = SimpleNamespace(source="manual-retry")
    repository = FakeRepository(task)
    provider = FakeProvider()
    registry = FakeProviderRegistry([provider], [build_candidate()])
    orchestrator = SubtitleOrchestrator(
        settings=build_settings(tmp_path),
        repository=repository,
        resolver=FakeResolver(video),
        provider_registry=registry,
    )

    orchestrator.process_video_task(task.id)

    placed_path = tmp_path / "Movie.Name.2024.zh-cn.extra-1.srt"
    assert repository.status_updates[-1] == (task.id, "completed", None)
    assert len(provider.download_calls) == 1
    assert repository.artifact_records[0]["path"] == str(placed_path)
    assert placed_path.exists()


def test_manual_retry_skips_duplicate_existing_subtitle_content(tmp_path: Path) -> None:
    video = tmp_path / "Movie.Name.2024.mkv"
    video.write_bytes(b"video")
    content = "1\n00:00:01,000 --> 00:00:02,000\n相同字幕\n"
    (tmp_path / "Movie.Name.2024.zh-cn.default.srt").write_text(content, encoding="utf-8")
    task = build_task(video)
    task.job = SimpleNamespace(source="jellyfin-manual")
    repository = FakeRepository(task)
    provider = FakeProvider(downloaded_content=content)
    registry = FakeProviderRegistry([provider], [build_candidate()])
    orchestrator = SubtitleOrchestrator(
        settings=build_settings(tmp_path),
        repository=repository,
        resolver=FakeResolver(video),
        provider_registry=registry,
    )

    orchestrator.process_video_task(task.id)

    assert repository.status_updates[-1] == (task.id, "failed", "duplicate_existing_subtitle")
    assert repository.artifact_records == []
    assert any(
        update["status"] == "skipped" and update["error_message"] == "duplicate_existing_subtitle"
        for update in repository.candidate_attempt_updates
    )


def test_path_resolution_records_result_details_without_generic_started_event(
    tmp_path: Path,
) -> None:
    video = tmp_path / "Movie.Name.2024.mkv"
    video.write_bytes(b"video")
    task = build_task(video)
    repository = FakeRepository(task)
    orchestrator = SubtitleOrchestrator(
        settings=build_settings(tmp_path),
        repository=repository,
        resolver=FakeResolver(video, strategy="mapping"),
        provider_registry=FakeProviderRegistry([FakeProvider()], []),
    )

    orchestrator.process_video_task(task.id)

    events = [event for event in repository.task_events if event["stage"] == "resolving"]
    assert len(events) == 1
    assert events[0]["status"] == "completed"
    assert events[0]["details"] == {
        "original_path": str(video),
        "resolved_path": str(video),
        "strategy": "mapping",
    }
    assert events[0]["message"] == f"路径解析：{video} -> {video}，策略 mapping"


def test_path_resolution_failure_records_readable_details(tmp_path: Path) -> None:
    missing = tmp_path / "missing.mkv"
    task = build_task(missing)
    repository = FakeRepository(task)
    orchestrator = SubtitleOrchestrator(
        settings=build_settings(tmp_path),
        repository=repository,
        resolver=FakeResolver(None),
        provider_registry=FakeProviderRegistry([], []),
    )

    orchestrator.process_video_task(task.id)

    event = repository.task_events[-1]
    assert event["stage"] == "resolving"
    assert event["status"] == "failed"
    assert event["message"] == f"路径解析失败：{missing}，未找到可用文件"
    assert event["details"] == {
        "original_path": str(missing),
        "resolved_path": None,
        "strategy": "not_found",
    }


def test_no_candidates_marks_task_failed_with_no_candidate_found(tmp_path: Path) -> None:
    video = tmp_path / "Movie.Name.2024.mkv"
    video.write_bytes(b"video")
    task = build_task(video)
    repository = FakeRepository(task)
    registry = FakeProviderRegistry([FakeProvider()], [])
    orchestrator = SubtitleOrchestrator(
        settings=build_settings(tmp_path),
        repository=repository,
        resolver=FakeResolver(video),
        provider_registry=registry,
    )

    orchestrator.process_video_task(task.id)

    assert repository.status_updates[-1] == (task.id, "failed", "no_candidate_found")
    assert repository.candidate_records == []
    assert repository.artifact_records == []
    summary = next(
        event
        for event in repository.task_events
        if event["stage"] == "searching" and event["status"] == "completed"
    )
    assert summary["details"] == {
        "candidate_count": 0,
        "provider_success_count": 0,
        "provider_failure_count": 0,
        "provider_skipped_count": 0,
    }


def test_task_source_is_recorded_once_with_readable_label(tmp_path: Path) -> None:
    video = tmp_path / "Movie.Name.2024.mkv"
    video.write_bytes(b"video")
    task = build_task(video)
    task.job = SimpleNamespace(source="moviepilot-csf", raw_payload_json={})
    repository = FakeRepository(task)
    orchestrator = SubtitleOrchestrator(
        settings=build_settings(tmp_path),
        repository=repository,
        resolver=FakeResolver(video),
        provider_registry=FakeProviderRegistry([], []),
    )

    orchestrator.process_video_task(task.id)
    orchestrator._record_task_source(task)

    source_events = [event for event in repository.task_events if event["stage"] == "task_source"]
    assert len(source_events) == 1
    assert source_events[0]["message"] == "任务来源：MoviePilot 下发"
    assert source_events[0]["details"]["source"] == "moviepilot-csf"


def test_all_skipped_providers_fail_with_no_compatible_provider(tmp_path: Path) -> None:
    video = tmp_path / "Movie.Name.2024.mkv"
    video.write_bytes(b"video")
    task = build_task(video)
    repository = FakeRepository(task)
    reports = [
        ProviderSearchReport(provider="subliminal:opensubtitles", status="started"),
        ProviderSearchReport(
            provider="subliminal:opensubtitles",
            status="skipped",
            error="unsupported_language",
            reason="unsupported_language",
        ),
    ]
    registry = FakeProviderRegistry([FakeProvider()], [], reports)
    orchestrator = SubtitleOrchestrator(
        settings=build_settings(tmp_path),
        repository=repository,
        resolver=FakeResolver(video),
        provider_registry=registry,
        embedded_subtitle_detector=FakeEmbeddedDetector(),
    )

    orchestrator.process_video_task(task.id)

    assert repository.status_updates[-1] == (
        task.id,
        "failed",
        "no_compatible_provider",
    )
    assert not any(
        event["stage"] == "provider_search" for event in repository.task_events
    )
    summary = next(
        event
        for event in repository.task_events
        if event["stage"] == "searching" and event["status"] == "completed"
    )
    assert summary["message"] == (
        "字幕搜索结束：候选 0 条，来源成功 0 个，失败 0 个，跳过 1 个"
    )
    assert summary["details"] == {
        "candidate_count": 0,
        "provider_success_count": 0,
        "provider_failure_count": 0,
        "provider_skipped_count": 1,
    }


def test_all_failed_provider_reports_are_distinguished_from_normal_zero_results(tmp_path: Path) -> None:
    class FailedProviderRegistry(FakeProviderRegistry):
        def __init__(self) -> None:
            super().__init__([FakeProvider()], [])
            self.search_reports = [
                ProviderSearchReport(provider="subliminal:one", status="started"),
                ProviderSearchReport(
                    provider="subliminal:one",
                    status="failed",
                    error="provider unavailable",
                ),
            ]

        def set_reporter(self, reporter) -> None:
            self.reporter = reporter

        def search(self, request: Any) -> list[SubtitleCandidate]:
            self.requests.append(request)
            for report in self.search_reports:
                self.reporter(report)
            return []

    video = tmp_path / "Movie.Name.2024.mkv"
    video.write_bytes(b"video")
    task = build_task(video)
    repository = FakeRepository(task)
    orchestrator = SubtitleOrchestrator(
        settings=build_settings(tmp_path),
        repository=repository,
        resolver=FakeResolver(video),
        provider_registry=FailedProviderRegistry(),
    )

    orchestrator.process_video_task(task.id)

    assert repository.status_updates[-1] == (task.id, "failed", "all_providers_failed")
    reports = [event for event in repository.task_events if event["stage"] == "provider_search"]
    assert [event["status"] for event in reports] == ["failed"]
    assert reports[0]["message"] == (
        "字幕来源 one：搜索失败，错误 provider unavailable，耗时 0 ms"
    )
    summary = next(
        event
        for event in repository.task_events
        if event["stage"] == "searching" and event["status"] == "completed"
    )
    assert summary["details"] == {
        "candidate_count": 0,
        "provider_success_count": 0,
        "provider_failure_count": 1,
        "provider_skipped_count": 0,
    }


def test_embedded_chinese_subtitle_skips_download(tmp_path: Path) -> None:
    video = tmp_path / "Movie.Name.2024.mkv"
    video.write_bytes(b"video")
    task = build_task(video)
    repository = FakeRepository(task)
    provider = FakeProvider()
    registry = FakeProviderRegistry([provider], [build_candidate()])
    embedded_detector = FakeEmbeddedDetector(
        SimpleNamespace(has_chinese=True, has_bilingual=False, streams=[])
    )
    orchestrator = SubtitleOrchestrator(
        settings=build_settings(tmp_path),
        repository=repository,
        resolver=FakeResolver(video),
        provider_registry=registry,
        embedded_subtitle_detector=embedded_detector,
    )

    orchestrator.process_video_task(task.id)

    assert embedded_detector.calls == [(video, "ffprobe")]
    assert repository.status_updates[-1] == (task.id, "skipped_embedded_subtitle", None)
    assert registry.requests == []
    assert provider.download_calls == []


def test_embedded_probe_records_stream_counts(tmp_path: Path) -> None:
    video = tmp_path / "Movie.Name.2024.mkv"
    video.write_bytes(b"video")
    task = build_task(video)
    repository = FakeRepository(task)
    embedded_detector = FakeEmbeddedDetector(
        SimpleNamespace(
            has_chinese=True,
            has_bilingual=True,
            streams=[
                SimpleNamespace(has_chinese=True, is_bilingual=True),
                SimpleNamespace(has_chinese=False, is_bilingual=False),
            ],
        )
    )
    orchestrator = SubtitleOrchestrator(
        settings=build_settings(tmp_path),
        repository=repository,
        resolver=FakeResolver(video),
        provider_registry=FakeProviderRegistry([FakeProvider()], []),
        embedded_subtitle_detector=embedded_detector,
    )

    orchestrator.process_video_task(task.id)

    event = next(
        event for event in repository.task_events if event["stage"] == "checking_embedded"
    )
    assert event["status"] == "completed"
    assert event["details"] == {
        "subtitle_stream_count": 2,
        "chinese_count": 1,
        "bilingual_count": 1,
    }
    assert event["message"] == "内封字幕检查：共 2 条字幕流，中文 1 条，双语 1 条"


def test_embedded_probe_warning_is_recorded_and_search_continues(tmp_path: Path) -> None:
    video = tmp_path / "Movie.Name.2024.mkv"
    video.write_bytes(b"video")
    task = build_task(video)
    repository = FakeRepository(task)
    registry = FakeProviderRegistry([FakeProvider()], [])
    orchestrator = SubtitleOrchestrator(
        settings=build_settings(tmp_path),
        repository=repository,
        resolver=FakeResolver(video),
        provider_registry=registry,
        embedded_subtitle_detector=FakeEmbeddedDetector(should_raise=True),
    )

    orchestrator.process_video_task(task.id)

    warning = next(
        event
        for event in repository.task_events
        if event["stage"] == "checking_embedded" and event["status"] == "warning"
    )
    assert warning["message"] == "内封字幕检查失败：probe failed，将继续搜索"
    assert warning["details"] == {"error": "probe failed", "continued": True}
    assert len(registry.requests) == 1


def test_orchestrator_records_stage_statuses_and_resolved_path(tmp_path: Path) -> None:
    video = tmp_path / "Movie.Name.2024.mkv"
    video.write_bytes(b"video")
    task = build_task(video)
    repository = FakeRepository(task)
    provider = FakeProvider()
    registry = FakeProviderRegistry([provider], [build_candidate()])
    orchestrator = SubtitleOrchestrator(
        settings=build_settings(tmp_path),
        repository=repository,
        resolver=FakeResolver(video),
        provider_registry=registry,
        embedded_subtitle_detector=FakeEmbeddedDetector(
            EmbeddedSubtitleResult(
                has_chinese=False,
                has_bilingual=False,
                streams=[],
            )
        ),
    )

    orchestrator.process_video_task(task.id)

    assert task.video_path_resolved == str(video)
    assert [status for _, status, _ in repository.status_updates] == [
        "resolving",
        "checking_existing",
        "checking_embedded",
        "searching",
        "downloading",
        "validating",
        "placing",
        "completed",
    ]
    assert [event["stage"] for event in repository.task_events][:5] == [
        "task_source",
        "resolving",
        "checking_existing",
        "checking_embedded",
        "searching",
    ]
    assert [event["status"] for event in repository.task_events[:5]] == [
        "completed",
        "completed",
        "completed",
        "completed",
        "started",
    ]
    assert not any(
        event["stage"] in {"downloading", "validating", "syncing", "placing"}
        for event in repository.task_events
    )


def test_preflight_logs_local_checks_before_summary_and_provider_search(tmp_path: Path) -> None:
    video = tmp_path / "Movie.Name.2024.mkv"
    video.write_bytes(b"video")
    task = build_task(video)
    repository = FakeRepository(task)
    embedded_detector = FakeEmbeddedDetector(
        EmbeddedSubtitleResult(
            has_chinese=False,
            has_bilingual=False,
            streams=[],
        )
    )
    registry = FakeProviderRegistry([FakeProvider()], [])
    orchestrator = SubtitleOrchestrator(
        settings=build_settings(tmp_path),
        repository=repository,
        resolver=FakeResolver(video),
        provider_registry=registry,
        embedded_subtitle_detector=embedded_detector,
    )

    assert orchestrator.preflight_video_task(task.id) is True
    orchestrator.process_video_task(task.id)

    stages = [event["stage"] for event in repository.task_events]
    assert stages[:6] == [
        "task_source",
        "resolving",
        "checking_existing",
        "checking_embedded",
        "preflight",
        "searching",
    ]
    assert stages.count("resolving") == 1
    assert stages.count("checking_existing") == 1
    assert stages.count("checking_embedded") == 1
    assert embedded_detector.calls == [(video, "ffprobe")]
    summary = repository.task_events[4]
    assert summary["message"] == (
        "本地检查完成：路径可用；外挂字幕 0 个，中文 0 个；"
        "内封字幕流 0 条，中文 0 条；等待 Provider 搜索槽位"
    )
    assert summary["details"] == {
        "path": {
            "original_path": str(video),
            "resolved_path": str(video),
            "strategy": "direct",
        },
        "external_subtitles": {
            "subtitle_count": 0,
            "chinese_count": 0,
            "bilingual_count": 0,
        },
        "embedded_subtitles": {
            "subtitle_stream_count": 0,
            "chinese_count": 0,
            "bilingual_count": 0,
        },
    }


def test_search_events_include_media_provider_results_and_summary(tmp_path: Path) -> None:
    video = tmp_path / "Show.S01E02.mkv"
    video.write_bytes(b"video")
    task = build_task(video)
    task.title = "Show"
    task.year = 2025
    task.season = 1
    task.episode = 2
    repository = FakeRepository(task)
    reports = [
        ProviderSearchReport(provider="subliminal:addic7ed", status="started"),
        ProviderSearchReport(
            provider="subliminal:addic7ed",
            status="completed",
            candidate_count=1,
            duration_ms=125,
        ),
        ProviderSearchReport(provider="subliminal:opensubtitles", status="started"),
        ProviderSearchReport(
            provider="subliminal:opensubtitles",
            status="failed",
            duration_ms=80,
            error="服务暂不可用",
        ),
    ]
    registry = FakeProviderRegistry([FakeProvider()], [build_candidate()], reports)
    orchestrator = SubtitleOrchestrator(
        settings=build_settings(tmp_path),
        repository=repository,
        resolver=FakeResolver(video),
        provider_registry=registry,
    )

    orchestrator.process_video_task(task.id)

    search_events = [
        event for event in repository.task_events if event["stage"] == "searching"
    ]
    assert search_events[0]["status"] == "started"
    assert search_events[0]["message"] == (
        "字幕搜索开始：Show (2025) S01E02，语言 zh-cn, zh-hant"
    )
    assert search_events[0]["details"] == {
        "title": "Show",
        "year": 2025,
        "season": 1,
        "episode": 2,
        "episode_code": "S01E02",
        "languages": ["zh-cn", "zh-hant"],
    }
    assert search_events[1]["status"] == "completed"
    assert search_events[1]["message"] == (
        "字幕搜索结束：候选 1 条，来源成功 1 个，失败 1 个，跳过 0 个"
    )
    assert search_events[1]["details"] == {
        "candidate_count": 1,
        "provider_success_count": 1,
        "provider_failure_count": 1,
        "provider_skipped_count": 0,
    }
    provider_events = [
        event for event in repository.task_events if event["stage"] == "provider_search"
    ]
    assert [event["message"] for event in provider_events] == [
        "字幕来源 addic7ed：返回 1 条，耗时 125 ms",
        "字幕来源 opensubtitles：搜索失败，错误 服务暂不可用，耗时 80 ms",
    ]


def test_provider_search_event_records_and_describes_search_context(tmp_path: Path) -> None:
    video = tmp_path / "Movie.2025.mkv"
    video.write_bytes(b"video")
    task = build_task(video)
    repository = FakeRepository(task)
    orchestrator = SubtitleOrchestrator(
        settings=build_settings(tmp_path),
        repository=repository,
        resolver=FakeResolver(video),
        provider_registry=FakeProviderRegistry([], []),
    )
    context = {
        "title": "How to Train Your Dragon",
        "title_source": "original_title",
        "year": 2025,
        "imdb_id": "tt26743210",
        "file_name": "Movie.2025.mkv",
        "media_type": "movie",
    }

    orchestrator._record_provider_search_report(
        task.id,
        ProviderSearchReport(
            provider="subliminal:opensubtitlescom",
            status="completed",
            candidate_count=5,
            duration_ms=1297,
            search_context=context,
        ),
    )

    event = repository.task_events[-1]
    assert event["details"]["search_context"] == context
    assert "原始标题“How to Train Your Dragon”" in event["message"]
    assert "年份 2025" in event["message"]
    assert "IMDb tt26743210" in event["message"]
    assert "文件名“Movie.2025.mkv”" in event["message"]


def test_successful_candidate_validates_places_and_records_artifact(tmp_path: Path) -> None:
    video = tmp_path / "Movie.Name.2024.mkv"
    video.write_bytes(b"video")
    task = build_task(video)
    repository = FakeRepository(task)
    provider = FakeProvider()
    candidate = build_candidate()
    registry = FakeProviderRegistry([provider], [candidate])
    orchestrator = SubtitleOrchestrator(
        settings=build_settings(tmp_path),
        repository=repository,
        resolver=FakeResolver(video),
        provider_registry=registry,
    )

    orchestrator.process_video_task(task.id)

    placed_path = tmp_path / "Movie.Name.2024.zh-cn.default.srt"
    assert repository.status_updates[-1] == (task.id, "completed", None)
    assert len(provider.download_calls) == 1
    assert len(repository.candidate_records) == 1
    assert repository.candidate_records[0]["provider"] == "fake"
    assert repository.candidate_records[0]["video_task_id"] == task.id
    assert len(repository.artifact_records) == 1
    assert repository.artifact_records[0]["kind"] == "placed"
    assert repository.artifact_records[0]["path"] == str(placed_path)
    assert repository.artifact_records[0]["is_synced"] is False
    assert placed_path.read_text(encoding="utf-8") == "1\n00:00:01,000 --> 00:00:02,000\n你好\n"


def test_sync_enabled_places_synced_subtitle_and_records_artifact(tmp_path: Path) -> None:
    video = tmp_path / "Movie.Name.2024.mkv"
    video.write_bytes(b"video")
    task = build_task(video)
    repository = FakeRepository(task)
    provider = FakeProvider(downloaded_content="1\n00:00:01,000 --> 00:00:02,000\n未同步\n")
    candidate = build_candidate()
    registry = FakeProviderRegistry([provider], [candidate])
    synced_content = "1\n00:00:01,000 --> 00:00:02,000\n已同步\n"
    syncer = FakeSyncer(synced_content=synced_content)
    orchestrator = SubtitleOrchestrator(
        settings=build_settings(tmp_path, sync_enabled=True),
        repository=repository,
        resolver=FakeResolver(video),
        provider_registry=registry,
        subtitle_syncer=syncer,
    )

    orchestrator.process_video_task(task.id)

    downloaded_path = tmp_path / "data" / "downloads" / "1" / "downloaded.srt"
    synced_path = tmp_path / "data" / "downloads" / "1" / "downloaded.synced.srt"
    placed_path = tmp_path / "Movie.Name.2024.zh-cn.default.srt"
    assert syncer.calls == [(video, downloaded_path, synced_path)]
    assert repository.status_updates[-1] == (task.id, "completed", None)
    assert repository.artifact_records[0]["path"] == str(placed_path)
    assert repository.artifact_records[0]["is_synced"] is True
    assert placed_path.read_text(encoding="utf-8") == synced_content


def test_sync_failure_marks_task_failed_by_default(tmp_path: Path) -> None:
    video = tmp_path / "Movie.Name.2024.mkv"
    video.write_bytes(b"video")
    task = build_task(video)
    repository = FakeRepository(task)
    provider = FakeProvider()
    candidate = build_candidate()
    registry = FakeProviderRegistry([provider], [candidate])
    syncer = FakeSyncer(success=False)
    orchestrator = SubtitleOrchestrator(
        settings=build_settings(tmp_path, sync_enabled=True),
        repository=repository,
        resolver=FakeResolver(video),
        provider_registry=registry,
        subtitle_syncer=syncer,
    )

    orchestrator.process_video_task(task.id)

    placed_path = tmp_path / "Movie.Name.2024.zh-cn.default.srt"
    assert repository.status_updates[-1] == (task.id, "failed", "sync_failed")
    assert repository.artifact_records == []
    assert repository.candidate_attempt_updates == [
        {
            "candidate_id": 1,
            "status": "running",
            "error_message": None,
            "attempts": None,
            "increment": True,
        },
        {
            "candidate_id": 1,
            "status": "failed",
            "error_message": "sync_failed",
            "attempts": None,
            "increment": False,
        },
    ]
    assert placed_path.exists() is False


def test_sync_failure_can_save_unsynced_subtitle_when_configured(tmp_path: Path) -> None:
    video = tmp_path / "Movie.Name.2024.mkv"
    video.write_bytes(b"video")
    task = build_task(video)
    repository = FakeRepository(task)
    provider = FakeProvider(downloaded_content="1\n00:00:01,000 --> 00:00:02,000\n未同步\n")
    candidate = build_candidate()
    registry = FakeProviderRegistry([provider], [candidate])
    syncer = FakeSyncer(success=False)
    orchestrator = SubtitleOrchestrator(
        settings=build_settings(
            tmp_path,
            sync_enabled=True,
            save_unsynced_on_sync_failure=True,
        ),
        repository=repository,
        resolver=FakeResolver(video),
        provider_registry=registry,
        subtitle_syncer=syncer,
    )

    orchestrator.process_video_task(task.id)

    placed_path = tmp_path / "Movie.Name.2024.zh-cn.default.srt"
    assert repository.status_updates[-1] == (task.id, "completed", None)
    assert repository.artifact_records[0]["path"] == str(placed_path)
    assert repository.artifact_records[0]["is_synced"] is False
    assert placed_path.read_text(encoding="utf-8") == "1\n00:00:01,000 --> 00:00:02,000\n未同步\n"


def test_low_quality_alignment_never_saves_unsynced_subtitle(tmp_path: Path) -> None:
    video = tmp_path / "Episode.S02E02.mkv"
    video.write_bytes(b"video")
    task = build_task(video)
    repository = FakeRepository(task)
    provider = FakeProvider(downloaded_content="1\n00:00:01,000 --> 00:00:02,000\n错误字幕\n")
    registry = FakeProviderRegistry([provider], [build_candidate()])
    syncer = FakeSyncer(
        success=False,
        reason="low_quality_alignment",
        score=-24976.575,
    )
    orchestrator = SubtitleOrchestrator(
        settings=build_settings(
            tmp_path,
            sync_enabled=True,
            save_unsynced_on_sync_failure=True,
        ),
        repository=repository,
        resolver=FakeResolver(video),
        provider_registry=registry,
        subtitle_syncer=syncer,
    )

    orchestrator.process_video_task(task.id)

    assert repository.status_updates[-1] == (
        task.id,
        "failed",
        "low_quality_alignment",
    )
    assert repository.artifact_records == []
    sync_events = [event for event in repository.task_events if event["stage"] == "candidate_sync"]
    assert sync_events[-1]["details"]["sync_score"] == -24976.575


@dataclass
class RetryAwareProvider:
    name: str = "fake"
    download_plan: dict[str, str | Exception] = field(default_factory=dict)
    download_calls: list[tuple[SubtitleCandidate, Path]] = field(default_factory=list)

    def download(self, candidate: SubtitleCandidate, target_dir: Path) -> DownloadedSubtitle:
        self.download_calls.append((candidate, target_dir))
        target_dir.mkdir(parents=True, exist_ok=True)
        outcome = self.download_plan.get(candidate.source_url)
        if isinstance(outcome, Exception):
            raise outcome
        path = target_dir / f"{candidate.title}.{candidate.format}"
        path.write_text(
            outcome or "1\n00:00:01,000 --> 00:00:02,000\n浣犲ソ\n",
            encoding="utf-8",
        )
        return DownloadedSubtitle(candidate=candidate, path=path)


class LazyBatchRegistry:
    def __init__(
        self,
        providers: list[FakeProvider],
        batches: list[tuple[str, list[SubtitleCandidate]]],
        *,
        wait_before: dict[str, float] | None = None,
    ) -> None:
        self.providers = providers
        self._batches = batches
        self.wait_before = wait_before or {}
        self.requests: list[Any] = []
        self.searched_providers: list[str] = []
        self.search_reports: list[ProviderSearchReport] = []
        self.reporter = None

    def set_reporter(self, reporter) -> None:
        self.reporter = reporter

    def search(self, request: Any) -> list[SubtitleCandidate]:
        raise AssertionError("legacy search should not be used when search_batches exists")

    def search_batches(self, request: Any, on_wait=None):
        self.requests.append(request)
        self.search_reports = []

        def iterate():
            for provider, candidates in self._batches:
                wait_seconds = self.wait_before.get(provider)
                if wait_seconds is not None and on_wait is not None:
                    on_wait(provider, wait_seconds, 999999.0)
                self.searched_providers.append(provider)
                report = ProviderSearchReport(
                    provider=provider,
                    status="completed",
                    candidate_count=len(candidates),
                )
                self.search_reports.append(report)
                if self.reporter is not None:
                    self.reporter(report)
                if candidates:
                    yield list(candidates)

        return iterate()


def _retry_settings(
    tmp_path: Path,
    *,
    max_candidate_attempts: int = 4,
) -> AppSettings:
    return AppSettings(
        data_dir=tmp_path / "data",
        sync={"enabled": False},
        subtitles={"max_candidate_attempts": max_candidate_attempts},
    )


def _ranked_retry_candidates() -> tuple[SubtitleCandidate, SubtitleCandidate]:
    return (
        SubtitleCandidate(
            provider="fake",
            language="zh-cn",
            is_bilingual=True,
            format="srt",
            title="Best match",
            source_url="https://example.invalid/best.srt",
            release_info="WEB-DL",
            confidence=0.95,
            raw_metadata={},
        ),
        SubtitleCandidate(
            provider="fake",
            language="zh-cn",
            is_bilingual=False,
            format="srt",
            title="Fallback match",
            source_url="https://example.invalid/fallback.srt",
            release_info="WEB-DL",
            confidence=0.5,
            raw_metadata={},
        ),
    )


def _provider_candidate(provider: str, suffix: str) -> SubtitleCandidate:
    return replace(
        build_candidate(),
        provider=provider,
        title=f"Movie Name {suffix}",
        source_url=f"https://example.invalid/{provider}/{suffix}",
        raw_metadata={"subtitle_id": suffix},
    )


def test_lazy_batches_stop_after_first_provider_candidate_succeeds(tmp_path: Path) -> None:
    video = tmp_path / "Movie.Name.2024.mkv"
    video.write_bytes(b"video")
    task = build_task(video)
    repository = FakeRepository(task)
    high = FakeProvider(name="high")
    low = FakeProvider(name="low")
    registry = LazyBatchRegistry(
        [high, low],
        [
            ("high", [_provider_candidate("high", "1")]),
            ("low", [_provider_candidate("low", "1")]),
        ],
    )
    orchestrator = SubtitleOrchestrator(
        settings=_retry_settings(tmp_path),
        repository=repository,
        resolver=FakeResolver(video),
        provider_registry=registry,
    )

    orchestrator.process_video_task(task.id)

    assert task.status == "completed"
    assert registry.searched_providers == ["high"]
    assert len(high.download_calls) == 1
    assert low.download_calls == []


def test_lazy_batches_resume_lower_provider_after_first_batch_fails(tmp_path: Path) -> None:
    video = tmp_path / "Movie.Name.2024.mkv"
    video.write_bytes(b"video")
    task = build_task(video)
    repository = FakeRepository(task)
    high = FakeProvider(name="high", downloaded_content="1\n无时间轴\n")
    low = FakeProvider(name="low")
    registry = LazyBatchRegistry(
        [high, low],
        [
            ("high", [_provider_candidate("high", "1")]),
            ("low", [_provider_candidate("low", "1")]),
        ],
    )
    orchestrator = SubtitleOrchestrator(
        settings=_retry_settings(tmp_path),
        repository=repository,
        resolver=FakeResolver(video),
        provider_registry=registry,
    )

    orchestrator.process_video_task(task.id)

    assert task.status == "completed"
    assert registry.searched_providers == ["high", "low"]
    assert len(high.download_calls) == 1
    assert len(low.download_calls) == 1


def test_failed_cached_candidate_falls_through_to_provider_batch(tmp_path: Path) -> None:
    video = tmp_path / "Show.S01E02.mkv"
    video.write_bytes(b"video")
    task = build_task(video)
    task.season = 1
    task.episode = 2
    repository = FakeRepository(task)
    cached_candidate = replace(
        _provider_candidate("cache-source", "cached"),
        raw_metadata={"bundle_reused": True},
    )
    bundle_cache = FakeBundleCache(cached_candidate, fail_materialize=True)
    provider = FakeProvider(name="low")
    registry = LazyBatchRegistry(
        [provider],
        [("low", [_provider_candidate("low", "1")])],
    )
    orchestrator = SubtitleOrchestrator(
        settings=_retry_settings(tmp_path),
        repository=repository,
        resolver=FakeResolver(video),
        provider_registry=registry,
        bundle_cache=bundle_cache,
    )

    orchestrator.process_video_task(task.id)

    assert task.status == "completed"
    assert bundle_cache.materialize_calls == 1
    assert registry.searched_providers == ["low"]
    assert len(provider.download_calls) == 1


def test_lazy_batches_share_global_candidate_attempt_budget(tmp_path: Path) -> None:
    video = tmp_path / "Movie.Name.2024.mkv"
    video.write_bytes(b"video")
    task = build_task(video)
    repository = FakeRepository(task)
    high = FakeProvider(name="high", downloaded_content="1\n无时间轴\n")
    low = FakeProvider(name="low", downloaded_content="1\n无时间轴\n")
    never = FakeProvider(name="never", downloaded_content="1\n无时间轴\n")
    registry = LazyBatchRegistry(
        [high, low, never],
        [
            (
                "high",
                [_provider_candidate("high", "1"), _provider_candidate("high", "2")],
            ),
            (
                "low",
                [
                    _provider_candidate("low", "1"),
                    _provider_candidate("low", "2"),
                    _provider_candidate("low", "3"),
                ],
            ),
            ("never", [_provider_candidate("never", "1")]),
        ],
    )
    orchestrator = SubtitleOrchestrator(
        settings=_retry_settings(tmp_path, max_candidate_attempts=4),
        repository=repository,
        resolver=FakeResolver(video),
        provider_registry=registry,
    )

    orchestrator.process_video_task(task.id)

    assert task.status == "failed"
    assert task.error_message == "missing_timestamps"
    assert registry.searched_providers == ["high", "low"]
    assert len(high.download_calls) == 2
    assert len(low.download_calls) == 2
    assert never.download_calls == []
    assert len(repository.candidate_records) == 4
    assert len(
        [
            event
            for event in repository.task_events
            if event["stage"] == "searching" and event["status"] == "completed"
        ]
    ) == 1
    assert len(
        [
            event
            for event in repository.task_events
            if event["stage"] == "task" and event["status"] == "failed"
        ]
    ) == 1


def test_search_batches_provider_wait_records_rounded_structured_event(tmp_path: Path) -> None:
    video = tmp_path / "Movie.Name.2024.mkv"
    video.write_bytes(b"video")
    task = build_task(video)
    repository = FakeRepository(task)
    provider = FakeProvider(name="high")
    registry = LazyBatchRegistry(
        [provider],
        [("high", [_provider_candidate("high", "1")])],
        wait_before={"high": 1.2},
    )
    orchestrator = SubtitleOrchestrator(
        settings=_retry_settings(tmp_path),
        repository=repository,
        resolver=FakeResolver(video),
        provider_registry=registry,
    )

    orchestrator.process_video_task(task.id)

    event = next(
        event for event in repository.task_events if event["stage"] == "provider_wait"
    )
    assert event["status"] == "waiting"
    assert event["message"] == "Provider high 冷却，约 2 秒后获得搜索槽位"
    assert event["details"] == {"provider": "high", "wait_seconds": 2}
    assert "ready_at" not in event["details"]


def test_retry_uses_next_ranked_candidate_after_invalid_download(tmp_path: Path) -> None:
    video = tmp_path / "Movie.Name.2024.mkv"
    video.write_bytes(b"video")
    task = build_task(video)
    repository = FakeRepository(task)
    first_candidate, second_candidate = _ranked_retry_candidates()
    provider = RetryAwareProvider(
        download_plan={
            first_candidate.source_url: "1\n浣犲ソ\n",
            second_candidate.source_url: "1\n00:00:01,000 --> 00:00:02,000\n浣犲ソ\n",
        }
    )
    registry = FakeProviderRegistry([provider], [second_candidate, first_candidate])
    orchestrator = SubtitleOrchestrator(
        settings=_retry_settings(tmp_path),
        repository=repository,
        resolver=FakeResolver(video),
        provider_registry=registry,
    )

    orchestrator.process_video_task(task.id)

    assert [call[0].source_url for call in provider.download_calls] == [
        first_candidate.source_url,
        second_candidate.source_url,
    ]
    assert repository.status_updates[-1] == (task.id, "completed", None)
    assert repository.candidate_attempt_updates == [
        {
            "candidate_id": 1,
            "status": "running",
            "error_message": None,
            "attempts": None,
            "increment": True,
        },
        {
            "candidate_id": 1,
            "status": "failed",
            "error_message": "missing_timestamps",
            "attempts": None,
            "increment": False,
        },
        {
            "candidate_id": 2,
            "status": "running",
            "error_message": None,
            "attempts": None,
            "increment": True,
        },
        {
            "candidate_id": 2,
            "status": "completed",
            "error_message": None,
            "attempts": None,
            "increment": False,
        },
    ]


def test_retry_uses_next_ranked_candidate_after_download_exception(tmp_path: Path) -> None:
    video = tmp_path / "Movie.Name.2024.mkv"
    video.write_bytes(b"video")
    task = build_task(video)
    repository = FakeRepository(task)
    first_candidate, second_candidate = _ranked_retry_candidates()
    provider = RetryAwareProvider(
        download_plan={
            first_candidate.source_url: RuntimeError("provider_unavailable"),
        }
    )
    registry = FakeProviderRegistry([provider], [second_candidate, first_candidate])
    orchestrator = SubtitleOrchestrator(
        settings=_retry_settings(tmp_path),
        repository=repository,
        resolver=FakeResolver(video),
        provider_registry=registry,
    )

    orchestrator.process_video_task(task.id)

    assert [call[0].source_url for call in provider.download_calls] == [
        first_candidate.source_url,
        second_candidate.source_url,
    ]
    assert repository.status_updates[-1] == (task.id, "completed", None)
    assert repository.candidate_attempt_updates == [
        {
            "candidate_id": 1,
            "status": "running",
            "error_message": None,
            "attempts": None,
            "increment": True,
        },
        {
            "candidate_id": 1,
            "status": "failed",
            "error_message": "provider_unavailable",
            "attempts": None,
            "increment": False,
        },
        {
            "candidate_id": 2,
            "status": "running",
            "error_message": None,
            "attempts": None,
            "increment": True,
        },
        {
            "candidate_id": 2,
            "status": "completed",
            "error_message": None,
            "attempts": None,
            "increment": False,
        },
    ]


def test_max_candidate_attempts_limits_retry_and_uses_last_failure_reason(tmp_path: Path) -> None:
    video = tmp_path / "Movie.Name.2024.mkv"
    video.write_bytes(b"video")
    task = build_task(video)
    repository = FakeRepository(task)
    first_candidate, second_candidate = _ranked_retry_candidates()
    provider = RetryAwareProvider(
        download_plan={
            first_candidate.source_url: RuntimeError("provider_unavailable"),
        }
    )
    registry = FakeProviderRegistry([provider], [second_candidate, first_candidate])
    orchestrator = SubtitleOrchestrator(
        settings=_retry_settings(tmp_path, max_candidate_attempts=1),
        repository=repository,
        resolver=FakeResolver(video),
        provider_registry=registry,
    )

    orchestrator.process_video_task(task.id)

    assert [call[0].source_url for call in provider.download_calls] == [
        first_candidate.source_url,
    ]
    assert repository.status_updates[-1] == (task.id, "failed", "provider_unavailable")
    assert repository.artifact_records == []


def test_manual_retry_filters_previous_placed_candidate_before_download(tmp_path: Path) -> None:
    video = tmp_path / "Movie.Name.2024.mkv"
    video.write_bytes(b"video")
    task = build_task(video)
    task.job = SimpleNamespace(
        source="manual-retry",
        raw_payload_json={"retry_of_task_id": 41},
    )
    repository = FakeRepository(task)
    repository.placed_candidates_by_task[41] = [
        SimpleNamespace(
            provider="fake",
            raw_metadata_json={"subtitle_id": 100},
            source_url="https://example.invalid/old-url",
        )
    ]
    previous = replace(
        build_candidate(),
        provider="fake",
        title="Previous subtitle",
        source_url="https://example.invalid/new-url-for-same-id",
        confidence=0.99,
        raw_metadata={"subtitle_id": 100},
    )
    next_candidate = replace(
        build_candidate(),
        provider="fake",
        title="Next subtitle",
        source_url="https://example.invalid/next",
        confidence=0.5,
        raw_metadata={"subtitle_id": 101},
    )
    provider = RetryAwareProvider(name="fake")
    orchestrator = SubtitleOrchestrator(
        settings=_retry_settings(tmp_path),
        repository=repository,
        resolver=FakeResolver(video),
        provider_registry=FakeProviderRegistry(
            [provider],
            [next_candidate, previous],
        ),
    )

    orchestrator.process_video_task(task.id)

    assert [call[0].source_url for call in provider.download_calls] == [
        next_candidate.source_url
    ]
    assert [record["source_url"] for record in repository.candidate_records] == [
        next_candidate.source_url
    ]
    event = next(
        event
        for event in repository.task_events
        if event["error_code"] == "retry_candidate_already_used"
    )
    assert event["details"] == {
        "provider": "fake",
        "title": "Previous subtitle",
        "link": previous.source_url,
        "source_url": previous.source_url,
        "identity": "fake|id:100",
        "retry_of_task_id": 41,
        "excluded_from_task_id": 41,
    }


def test_manual_retry_without_previous_placed_candidate_does_not_filter(tmp_path: Path) -> None:
    video = tmp_path / "Movie.Name.2024.mkv"
    video.write_bytes(b"video")
    task = build_task(video)
    task.job = SimpleNamespace(
        source="manual-retry",
        raw_payload_json={"retry_of_task_id": 41},
    )
    candidate = replace(build_candidate(), raw_metadata={"subtitle_id": 100})
    repository = FakeRepository(task)
    provider = RetryAwareProvider(name="fake")
    orchestrator = SubtitleOrchestrator(
        settings=_retry_settings(tmp_path),
        repository=repository,
        resolver=FakeResolver(video),
        provider_registry=FakeProviderRegistry([provider], [candidate]),
    )

    orchestrator.process_video_task(task.id)

    assert len(provider.download_calls) == 1
    assert not any(
        event["error_code"] == "retry_candidate_already_used"
        for event in repository.task_events
    )


def test_manual_retry_walks_failed_retry_chain_to_exclude_ancestor_candidate(
    tmp_path: Path,
) -> None:
    video = tmp_path / "Movie.Name.2024.mkv"
    video.write_bytes(b"video")
    task = build_task(video)
    task.id = 30
    task.job = SimpleNamespace(
        source="manual-retry",
        raw_payload_json={"retry_of_task_id": 20},
    )
    repository = FakeRepository(task)
    repository.retry_parent_by_task[20] = 10
    repository.placed_candidates_by_task[10] = [
        SimpleNamespace(
            provider="fake",
            raw_metadata_json={"subtitle_id": 100},
            source_url="https://example.invalid/from-a",
        )
    ]
    ancestor_candidate = replace(
        build_candidate(),
        provider="fake",
        title="Candidate from A",
        source_url="https://example.invalid/same-id-new-url",
        raw_metadata={"subtitle_id": 100},
    )
    provider = RetryAwareProvider(name="fake")
    orchestrator = SubtitleOrchestrator(
        settings=_retry_settings(tmp_path),
        repository=repository,
        resolver=FakeResolver(video),
        provider_registry=FakeProviderRegistry([provider], [ancestor_candidate]),
    )

    orchestrator.process_video_task(task.id)

    assert provider.download_calls == []
    event = next(
        event
        for event in repository.task_events
        if event["error_code"] == "retry_candidate_already_used"
    )
    assert event["details"]["retry_of_task_id"] == 20
    assert event["details"]["excluded_from_task_id"] == 10
    assert event["details"]["identity"] == "fake|id:100"


def test_manual_retry_stops_on_retry_chain_cycle(tmp_path: Path) -> None:
    video = tmp_path / "Movie.Name.2024.mkv"
    video.write_bytes(b"video")
    task = build_task(video)
    task.id = 30
    task.job = SimpleNamespace(
        source="manual-retry",
        raw_payload_json={"retry_of_task_id": 20},
    )
    repository = FakeRepository(task)
    repository.retry_parent_by_task = {20: 10, 10: 20}
    candidate = build_candidate()
    provider = RetryAwareProvider(name="fake")
    orchestrator = SubtitleOrchestrator(
        settings=_retry_settings(tmp_path),
        repository=repository,
        resolver=FakeResolver(video),
        provider_registry=FakeProviderRegistry([provider], [candidate]),
    )

    orchestrator.process_video_task(task.id)

    assert repository.retry_parent_calls == [20, 10]
    assert len(provider.download_calls) == 1


def test_manual_retry_different_identity_still_uses_content_duplicate_fallback(
    tmp_path: Path,
) -> None:
    video = tmp_path / "Movie.Name.2024.mkv"
    video.write_bytes(b"video")
    content = "1\n00:00:01,000 --> 00:00:02,000\n相同字幕\n"
    (tmp_path / "Movie.Name.2024.zh-cn.default.srt").write_text(content, encoding="utf-8")
    task = build_task(video)
    task.job = SimpleNamespace(
        source="manual-retry",
        raw_payload_json={"retry_of_task_id": 41},
    )
    repository = FakeRepository(task)
    repository.placed_candidates_by_task[41] = [
        SimpleNamespace(
            provider="fake",
            raw_metadata_json={"subtitle_id": 100},
            source_url="https://example.invalid/old",
        )
    ]
    candidate = replace(
        build_candidate(),
        raw_metadata={"subtitle_id": 101},
        source_url="https://example.invalid/new",
    )
    provider = RetryAwareProvider(
        name="fake",
        download_plan={candidate.source_url: content},
    )
    orchestrator = SubtitleOrchestrator(
        settings=_retry_settings(tmp_path),
        repository=repository,
        resolver=FakeResolver(video),
        provider_registry=FakeProviderRegistry([provider], [candidate]),
    )

    orchestrator.process_video_task(task.id)

    assert len(provider.download_calls) == 1
    assert repository.status_updates[-1] == (
        task.id,
        "failed",
        "duplicate_existing_subtitle",
    )


def test_placement_failure_marks_candidate_attempt_failed(tmp_path: Path, monkeypatch) -> None:
    video = tmp_path / "Movie.Name.2024.mkv"
    video.write_bytes(b"video")
    placed_path = tmp_path / "Movie.Name.2024.zh-cn.default.srt"
    task = build_task(video)
    repository = FakeRepository(task)
    provider = FakeProvider()
    candidate = build_candidate()
    registry = FakeProviderRegistry([provider], [candidate])

    def fail_placement(*args: Any, **kwargs: Any) -> Path:
        raise FileExistsError(f"destination already exists: {placed_path}")

    monkeypatch.setattr("subtitle_sidecar.pipeline.orchestrator.safe_place_subtitle", fail_placement)
    orchestrator = SubtitleOrchestrator(
        settings=build_settings(tmp_path),
        repository=repository,
        resolver=FakeResolver(video),
        provider_registry=registry,
    )

    orchestrator.process_video_task(task.id)

    assert repository.status_updates[-1] == (
        task.id,
        "failed",
        f"destination already exists: {placed_path}",
    )
    assert repository.candidate_attempt_updates == [
        {
            "candidate_id": 1,
            "status": "running",
            "error_message": None,
            "attempts": None,
            "increment": True,
        },
        {
            "candidate_id": 1,
            "status": "failed",
            "error_message": f"destination already exists: {placed_path}",
            "attempts": None,
            "increment": False,
        },
    ]
    assert repository.artifact_records == []


def test_non_json_provider_metadata_does_not_break_candidate_recording(
    tmp_path: Path,
) -> None:
    video = tmp_path / "Movie.Name.2024.mkv"
    video.write_bytes(b"video")
    engine = create_sqlite_engine(f"sqlite:///{tmp_path / 'state.sqlite3'}")
    create_tables(engine)
    candidate = SubtitleCandidate(
        provider="fake",
        language="zh-cn",
        is_bilingual=True,
        format="srt",
        title="Movie Name bilingual",
        source_url="https://example.invalid/subtitle.srt",
        release_info="WEB-DL",
        confidence=0.9,
        raw_metadata={
            "subtitle": NonJsonSubtitle(),
            "metadata": {"kind": "bilingual"},
        },
    )
    provider = FakeProvider()
    registry = FakeProviderRegistry([provider], [candidate])

    with session_scope(engine) as session:
        repository = Repository(session)
        job = repository.create_job(
            JobCreate(
                source="test",
                raw_payload={"physical_video_file_full_path": str(video)},
                video_path_original=str(video),
            )
        )
        task_id = job.video_tasks[0].id
        orchestrator = SubtitleOrchestrator(
            settings=build_settings(tmp_path),
            repository=repository,
            resolver=FakeResolver(video),
            provider_registry=registry,
        )

        orchestrator.process_video_task(task_id)

    with session_scope(engine) as session:
        repository = Repository(session)
        task = repository.get_video_task(task_id)

    assert task is not None
    assert task.status == "completed"
    assert task.candidates[0].raw_metadata_json["subtitle"] == "OpenSubtitlesSubtitle(test)"
    assert task.candidates[0].raw_metadata_json["metadata"] == {"kind": "bilingual"}
    assert len(task.candidates[0].raw_metadata_json["content_sha256"]) == 64
    assert len(task.candidates[0].raw_metadata_json["text_fingerprint"]) == 64


def test_search_request_uses_cached_jellyfin_ids_and_original_title(tmp_path: Path) -> None:
    video = tmp_path / "Localized.Name.2025.mkv"
    video.write_bytes(b"video")
    task = build_task(video)
    task.media_server_id = "jellyfin-item-id"
    repository = FakeRepository(task)
    repository.get_jellyfin_media_item = lambda item_id: SimpleNamespace(
        jellyfin_item_id=item_id,
        original_title="Original English Title",
        provider_ids_json={"Imdb": "tt1234567", "Tmdb": "7654321"},
    )
    orchestrator = SubtitleOrchestrator(
        settings=build_settings(tmp_path),
        repository=repository,
        resolver=FakeResolver(video),
        provider_registry=FakeProviderRegistry([FakeProvider()], []),
    )

    request = orchestrator._build_search_request(task, video)

    assert request.imdb_id == "tt1234567"
    assert request.tmdb_id == "7654321"
    assert request.original_title == "Original English Title"


def test_episode_search_request_strips_episode_suffix_from_original_title(tmp_path: Path) -> None:
    video = tmp_path / "Localized.Name.S01E04.mkv"
    video.write_bytes(b"video")
    task = build_task(video)
    task.season = 1
    task.episode = 4
    task.media_server_id = "jellyfin-item-id"
    repository = FakeRepository(task)
    repository.get_jellyfin_media_item = lambda item_id: SimpleNamespace(
        jellyfin_item_id=item_id,
        original_title="Ted - S01E04",
        provider_ids_json={},
    )
    orchestrator = SubtitleOrchestrator(
        settings=build_settings(tmp_path),
        repository=repository,
        resolver=FakeResolver(video),
        provider_registry=FakeProviderRegistry([FakeProvider()], []),
    )

    request = orchestrator._build_search_request(task, video)

    assert request.original_title == "Ted"


def test_episode_search_request_uses_parent_series_identity(tmp_path: Path) -> None:
    video = tmp_path / "本地剧名 Local Show - S02E04 - 2160p.mkv"
    video.write_bytes(b"video")
    task = build_task(video)
    task.title = "本地剧名 Local Show - S02E04 - 2160p"
    task.year = None
    task.season = 2
    task.episode = 4
    task.media_server_id = "episode-id"
    repository = FakeRepository(task)
    episode = SimpleNamespace(
        jellyfin_item_id="episode-id",
        series_id="series-id",
        series_name="本地剧名",
        original_title="Episode Four",
        year=2026,
        provider_ids_json={"Tmdb": "episode-tmdb"},
    )
    series = SimpleNamespace(
        jellyfin_item_id="series-id",
        name="本地剧名",
        original_title="Local Show",
        year=2025,
        provider_ids_json={"Imdb": "tt1234567", "Tmdb": "series-tmdb"},
    )
    repository.get_jellyfin_media_item = lambda item_id: {
        "episode-id": episode,
        "series-id": series,
    }.get(item_id)
    orchestrator = SubtitleOrchestrator(
        settings=build_settings(tmp_path),
        repository=repository,
        resolver=FakeResolver(video),
        provider_registry=FakeProviderRegistry([FakeProvider()], []),
    )

    request = orchestrator._build_search_request(task, video)

    assert request.title == "本地剧名"
    assert request.original_title == "Local Show"
    assert request.year == 2025
    assert request.imdb_id == "tt1234567"
    assert request.tmdb_id == "series-tmdb"
    assert request.series_id == "series-id"


def test_path_identity_fills_moviepilot_episode_metadata(tmp_path: Path) -> None:
    series_dir = tmp_path / "悬案 Unsettled Case (2026)"
    season_dir = series_dir / "Season 1"
    season_dir.mkdir(parents=True)
    video = season_dir / "悬案 Unsettled Case - S01E14 - 第 14 集 - 1080p.mkv"
    video.write_bytes(b"video")
    task = build_task(video)
    task.title = None
    task.year = None
    task.season = None
    task.episode = None
    repository = FakeRepository(task)
    orchestrator = SubtitleOrchestrator(
        settings=build_settings(tmp_path),
        repository=repository,
        resolver=FakeResolver(video),
        provider_registry=FakeProviderRegistry([FakeProvider()], []),
    )

    orchestrator._enrich_task_identity_from_path(task, video)
    request = orchestrator._build_search_request(task, video)

    assert task.title == "悬案 Unsettled Case"
    assert task.year == 2026
    assert task.season == 1
    assert task.episode == 14
    assert request.media_type == "episode"
    assert request.title == "悬案 Unsettled Case"
    assert request.season == 1
    assert request.episode == 14
    assert repository.task_events[-1]["details"]["source"] == "path"


def test_process_video_task_refreshes_jellyfin_item_by_path_after_success(tmp_path: Path) -> None:
    video = tmp_path / "Movie.2025.mkv"
    video.write_bytes(b"video")
    task = build_task(video)
    repository = FakeRepository(task)
    repository.settings["jellyfin"] = {
        "server_url": "http://jellyfin.test",
        "api_key": "secret",
        "user_id": "user-1",
    }
    repository.jellyfin_item = SimpleNamespace(
        jellyfin_item_id="jf-1",
        path=str(video),
        subtitle_status="missing",
        has_external_chinese_subtitle=False,
        has_embedded_chinese_subtitle=False,
        has_bilingual_subtitle=False,
    )
    candidate = build_candidate()
    provider = FakeProvider()
    registry = FakeProviderRegistry([provider], [candidate])
    refresh_calls: list[str] = []
    get_item_calls: list[str] = []

    class FakeJellyfinClient:
        def __init__(self, **_: Any) -> None:
            pass

        def refresh_item(self, item_id: str) -> None:
            refresh_calls.append(item_id)

        def get_item(self, item_id: str) -> dict[str, Any]:
            get_item_calls.append(item_id)
            return {
                "id": item_id,
                "path": str(video),
                "media_streams": [],
            }

    orchestrator = SubtitleOrchestrator(
        settings=build_settings(tmp_path),
        repository=repository,
        resolver=FakeResolver(video),
        provider_registry=registry,
        jellyfin_client_factory=FakeJellyfinClient,
    )

    orchestrator.process_video_task(task.id)

    assert task.status == "completed"
    assert repository.jellyfin_mark_ready_calls == [
        {"jellyfin_item_id": "jf-1", "path": str(video)}
    ]
    assert refresh_calls == ["jf-1"]
    assert get_item_calls == ["jf-1"]
    assert repository.jellyfin_state_updates == [
        {
            "jellyfin_item_id": "jf-1",
            "path": str(video),
            "subtitle_status": "has_chinese",
            "has_external_chinese_subtitle": True,
            "has_embedded_chinese_subtitle": False,
            "has_bilingual_subtitle": False,
        }
    ]
