from app.services.iplimit import SlidingWindowLimiter


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_limit_applies_per_key_within_the_window():
    clock = FakeClock()
    limiter = SlidingWindowLimiter(window_seconds=3600, clock=clock)
    assert limiter.allow("1.1.1.1", 2) and limiter.allow("1.1.1.1", 2)
    assert not limiter.allow("1.1.1.1", 2)
    assert limiter.allow("2.2.2.2", 2)  # other clients are unaffected


def test_hits_expire_after_the_window():
    clock = FakeClock()
    limiter = SlidingWindowLimiter(window_seconds=3600, clock=clock)
    assert limiter.allow("1.1.1.1", 1)
    clock.now += 3599
    assert not limiter.allow("1.1.1.1", 1)
    clock.now += 2
    assert limiter.allow("1.1.1.1", 1)


def test_rejected_attempts_do_not_extend_the_block():
    clock = FakeClock()
    limiter = SlidingWindowLimiter(window_seconds=10, clock=clock)
    assert limiter.allow("k", 1)
    for _ in range(5):
        clock.now += 1
        assert not limiter.allow("k", 1)
    clock.now += 6
    assert limiter.allow("k", 1)


def test_idle_keys_are_swept():
    clock = FakeClock()
    limiter = SlidingWindowLimiter(window_seconds=10, clock=clock, sweep_every=3)
    for i in range(3):
        limiter.allow(f"10.0.0.{i}", 5)
    clock.now += 11
    limiter.allow("10.0.0.99", 5)
    limiter.allow("10.0.0.99", 5)
    limiter.allow("10.0.0.99", 5)
    assert set(limiter._hits) == {"10.0.0.99"}


def test_tracked_keys_are_bounded_stale_keys_go_first():
    # Security audit L2: a flood of distinct addresses must not grow the table without bound.
    clock = FakeClock()
    limiter = SlidingWindowLimiter(window_seconds=10, clock=clock, max_keys=3)
    limiter.allow("old", 5)
    clock.now += 11  # "old" is now outside the window
    limiter.allow("a", 5)
    limiter.allow("b", 5)
    limiter.allow("c", 5)  # table full: the stale key is swept, the fresh ones stay
    assert set(limiter._hits) == {"a", "b", "c"}


def test_when_full_of_fresh_keys_the_least_recently_touched_are_evicted():
    clock = FakeClock()
    limiter = SlidingWindowLimiter(window_seconds=3600, clock=clock, max_keys=3)
    for key in ("a", "b", "c"):
        limiter.allow(key, 1)
        clock.now += 1
    assert not limiter.allow("a", 1)  # a blocked attempt counts as activity: "a" stays
    limiter.allow("d", 1)
    assert len(limiter._hits) <= 3
    assert "a" in limiter._hits and "d" in limiter._hits and "b" not in limiter._hits
    assert not limiter.allow("a", 1)  # its block survived the eviction


def test_eviction_frees_a_batch_so_a_flood_does_not_sweep_on_every_new_key():
    clock = FakeClock()
    limiter = SlidingWindowLimiter(window_seconds=3600, clock=clock, max_keys=100)
    for i in range(100):
        limiter.allow(f"k{i}", 1)
    limiter.allow("new", 1)
    assert len(limiter._hits) <= 91  # at least 10% freed at once
    assert "new" in limiter._hits


def test_full_reports_a_blocked_key_without_recording_a_hit():
    clock = FakeClock()
    limiter = SlidingWindowLimiter(window_seconds=10, clock=clock)
    assert not limiter.full("k", 1)
    assert not limiter.full("k", 1)  # checking records nothing
    assert limiter.allow("k", 1)
    assert limiter.full("k", 1)
    clock.now += 11
    assert not limiter.full("k", 1)
