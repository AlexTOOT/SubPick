from pathlib import Path

from fastapi.testclient import TestClient

from subtitle_sidecar.main import create_app
from subtitle_sidecar.db.models import SubtitleArtifact, SubtitleCandidateRecord, TaskEvent
from subtitle_sidecar.db.repository import JobCreate, Repository
from subtitle_sidecar.db.session import session_scope


def _wait_for_queue(app) -> None:
    assert app.state.task_queue.wait_until_idle(timeout=2)


def _create_observable_task(app, *, video_path: str = "/media/Movie/Movie.mkv") -> tuple[int, int]:
    with session_scope(app.state.engine) as session:
        repo = Repository(session)
        job = repo.create_job(
            JobCreate(
                source="moviepilot-csf",
                raw_payload={"physical_video_file_full_path": video_path},
                video_path_original=video_path,
                media_server_id="jellyfin-id",
            )
        )
        task = job.video_tasks[0]
        task.video_path_resolved = "/library/Movie/Movie.mkv"
        task.result_subtitle_path = "/library/Movie/Movie.zh.srt"
        task.error_message = None
        candidate = repo.record_candidate(
            video_task_id=task.id,
            provider="fake",
            language="zh-cn",
            is_bilingual=True,
            format="srt",
            title="Movie bilingual",
            score=98.5,
            release_info="WEB-DL",
            source_url="https://example.invalid/sub.srt",
            raw_metadata={"rank": 1},
        )
        repo.update_candidate_attempt(
            candidate_id=candidate.id,
            status="failed",
            error_message="missing_timestamps",
            attempts=1,
        )
        repo.record_artifact(
            video_task_id=task.id,
            candidate_id=candidate.id,
            kind="placed",
            path="/library/Movie/Movie.zh.srt",
            is_synced=True,
        )
        repo.record_task_event(
            video_task_id=task.id,
            stage="search",
            status="started",
            message="search started",
            details={"provider": "fake"},
        )
        repo.record_task_event(
            video_task_id=task.id,
            stage="download",
            status="completed",
            message="subtitle saved",
        )
        repo.update_video_task_status(task.id, "completed")
        return job.id, task.id


def test_add_job_accepts_csf_payload(client, app):
    payload = {
        "video_type": "movie",
        "physical_video_file_full_path": "/media/Movie/Movie.mkv",
        "task_priority_level": 1,
        "media_server_inside_video_id": "jellyfin-id",
        "is_bluray": False,
    }

    response = client.post("/api/v1/add-job", json=payload)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "queued"
    assert body["job_id"] > 0

    with session_scope(app.state.engine) as session:
        repo = Repository(session)
        job = repo.get_job(body["job_id"])

    assert job is not None
    assert job.status == "queued"
    assert job.raw_payload_json == payload
    assert len(job.video_tasks) == 1
    assert job.video_tasks[0].status == "queued"
    assert job.video_tasks[0].video_path_original == "/media/Movie/Movie.mkv"
    assert job.video_tasks[0].media_server_id == "jellyfin-id"


def test_add_job_accepts_moviepilot_plugin_integer_video_type(client):
    payload = {
        "video_type": 0,
        "physical_video_file_full_path": "/media/Movie/Movie.mkv",
        "task_priority_level": 3,
        "media_server_inside_video_id": "",
        "is_bluray": False,
    }

    response = client.post("/api/v1/add-job", json=payload)

    assert response.status_code == 200
    assert response.json()["job_id"] > 0


def test_health(client):
    response = client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_jobs_can_be_searched_by_title_path_and_task_id(client, app):
    with session_scope(app.state.engine) as session:
        repo = Repository(session)
        wanted = repo.create_job(
            JobCreate(
                source="manual",
                raw_payload={},
                video_path_original="/media/TV/Attack.on.Titan.S01E01.mkv",
            )
        )
        wanted.video_tasks[0].title = "进击的巨人"
        unwanted = repo.create_job(
            JobCreate(
                source="manual",
                raw_payload={},
                video_path_original="/media/Movie/Unrelated.mkv",
            )
        )
        wanted_task_id = wanted.video_tasks[0].id
        unwanted_job_id = unwanted.id

    by_title = client.get("/api/v1/jobs?limit=25&offset=0&search=进击的巨人")
    by_path = client.get("/api/v1/jobs?limit=25&offset=0&search=Attack.on.Titan")
    by_id = client.get(f"/api/v1/jobs?limit=25&offset=0&search={wanted_task_id}")

    for response in (by_title, by_path, by_id):
        assert response.status_code == 200
        assert response.headers["x-total-count"] == "1"
        assert response.json()[0]["job_id"] != unwanted_job_id


def test_logs_endpoint_is_persistent_and_filters_by_cursor(token_app, token_client):
    with session_scope(token_app.state.engine) as session:
        repo = Repository(session)
        job = repo.create_job(
            JobCreate(
                source="moviepilot-csf",
                raw_payload={"physical_video_file_full_path": "/media/A.mkv"},
                video_path_original="/media/A.mkv",
            )
        )
        task_id = job.video_tasks[0].id
        repo.record_task_event(
            video_task_id=task_id,
            stage="provider_search",
            status="completed",
            message="provider detail must stay in task history",
            details={"provider": "assrt"},
        )
        repo.record_system_event(
            category="system",
            event="system_started",
            message="started",
        )
        failed_event = repo.record_system_event(
            category="task",
            event="task_failed",
            level="ERROR",
            message="download failed",
            task_id=task_id,
        )

    response = token_client.get(
        f"/api/v1/logs?after_id=0&limit=200&level=error&task_id={task_id}&category=task"
    )

    assert response.status_code == 200
    assert response.json()["entries"][0]["ts"].endswith("+00:00")
    assert response.json() == {
        "entries": [
            {
                "id": failed_event.id,
                "ts": response.json()["entries"][0]["ts"],
                "level": "error",
                "event": "task_failed",
                "category": "task",
                "task_id": task_id,
                "message": "download failed",
            }
        ],
        "next_after_id": failed_event.id,
    }
    assert "provider detail must stay in task history" not in response.text

    invalid_response = token_client.get("/api/v1/logs?limit=501")
    assert invalid_response.status_code == 422


def test_logs_initial_page_returns_latest_entries_in_chronological_order(token_app, token_client):
    with session_scope(token_app.state.engine) as session:
        repo = Repository(session)
        events = [
            repo.record_system_event(
                category="system",
                event="test_event",
                message=f"event {index}",
            )
            for index in range(3)
        ]

    response = token_client.get("/api/v1/logs?after_id=0&limit=2")

    assert response.status_code == 200
    assert [entry["id"] for entry in response.json()["entries"]] == [events[1].id, events[2].id]
    assert response.json()["next_after_id"] == events[2].id


def test_health_check_run_is_persisted_as_a_system_log(token_client):
    recorded = token_client.post(
        "/api/v1/diagnostics/health-runs",
        json={
            "checks": [
                {"name": "数据库", "group": "运行环境", "status": "ok", "detail": "ok"},
                {
                    "name": "ASSRT",
                    "group": "外部连接",
                    "status": "warning",
                    "detail": "配额不足",
                },
            ]
        },
    )
    response = token_client.get("/api/v1/logs?category=health&limit=1")

    assert recorded.status_code == 204
    assert response.status_code == 200
    assert response.json()["entries"][0]["event"] == "health_check_completed"
    assert response.json()["entries"][0]["level"] == "warning"
    assert response.json()["entries"][0]["message"] == (
        "健康检查完成，存在警告：正常 1，警告 1，错误 0，未启用 0"
    )


def test_diagnostics_is_local_only_redacted_and_degraded_when_checks_fail(
    token_app,
    token_client,
    tmp_path: Path,
    monkeypatch,
):
    token_app.state.settings.jellyfin.server_url = "http://jellyfin.example.invalid"
    token_app.state.settings.jellyfin.api_key = "diagnostics-api-key"
    token_app.state.settings.jellyfin.user_id = "diagnostics-user-id"
    monkeypatch.setattr("subtitle_sidecar.diagnostics.shutil.which", lambda executable: None)

    response = token_client.get("/api/v1/diagnostics")

    assert response.status_code == 200
    body = response.json()
    serialized = response.text
    assert body["overall_status"] == "degraded"
    assert body["queue"] == {
        "active_task_id": None,
        "queued_count": 0,
        "search_interval_seconds": 0.0,
        "provider_cooldowns": {
            "assrt": 0.0,
            "subdl": 0.0,
            "subliminal": 0.0,
            "zimuku": 0.0,
        },
        "next_provider_ready_seconds": 0.0,
    }
    assert body["jellyfin"] == {
        "configured": True,
        "connected": False,
        "last_checked_at": None,
    }
    assert "paths" not in body
    assert all(tool["status"] == "degraded" for tool in body["tools"])
    assert "jellyfin.example.invalid" not in serialized
    assert "diagnostics-api-key" not in serialized
    assert "diagnostics-user-id" not in serialized


def test_diagnostics_export_is_redacted_and_downloadable(token_app, token_client):
    token_app.state.settings.jellyfin.api_key = "diagnostics-api-key"

    response = token_client.get("/api/v1/diagnostics/export")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert "attachment; filename=subtitle-sidecar-diagnostics.json" in response.headers[
        "content-disposition"
    ]
    assert "diagnostics-api-key" not in response.text
    assert response.json()["compatibility"]["status"] == "ok"


def test_github_settings_are_redacted_and_used_for_dependency_checks(token_app, token_client):
    class FakeResponse:
        status_code = 200

        def json(self):
            return {"tag_name": "v9.9.9", "html_url": "https://github.test/release"}

    class FakeClient:
        def __init__(self):
            self.headers = []

        def get(self, _url, **kwargs):
            self.headers.append(kwargs["headers"])
            return FakeResponse()

    fake_client = FakeClient()
    token_app.state.subliminal_update_client = fake_client

    saved = token_client.put("/api/v1/github/settings", json={"api_key": "github-test-secret"})
    loaded = token_client.get("/api/v1/github/settings")
    updates = token_client.post("/api/v1/diagnostics/dependency-updates")

    assert saved.json() == {"api_key_configured": True}
    assert loaded.json() == {"api_key_configured": True}
    assert updates.status_code == 200
    assert set(updates.json()) == {"subliminal", "ffsubsync"}
    assert all(headers["Authorization"] == "Bearer github-test-secret" for headers in fake_client.headers)
    assert "github-test-secret" not in f"{saved.text}{loaded.text}{updates.text}"


def test_server_token_settings_are_plaintext_and_persist_across_restart(tmp_path):
    token = "moviepilot-generated-token"
    app = create_app(data_dir=tmp_path, job_processor=lambda task_id: None)

    with TestClient(app) as client:
        assert client.get("/api/v1/server/settings").json() == {"token": ""}
        saved = client.put("/api/v1/server/settings", json={"token": token})
        unauthorized = client.post(
            "/api/v1/add-job",
            json={"physical_video_file_full_path": "/media/Movie/Movie.mkv"},
        )
        authorized = client.post(
            "/api/v1/add-job",
            json={"physical_video_file_full_path": "/media/Movie/Movie.mkv"},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert saved.json() == {"token": token}
    assert unauthorized.status_code == 401
    assert authorized.status_code == 200

    restarted = create_app(data_dir=tmp_path, job_processor=lambda task_id: None)
    with TestClient(restarted) as client:
        assert client.get("/api/v1/server/settings").json() == {"token": token}
        disabled = client.put("/api/v1/server/settings", json={"token": ""})
        unprotected = client.post(
            "/api/v1/add-job",
            json={"physical_video_file_full_path": "/media/Movie/Another.mkv"},
        )

    assert disabled.json() == {"token": ""}
    assert unprotected.status_code == 200


def test_setup_wizard_dismissal_persists_across_restart(tmp_path):
    data_dir = tmp_path / "data"
    app = create_app(data_dir=data_dir, job_processor=lambda task_id: None)

    with TestClient(app) as client:
        before = client.get("/api/v1/diagnostics")
        dismissed = client.put("/api/v1/setup/wizard", json={"dismissed": True})
        after = client.get("/api/v1/diagnostics")

    assert before.status_code == 200
    assert before.json()["setup"]["dismissed"] is False
    assert dismissed.json() == {"dismissed": True}
    assert after.json()["setup"]["dismissed"] is True

    restarted = create_app(data_dir=data_dir, job_processor=lambda task_id: None)
    with TestClient(restarted) as client:
        persisted = client.get("/api/v1/diagnostics")
        restored = client.put("/api/v1/setup/wizard", json={"dismissed": False})

    assert persisted.json()["setup"]["dismissed"] is True
    assert restored.json() == {"dismissed": False}


def test_moviepilot_connection_is_verified_by_first_authenticated_callback(tmp_path):
    media_file = tmp_path / "Movie.mkv"
    media_file.write_bytes(b"video")
    app = create_app(
        data_dir=tmp_path / "data",
        token="moviepilot-token",
        job_processor=lambda task_id: None,
    )

    with TestClient(app) as client:
        before = client.get("/api/v1/diagnostics").json()
        callback = client.post(
            "/api/v1/add-job",
            json={"physical_video_file_full_path": str(media_file)},
            headers={"Authorization": "Bearer moviepilot-token"},
        )
        after = client.get("/api/v1/diagnostics").json()

    assert callback.status_code == 200
    assert before["moviepilot"]["connected"] is False
    assert before["moviepilot"]["token_configured"] is True
    assert after["moviepilot"]["connected"] is True
    assert after["moviepilot"]["last_received_path"] == str(media_file)
    assert after["moviepilot"]["last_callback_at"]


def test_settings_backup_can_be_exported_and_imported(tmp_path):
    app = create_app(data_dir=tmp_path / "data", job_processor=lambda task_id: None)

    with TestClient(app) as client:
        client.put("/api/v1/server/settings", json={"token": "backup-token"})
        exported = client.get("/api/v1/settings/export")
        client.put("/api/v1/server/settings", json={"token": "changed-token"})
        imported = client.put("/api/v1/settings/import", json=exported.json())
        restored = client.get("/api/v1/server/settings")

    assert exported.status_code == 200
    assert "attachment; filename=subpick-settings.json" in exported.headers[
        "content-disposition"
    ]
    assert imported.json() == {"imported": True, "restart_required": False}
    assert restored.json() == {"token": "backup-token"}


def test_add_job_requires_bearer_token_when_configured(token_client):
    payload = {
        "physical_video_file_full_path": "/media/Movie/Movie.mkv",
    }

    response = token_client.post("/api/v1/add-job", json=payload)

    assert response.status_code == 401


def test_add_job_rejects_wrong_bearer_token(token_client):
    payload = {
        "physical_video_file_full_path": "/media/Movie/Movie.mkv",
    }

    response = token_client.post(
        "/api/v1/add-job",
        json=payload,
        headers={"Authorization": "Bearer wrong-token"},
    )

    assert response.status_code == 401


def test_add_job_accepts_correct_bearer_token(token_client):
    payload = {
        "physical_video_file_full_path": "/media/Movie/Movie.mkv",
    }

    response = token_client.post(
        "/api/v1/add-job",
        json=payload,
        headers={"Authorization": "Bearer secret-token"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "queued"


def test_add_job_schedules_created_video_task_for_processing(tmp_path: Path):
    directly_processed_task_ids: list[int] = []
    enqueued_task_ids: list[int] = []

    def fake_processor(task_id: int) -> None:
        directly_processed_task_ids.append(task_id)

    app = create_app(data_dir=tmp_path, job_processor=fake_processor)
    app.state.settings.queue.search_interval_seconds = 0

    with TestClient(app) as client:
        app.state.enqueue_task = enqueued_task_ids.append
        response = client.post(
            "/api/v1/add-job",
            json={"physical_video_file_full_path": "/media/Movie/Movie.mkv"},
        )

    assert response.status_code == 200
    assert directly_processed_task_ids == []
    assert enqueued_task_ids and enqueued_task_ids[0] > 0

    with session_scope(app.state.engine) as session:
        repo = Repository(session)
        task = repo.get_video_task(enqueued_task_ids[0])

    assert task is not None
    assert task.video_path_original == "/media/Movie/Movie.mkv"


def test_create_app_loads_token_from_yaml_config(tmp_path: Path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "\n".join(
            [
                "server:",
                "  token: yaml-token",
                "providers:",
                "  subliminal:",
                "    enabled: true",
                "    languages:",
                "      - zho",
            ]
        ),
        encoding="utf-8",
    )

    app = create_app(data_dir=tmp_path, config_path=config_path, job_processor=lambda task_id: None)

    with TestClient(app) as client:
        unauthorized = client.post(
            "/api/v1/add-job",
            json={"physical_video_file_full_path": "/media/Movie/Movie.mkv"},
        )
        authorized = client.post(
            "/api/v1/add-job",
            json={"physical_video_file_full_path": "/media/Movie/Movie.mkv"},
            headers={"Authorization": "Bearer yaml-token"},
        )

    assert unauthorized.status_code == 401
    assert authorized.status_code == 200
    assert app.state.settings.providers.subliminal.enabled is True


def test_create_app_allows_data_dir_from_environment(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SUBTITLE_SIDECAR_DATA_DIR", str(tmp_path))

    app = create_app(job_processor=lambda task_id: None)

    assert app.state.settings.data_dir == tmp_path
    assert app.state.settings.cache_dir == tmp_path / "cache"


def test_default_app_processes_missing_video_in_background(tmp_path: Path):
    app = create_app(data_dir=tmp_path)
    app.state.settings.queue.search_interval_seconds = 0

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/add-job",
            json={"physical_video_file_full_path": "/missing/Movie.mkv"},
        )
        _wait_for_queue(app)

    assert response.status_code == 200

    with session_scope(app.state.engine) as session:
        repo = Repository(session)
        job = repo.get_job(response.json()["job_id"])

    assert job is not None
    assert job.video_tasks[0].status == "failed"
    assert job.video_tasks[0].error_message == "video_not_found"


def test_list_jobs_returns_recent_jobs_with_task_summary(client, app):
    first_job_id, first_task_id = _create_observable_task(app, video_path="/media/Movie/A.mkv")
    second_job_id, second_task_id = _create_observable_task(app, video_path="/media/Movie/B.mkv")

    response = client.get("/api/v1/jobs")

    assert response.status_code == 200
    body = response.json()
    assert [job["job_id"] for job in body] == [second_job_id, first_job_id]
    assert body[0]["status"] == "completed"
    assert body[0]["created_at"]
    assert body[0]["updated_at"]
    assert body[0]["video_tasks"] == [
        {
            "id": second_task_id,
            "job_id": second_job_id,
            "status": "completed",
            "video_path_original": "/media/Movie/B.mkv",
            "result_subtitle_path": "/library/Movie/Movie.zh.srt",
            "created_at": body[0]["video_tasks"][0]["created_at"],
            "updated_at": body[0]["video_tasks"][0]["updated_at"],
        }
    ]


def test_get_task_detail_returns_candidates_artifacts_and_events(client, app):
    _, task_id = _create_observable_task(app)

    response = client.get(f"/api/v1/tasks/{task_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == task_id
    assert body["job_id"] > 0
    assert body["status"] == "completed"
    assert body["error_message"] is None
    assert body["video_path_original"] == "/media/Movie/Movie.mkv"
    assert body["video_path_resolved"] == "/library/Movie/Movie.mkv"
    assert body["result_subtitle_path"] == "/library/Movie/Movie.zh.srt"
    assert body["created_at"]
    assert body["updated_at"]
    assert body["candidates"] == [
        {
            "id": body["candidates"][0]["id"],
            "provider": "fake",
            "language": "zh-cn",
            "is_bilingual": True,
            "format": "srt",
            "score": 98.5,
            "title": "Movie bilingual",
            "release_info": "WEB-DL",
            "source_url": "https://example.invalid/sub.srt",
            "download_status": "failed",
            "attempt_count": 1,
            "last_attempt_status": "failed",
            "last_error_message": "missing_timestamps",
            "created_at": body["candidates"][0]["created_at"],
            "raw_metadata": {"rank": 1},
        }
    ]
    assert body["artifacts"] == [
        {
            "id": body["artifacts"][0]["id"],
            "candidate_id": body["candidates"][0]["id"],
            "kind": "placed",
            "path": "/library/Movie/Movie.zh.srt",
            "is_synced": True,
            "created_at": body["artifacts"][0]["created_at"],
        }
    ]
    assert [event["stage"] for event in body["events"]] == ["search", "download"]
    assert body["events"][0]["details"] == {"provider": "fake"}


def test_get_task_events_returns_latest_window_in_ascending_id_order(client, app):
    _, task_id = _create_observable_task(app)

    with session_scope(app.state.engine) as session:
        repo = Repository(session)
        for index in range(205):
            repo.record_task_event(
                video_task_id=task_id,
                stage="poll",
                status="progress",
                message=f"event-{index}",
            )

    response = client.get(f"/api/v1/tasks/{task_id}/events")

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 200
    assert body[0]["message"] == "event-5"
    assert body[-1]["message"] == "event-204"
    assert [event["id"] for event in body] == sorted(event["id"] for event in body)


def test_get_job_returns_404_for_missing_job(client):
    response = client.get("/api/v1/jobs/999999")

    assert response.status_code == 404
    assert response.json() == {"detail": "Job not found"}


def test_get_task_detail_returns_404_for_missing_task(client):
    response = client.get("/api/v1/tasks/999999")

    assert response.status_code == 404
    assert response.json() == {"detail": "Task not found"}


def test_get_task_events_returns_404_for_missing_task(client):
    response = client.get("/api/v1/tasks/999999/events")

    assert response.status_code == 404
    assert response.json() == {"detail": "Task not found"}


def test_management_endpoints_do_not_require_bearer_token_when_configured(
    token_app,
    token_client,
):
    job_id, task_id = _create_observable_task(token_app)

    jobs_response = token_client.get("/api/v1/jobs")
    detail_response = token_client.get(f"/api/v1/tasks/{task_id}")
    events_response = token_client.get(f"/api/v1/tasks/{task_id}/events")

    assert jobs_response.status_code == 200
    assert jobs_response.json()[0]["job_id"] == job_id
    assert detail_response.status_code == 200
    assert detail_response.json()["id"] == task_id
    assert events_response.status_code == 200


def test_retry_task_creates_new_job_preserves_original_and_schedules_processing(
    token_app,
    token_client,
):
    processed_task_ids: list[int] = []
    token_app.state.enqueue_task = processed_task_ids.append

    with session_scope(token_app.state.engine) as session:
        repo = Repository(session)
        original_job = repo.create_job(
            JobCreate(
                source="moviepilot-csf",
                raw_payload={
                    "physical_video_file_full_path": "/media/Show/S01E02.mkv",
                    "task_priority_level": 2,
                },
                video_path_original="/media/Show/S01E02.mkv",
                media_server_id="jellyfin-episode-id",
            )
        )
        original_task = original_job.video_tasks[0]
        original_task.title = "Show Name"
        original_task.year = 2025
        original_task.season = 1
        original_task.episode = 2
        original_job_id = original_job.id
        original_task_id = original_task.id

    response = token_client.post(f"/api/v1/tasks/{original_task_id}/retry")

    assert response.status_code == 200
    body = response.json()
    assert body["job_id"] != original_job_id
    assert body["task_id"] != original_task_id
    assert body["status"] == "queued"
    assert processed_task_ids == [body["task_id"]]

    with session_scope(token_app.state.engine) as session:
        repo = Repository(session)
        original_task = repo.get_video_task(original_task_id)
        retry_job = repo.get_job(body["job_id"])

    assert original_task is not None
    assert retry_job is not None
    assert retry_job.source == "manual-retry"
    assert retry_job.raw_payload_json == {
        "physical_video_file_full_path": "/media/Show/S01E02.mkv",
        "task_priority_level": 2,
        "retry_of_task_id": original_task_id,
    }
    assert len(retry_job.video_tasks) == 1
    retry_task = retry_job.video_tasks[0]
    assert retry_task.id == body["task_id"]
    assert retry_task.video_path_original == "/media/Show/S01E02.mkv"
    assert retry_task.media_server_id == "jellyfin-episode-id"
    assert retry_task.title == "Show Name"
    assert retry_task.year == 2025
    assert retry_task.season == 1
    assert retry_task.episode == 2


def test_retry_task_returns_404_for_missing_task(token_client):
    response = token_client.post("/api/v1/tasks/999999/retry")

    assert response.status_code == 404
    assert response.json() == {"detail": "Task not found"}


def test_batch_retry_reports_per_task_results_and_enqueues_created_tasks(token_app, token_client):
    enqueued_task_ids: list[int] = []
    token_app.state.enqueue_task = enqueued_task_ids.append

    with session_scope(token_app.state.engine) as session:
        repo = Repository(session)
        first_job = repo.create_job(
            JobCreate(
                source="moviepilot-csf",
                raw_payload={"physical_video_file_full_path": "/media/A.mkv"},
                video_path_original="/media/A.mkv",
                media_server_id="jf-a",
            )
        )
        second_job = repo.create_job(
            JobCreate(
                source="moviepilot-csf",
                raw_payload={"physical_video_file_full_path": "/media/B.mkv"},
                video_path_original="/media/B.mkv",
                media_server_id="jf-b",
            )
        )
        first_task_id = first_job.video_tasks[0].id
        second_task_id = second_job.video_tasks[0].id

    response = token_client.post(
        "/api/v1/tasks/batch-retry",
        json={"task_ids": [first_task_id, 999999, second_task_id]},
    )

    assert response.status_code == 200
    body = response.json()
    assert [item["task_id"] for item in body["results"]] == [
        first_task_id,
        999999,
        second_task_id,
    ]
    assert body["results"][0]["ok"] is True
    assert body["results"][0]["new_task_id"] in enqueued_task_ids
    assert body["results"][1] == {
        "task_id": 999999,
        "ok": False,
        "job_id": None,
        "new_task_id": None,
        "status": "not_found",
        "error": "Task not found",
    }
    assert body["results"][2]["ok"] is True
    assert body["results"][2]["new_task_id"] in enqueued_task_ids


def test_batch_delete_reports_per_task_results(token_app, token_client):
    with session_scope(token_app.state.engine) as session:
        repo = Repository(session)
        deleted_job = repo.create_job(
            JobCreate(
                source="moviepilot-csf",
                raw_payload={"physical_video_file_full_path": "/media/A.mkv"},
                video_path_original="/media/A.mkv",
            )
        )
        kept_job = repo.create_job(
            JobCreate(
                source="moviepilot-csf",
                raw_payload={"physical_video_file_full_path": "/media/B.mkv"},
                video_path_original="/media/B.mkv",
            )
        )
        deleted_task_id = deleted_job.video_tasks[0].id
        kept_task_id = kept_job.video_tasks[0].id

    response = token_client.post(
        "/api/v1/tasks/batch-delete",
        json={"task_ids": [deleted_task_id, 999999]},
    )

    assert response.status_code == 200
    assert response.json() == {
        "results": [
            {
                "task_id": deleted_task_id,
                "ok": True,
                "deleted": True,
                "subtitle_deleted": False,
                "error": None,
            },
            {
                "task_id": 999999,
                "ok": False,
                "deleted": False,
                "subtitle_deleted": False,
                "error": "Task not found",
            },
        ]
    }

    with session_scope(token_app.state.engine) as session:
        repo = Repository(session)
        assert repo.get_video_task(deleted_task_id) is None
        assert repo.get_video_task(kept_task_id) is not None


def test_delete_task_cascades_records_preserves_files_and_unrelated_task(
    token_app,
    token_client,
    tmp_path: Path,
):
    media_path = tmp_path / "Movie.mkv"
    subtitle_path = tmp_path / "Movie.zh-cn.srt"
    media_path.write_bytes(b"video")
    subtitle_path.write_text("subtitle", encoding="utf-8")
    deleted_job_id, deleted_task_id = _create_observable_task(
        token_app,
        video_path=str(media_path),
    )
    unrelated_job_id, unrelated_task_id = _create_observable_task(
        token_app,
        video_path="/media/Other.mkv",
    )

    with session_scope(token_app.state.engine) as session:
        repo = Repository(session)
        deleted_task = repo.get_video_task(deleted_task_id)
        assert deleted_task is not None
        deleted_task.video_path_resolved = str(media_path)
        deleted_task.artifacts[0].path = str(subtitle_path)
        candidate_id = deleted_task.candidates[0].id
        artifact_id = deleted_task.artifacts[0].id
        event_ids = [event.id for event in deleted_task.events]

    response = token_client.delete(f"/api/v1/tasks/{deleted_task_id}")

    assert response.status_code == 204
    assert response.content == b""
    assert media_path.exists()
    assert subtitle_path.exists()

    with session_scope(token_app.state.engine) as session:
        repo = Repository(session)
        assert repo.get_video_task(deleted_task_id) is None
        assert repo.get_job(deleted_job_id) is None
        assert session.get(SubtitleCandidateRecord, candidate_id) is None
        assert session.get(SubtitleArtifact, artifact_id) is None
        assert all(session.get(TaskEvent, event_id) is None for event_id in event_ids)
        assert repo.get_video_task(unrelated_task_id) is not None
        assert repo.get_job(unrelated_job_id) is not None


def test_delete_task_returns_404_for_missing_task(token_client):
    response = token_client.delete("/api/v1/tasks/999999")

    assert response.status_code == 404
    assert response.json() == {"detail": "Task not found"}


def test_delete_completed_task_can_remove_only_its_placed_subtitle(
    token_app,
    token_client,
    tmp_path: Path,
):
    media_path = tmp_path / "Movie.mkv"
    subtitle_path = tmp_path / "Movie.zh-cn.default.srt"
    unrelated_path = tmp_path / "Movie.commentary.srt"
    media_path.write_bytes(b"video")
    subtitle_path.write_text("sidecar subtitle", encoding="utf-8")
    unrelated_path.write_text("keep", encoding="utf-8")
    _job_id, task_id = _create_observable_task(token_app, video_path=str(media_path))
    with session_scope(token_app.state.engine) as session:
        task = Repository(session).get_video_task(task_id)
        assert task is not None
        task.video_path_resolved = str(media_path)
        task.result_subtitle_path = str(subtitle_path)
        task.artifacts[0].path = str(subtitle_path)

    response = token_client.delete(f"/api/v1/tasks/{task_id}?delete_subtitle=true")

    assert response.status_code == 204
    assert not subtitle_path.exists()
    assert unrelated_path.exists()
    assert media_path.exists()


def test_delete_non_completed_task_never_removes_subtitle(
    token_app,
    token_client,
    tmp_path: Path,
):
    media_path = tmp_path / "Movie.mkv"
    subtitle_path = tmp_path / "Movie.zh-cn.default.srt"
    media_path.write_bytes(b"video")
    subtitle_path.write_text("keep", encoding="utf-8")
    _job_id, task_id = _create_observable_task(token_app, video_path=str(media_path))
    with session_scope(token_app.state.engine) as session:
        repo = Repository(session)
        task = repo.get_video_task(task_id)
        assert task is not None
        task.video_path_resolved = str(media_path)
        task.result_subtitle_path = str(subtitle_path)
        task.artifacts[0].path = str(subtitle_path)
        repo.update_video_task_status(task_id, "failed", "test_failure")

    response = token_client.delete(f"/api/v1/tasks/{task_id}?delete_subtitle=true")

    assert response.status_code == 204
    assert subtitle_path.exists()
    assert media_path.exists()
