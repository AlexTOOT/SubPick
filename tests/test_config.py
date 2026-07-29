from pathlib import Path

import yaml

from subtitle_sidecar.config import (
    DEFAULT_PROVIDER_ORDER,
    DEFAULT_SUBLIMINAL_PROVIDERS,
    AppSettings,
    PathMapping,
    load_settings,
    merge_subliminal_provider_settings,
)
from subtitle_sidecar.main import _provider_order


def test_default_settings_are_safe(tmp_path: Path) -> None:
    settings = AppSettings(data_dir=tmp_path)

    assert settings.server.host == "0.0.0.0"
    assert settings.server.port == 19035
    assert settings.subtitles.overwrite is False
    assert settings.subtitles.max_candidate_attempts == 4
    assert settings.queue.search_interval_seconds == 60.0
    assert settings.sync.keep_backup is True
    assert settings.ai.enabled is False
    assert settings.providers.order == list(DEFAULT_PROVIDER_ORDER)
    assert settings.providers.subliminal.providers == list(DEFAULT_SUBLIMINAL_PROVIDERS)
    assert settings.providers.zimuku.moviepilot_ocr_url == "http://moviepilot-ocr:9899"


def test_subpick_home_creates_single_directory_runtime_layout(
    tmp_path: Path,
    monkeypatch,
) -> None:
    appdata = tmp_path / "subpick"
    monkeypatch.setenv("SUBPICK_HOME", str(appdata))

    settings = load_settings()

    assert settings.appdata_dir == appdata
    assert settings.runtime_config_path == appdata / "config.yaml"
    assert settings.data_dir == appdata / "data"
    assert settings.cache_dir == appdata / "cache"
    assert settings.runtime_config_path.is_file()
    assert settings.data_dir.is_dir()
    assert settings.cache_dir.is_dir()
    payload = yaml.safe_load(settings.runtime_config_path.read_text(encoding="utf-8"))
    assert payload["paths"]["mappings"] == []
    assert payload["server"]["token"] == ""
    assert payload["providers"]["zimuku"]["moviepilot_ocr_url"] == (
        "http://moviepilot-ocr:9899"
    )


def test_subpick_home_does_not_replace_existing_config(
    tmp_path: Path,
    monkeypatch,
) -> None:
    appdata = tmp_path / "subpick"
    appdata.mkdir()
    config_path = appdata / "config.yaml"
    config_path.write_text("queue:\n  search_interval_seconds: 12\n", encoding="utf-8")
    monkeypatch.setenv("SUBPICK_HOME", str(appdata))

    settings = load_settings()

    assert settings.queue.search_interval_seconds == 12
    assert config_path.read_text(encoding="utf-8") == (
        "queue:\n  search_interval_seconds: 12\n"
    )


def test_path_mapping_rewrites_prefix() -> None:
    mapping = PathMapping(from_path="/moviepilot/media", to_path="/media")

    assert mapping.rewrite("/moviepilot/media/Movies/A.mkv") == "/media/Movies/A.mkv"
    assert mapping.rewrite("/other/A.mkv") is None


def test_example_config_excludes_deprecated_library_paths_and_has_provider_block() -> None:
    example_path = Path("config.example.yaml")

    payload = yaml.safe_load(example_path.read_text(encoding="utf-8"))

    assert "libraries" not in payload["paths"]
    assert payload["providers"]["subliminal"]["enabled"] is True
    assert payload["providers"]["subliminal"]["providers"] == [
        "opensubtitles",
        "opensubtitlescom",
    ]
    assert (
        payload["providers"]["zimuku"]["moviepilot_ocr_url"]
        == "http://moviepilot-ocr:9899"
    )


def test_load_settings_reads_yaml_provider_settings(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "\n".join(
            [
                "data_dir: /yaml-data",
                "server:",
                "  token: yaml-token",
                "providers:",
                "  order: [assrt, subdl, subliminal]",
                "  subliminal:",
                "    enabled: true",
                "    providers:",
                "      - opensubtitles",
                "    languages:",
                "      - zho",
                "      - chi",
                "queue:",
                "  search_interval_seconds: 15",
            ]
        ),
        encoding="utf-8",
    )

    settings = load_settings(config_path=config_path, data_dir=tmp_path, token="override-token")

    assert settings.data_dir == tmp_path
    assert settings.server.token == "override-token"
    assert settings.providers.subliminal.enabled is True
    assert settings.providers.order == ["assrt", "subdl", "subliminal"]
    assert settings.providers.subliminal.providers == ["opensubtitles"]
    assert settings.providers.subliminal.languages == ["zho", "chi"]
    assert settings.queue.search_interval_seconds == 15.0


def test_load_settings_ignores_legacy_library_paths(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "paths:\n  libraries:\n    - /legacy/media\n",
        encoding="utf-8",
    )

    settings = load_settings(config_path=config_path, data_dir=tmp_path)

    assert not hasattr(settings.paths, "libraries")


def test_legacy_yaml_without_provider_list_uses_subliminal_26_defaults(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "providers:\n  subliminal:\n    enabled: true\n    languages: [zh-cn]\n",
        encoding="utf-8",
    )

    settings = load_settings(config_path=config_path, data_dir=tmp_path)

    assert settings.providers.subliminal.enabled is True
    assert settings.providers.subliminal.providers == list(DEFAULT_SUBLIMINAL_PROVIDERS)


def test_legacy_db_setting_without_provider_list_keeps_defaults(tmp_path: Path) -> None:
    defaults = AppSettings(data_dir=tmp_path).providers.subliminal

    merged = merge_subliminal_provider_settings(
        defaults,
        {"enabled": True, "languages": ["zh-hant"]},
    )

    assert merged.enabled is True
    assert merged.languages == ["zh-hant"]
    assert merged.providers == list(DEFAULT_SUBLIMINAL_PROVIDERS)


def test_provider_order_uses_persisted_app_setting_before_config(tmp_path: Path) -> None:
    settings = AppSettings(data_dir=tmp_path)
    settings.providers.order = ["subliminal", "assrt"]

    assert _provider_order({"order": ["zimuku", "assrt"]}, settings) == ["zimuku", "assrt"]
    assert _provider_order(None, settings) == ["subliminal", "assrt"]
