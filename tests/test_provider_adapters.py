from __future__ import annotations

from pathlib import Path
from typing import Any

import subtitle_sidecar.providers.adapters as provider_adapters
from subtitle_sidecar.providers.adapters import (
    AssrtAdapterFactory,
    SubdlAdapterFactory,
    SubliminalAdapterFactory,
    ZimukuAdapterFactory,
    build_enabled_adapters,
    build_recommended_provider_intervals,
)
from subtitle_sidecar.providers.base import (
    DownloadedSubtitle,
    ProviderAdapterMetadata,
    SubtitleCandidate,
    SubtitleProvider,
    SubtitleSearchRequest,
)


class FakeProvider:
    def __init__(self, name: str) -> None:
        self.name = name

    def search(self, request: SubtitleSearchRequest) -> list[SubtitleCandidate]:
        return []

    def download(self, candidate: SubtitleCandidate, target_dir: Path) -> DownloadedSubtitle:
        raise NotImplementedError


class FakeFactory:
    def __init__(self, name: str) -> None:
        self.metadata = ProviderAdapterMetadata(
            name=name,
            display_name=name.title(),
            version="1.0.0",
        )

    def create(self, settings: dict[str, Any]) -> SubtitleProvider:
        return FakeProvider(self.metadata.name)


def test_builtin_adapter_metadata_declares_capabilities() -> None:
    factories = [
        SubliminalAdapterFactory(),
        AssrtAdapterFactory(),
        SubdlAdapterFactory(),
        ZimukuAdapterFactory(),
    ]

    metadata_by_name = {factory.metadata.name: factory.metadata for factory in factories}

    assert metadata_by_name["subliminal"].media_scopes == ("movie", "episode")
    assert "imdb" in metadata_by_name["subliminal"].lookup_keys
    assert metadata_by_name["subliminal"].transport == "python-library"
    assert metadata_by_name["assrt"].media_scopes == ("movie", "episode", "season_pack")
    assert metadata_by_name["assrt"].requires_auth is True
    assert metadata_by_name["assrt"].recommended_interval_seconds == 12.0
    assert metadata_by_name["subdl"].lookup_keys == (
        "imdb",
        "tmdb",
        "title",
        "original_title",
        "filename",
    )
    assert metadata_by_name["zimuku"].requires_captcha is True
    assert all(metadata.stable_candidate_identity for metadata in metadata_by_name.values())


def test_build_enabled_adapters_uses_configured_top_level_order(monkeypatch) -> None:
    factories = {
        "subliminal": FakeFactory("subliminal"),
        "assrt": FakeFactory("assrt"),
        "subdl": FakeFactory("subdl"),
        "zimuku": FakeFactory("zimuku"),
    }
    monkeypatch.setattr(provider_adapters, "discover_adapter_factories", lambda: factories)

    providers = build_enabled_adapters(
        {
            "subliminal": {"enabled": True},
            "assrt": {"enabled": True},
            "subdl": {"enabled": True},
            "zimuku": {"enabled": True},
        },
        order=["zimuku", "assrt", "subdl", "subliminal"],
    )

    assert [provider.name for provider in providers] == [
        "zimuku",
        "assrt",
        "subdl",
        "subliminal",
    ]


def test_build_enabled_adapters_appends_unknown_external_adapters_stably(monkeypatch) -> None:
    factories = {
        "subliminal": FakeFactory("subliminal"),
        "external_a": FakeFactory("external_a"),
        "assrt": FakeFactory("assrt"),
        "external_b": FakeFactory("external_b"),
    }
    monkeypatch.setattr(provider_adapters, "discover_adapter_factories", lambda: factories)

    providers = build_enabled_adapters(
        {
            "subliminal": {"enabled": True},
            "assrt": {"enabled": True},
            "external_a": {"enabled": True},
            "external_b": {"enabled": True},
        },
        order=["assrt", "subliminal"],
    )

    assert [provider.name for provider in providers] == [
        "assrt",
        "subliminal",
        "external_a",
        "external_b",
    ]


def test_build_recommended_provider_intervals_uses_adapter_metadata(monkeypatch) -> None:
    factories = {
        "subliminal": FakeFactory("subliminal"),
        "assrt": FakeFactory("assrt"),
        "external_a": FakeFactory("external_a"),
    }
    factories["subliminal"].metadata = ProviderAdapterMetadata(
        name="subliminal",
        display_name="Subliminal",
        version="1.0.0",
        recommended_interval_seconds=60.0,
    )
    factories["assrt"].metadata = ProviderAdapterMetadata(
        name="assrt",
        display_name="ASSRT",
        version="1.0.0",
        recommended_interval_seconds=12.0,
    )
    factories["external_a"].metadata = ProviderAdapterMetadata(
        name="external_a",
        display_name="External A",
        version="1.0.0",
        recommended_interval_seconds=2.5,
    )
    monkeypatch.setattr(provider_adapters, "discover_adapter_factories", lambda: factories)

    intervals = build_recommended_provider_intervals(order=["assrt", "subliminal"])

    assert intervals == {
        "assrt": 12.0,
        "subliminal": 60.0,
        "external_a": 2.5,
    }
