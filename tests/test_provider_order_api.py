from pathlib import Path

from fastapi.testclient import TestClient

import subtitle_sidecar.api.routes as api_routes
from subtitle_sidecar.main import create_app
from subtitle_sidecar.providers.base import ProviderAdapterMetadata


class FakeFactory:
    def __init__(self, name: str) -> None:
        self.metadata = ProviderAdapterMetadata(
            name=name,
            display_name=name.replace("_", " ").title(),
            version="9.1.0",
            media_scopes=("movie", "episode"),
            lookup_keys=("tmdb", "title"),
            transport="api",
            requires_auth=True,
            requires_captcha=False,
            supports_archives=True,
            recommended_interval_seconds=7.5,
            stable_candidate_identity=True,
        )

    def create(self, settings):
        raise AssertionError("metadata API must not instantiate adapters")


class FakeAssrtProvider:
    def __init__(self, config) -> None:
        self.config = config

    def quota(self) -> int:
        return 5


def _factories_with_external():
    factories = api_routes.discover_adapter_factories()
    return {**factories, "external_demo": FakeFactory("external_demo")}


def test_provider_order_reports_metadata_enabled_state_and_persists(
    tmp_path: Path,
    monkeypatch,
) -> None:
    factories = _factories_with_external()
    monkeypatch.setattr(api_routes, "discover_adapter_factories", lambda: factories)
    app = create_app(data_dir=tmp_path / "data", job_processor=lambda task_id: None)
    app.state.settings.providers.adapters["external_demo"] = {"enabled": True}
    app.state.assrt_provider_factory = FakeAssrtProvider

    with TestClient(app) as client:
        client.put(
            "/api/v1/providers/assrt/settings",
            json={"enabled": True, "token": "secret"},
        )
        initial = client.get("/api/v1/providers/order")
        saved = client.put(
            "/api/v1/providers/order",
            json={"order": ["external_demo", "assrt"]},
        )
        reloaded = client.get("/api/v1/providers/order")

    assert initial.status_code == saved.status_code == reloaded.status_code == 200
    assert saved.json()["order"] == [
        "external_demo",
        "assrt",
        "subliminal",
        "subdl",
        "zimuku",
    ]
    assert reloaded.json() == saved.json()
    adapters = {item["name"]: item for item in saved.json()["adapters"]}
    assert adapters["assrt"]["enabled"] is True
    assert adapters["external_demo"] == {
        "name": "external_demo",
        "display_name": "External Demo",
        "version": "9.1.0",
        "enabled": True,
        "capabilities": {
            "media_scopes": ["movie", "episode"],
            "lookup_keys": ["tmdb", "title"],
            "transport": "api",
            "requires_auth": True,
            "requires_captcha": False,
            "supports_archives": True,
            "recommended_interval_seconds": 7.5,
            "stable_candidate_identity": True,
        },
    }


def test_provider_order_rejects_duplicate_and_unknown_names(
    tmp_path: Path,
    monkeypatch,
) -> None:
    factories = _factories_with_external()
    monkeypatch.setattr(api_routes, "discover_adapter_factories", lambda: factories)
    app = create_app(data_dir=tmp_path / "data", job_processor=lambda task_id: None)

    with TestClient(app) as client:
        duplicate = client.put(
            "/api/v1/providers/order",
            json={"order": ["assrt", "assrt"]},
        )
        unknown = client.put(
            "/api/v1/providers/order",
            json={"order": ["not-installed"]},
        )

    assert duplicate.status_code == 400
    assert duplicate.json()["detail"] == "Duplicate provider names: assrt"
    assert unknown.status_code == 400
    assert unknown.json()["detail"] == "Unknown provider names: not-installed"
