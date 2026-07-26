from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from threading import Lock
from time import monotonic, sleep


WaitCallback = Callable[[str, float, float], None]


class ProviderSearchScheduler:
    def __init__(
        self,
        intervals: Mapping[str, float],
        *,
        clock: Callable[[], float] = monotonic,
        sleeper: Callable[[float], None] = sleep,
    ) -> None:
        self._clock = clock
        self._sleeper = sleeper
        self._lock = Lock()
        self._intervals: dict[str, float] = {
            name: self._normalize_interval(seconds) for name, seconds in intervals.items()
        }
        self._next_allowed_at: dict[str, float] = {}

    def update_intervals(self, intervals: Mapping[str, float]) -> None:
        with self._lock:
            for name, seconds in intervals.items():
                self._intervals[name] = self._normalize_interval(seconds)

    def acquire(
        self,
        ordered_names: Sequence[str],
        on_wait: WaitCallback | None = None,
    ) -> str | None:
        if not ordered_names:
            return None

        while True:
            wait_target: tuple[str, float, float] | None = None
            now = self._clock()
            with self._lock:
                for name in ordered_names:
                    interval = self._intervals.get(name, 0.0)
                    ready_at = self._next_allowed_at.get(name, 0.0)
                    if interval <= 0 or ready_at <= now:
                        self._next_allowed_at[name] = now + interval if interval > 0 else now
                        return name
                    if wait_target is None or ready_at < wait_target[2]:
                        wait_seconds = max(0.0, ready_at - now)
                        wait_target = (name, wait_seconds, ready_at)

            if wait_target is None:
                return None

            if on_wait is not None:
                on_wait(*wait_target)
            if wait_target[1] > 0:
                self._sleeper(wait_target[1])

    def mark_completed(self, name: str) -> None:
        now = self._clock()
        with self._lock:
            interval = self._intervals.get(name, 0.0)
            if interval <= 0:
                return
            completed_ready_at = now + interval
            reserved_ready_at = self._next_allowed_at.get(name, 0.0)
            self._next_allowed_at[name] = max(reserved_ready_at, completed_ready_at)

    def snapshot(self) -> dict[str, dict[str, float]]:
        now = self._clock()
        with self._lock:
            names = set(self._intervals) | set(self._next_allowed_at)
            snapshot: dict[str, dict[str, float]] = {}
            for name in sorted(names):
                interval = self._intervals.get(name, 0.0)
                if interval <= 0:
                    ready_at = now
                    remaining = 0.0
                else:
                    ready_at = self._next_allowed_at.get(name, 0.0)
                    remaining = max(0.0, ready_at - now)
                    if remaining <= 0:
                        ready_at = now
                snapshot[name] = {
                    "ready_at": ready_at,
                    "remaining_seconds": remaining,
                }
            return snapshot

    @staticmethod
    def _normalize_interval(seconds: float) -> float:
        return max(0.0, float(seconds))
