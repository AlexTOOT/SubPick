from pathlib import Path

from fastapi.testclient import TestClient

from subtitle_sidecar.main import create_app


def test_path_mapping_check_uses_explicit_sample_without_saving(tmp_path: Path) -> None:
    media = tmp_path / "media"
    media.mkdir()
    video = media / "Movie.mkv"
    video.write_bytes(b"video")
    app = create_app(data_dir=tmp_path / "data", job_processor=lambda task_id: None)

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/paths/check",
            json={
                "mappings": [{"from_path": "/mp/media", "to_path": str(media)}],
                "sample_path": "/mp/media/Movie.mkv",
            },
        )
        settings = client.get("/api/v1/paths/settings")

    assert response.status_code == 200
    assert response.json() == {
        "original_path": "/mp/media/Movie.mkv",
        "resolved_path": str(video),
        "strategy": "mapping",
        "exists": True,
    }
    assert settings.json()["mappings"] == []


def test_path_mapping_is_saved_restored_and_clears_callback_issue(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    media = tmp_path / "media"
    media.mkdir()
    video = media / "Movie.mkv"
    video.write_bytes(b"video")
    app = create_app(data_dir=data_dir, job_processor=lambda task_id: None)

    with TestClient(app) as client:
        callback = client.post(
            "/api/v1/add-job",
            json={"physical_video_file_full_path": "/mp/media/Movie.mkv"},
        )
        before = client.get("/api/v1/paths/settings")
        saved = client.put(
            "/api/v1/paths/settings",
            json={"mappings": [{"from_path": "/mp/media", "to_path": str(media)}]},
        )

    assert callback.status_code == 200
    assert before.json()["needs_attention"] is True
    assert saved.status_code == 200
    assert saved.json()["latest_moviepilot_path"] == "/mp/media/Movie.mkv"
    assert saved.json()["path_issue"] is None
    assert saved.json()["needs_attention"] is False

    restarted = create_app(data_dir=data_dir, job_processor=lambda task_id: None)
    with TestClient(restarted) as client:
        restored = client.get("/api/v1/paths/settings")
        diagnostics = client.get("/api/v1/diagnostics")

    assert restored.json()["mappings"] == [
        {"from_path": "/mp/media", "to_path": str(media)}
    ]
    assert diagnostics.json()["moviepilot"]["last_received_path"] == "/mp/media/Movie.mkv"


def test_path_mapping_test_requires_a_sample_when_no_callback_exists(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path, job_processor=lambda task_id: None)

    with TestClient(app) as client:
        response = client.post("/api/v1/paths/check", json={"mappings": []})

    assert response.status_code == 422


def test_paths_are_included_in_settings_backup_and_import(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    media = tmp_path / "media"
    media.mkdir()
    app = create_app(data_dir=data_dir, job_processor=lambda task_id: None)

    with TestClient(app) as client:
        saved = client.put(
            "/api/v1/paths/settings",
            json={"mappings": [{"from_path": "/old", "to_path": str(media)}]},
        )
        exported = client.get("/api/v1/settings/export")
        client.put("/api/v1/paths/settings", json={"mappings": []})
        imported = client.put("/api/v1/settings/import", json=exported.json())
        restored = client.get("/api/v1/paths/settings")

    assert saved.status_code == 200
    assert exported.json()["settings"]["paths"] == {
        "mappings": [{"from": "/old", "to": str(media)}]
    }
    assert imported.status_code == 200
    assert restored.json()["mappings"] == [
        {"from_path": "/old", "to_path": str(media)}
    ]
