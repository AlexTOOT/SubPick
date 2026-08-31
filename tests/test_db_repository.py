import pytest

from subtitle_sidecar.db.models import Job
from subtitle_sidecar.db.repository import JellyfinMediaItemData, JobCreate, Repository
from subtitle_sidecar.db.session import create_sqlite_engine, create_tables, session_scope


def test_create_job_and_video_task(tmp_path) -> None:
    engine = create_sqlite_engine(f"sqlite:///{tmp_path / 'test.sqlite3'}")
    create_tables(engine)

    with session_scope(engine) as session:
        repo = Repository(session)
        job = repo.create_job(
            JobCreate(
                source="moviepilot-csf",
                raw_payload={"physical_video_file_full_path": "/media/A.mkv"},
                video_path_original="/media/A.mkv",
            )
        )

        loaded = repo.get_job(job.id)

    assert loaded is not None
    assert loaded.status == "queued"
    assert loaded.error_message is None
    assert loaded.raw_payload_json == {"physical_video_file_full_path": "/media/A.mkv"}
    assert len(loaded.video_tasks) == 1
    task = loaded.video_tasks[0]
    assert task.video_path_original == "/media/A.mkv"
    assert task.video_path_resolved is None
    assert task.media_server_id is None
    assert task.title is None
    assert task.year is None
    assert task.season is None
    assert task.episode is None
    assert task.result_subtitle_path is None


def test_update_video_task_status_persists_error_message(tmp_path) -> None:
    engine = create_sqlite_engine(f"sqlite:///{tmp_path / 'test.sqlite3'}")
    create_tables(engine)

    with session_scope(engine) as session:
        repo = Repository(session)
        job = repo.create_job(
            JobCreate(
                source="moviepilot-csf",
                raw_payload={"physical_video_file_full_path": "/media/A.mkv"},
                video_path_original="/media/A.mkv",
            )
        )
        task_id = job.video_tasks[0].id

        updated = repo.update_video_task_status(task_id, "failed", "video_not_found")

        assert updated.status == "failed"
        assert updated.error_message == "video_not_found"

    with session_scope(engine) as session:
        repo = Repository(session)
        loaded = repo.get_job(job.id)

    assert loaded is not None
    assert loaded.status == "failed"
    assert loaded.video_tasks[0].status == "failed"
    assert loaded.video_tasks[0].error_message == "video_not_found"


def test_task_status_transitions_create_concise_system_events(tmp_path) -> None:
    engine = create_sqlite_engine(f"sqlite:///{tmp_path / 'test.sqlite3'}")
    create_tables(engine)

    with session_scope(engine) as session:
        repo = Repository(session)
        job = repo.create_job(
            JobCreate(
                source="manual",
                raw_payload={},
                video_path_original="/media/Movie.mkv",
            )
        )
        task_id = job.video_tasks[0].id
        repo.update_video_task_status(task_id, "resolving")
        repo.update_video_task_status(task_id, "searching")
        repo.update_video_task_status(task_id, "failed", "no candidates")
        events = repo.list_system_events(category="task", task_id=task_id)

    assert [event.event for event in events] == ["task_started", "task_failed"]
    assert events[0].message == f"任务 #{task_id} 开始：Movie.mkv"
    assert events[1].message == f"任务 #{task_id} 失败：no candidates"
    assert events[1].level == "ERROR"


def test_system_events_are_persistent_filterable_and_prunable(tmp_path) -> None:
    engine = create_sqlite_engine(f"sqlite:///{tmp_path / 'test.sqlite3'}")
    create_tables(engine)

    with session_scope(engine) as session:
        repo = Repository(session)
        first = repo.record_system_event(
            category="system",
            event="system_started",
            message="started",
        )
        second = repo.record_system_event(
            category="health",
            event="health_check_completed",
            level="WARNING",
            message="warning",
        )
        assert [event.id for event in repo.list_system_events(after_id=first.id)] == [second.id]
        assert [event.id for event in repo.list_system_events(level="warning")] == [second.id]
        assert repo.prune_system_events(retention_days=30, max_entries=1) == 1
        assert [event.id for event in repo.list_system_events()] == [second.id]


def test_resolving_task_status_refreshes_parent_job_summary(tmp_path) -> None:
    engine = create_sqlite_engine(f"sqlite:///{tmp_path / 'test.sqlite3'}")
    create_tables(engine)

    with session_scope(engine) as session:
        repo = Repository(session)
        job = repo.create_job(
            JobCreate(
                source="moviepilot-csf",
                raw_payload={"physical_video_file_full_path": "/media/A.mkv"},
                video_path_original="/media/A.mkv",
            )
        )
        job_id = job.id
        task_id = job.video_tasks[0].id

        repo.update_video_task_status(task_id, "resolving")
        running_job = repo.get_job(job_id)
        assert running_job is not None
        assert running_job.status == "running"

        repo.update_video_task_status(task_id, "completed")
        completed_job = repo.get_job(job_id)
        assert completed_job is not None
        assert completed_job.status == "completed"


def test_searching_task_status_refreshes_parent_job_summary(tmp_path) -> None:
    engine = create_sqlite_engine(f"sqlite:///{tmp_path / 'test.sqlite3'}")
    create_tables(engine)

    with session_scope(engine) as session:
        repo = Repository(session)
        job = repo.create_job(
            JobCreate(
                source="moviepilot-csf",
                raw_payload={"physical_video_file_full_path": "/media/A.mkv"},
                video_path_original="/media/A.mkv",
            )
        )
        task_id = job.video_tasks[0].id

        repo.update_video_task_status(task_id, "searching")
        assert repo.get_job(job.id).status == "running"

        repo.update_video_task_status(task_id, "completed")
        assert repo.get_job(job.id).status == "completed"


def test_get_video_task_returns_task_with_relationships(tmp_path) -> None:
    engine = create_sqlite_engine(f"sqlite:///{tmp_path / 'test.sqlite3'}")
    create_tables(engine)

    with session_scope(engine) as session:
        repo = Repository(session)
        job = repo.create_job(
            JobCreate(
                source="moviepilot-csf",
                raw_payload={"physical_video_file_full_path": "/media/A.mkv"},
                video_path_original="/media/A.mkv",
            )
        )
        task_id = job.video_tasks[0].id
        candidate = repo.record_candidate(
            video_task_id=task_id,
            provider="fake",
            language="zh-cn",
            is_bilingual=True,
            format="srt",
            title="Movie bilingual",
            score=123.0,
            release_info="WEB-DL",
            source_url="https://example.invalid/sub.srt",
            raw_metadata={"confidence": 0.8},
        )
        repo.record_artifact(
            video_task_id=task_id,
            candidate_id=candidate.id,
            kind="downloaded",
            path="/media/A.zh-cn.srt",
            is_synced=False,
        )
        repo.record_task_event(
            video_task_id=task_id,
            stage="download",
            status="started",
            message="fetching subtitle",
            details={"provider": "fake"},
        )

        loaded_task = repo.get_video_task(task_id)

    assert loaded_task is not None
    assert loaded_task.id == task_id
    assert loaded_task.video_path_original == "/media/A.mkv"
    assert len(loaded_task.candidates) == 1
    assert loaded_task.candidates[0].provider == "fake"
    assert loaded_task.candidates[0].raw_metadata_json == {"confidence": 0.8}
    assert len(loaded_task.artifacts) == 1
    assert loaded_task.artifacts[0].path == "/media/A.zh-cn.srt"
    assert loaded_task.artifacts[0].candidate_id == candidate.id
    assert len(loaded_task.events) == 1
    assert loaded_task.events[0].stage == "download"
    assert loaded_task.events[0].details_json == {"provider": "fake"}


def test_list_placed_candidates_for_completed_task(tmp_path) -> None:
    engine = create_sqlite_engine(f"sqlite:///{tmp_path / 'test.sqlite3'}")
    create_tables(engine)

    with session_scope(engine) as session:
        repo = Repository(session)
        job = repo.create_job(
            JobCreate(
                source="manual-retry",
                raw_payload={},
                video_path_original="/media/A.mkv",
            )
        )
        task_id = job.video_tasks[0].id
        candidate = repo.record_candidate(
            video_task_id=task_id,
            provider="assrt",
            language="zh-cn",
            is_bilingual=False,
            format="ass",
            title="A",
            score=100,
            release_info=None,
            source_url="https://assrt.net/xml/sub/123/123456.xml",
            raw_metadata={"assrt_subtitle_id": 123456},
        )
        repo.record_artifact(
            video_task_id=task_id,
            candidate_id=candidate.id,
            kind="placed",
            path="/media/A.zh-cn.ass",
        )

        assert repo.list_placed_candidates_for_task(task_id) == []
        repo.update_video_task_status(task_id, "completed")
        placed = repo.list_placed_candidates_for_task(task_id)

    assert [item.id for item in placed] == [candidate.id]


def test_get_retry_parent_task_id_reads_job_payload(tmp_path) -> None:
    engine = create_sqlite_engine(f"sqlite:///{tmp_path / 'test.sqlite3'}")
    create_tables(engine)

    with session_scope(engine) as session:
        repo = Repository(session)
        parent = repo.create_job(
            JobCreate(
                source="moviepilot-csf",
                raw_payload={},
                video_path_original="/media/A.mkv",
            )
        )
        child = repo.create_job(
            JobCreate(
                source="manual-retry",
                raw_payload={"retry_of_task_id": parent.video_tasks[0].id},
                video_path_original="/media/A.mkv",
            )
        )

        assert repo.get_retry_parent_task_id(child.video_tasks[0].id) == parent.video_tasks[0].id
        assert repo.get_retry_parent_task_id(parent.video_tasks[0].id) is None
        assert repo.get_retry_parent_task_id(99999) is None


def test_mark_jellyfin_media_item_has_chinese_subtitle_falls_back_to_path(tmp_path) -> None:
    engine = create_sqlite_engine(f"sqlite:///{tmp_path / 'test.sqlite3'}")
    create_tables(engine)

    with session_scope(engine) as session:
        repo = Repository(session)
        repo.upsert_jellyfin_media_item(
            JellyfinMediaItemData(
                jellyfin_item_id="jf-1",
                library_id="lib-1",
                library_name="Movies",
                item_type="Movie",
                name="Movie",
                path="/media/Movie/Movie.mkv",
                subtitle_status="missing",
            )
        )

        item = repo.mark_jellyfin_media_item_has_chinese_subtitle(
            None,
            path="/media/Movie/Movie.mkv",
        )

        assert item is not None
        assert item.jellyfin_item_id == "jf-1"
        assert item.subtitle_status == "has_chinese"
        assert item.has_external_chinese_subtitle is True


def test_jellyfin_ignore_is_limited_to_movies_and_series_and_survives_upsert(tmp_path) -> None:
    engine = create_sqlite_engine(f"sqlite:///{tmp_path / 'test.sqlite3'}")
    create_tables(engine)

    with session_scope(engine) as session:
        repo = Repository(session)
        movie_data = JellyfinMediaItemData(
            jellyfin_item_id="movie-1",
            library_id="movie-lib",
            library_name="Movies",
            item_type="Movie",
            name="Movie",
            path="/media/Movie.mkv",
            subtitle_status="missing",
        )
        repo.upsert_jellyfin_media_item(movie_data)
        ignored = repo.set_jellyfin_media_item_ignored("movie-1", ignored=True)

        assert ignored is not None
        assert ignored.ignored is True

    with session_scope(engine) as session:
        repo = Repository(session)
        repo.upsert_jellyfin_media_item(movie_data)
        assert repo.get_jellyfin_media_item("movie-1").ignored is True

        repo.upsert_jellyfin_media_item(
            JellyfinMediaItemData(
                jellyfin_item_id="episode-1",
                library_id="tv-lib",
                library_name="TV",
                item_type="Episode",
                name="Episode",
                path="/media/Show.S01E01.mkv",
            )
        )
        with pytest.raises(ValueError, match="only Movie and Series"):
            repo.set_jellyfin_media_item_ignored("episode-1", ignored=True)


def test_record_candidate_and_artifact_persist(tmp_path) -> None:
    engine = create_sqlite_engine(f"sqlite:///{tmp_path / 'test.sqlite3'}")
    create_tables(engine)

    with session_scope(engine) as session:
        repo = Repository(session)
        job = repo.create_job(
            JobCreate(
                source="moviepilot-csf",
                raw_payload={"physical_video_file_full_path": "/media/A.mkv"},
                video_path_original="/media/A.mkv",
            )
        )
        task_id = job.video_tasks[0].id

        candidate = repo.record_candidate(
            video_task_id=task_id,
            provider="fake",
            language="zh-cn",
            is_bilingual=False,
            format="ass",
            title="Movie Chinese",
            score=98.5,
            release_info="BluRay",
            source_url="https://example.invalid/sub.ass",
            raw_metadata={"source": "test"},
        )
        repo.merge_candidate_metadata(
            candidate.id,
            {"content_sha256": "abc", "text_fingerprint": "def"},
        )
        artifact = repo.record_artifact(
            video_task_id=task_id,
            candidate_id=candidate.id,
            kind="placed",
            path="/media/A.zh-cn.default.ass",
            is_synced=True,
        )

        assert candidate.id > 0
        assert artifact.id > 0

    with session_scope(engine) as session:
        repo = Repository(session)
        loaded_task = repo.get_video_task(task_id)

    assert loaded_task is not None
    assert loaded_task.candidates[0].title == "Movie Chinese"
    assert loaded_task.candidates[0].download_status == "queued"
    assert loaded_task.candidates[0].attempt_count == 0
    assert loaded_task.candidates[0].last_attempt_status is None
    assert loaded_task.candidates[0].last_error_message is None
    assert loaded_task.candidates[0].raw_metadata_json == {
        "source": "test",
        "content_sha256": "abc",
        "text_fingerprint": "def",
    }
    assert loaded_task.artifacts[0].kind == "placed"
    assert loaded_task.artifacts[0].is_synced is True


def test_update_candidate_attempt_persists_status_error_and_attempts(tmp_path) -> None:
    engine = create_sqlite_engine(f"sqlite:///{tmp_path / 'test.sqlite3'}")
    create_tables(engine)

    with session_scope(engine) as session:
        repo = Repository(session)
        job = repo.create_job(
            JobCreate(
                source="moviepilot-csf",
                raw_payload={"physical_video_file_full_path": "/media/A.mkv"},
                video_path_original="/media/A.mkv",
            )
        )
        task_id = job.video_tasks[0].id
        candidate = repo.record_candidate(
            video_task_id=task_id,
            provider="fake",
            language="zh-cn",
            is_bilingual=False,
            format="ass",
            title="Movie Chinese",
            score=98.5,
            release_info="BluRay",
            source_url="https://example.invalid/sub.ass",
            raw_metadata={"source": "test"},
        )

        updated = repo.update_candidate_attempt(
            candidate_id=candidate.id,
            status="failed",
            error_message="missing_timestamps",
            attempts=1,
        )

        assert updated.download_status == "failed"
        assert updated.attempt_count == 1
        assert updated.last_attempt_status == "failed"
        assert updated.last_error_message == "missing_timestamps"

    with session_scope(engine) as session:
        repo = Repository(session)
        loaded_task = repo.get_video_task(task_id)

    assert loaded_task is not None
    assert loaded_task.candidates[0].download_status == "failed"
    assert loaded_task.candidates[0].attempt_count == 1
    assert loaded_task.candidates[0].last_attempt_status == "failed"
    assert loaded_task.candidates[0].last_error_message == "missing_timestamps"


def test_find_completed_episode_content_duplicate_only_matches_other_episode(
    tmp_path,
) -> None:
    engine = create_sqlite_engine(f"sqlite:///{tmp_path / 'test.sqlite3'}")
    create_tables(engine)

    with session_scope(engine) as session:
        repo = Repository(session)
        job = repo.create_job(
            JobCreate(
                source="moviepilot-csf",
                raw_payload={
                    "physical_video_file_full_path": "/media/Show/S01E01.mkv",
                    "media_identity": {"series_id": "tmdb:123"},
                },
                video_path_original="/media/Show/S01E01.mkv",
            )
        )
        task = job.video_tasks[0]
        task.season = 1
        task.episode = 1
        candidate = repo.record_candidate(
            video_task_id=task.id,
            provider="fake",
            language="zh-cn",
            is_bilingual=False,
            format="srt",
            title="Show S01E01",
            score=90,
            release_info="WEB-DL",
            source_url="https://example.invalid/1",
            raw_metadata={
                "content_sha256": "same-content",
                "text_fingerprint": "same-text",
            },
        )
        repo.record_artifact(
            video_task_id=task.id,
            candidate_id=candidate.id,
            kind="placed",
            path="/media/Show/S01E01.zh.srt",
            is_synced=False,
        )
        repo.update_video_task_status(task.id, "completed")
        placed_task_id = task.id

        assert repo.find_completed_episode_content_duplicate(
            series_id="tmdb:123",
            season=1,
            episode=2,
            content_identity={"content_sha256": "same-content"},
        ) == placed_task_id
        assert repo.find_completed_episode_content_duplicate(
            series_id="tmdb:123",
            season=1,
            episode=1,
            content_identity={"text_fingerprint": "same-text"},
        ) is None
        assert repo.find_completed_episode_content_duplicate(
            series_id="tmdb:999",
            season=1,
            episode=2,
            content_identity={"content_sha256": "same-content"},
        ) is None


def test_update_candidate_attempt_increment_true_increments_real_db_row(tmp_path) -> None:
    engine = create_sqlite_engine(f"sqlite:///{tmp_path / 'test.sqlite3'}")
    create_tables(engine)

    with session_scope(engine) as session:
        repo = Repository(session)
        job = repo.create_job(
            JobCreate(
                source="moviepilot-csf",
                raw_payload={"physical_video_file_full_path": "/media/A.mkv"},
                video_path_original="/media/A.mkv",
            )
        )
        task_id = job.video_tasks[0].id
        candidate = repo.record_candidate(
            video_task_id=task_id,
            provider="fake",
            language="zh-cn",
            is_bilingual=False,
            format="ass",
            title="Movie Chinese",
            score=98.5,
            release_info="BluRay",
            source_url="https://example.invalid/sub.ass",
            raw_metadata={"source": "test"},
        )

        first_update = repo.update_candidate_attempt(
            candidate_id=candidate.id,
            status="running",
            increment=True,
        )
        assert first_update.attempt_count == 1
        assert first_update.download_status == "running"

        second_update = repo.update_candidate_attempt(
            candidate_id=candidate.id,
            status="completed",
            increment=True,
        )

        assert second_update.attempt_count == 2
        assert second_update.download_status == "completed"

    with session_scope(engine) as session:
        repo = Repository(session)
        loaded_task = repo.get_video_task(task_id)

    assert loaded_task is not None
    assert loaded_task.candidates[0].attempt_count == 2
    assert loaded_task.candidates[0].last_attempt_status == "completed"
    assert loaded_task.candidates[0].last_error_message is None


def test_record_and_list_task_events_persist_in_id_order(tmp_path) -> None:
    engine = create_sqlite_engine(f"sqlite:///{tmp_path / 'test.sqlite3'}")
    create_tables(engine)

    with session_scope(engine) as session:
        repo = Repository(session)
        job = repo.create_job(
            JobCreate(
                source="moviepilot-csf",
                raw_payload={"physical_video_file_full_path": "/media/A.mkv"},
                video_path_original="/media/A.mkv",
            )
        )
        task_id = job.video_tasks[0].id

        first_event = repo.record_task_event(
            video_task_id=task_id,
            stage="resolve",
            status="started",
            message="resolving media path",
            details={"attempt": 1},
        )
        second_event = repo.record_task_event(
            video_task_id=task_id,
            stage="resolve",
            status="failed",
            message="path missing",
            error_code="video_not_found",
            details={"attempt": 2},
        )

        listed_events = repo.list_task_events(task_id)

    assert [event.id for event in listed_events] == [first_event.id, second_event.id]
    assert listed_events[0].message == "resolving media path"
    assert listed_events[0].error_code is None
    assert listed_events[0].details_json == {"attempt": 1}
    assert listed_events[1].error_code == "video_not_found"
    assert listed_events[1].details_json == {"attempt": 2}


def test_list_task_events_returns_latest_limit_in_ascending_id_order(tmp_path) -> None:
    engine = create_sqlite_engine(f"sqlite:///{tmp_path / 'test.sqlite3'}")
    create_tables(engine)

    with session_scope(engine) as session:
        repo = Repository(session)
        job = repo.create_job(
            JobCreate(
                source="moviepilot-csf",
                raw_payload={"physical_video_file_full_path": "/media/A.mkv"},
                video_path_original="/media/A.mkv",
            )
        )
        task_id = job.video_tasks[0].id

        for index in range(205):
            repo.record_task_event(
                video_task_id=task_id,
                stage="download",
                status="progress",
                message=str(index),
            )

        listed_events = repo.list_task_events(task_id)

    assert len(listed_events) == 200
    assert listed_events[0].message == "5"
    assert listed_events[-1].message == "204"
    assert [event.id for event in listed_events] == sorted(event.id for event in listed_events)


def test_prune_task_events_keeps_newest_entries_within_limit(tmp_path) -> None:
    engine = create_sqlite_engine(f"sqlite:///{tmp_path / 'test.sqlite3'}")
    create_tables(engine)

    with session_scope(engine) as session:
        repo = Repository(session)
        job = repo.create_job(
            JobCreate(
                source="moviepilot-csf",
                raw_payload={"physical_video_file_full_path": "/media/A.mkv"},
                video_path_original="/media/A.mkv",
            )
        )
        task_id = job.video_tasks[0].id
        for index in range(4):
            repo.record_task_event(
                video_task_id=task_id,
                stage="searching",
                status="completed",
                message=str(index),
            )
        assert repo.prune_task_events(retention_days=30, max_entries=2) == 2
        remaining = repo.list_task_events(task_id)

    assert [event.message for event in remaining] == ["2", "3"]


def test_session_scope_rolls_back_on_exception(tmp_path) -> None:
    engine = create_sqlite_engine(f"sqlite:///{tmp_path / 'test.sqlite3'}")
    create_tables(engine)

    try:
        with session_scope(engine) as session:
            repo = Repository(session)
            repo.create_job(
                JobCreate(
                    source="moviepilot-csf",
                    raw_payload={"physical_video_file_full_path": "/media/A.mkv"},
                    video_path_original="/media/A.mkv",
                )
            )
            raise RuntimeError("force rollback")
    except RuntimeError:
        pass

    with session_scope(engine) as session:
        assert session.query(Job).count() == 0
