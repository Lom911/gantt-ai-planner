import asyncio
from contextlib import suppress
from datetime import UTC, datetime

import pytest

from app.logging_setup import AccessLogMiddleware
from app.services.ops import RequestMetrics, read_backup_status

NOW = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_rolling_window_counts_requests_errors_and_p95():
    clock = Clock()
    metrics = RequestMetrics(window_seconds=900, clock=clock)
    for ms in range(1, 101):  # 1..100 ms
        metrics.record(200, float(ms))
    metrics.record(503, 7.0)
    metrics.record(500, None)  # a stream: counted, its duration isn't a latency
    snap = metrics.snapshot()
    assert snap.requests == 102 and snap.errors_5xx == 2
    assert snap.error_rate == pytest.approx(2 / 102, abs=1e-4)
    assert snap.p95_ms == 95.0  # nearest rank over the 101 timed requests
    clock.now += 901
    metrics.record(404, 3.0)
    snap = metrics.snapshot()
    assert (snap.requests, snap.errors_5xx, snap.p95_ms) == (1, 0, 3.0)


def test_empty_window_reports_zeros():
    snap = RequestMetrics().snapshot()
    assert (snap.requests, snap.errors_5xx, snap.error_rate, snap.p95_ms) == (0, 0, 0.0, 0.0)


def test_window_memory_is_bounded():
    metrics = RequestMetrics(max_samples=10)
    for _ in range(25):
        metrics.record(200, 1.0)
    assert metrics.snapshot().requests == 10


def _serve(app, path, metrics):
    async def receive():
        return {"type": "http.request"}

    async def send(message):
        pass

    scope = {"type": "http", "method": "GET", "path": path, "query_string": b"", "headers": []}
    middleware = AccessLogMiddleware(app, metrics=metrics)
    with suppress(RuntimeError):  # the app's own exception, re-raised after logging
        asyncio.run(middleware(scope, receive, send))


async def _ok(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b""})


async def _stream(scope, receive, send):
    headers = [(b"content-type", b"text/event-stream; charset=utf-8")]
    await send({"type": "http.response.start", "status": 200, "headers": headers})
    await send({"type": "http.response.body", "body": b""})


async def _boom(scope, receive, send):
    raise RuntimeError("boom")


def test_access_log_feeds_the_window_but_not_with_probes():
    metrics = RequestMetrics()
    _serve(_ok, "/api/plan", metrics)
    _serve(_ok, "/healthz", metrics)  # uptime probes would dilute the error rate
    _serve(_ok, "/api/ops/status", metrics)  # the monitor's own polling
    _serve(_boom, "/api/plan", metrics)  # an exception before any response is a 500
    _serve(_stream, "/api/events", metrics)
    snap = metrics.snapshot()
    assert snap.requests == 3 and snap.errors_5xx == 1
    assert [s[2] is None for s in metrics._samples] == [False, False, True]


def _status_file(tmp_path, text):
    path = tmp_path / "backup-status"
    path.write_text(text, encoding="utf-8")
    return str(path)


LOCAL_OK = (
    "timestamp=2026-09-27T03:15:04Z\nstatus=ok\nexit_code=0\nsize_bytes=183422\n"
    "file=/var/backups/gantt-planner/2026-09-27.dump\nlast_ok=2026-09-27T03:15:04Z\n"
)


def test_backup_ok(tmp_path):
    assert read_backup_status(_status_file(tmp_path, LOCAL_OK), NOW) == {
        "status": "ok",
        "last_ok": "2026-09-27T03:15:04Z",
        "age_hours": pytest.approx(8.75, abs=0.01),
        # No offsite_* lines: the offsite copy has never run on this server (not installed,
        # or a file older than it) - no offsite section rather than an alerting "unknown".
        "offsite": None,
    }


def test_offsite_copy_ok(tmp_path):
    path = _status_file(
        tmp_path,
        LOCAL_OK + "offsite_status=ok\noffsite_last_ok=2026-09-27T03:16:30Z\n"
        "offsite_file=dumps/2026-09-27.dump.cms\n",
    )
    status = read_backup_status(path, NOW)
    assert status["status"] == "ok"
    assert status["offsite"] == {
        "status": "ok",
        "last_ok": "2026-09-27T03:16:30Z",
        "age_hours": pytest.approx(8.73, abs=0.01),
    }


def test_offsite_copy_failed_while_the_local_dump_succeeded(tmp_path):
    # The defect this section exists for: a good local dump, a failed push to the backups repo.
    path = _status_file(
        tmp_path,
        LOCAL_OK + "offsite_status=fail\noffsite_last_ok=2026-09-24T03:16:00Z\n"
        "offsite_file=dumps/2026-09-24.dump.cms\n",
    )
    status = read_backup_status(path, NOW)
    assert status["status"] == "ok"
    assert status["offsite"] == {
        "status": "fail",
        "last_ok": "2026-09-24T03:16:00Z",
        "age_hours": pytest.approx(80.73, abs=0.01),
    }


def test_offsite_copy_that_never_succeeded(tmp_path):
    path = _status_file(tmp_path, LOCAL_OK + "offsite_status=fail\noffsite_last_ok=\n")
    offsite = read_backup_status(path, NOW)["offsite"]
    assert offsite == {"status": "fail", "last_ok": None, "age_hours": None}


def test_offsite_copy_not_configured(tmp_path):
    path = _status_file(
        tmp_path, LOCAL_OK + "offsite_status=not_configured\noffsite_last_ok=\noffsite_file=\n"
    )
    offsite = read_backup_status(path, NOW)["offsite"]
    assert offsite == {"status": "not_configured", "last_ok": None, "age_hours": None}


@pytest.mark.parametrize(
    "offsite_lines",
    [
        "offsite_status=maybe\noffsite_last_ok=2026-09-27T03:16:30Z\n",
        "offsite_last_ok=2026-09-27T03:16:30Z\noffsite_file=dumps/2026-09-27.dump.cms\n",
        "offsite_status=\n",
    ],
)
def test_garbled_offsite_lines_are_unknown(tmp_path, offsite_lines):
    offsite = read_backup_status(_status_file(tmp_path, LOCAL_OK + offsite_lines), NOW)["offsite"]
    assert offsite == {"status": "unknown", "last_ok": None, "age_hours": None}


def test_offsite_lines_are_read_even_if_the_local_ones_are_garbled(tmp_path):
    # Two scripts write the file; one's broken lines don't hide the other's.
    path = _status_file(tmp_path, "status=maybe\noffsite_status=fail\noffsite_last_ok=\n")
    status = read_backup_status(path, NOW)
    assert status["status"] == "unknown"
    assert status["offsite"] == {"status": "fail", "last_ok": None, "age_hours": None}


def test_backup_failed_after_an_earlier_success(tmp_path):
    path = _status_file(
        tmp_path,
        "timestamp=2026-09-27T03:15:04Z\nstatus=fail\nexit_code=1\nsize_bytes=0\nfile=-\n"
        "last_ok=2026-09-25T03:15:02Z\n",
    )
    status = read_backup_status(path, NOW)
    assert status["status"] == "fail" and status["last_ok"] == "2026-09-25T03:15:02Z"
    assert status["age_hours"] == pytest.approx(56.75, abs=0.01)


def test_backup_that_never_succeeded(tmp_path):
    path = _status_file(tmp_path, "timestamp=2026-09-27T03:15:04Z\nstatus=fail\nlast_ok=\n")
    assert read_backup_status(path, NOW) == {
        "status": "fail",
        "last_ok": None,
        "age_hours": None,
        "offsite": None,
    }


UNKNOWN = {"status": "unknown", "last_ok": None, "age_hours": None}


@pytest.mark.parametrize(
    "text",
    ["", "garbage\x00\x01", "status=maybe\nlast_ok=2026-09-27T03:15:04Z\n", "status\nok\n"],
)
def test_garbled_backup_status_is_unknown(tmp_path, text):
    assert read_backup_status(_status_file(tmp_path, text), NOW) == {**UNKNOWN, "offsite": None}


def test_missing_backup_status_is_unknown(tmp_path):
    # Unreadable: whether the offsite copy ran can't be told either.
    assert read_backup_status(str(tmp_path / "nope"), NOW) == {**UNKNOWN, "offsite": UNKNOWN}


def test_ok_without_a_readable_moment_is_unknown(tmp_path):
    # "ok" with no parseable time has no age to go stale, so it would never alert: a garbled
    # file must not look healthy. Both scripts always write the moment of a good run.
    path = _status_file(
        tmp_path,
        "status=ok\nlast_ok=yesterday\ntimestamp=also-bad\n"
        "offsite_status=ok\noffsite_last_ok=today\n",
    )
    assert read_backup_status(path, NOW) == {**UNKNOWN, "offsite": UNKNOWN}


def test_offsite_ok_without_last_ok_is_unknown(tmp_path):
    path = _status_file(tmp_path, LOCAL_OK + "offsite_status=ok\noffsite_file=dumps/x.dump.cms\n")
    assert read_backup_status(path, NOW)["offsite"] == UNKNOWN


def test_failed_run_without_last_ok_stays_failed(tmp_path):
    # Never succeeded: no good moment to report, but "fail" alerts by itself.
    path = _status_file(tmp_path, LOCAL_OK + "offsite_status=fail\noffsite_last_ok=\n")
    assert read_backup_status(path, NOW)["offsite"] == {
        "status": "fail",
        "last_ok": None,
        "age_hours": None,
    }
