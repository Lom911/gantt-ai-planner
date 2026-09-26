"""In-memory per-client-IP sliding-window limits.

Kept in process memory, like the event bus and the session locks: the app runs a single
worker (see README), and a limit that resets on restart is fine for its purpose — stopping
one client from creating sessions or burning the shared daily chat quota in bulk.
"""

import time
from collections import OrderedDict, deque
from collections.abc import Callable


class SlidingWindowLimiter:
    def __init__(
        self,
        *,
        window_seconds: float,
        clock: Callable[[], float] = time.monotonic,
        sweep_every: int = 1000,
        max_keys: int = 100_000,
    ) -> None:
        self._window = window_seconds
        self._clock = clock
        self._sweep_every = sweep_every
        self._max_keys = max_keys
        self._calls = 0
        # Least recently touched key first (allow() moves a key to the end): the eviction
        # order once the table is full (security audit L2: a flood of distinct addresses —
        # IPv6 /64s are cheap — must not grow it without bound).
        self._hits: OrderedDict[str, deque[float]] = OrderedDict()

    def allow(self, key: str, limit: int) -> bool:
        """Records a hit for `key` and returns True, unless `key` already has `limit` hits
        within the window (then nothing is recorded, so retrying doesn't extend the block)."""
        now = self._clock()
        self._calls += 1
        if self._calls % self._sweep_every == 0:
            self._sweep(now)
        hits = self._hits.get(key)
        if hits is None:
            if len(self._hits) >= self._max_keys:
                self._make_room(now)
            hits = self._hits[key] = deque()
        else:
            # A blocked attempt counts as activity too: an address that keeps hammering must
            # not be the one evicted (which would lift its block).
            self._hits.move_to_end(key)
        self._expire(hits, now)
        if len(hits) >= limit:
            return False
        hits.append(now)
        return True

    def full(self, key: str, limit: int) -> bool:
        """True if `key` already has `limit` hits within the window. Records nothing: lets a
        caller check a second limit before spending a hit on the first one."""
        hits = self._hits.get(key)
        if hits is None:
            return False
        self._expire(hits, self._clock())
        return len(hits) >= limit

    def _expire(self, hits: deque[float], now: float) -> None:
        while hits and hits[0] <= now - self._window:
            hits.popleft()

    def _sweep(self, now: float) -> None:
        cutoff = now - self._window
        for key in [k for k, hits in self._hits.items() if not hits or hits[-1] <= cutoff]:
            del self._hits[key]

    def _make_room(self, now: float) -> None:
        """Table full: drop idle keys, and if that isn't enough (a flood of fresh addresses),
        evict the least recently touched tenth at once, so that not every new key of the
        flood pays for a full sweep."""
        self._sweep(now)
        target = self._max_keys - max(1, self._max_keys // 10)
        while len(self._hits) > target:
            self._hits.popitem(last=False)
