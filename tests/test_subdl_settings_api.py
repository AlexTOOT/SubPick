from pathlib import Path

from fastapi.testclient import TestClient

from subtitle_sidecar.main import create_app


class FakeSubdlProvider:
    def __init__(self, config) -> None:
        self.config = config

    def usage(self):
        return {
            "plan": {"name": "Free", "is_pro": False},
            "usage": {
                "search": {"remaining": 2000, "limit": 2000, "reset_at": "2026-07-14T00:00:00Z"},
                "downloads": {"remaining": 50, "limit": 50},
            },
        }


def test_subdl_settings_persist_without_returning_key_and_usage_is_available(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path / "data", job_processor=lambda task_id: None)
    app.state.subdl_provider_factory = FakeSubdlProvider

    with TestClient(app) as client:
        saved = client.put(
            "/api/v1/providers/subdl/settings",
            json={
                "enabled": True,
                "api_key": "subdl-test-secret",
                "timeout_seconds": 20,
                "requests_per_minute": 20,
                "use_api_key_for_downloads": False,
            },
        )
        loaded = client.get("/api/v1/providers/subdl/settings")
        usage = client.post("/api/v1/providers/subdl/usage")
        diagnostics = client.get("/api/v1/diagnostics")

    assert saved.status_code == loaded.status_code == usage.status_code == 200
    assert loaded.json() == {
        "enabled": True,
        "api_key_configured": True,
        "timeout_seconds": 20.0,
        "requests_per_minute": 20,
        "use_api_key_for_downloads": False,
        "status": "configured",
    }
    assert usage.json() == {
        "plan_name": "Free",
        "is_pro": False,
        "search_remaining": 2000,
        "search_limit": 2000,
        "download_remaining": 50,
        "download_limit": 50,
        "reset_at": "2026-07-14T00:00:00Z",
    }
    assert "subdl-test-secret" not in f"{loaded.json()}{diagnostics.json()}"
