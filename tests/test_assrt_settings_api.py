from pathlib import Path

from fastapi.testclient import TestClient

from subtitle_sidecar.main import create_app


class FakeAssrtProvider:
    def __init__(self, config) -> None:
        self.config = config

    def quota(self) -> int:
        return 5


def test_assrt_settings_persist_without_returning_token_and_quota_can_be_checked(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path / "data", job_processor=lambda task_id: None)
    app.state.assrt_provider_factory = FakeAssrtProvider

    with TestClient(app) as client:
        initial = client.get("/api/v1/providers/assrt/settings")
        saved = client.put(
            "/api/v1/providers/assrt/settings",
            json={
                "enabled": True,
                "token": "assrt-test-secret",
                "timeout_seconds": 20,
                "requests_per_minute": 5,
            },
        )
        loaded = client.get("/api/v1/providers/assrt/settings")
        quota = client.post("/api/v1/providers/assrt/quota")
        diagnostics = client.get("/api/v1/diagnostics")

    assert initial.json()["status"] == "disabled"
    assert saved.status_code == loaded.status_code == quota.status_code == 200
    assert loaded.json() == {
        "enabled": True,
        "token_configured": True,
        "timeout_seconds": 20.0,
        "requests_per_minute": 5,
        "status": "configured",
    }
    assert quota.json() == {"quota": 5}
    assert "assrt-test-secret" not in f"{loaded.json()}{diagnostics.json()}"
