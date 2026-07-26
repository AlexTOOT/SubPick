from subtitle_sidecar.providers.negative_cache import ProviderNegativeCache


def test_negative_cache_expires_entries() -> None:
    now = [10.0]
    cache = ProviderNegativeCache(ttl_seconds=30, clock=lambda: now[0])

    cache.remember(("provider", "query"))

    assert cache.contains(("provider", "query")) is True
    assert cache.remaining_seconds(("provider", "query")) == 30

    now[0] = 40.0
    assert cache.contains(("provider", "query")) is False
    assert cache.remaining_seconds(("provider", "query")) == 0
