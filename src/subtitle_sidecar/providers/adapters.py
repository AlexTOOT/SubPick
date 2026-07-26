from __future__ import annotations

from collections.abc import Mapping
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

from subtitle_sidecar import ADAPTER_VERSIONS
from subtitle_sidecar.config import DEFAULT_PROVIDER_ORDER
from subtitle_sidecar.providers.assrt_adapter import AssrtProvider
from subtitle_sidecar.providers.base import ProviderAdapterFactory, ProviderAdapterMetadata, SubtitleProvider
from subtitle_sidecar.providers.subdl_adapter import SubdlProvider
from subtitle_sidecar.providers.subliminal_adapter import SubliminalProvider
from subtitle_sidecar.providers.zimuku_adapter import ZimukuProvider


ENTRY_POINT_GROUP = "subtitle_sidecar.providers"


class SubliminalAdapterFactory:
    metadata = ProviderAdapterMetadata(
        name="subliminal",
        display_name="Subliminal",
        version=ADAPTER_VERSIONS["subliminal"],
        homepage="https://github.com/Diaoul/subliminal",
        media_scopes=("movie", "episode"),
        lookup_keys=("imdb", "title", "original_title", "filename"),
        transport="python-library",
        requires_auth=False,
        requires_captcha=False,
        supports_archives=False,
        recommended_interval_seconds=60.0,
        stable_candidate_identity=True,
    )

    def create(self, settings: Mapping[str, Any]) -> SubtitleProvider:
        authentication = settings.get("authentication") or {}
        return SubliminalProvider(
            providers=_string_list(settings.get("providers")),
            languages=_string_list(settings.get("languages")),
            authentication={
                str(name): dict(credentials)
                for name, credentials in authentication.items()
                if isinstance(credentials, Mapping)
            },
        )


class AssrtAdapterFactory:
    metadata = ProviderAdapterMetadata(
        name="assrt",
        display_name="ASSRT",
        version=ADAPTER_VERSIONS["assrt"],
        homepage="https://assrt.net/",
        media_scopes=("movie", "episode", "season_pack"),
        lookup_keys=("title", "original_title", "filename"),
        transport="api",
        requires_auth=True,
        requires_captcha=False,
        supports_archives=True,
        recommended_interval_seconds=12.0,
        stable_candidate_identity=True,
        attribution="字幕服务由 assrt.net 提供",
    )

    def create(self, settings: Mapping[str, Any]) -> SubtitleProvider:
        return AssrtProvider(
            token=str(settings.get("token") or ""),
            timeout_seconds=float(settings.get("timeout_seconds") or 15.0),
            requests_per_minute=int(settings.get("requests_per_minute") or 5),
            negative_cache=settings.get("_negative_cache"),
        )


class SubdlAdapterFactory:
    metadata = ProviderAdapterMetadata(
        name="subdl",
        display_name="SubDL",
        version=ADAPTER_VERSIONS["subdl"],
        homepage="https://subdl.com/developers",
        media_scopes=("movie", "episode"),
        lookup_keys=("imdb", "tmdb", "title", "original_title", "filename"),
        transport="api",
        requires_auth=True,
        requires_captcha=False,
        supports_archives=True,
        recommended_interval_seconds=3.0,
        stable_candidate_identity=True,
        attribution="字幕服务由 SubDL 提供",
    )

    def create(self, settings: Mapping[str, Any]) -> SubtitleProvider:
        return SubdlProvider(
            api_key=str(settings.get("api_key") or ""),
            timeout_seconds=float(settings.get("timeout_seconds") or 15.0),
            requests_per_minute=int(settings.get("requests_per_minute") or 20),
            use_api_key_for_downloads=bool(settings.get("use_api_key_for_downloads")),
        )


class ZimukuAdapterFactory:
    metadata = ProviderAdapterMetadata(
        name="zimuku",
        display_name="Zimuku",
        version=ADAPTER_VERSIONS["zimuku"],
        homepage="https://zimuku.org/",
        media_scopes=("movie", "episode", "season_pack"),
        lookup_keys=("title", "original_title", "filename"),
        transport="web",
        requires_auth=False,
        requires_captcha=True,
        supports_archives=True,
        recommended_interval_seconds=8.0,
        stable_candidate_identity=True,
        attribution="字幕服务由 Zimuku 提供",
    )

    def create(self, settings: Mapping[str, Any]) -> SubtitleProvider:
        return ZimukuProvider(
            anti_captcha_api_key=str(settings.get("anti_captcha_api_key") or ""),
            moviepilot_ocr_url=str(settings.get("moviepilot_ocr_url") or ""),
            base_url=str(settings.get("base_url") or "https://srtku.com"),
            timeout_seconds=float(settings.get("timeout_seconds") or 30.0),
            request_delay_seconds=float(settings.get("request_delay_seconds") or 1.0),
            captcha_debug_dir=(
                Path(str(settings["captcha_debug_dir"]))
                if settings.get("captcha_debug_capture") and settings.get("captcha_debug_dir")
                else None
            ),
        )


def discover_adapter_factories() -> dict[str, ProviderAdapterFactory]:
    """Load bundled adapters plus trusted third-party entry-point adapters."""

    factories: dict[str, ProviderAdapterFactory] = {
        factory.metadata.name: factory
        for factory in (
            SubliminalAdapterFactory(),
            AssrtAdapterFactory(),
            SubdlAdapterFactory(),
            ZimukuAdapterFactory(),
        )
    }
    try:
        discovered = entry_points(group=ENTRY_POINT_GROUP)
    except TypeError:  # pragma: no cover - Python 3.10 compatibility for external packages
        discovered = entry_points().get(ENTRY_POINT_GROUP, [])
    for entry_point in discovered:
        factory = entry_point.load()
        if isinstance(factory, type):
            factory = factory()
        metadata = getattr(factory, "metadata", None)
        create = getattr(factory, "create", None)
        if isinstance(metadata, ProviderAdapterMetadata) and callable(create):
            factories.setdefault(metadata.name, factory)
    return factories


def build_enabled_adapters(
    settings_by_name: Mapping[str, Mapping[str, Any]],
    order: list[str] | tuple[str, ...] | None = None,
) -> list[SubtitleProvider]:
    providers: list[SubtitleProvider] = []
    for name, factory in _ordered_factories(discover_adapter_factories(), order).items():
        settings = settings_by_name.get(name) or {}
        if bool(settings.get("enabled")):
            providers.append(factory.create(settings))
    return providers


def build_recommended_provider_intervals(
    order: list[str] | tuple[str, ...] | None = None,
) -> dict[str, float]:
    intervals: dict[str, float] = {}
    for name, factory in _ordered_factories(discover_adapter_factories(), order).items():
        interval = getattr(factory.metadata, "recommended_interval_seconds", 0.0)
        intervals[name] = max(0.0, float(interval or 0.0))
    return intervals


def _string_list(value: Any) -> list[str]:
    return [str(item) for item in value] if isinstance(value, list) else []


def _ordered_factories(
    factories: Mapping[str, ProviderAdapterFactory],
    order: list[str] | tuple[str, ...] | None = None,
) -> dict[str, ProviderAdapterFactory]:
    configured_order = tuple(str(name) for name in (order or DEFAULT_PROVIDER_ORDER))
    ordered: dict[str, ProviderAdapterFactory] = {}
    for name in configured_order:
        if name in factories and name not in ordered:
            ordered[name] = factories[name]
    for name, factory in factories.items():
        if name not in ordered:
            ordered[name] = factory
    return ordered
