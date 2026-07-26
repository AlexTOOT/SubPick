from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Lock
from time import monotonic, sleep

from subtitle_sidecar.providers.scheduler import ProviderSearchScheduler


class FakeClock:
    def __init__(self, initial: float = 0.0) -> None:
        self._now = initial
        self._lock = Lock()

    def now(self) -> float:
        with self._lock:
            return self._now

    def sleep(self, seconds: float) -> None:
        with self._lock:
            self._now += seconds


def test_acquire_returns_none_for_empty_order() -> None:
    scheduler = ProviderSearchScheduler({"assrt": 10.0})

    assert scheduler.acquire([]) is None


def test_acquire_prefers_first_ready_provider() -> None:
    clock = FakeClock()
    scheduler = ProviderSearchScheduler(
        {"assrt": 30.0, "subdl": 10.0},
        clock=clock.now,
        sleeper=clock.sleep,
    )

    assert scheduler.acquire(["assrt", "subdl"]) == "assrt"
    assert scheduler.acquire(["assrt", "subdl"]) == "subdl"


def test_low_priority_provider_bypasses_high_priority_cooldown() -> None:
    clock = FakeClock()
    scheduler = ProviderSearchScheduler(
        {"assrt": 30.0, "subdl": 5.0},
        clock=clock.now,
        sleeper=clock.sleep,
    )

    assert scheduler.acquire(["assrt", "subdl"]) == "assrt"
    assert scheduler.acquire(["assrt", "subdl"]) == "subdl"


def test_high_priority_provider_recovers_after_cooldown() -> None:
    clock = FakeClock()
    scheduler = ProviderSearchScheduler(
        {"assrt": 30.0, "subdl": 5.0},
        clock=clock.now,
        sleeper=clock.sleep,
    )

    assert scheduler.acquire(["assrt", "subdl"]) == "assrt"
    assert scheduler.acquire(["assrt", "subdl"]) == "subdl"
    clock.sleep(30.0)

    assert scheduler.acquire(["assrt", "subdl"]) == "assrt"


def test_acquire_waits_for_earliest_provider_when_all_are_cooling_down() -> None:
    clock = FakeClock()
    waits: list[tuple[str, float, float]] = []
    scheduler = ProviderSearchScheduler(
        {"assrt": 30.0, "subdl": 10.0},
        clock=clock.now,
        sleeper=clock.sleep,
    )

    assert scheduler.acquire(["assrt", "subdl"]) == "assrt"
    assert scheduler.acquire(["subdl"]) == "subdl"

    selected = scheduler.acquire(
        ["assrt", "subdl"],
        on_wait=lambda provider, wait_seconds, ready_at: waits.append(
            (provider, wait_seconds, ready_at)
        ),
    )

    assert selected == "subdl"
    assert waits == [("subdl", 10.0, 10.0)]
    assert clock.now() == 10.0


def test_snapshot_reports_remaining_seconds_and_ready_at() -> None:
    clock = FakeClock()
    scheduler = ProviderSearchScheduler(
        {"assrt": 30.0, "subdl": 0.0},
        clock=clock.now,
        sleeper=clock.sleep,
    )
    scheduler.acquire(["assrt"])
    clock.sleep(12.5)

    snapshot = scheduler.snapshot()

    assert snapshot["assrt"]["ready_at"] == 30.0
    assert snapshot["assrt"]["remaining_seconds"] == 17.5
    assert snapshot["subdl"]["ready_at"] == 12.5
    assert snapshot["subdl"]["remaining_seconds"] == 0.0


def test_update_intervals_does_not_extend_existing_reservation() -> None:
    clock = FakeClock()
    scheduler = ProviderSearchScheduler(
        {"assrt": 10.0},
        clock=clock.now,
        sleeper=clock.sleep,
    )
    scheduler.acquire(["assrt"])

    scheduler.update_intervals({"assrt": 60.0})
    clock.sleep(10.0)

    assert scheduler.acquire(["assrt"]) == "assrt"
    assert scheduler.snapshot()["assrt"]["ready_at"] == 70.0


def test_mark_completed_pushes_next_slot_from_completion_time() -> None:
    clock = FakeClock()
    scheduler = ProviderSearchScheduler(
        {"assrt": 10.0},
        clock=clock.now,
        sleeper=clock.sleep,
    )

    assert scheduler.acquire(["assrt"]) == "assrt"
    clock.sleep(4.0)
    scheduler.mark_completed("assrt")

    snapshot = scheduler.snapshot()

    assert snapshot["assrt"]["ready_at"] == 14.0
    assert snapshot["assrt"]["remaining_seconds"] == 10.0


def test_mark_completed_keeps_more_conservative_existing_reservation() -> None:
    clock = FakeClock()
    scheduler = ProviderSearchScheduler(
        {"assrt": 10.0},
        clock=clock.now,
        sleeper=clock.sleep,
    )

    assert scheduler.acquire(["assrt"]) == "assrt"
    clock.sleep(1.0)
    scheduler.mark_completed("assrt")
    clock.sleep(12.0)
    scheduler.mark_completed("assrt")

    snapshot = scheduler.snapshot()

    assert snapshot["assrt"]["ready_at"] == 23.0
    assert snapshot["assrt"]["remaining_seconds"] == 10.0


def test_zero_interval_provider_is_always_ready() -> None:
    clock = FakeClock()
    scheduler = ProviderSearchScheduler(
        {"subliminal": 0.0},
        clock=clock.now,
        sleeper=clock.sleep,
    )

    assert scheduler.acquire(["subliminal"]) == "subliminal"
    assert scheduler.acquire(["subliminal"]) == "subliminal"
    assert scheduler.snapshot()["subliminal"]["remaining_seconds"] == 0.0


def test_acquire_is_thread_safe_for_basic_concurrency() -> None:
    scheduler = ProviderSearchScheduler({"assrt": 0.03}, clock=monotonic, sleeper=sleep)
    barrier = Barrier(2)

    def acquire_once() -> str | None:
        barrier.wait()
        return scheduler.acquire(["assrt"])

    started_at = monotonic()
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: acquire_once(), range(2)))
    elapsed = monotonic() - started_at

    assert results == ["assrt", "assrt"]
    assert elapsed >= 0.025
