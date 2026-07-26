from __future__ import annotations

from collections.abc import Callable, Hashable
from threading import Lock
from time import monotonic


class ProviderNegativeCache:
    """Process-local TTL cache for successful provider searches with no results."""

    def __init__(
        self,
        *,
        ttl_seconds: float = 12 * 60 * 60,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self._ttl_seconds = max(0.0, float(ttl_seconds))
        self._clock = clock
        self._lock = Lock()
        self._expires_at: dict[Hashable, float] = {}

    def contains(self, key: Hashable) -> bool:
        now = self._clock()
        with self._lock:
            expires_at = self._expires_at.get(key)
            if expires_at is None:
                return False
            if expires_at <= now:
                self._expires_at.pop(key, None)
                return False
            return True

    def remember(self, key: Hashable) -> None:
        if self._ttl_seconds <= 0:
            return
        with self._lock:
            self._expires_at[key] = self._clock() + self._ttl_seconds

    def remaining_seconds(self, key: Hashable) -> float:
        now = self._clock()
        with self._lock:
            expires_at = self._expires_at.get(key, 0.0)
            return max(0.0, expires_at - now)
