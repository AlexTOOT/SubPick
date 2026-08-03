from pathlib import Path

from fastapi.testclient import TestClient

from subtitle_sidecar.api.schemas import SubliminalProviderAuthenticationRequest
from subtitle_sidecar.api.schemas import SubliminalProviderSettingsRequest
from subtitle_sidecar.main import create_app


def test_subliminal_settings_request_authentication_defaults_are_isolated() -> None:
    first = SubliminalProviderSettingsRequest(
        enabled=True,
        providers=["opensubtitlescom"],
        languages=["zh-cn"],
    )
    second = SubliminalProviderSettingsRequest(
        enabled=True,
        providers=["opensubtitlescom"],
        languages=["zh-hant"],
    )

    first.authentication["opensubtitlescom"] = SubliminalProviderAuthenticationRequest()

    assert second.authentication == {}


def test_subliminal_settings_persist_without_returning_secrets(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
providers:
  subliminal:
    enabled: true
    providers: [opensubtitlescom]
    languages: [zh-cn]
    authentication:
      opensubtitlescom:
        username: yaml-user
        password: yaml-password
        apikey: yaml-key
""".strip(),
        encoding="utf-8",
    )
    app = create_app(
        data_dir=tmp_path / "data",
        config_path=config_path,
        job_processor=lambda task_id: None,
    )

    with TestClient(app) as client:
        initial = client.get("/api/v1/providers/subliminal/settings")
        saved = client.put(
            "/api/v1/providers/subliminal/settings",
            json={
                "enabled": True,
                "providers": ["opensubtitlescom"],
                "languages": ["zh-cn", "zh-hant"],
                "authentication": {
                    "opensubtitlescom": {
                        "username": "osc-user",
                        "password": "osc-password",
                        "apikey": "osc-key",
                    },
                },
            },
        )
        loaded = client.get("/api/v1/providers/subliminal/settings")
        diagnostics = client.get("/api/v1/diagnostics")

    assert initial.status_code == 200
    assert initial.json()["authentication"]["opensubtitlescom"]["password_configured"] is True
    assert saved.status_code == loaded.status_code == 200
    response = loaded.json()
    assert response["providers"] == ["opensubtitlescom"]
    serialized = f"{response}{diagnostics.json()}"
    assert "yaml-password" not in serialized
    assert "yaml-key" not in serialized
    assert "osc-password" not in serialized
    assert "osc-key" not in serialized


def test_opensubtitlescom_requires_complete_authentication_when_enabled(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path / "data", job_processor=lambda task_id: None)
    with TestClient(app) as client:
        rejected = client.put(
            "/api/v1/providers/subliminal/settings",
            json={
                "enabled": True,
                "providers": ["opensubtitlescom"],
                "languages": ["zh-cn"],
                "authentication": {"opensubtitlescom": {"username": "user"}},
            },
        )
        retired_legacy = client.put(
            "/api/v1/providers/subliminal/settings",
            json={
                "enabled": True,
                "providers": ["opensubtitles"],
                "languages": ["zh-cn"],
            },
        )

    assert rejected.status_code == 422
    assert retired_legacy.status_code == 422
