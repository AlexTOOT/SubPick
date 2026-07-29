import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from pydantic_settings import BaseSettings, SettingsConfigDict
import yaml


DEFAULT_SUBLIMINAL_PROVIDERS = (
    "opensubtitles",
    "opensubtitlescom",
)
DEFAULT_PROVIDER_ORDER = (
    "subliminal",
    "assrt",
    "subdl",
    "zimuku",
)


class ServerSettings(BaseModel):
    host: str = "0.0.0.0"
    port: int = 19035
    token: str = ""


class PathMapping(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    from_path: str = Field(alias="from")
    to_path: str = Field(alias="to")

    def rewrite(self, path: str) -> str | None:
        normalized_from = self.from_path.rstrip("/\\")
        normalized_to = self.to_path.rstrip("/\\")
        if path == normalized_from:
            return normalized_to
        for separator in ("/", "\\"):
            prefix = f"{normalized_from}{separator}"
            if path.startswith(prefix):
                suffix = path[len(prefix) :]
                normalized_suffix = suffix.replace("\\", "/")
                return f"{normalized_to}/{normalized_suffix}"
        return None


class PathsSettings(BaseModel):
    mappings: list[PathMapping] = Field(default_factory=list)


class SubtitleSettings(BaseModel):
    preferred: str = "bilingual"
    fallback: list[str] = Field(default_factory=lambda: ["zh-cn", "zh-hant"])
    overwrite: bool = False
    save_unsynced_on_sync_failure: bool = False
    max_candidate_attempts: int = 4


class ProbeSettings(BaseModel):
    ffprobe_path: str = "ffprobe"
    mkvmerge_path: str = "mkvmerge"
    use_mkvmerge_when_available: bool = True


class SyncSettings(BaseModel):
    enabled: bool = True
    mode: str = "conservative"
    keep_backup: bool = True


class QueueSettings(BaseModel):
    search_interval_seconds: float = 60.0
    recover_interrupted_tasks: bool = True


class LoggingSettings(BaseModel):
    retention_days: int = Field(default=30, ge=1, le=3650)
    max_task_events: int = Field(default=50000, ge=1000, le=1000000)


class JellyfinSettings(BaseModel):
    server_url: str = ""
    api_key: str = ""
    user_id: str = ""


class GitHubSettings(BaseModel):
    api_key: str = ""


class AISettings(BaseModel):
    enabled: bool = False
    provider: str = ""
    model: str = ""
    api_base: str = ""
    api_key: str = ""


class SubliminalAuthenticationSettings(BaseModel):
    username: str = ""
    password: str = ""
    apikey: str = ""


class SubliminalProviderSettings(BaseModel):
    enabled: bool = False
    providers: list[str] = Field(default_factory=lambda: list(DEFAULT_SUBLIMINAL_PROVIDERS))
    languages: list[str] = Field(default_factory=list)
    authentication: dict[str, SubliminalAuthenticationSettings] = Field(
        default_factory=dict
    )


class AssrtProviderSettings(BaseModel):
    enabled: bool = False
    token: str = ""
    timeout_seconds: float = Field(default=15.0, ge=1.0, le=120.0)
    # ASSRT's current effective quota for this deployment is five API calls/minute.
    requests_per_minute: int = Field(default=5, ge=1, le=5)


class SubdlProviderSettings(BaseModel):
    enabled: bool = False
    api_key: str = ""
    timeout_seconds: float = Field(default=15.0, ge=1.0, le=120.0)
    requests_per_minute: int = Field(default=20, ge=1, le=60)
    use_api_key_for_downloads: bool = False


class ZimukuProviderSettings(BaseModel):
    enabled: bool = False
    anti_captcha_api_key: str = ""
    moviepilot_ocr_url: str = "http://moviepilot-ocr:9899"
    captcha_debug_capture: bool = False
    base_url: str = "https://srtku.com"
    timeout_seconds: float = Field(default=30.0, ge=5.0, le=180.0)
    request_delay_seconds: float = Field(default=1.0, ge=0.0, le=30.0)


class ProviderSettings(BaseModel):
    order: list[str] = Field(default_factory=lambda: list(DEFAULT_PROVIDER_ORDER))
    subliminal: SubliminalProviderSettings = Field(default_factory=SubliminalProviderSettings)
    assrt: AssrtProviderSettings = Field(default_factory=AssrtProviderSettings)
    subdl: SubdlProviderSettings = Field(default_factory=SubdlProviderSettings)
    zimuku: ZimukuProviderSettings = Field(default_factory=ZimukuProviderSettings)
    # Third-party entry-point adapters read their own mapping from this namespace.
    adapters: dict[str, dict[str, Any]] = Field(default_factory=dict)


def merge_subliminal_provider_settings(
    defaults: SubliminalProviderSettings,
    stored: dict[str, Any] | None,
) -> SubliminalProviderSettings:
    """Overlay persisted provider settings while retaining omitted YAML credentials."""
    payload = defaults.model_dump()
    if not stored:
        return SubliminalProviderSettings(**payload)
    for key in ("enabled", "providers", "languages"):
        if key in stored:
            payload[key] = stored[key]
    authentication = dict(payload.get("authentication") or {})
    for provider, credentials in (stored.get("authentication") or {}).items():
        merged_credentials = dict(authentication.get(provider) or {})
        if isinstance(credentials, dict):
            merged_credentials.update(credentials)
        authentication[provider] = merged_credentials
    payload["authentication"] = authentication
    return SubliminalProviderSettings(**payload)


def merge_assrt_provider_settings(
    defaults: AssrtProviderSettings,
    stored: dict[str, Any] | None,
) -> AssrtProviderSettings:
    """Overlay persisted ASSRT settings without ever requiring a secret round-trip."""

    payload = defaults.model_dump()
    if not stored:
        return AssrtProviderSettings(**payload)
    for key in ("enabled", "token", "timeout_seconds", "requests_per_minute"):
        if key in stored:
            payload[key] = stored[key]
    return AssrtProviderSettings(**payload)


def merge_subdl_provider_settings(
    defaults: SubdlProviderSettings,
    stored: dict[str, Any] | None,
) -> SubdlProviderSettings:
    """Overlay persisted SubDL settings without returning an API key to callers."""

    payload = defaults.model_dump()
    if not stored:
        return SubdlProviderSettings(**payload)
    for key in (
        "enabled",
        "api_key",
        "timeout_seconds",
        "requests_per_minute",
        "use_api_key_for_downloads",
    ):
        if key in stored:
            payload[key] = stored[key]
    return SubdlProviderSettings(**payload)


def merge_zimuku_provider_settings(
    defaults: ZimukuProviderSettings,
    stored: dict[str, Any] | None,
) -> ZimukuProviderSettings:
    """Overlay persisted Zimuku settings without returning the captcha key."""

    payload = defaults.model_dump()
    if not stored:
        return ZimukuProviderSettings(**payload)
    for key in (
        "enabled",
        "anti_captcha_api_key",
        "moviepilot_ocr_url",
        "captcha_debug_capture",
        "base_url",
        "timeout_seconds",
        "request_delay_seconds",
    ):
        if key in stored:
            payload[key] = stored[key]
    return ZimukuProviderSettings(**payload)


class AppSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SUBTITLE_SIDECAR_",
        env_nested_delimiter="__",
    )

    data_dir: Path = Path("/data")
    cache_dir: Path = Path("/cache")
    appdata_dir: Path | None = Field(default=None, exclude=True)
    runtime_config_path: Path | None = Field(default=None, exclude=True)
    config_version: int = Field(default=1, ge=1)
    server: ServerSettings = Field(default_factory=ServerSettings)
    paths: PathsSettings = Field(default_factory=PathsSettings)
    subtitles: SubtitleSettings = Field(default_factory=SubtitleSettings)
    providers: ProviderSettings = Field(default_factory=ProviderSettings)
    probe: ProbeSettings = Field(default_factory=ProbeSettings)
    sync: SyncSettings = Field(default_factory=SyncSettings)
    queue: QueueSettings = Field(default_factory=QueueSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    jellyfin: JellyfinSettings = Field(default_factory=JellyfinSettings)
    github: GitHubSettings = Field(default_factory=GitHubSettings)
    ai: AISettings = Field(default_factory=AISettings)

    @property
    def database_url(self) -> str:
        return f"sqlite:///{self.data_dir / 'subtitle-sidecar.sqlite3'}"


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_settings(
    config_path: Path | None = None,
    data_dir: Path | None = None,
    token: str | None = None,
) -> AppSettings:
    resolved_config_path = config_path
    appdata_value = os.environ.get("SUBPICK_HOME", "").strip()
    appdata_dir = Path(appdata_value) if appdata_value else None
    if resolved_config_path is None and appdata_dir is not None:
        _ensure_appdata_layout(appdata_dir)
        resolved_config_path = appdata_dir / "config.yaml"

    payload: dict[str, Any] = {}
    if resolved_config_path is not None and resolved_config_path.exists():
        loaded = yaml.safe_load(resolved_config_path.read_text(encoding="utf-8")) or {}
        if not isinstance(loaded, dict):
            raise ValueError("Config file must contain a YAML object at the top level")
        payload = _deep_merge(payload, loaded)

    if data_dir is not None:
        payload["data_dir"] = data_dir
        payload.setdefault("cache_dir", data_dir / "cache")
    elif environment_data_dir := os.environ.get("SUBTITLE_SIDECAR_DATA_DIR", "").strip():
        if "SUBTITLE_SIDECAR_CACHE_DIR" not in os.environ:
            payload.setdefault("cache_dir", Path(environment_data_dir) / "cache")
    elif appdata_dir is not None and "SUBTITLE_SIDECAR_DATA_DIR" not in os.environ:
        payload.setdefault("data_dir", appdata_dir / "data")
        if "SUBTITLE_SIDECAR_CACHE_DIR" not in os.environ:
            payload.setdefault("cache_dir", appdata_dir / "cache")

    if token is not None:
        payload["server"] = _deep_merge(payload.get("server", {}), {"token": token})

    payload["appdata_dir"] = appdata_dir
    payload["runtime_config_path"] = resolved_config_path
    return AppSettings(**payload)


def _ensure_appdata_layout(appdata_dir: Path) -> None:
    appdata_dir.mkdir(parents=True, exist_ok=True)
    (appdata_dir / "data").mkdir(parents=True, exist_ok=True)
    (appdata_dir / "cache").mkdir(parents=True, exist_ok=True)
    config_path = appdata_dir / "config.yaml"
    if config_path.exists():
        return
    config_path.write_text(
        yaml.safe_dump(
            _generated_config_payload(appdata_dir),
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def _generated_config_payload(appdata_dir: Path) -> dict[str, Any]:
    return {
        "config_version": 1,
        "data_dir": str(appdata_dir / "data"),
        "cache_dir": str(appdata_dir / "cache"),
        "server": {"host": "0.0.0.0", "port": 19035, "token": ""},
        "paths": {"mappings": []},
        "subtitles": {
            "preferred": "bilingual",
            "fallback": ["zh-cn", "zh-hant"],
            "overwrite": False,
            "save_unsynced_on_sync_failure": False,
            "max_candidate_attempts": 4,
        },
        "queue": {
            "search_interval_seconds": 60,
            "recover_interrupted_tasks": True,
        },
        "logging": {"retention_days": 30, "max_task_events": 50000},
        "jellyfin": {"server_url": "", "api_key": "", "user_id": ""},
        "github": {"api_key": ""},
        "providers": {
            "order": list(DEFAULT_PROVIDER_ORDER),
            "subliminal": {
                "enabled": True,
                "providers": list(DEFAULT_SUBLIMINAL_PROVIDERS),
                "languages": ["zh-cn", "zh-hant"],
                "authentication": {},
            },
            "assrt": {"enabled": False, "token": ""},
            "subdl": {"enabled": False, "api_key": ""},
            "zimuku": {
                "enabled": False,
                "moviepilot_ocr_url": "http://moviepilot-ocr:9899",
                "anti_captcha_api_key": "",
            },
        },
    }
