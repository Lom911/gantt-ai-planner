"""What GET /api/ops/status reports, for alerting without a metrics backend: a rolling window
of recent requests (fed by AccessLogMiddleware) and the nightly backup's status file."""

import math
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.services.confirmations import iso_utc

WINDOW_MINUTES = 15
# Newest samples kept: ~20 requests/s (the load test's peak per core) for 15 minutes is 18k,
# so this only cuts in under a flood — then the window covers the newest requests only.
MAX_SAMPLES = 50_000
_BACKUP_STATUS_MAX_BYTES = 4096


@dataclass(frozen=True)
class MetricsSnapshot:
    requests: int
    errors_5xx: int
    error_rate: float
    p95_ms: float


class RequestMetrics:
    """Requests of the last `window_seconds`: status and duration of each. In process memory,
    like the other per-process state (single worker); a restart starts an empty window."""

    def __init__(
        self,
        *,
        window_seconds: float = WINDOW_MINUTES * 60,
        clock: Callable[[], float] = time.monotonic,
        max_samples: int = MAX_SAMPLES,
    ) -> None:
        self._window = window_seconds
        self._clock = clock
        # (finished at, status, duration in ms or None for a stream, whose duration is how
        # long the client stayed connected rather than how fast we answered)
        self._samples: deque[tuple[float, int, float | None]] = deque(maxlen=max_samples)

    def record(self, status: int, duration_ms: float | None) -> None:
        self._samples.append((self._clock(), status, duration_ms))

    def snapshot(self) -> MetricsSnapshot:
        cutoff = self._clock() - self._window
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()
        requests = len(self._samples)
        errors = sum(1 for _, status, _ in self._samples if status >= 500)
        durations = sorted(d for _, _, d in self._samples if d is not None)
        # Nearest-rank percentile: the smallest duration at or above 95 % of the requests.
        p95 = durations[math.ceil(0.95 * len(durations)) - 1] if durations else 0.0
        return MetricsSnapshot(
            requests=requests,
            errors_5xx=errors,
            error_rate=round(errors / requests, 4) if requests else 0.0,
            p95_ms=round(p95, 1),
        )


def _parse_moment(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def read_backup_status(path: str, now: datetime) -> dict[str, Any]:
    """The nightly backup's status file (written by deploy/backup.sh: `key=value` lines —
    timestamp, status=ok|fail, last_ok, ...) as {status, last_ok, age_hours}. A missing,
    unreadable or garbled file is status "unknown": the alert should fire on that too."""
    unknown: dict[str, Any] = {"status": "unknown", "last_ok": None, "age_hours": None}
    try:
        with open(Path(path), "rb") as fh:
            raw = fh.read(_BACKUP_STATUS_MAX_BYTES).decode("utf-8", errors="replace")
    except OSError:
        return unknown
    fields: dict[str, str] = {}
    for line in raw.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            fields[key.strip()] = value.strip()
    status = fields.get("status")
    if status not in ("ok", "fail"):
        return unknown
    last_ok = _parse_moment(fields.get("last_ok"))
    if last_ok is None and status == "ok":
        last_ok = _parse_moment(fields.get("timestamp"))  # this very run succeeded
    return {
        "status": status,
        "last_ok": iso_utc(last_ok) if last_ok else None,
        "age_hours": round((now - last_ok).total_seconds() / 3600, 2) if last_ok else None,
    }
