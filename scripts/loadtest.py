"""Baseline load test for a running stack (roadmap «Нагрузочные тесты»), usable as a gate.

Each virtual user: create a session → open the live event stream → load the plan → a few
plan edits (each stores a new version) → one chat turn → Excel export → delete the session.
Stages of concurrent users run for a fixed time; per-endpoint latency percentiles, error
counts and throughput are printed as a Markdown table.

An error is an HTTP status >= 400, a transport failure, or - for the chat turn, whose SSE
stream always starts with HTTP 200 - a stream that ends in an `error` event or ends without
a terminal `done` event.

Gate: after the last stage a summary line compares the whole run (all stages, all endpoints)
with --max-error-rate (default 0.01) and, if given, --max-p95-ms (global p95 over every
request); the exit code is 1 when either is breached, 0 otherwise.

Run against the local e2e stack (fake LLM, raised per-IP limits in docker-compose.yml):
    cd backend && uv run python ../scripts/loadtest.py --base-url http://127.0.0.1:8000 \
        --origin http://localhost:8000 --stages 10,25,50 --seconds 60 --max-p95-ms 5000
Never point it at production: per-IP limits there would (correctly) reject most of it.
"""

import argparse
import asyncio
import json
import statistics
import sys
import time
from collections import defaultdict
from collections.abc import Awaitable

import httpx

# An outcome is an HTTP status code, or a label for a failure that has none:
# "transport" (connect/read error), "sse:error:<code>", "sse:incomplete".
Outcome = int | str


class Stats:
    def __init__(self) -> None:
        self.lat: dict[str, list[float]] = defaultdict(list)
        self.err: dict[str, int] = defaultdict(int)
        self.codes: dict[str, dict[Outcome, int]] = defaultdict(lambda: defaultdict(int))

    def add(self, name: str, ms: float, outcome: Outcome) -> None:
        self.lat[name].append(ms)
        self.codes[name][outcome] += 1
        if isinstance(outcome, str) or outcome >= 400:
            self.err[name] += 1


def parse_sse(buffer: str) -> tuple[list[str], str]:
    """The `data` payloads of the complete events in `buffer`, and the trailing partial block.
    Same rules as the frontend's parseSSE / evals.run.parse_sse: a blank line ends an event,
    `:` comment lines (pings) are ignored, multi-line `data:` is joined with a newline."""
    blocks = buffer.replace("\r\n", "\n").split("\n\n")
    rest = blocks.pop()
    payloads = []
    for block in blocks:
        data = [
            line[5:].removeprefix(" ") for line in block.split("\n") if line.startswith("data:")
        ]
        if data:
            payloads.append("\n".join(data))
    return payloads, rest


def chat_failure(payloads: list[str]) -> str | None:
    """None when the chat stream ended with a `done` event, else why it counts as an error."""
    last: dict[str, object] | None = None
    for data in payloads:
        try:
            event = json.loads(data)
        except ValueError:
            continue
        if isinstance(event, dict):
            last = event
    if last is not None and last.get("type") == "error":
        return f"sse:error:{last.get('code', '?')}"
    if last is None or last.get("type") != "done":
        return "sse:incomplete"
    return None


async def timed(
    stats: Stats,
    name: str,
    call: Awaitable[httpx.Response] | Awaitable[tuple[httpx.Response, str | None]],
) -> httpx.Response | None:
    t0 = time.perf_counter()
    try:
        result = await call
    except Exception:
        stats.add(name, (time.perf_counter() - t0) * 1000, "transport")
        return None
    response, failure = result if isinstance(result, tuple) else (result, None)
    ms = (time.perf_counter() - t0) * 1000
    stats.add(name, ms, failure if failure and response.status_code < 400 else response.status_code)
    return response


async def user(base: str, origin: str, stats: Stats, stop_at: float) -> None:
    while time.monotonic() < stop_at:
        async with httpx.AsyncClient(base_url=base, headers={"Origin": origin}, timeout=30) as c:
            if (
                r := await timed(stats, "POST /api/session", c.post("/api/session"))
            ) is None or r.status_code != 200:
                await asyncio.sleep(1)
                continue
            events = asyncio.create_task(_hold_events(c))
            await timed(stats, "GET /api/plan", c.get("/api/plan"))
            for i in range(5):
                op = {"ops": [{"op": "update_task", "id": 1 + i, "duration": 2 + i}]}
                await timed(
                    stats, "POST /api/plan/operations", c.post("/api/plan/operations", json=op)
                )
            await timed(stats, "POST /api/plan/undo", c.post("/api/plan/undo"))
            await timed(stats, "POST /api/chat (fake LLM)", _chat(c))
            await timed(stats, "GET /api/plan/export", c.get("/api/plan/export?today=2026-09-28"))
            await timed(stats, "DELETE /api/session", c.delete("/api/session"))
            events.cancel()


async def _hold_events(c: httpx.AsyncClient) -> None:
    try:
        async with c.stream("GET", "/api/events", timeout=None) as r:
            async for _ in r.aiter_bytes():
                pass
    except (asyncio.CancelledError, httpx.HTTPError):
        pass


async def _chat(c: httpx.AsyncClient) -> tuple[httpx.Response, str | None]:
    """One chat turn; the failure label is set when the SSE stream did not end in `done`."""
    async with c.stream(
        "POST", "/api/chat", json={"message": "Сдвинь все задачи Дмитрия на 3 дня"}
    ) as r:
        if r.status_code >= 400:
            await r.aread()
            return r, None
        buffer, payloads = "", []
        async for chunk in r.aiter_text():
            buffer += chunk
            complete, buffer = parse_sse(buffer)
            payloads += complete
        # A last event without the closing blank line still counts.
        payloads += parse_sse(buffer + "\n\n")[0]
        return r, chat_failure(payloads)


def pct(values: list[float], q: int) -> float:
    return (
        statistics.quantiles(values, n=100, method="inclusive")[q - 1]
        if len(values) >= 2
        else values[0]
    )


async def stage(base: str, origin: str, users: int, seconds: int) -> tuple[Stats, float]:
    stats = Stats()
    stop_at = time.monotonic() + seconds
    t0 = time.perf_counter()
    await asyncio.gather(*(user(base, origin, stats, stop_at) for _ in range(users)))
    return stats, time.perf_counter() - t0


def report(users: int, stats: Stats, elapsed: float) -> None:
    total = sum(len(v) for v in stats.lat.values())
    errors = sum(stats.err.values())
    print(
        f"\n### {users} одновременных пользователей — {total} запросов за {elapsed:.0f} с "
        f"({total / elapsed:.0f} req/s), ошибок: {errors}\n"
    )
    print("| Запрос | N | p50, мс | p95, мс | p99, мс | ошибки |")
    print("|---|---:|---:|---:|---:|---:|")
    for name, lat in sorted(stats.lat.items()):
        codes = {k: v for k, v in stats.codes[name].items() if isinstance(k, str) or k >= 400}
        percentiles = " | ".join(f"{pct(lat, q):.0f}" for q in (50, 95, 99))
        print(f"| {name} | {len(lat)} | {percentiles} | {stats.err[name]} {codes or ''} |")


def gate(stages: list[Stats], max_error_rate: float, max_p95_ms: float | None) -> bool:
    """Prints the summary line for the whole run; True when every threshold holds."""
    latencies = [ms for s in stages for values in s.lat.values() for ms in values]
    errors = sum(n for s in stages for n in s.err.values())
    if not latencies:
        print("\nSUMMARY: no requests completed -> FAIL")
        return False
    rate = errors / len(latencies)
    p95 = pct(latencies, 95)
    breaches = []
    if rate > max_error_rate:
        breaches.append(f"error rate {rate:.2%} > {max_error_rate:.2%}")
    if max_p95_ms is not None and p95 > max_p95_ms:
        breaches.append(f"p95 {p95:.0f} ms > {max_p95_ms:.0f} ms")
    p95_limit = f"max {max_p95_ms:.0f} ms" if max_p95_ms is not None else "no limit"
    verdict = "PASS" if not breaches else "FAIL: " + "; ".join(breaches)
    print(
        f"\nSUMMARY: requests={len(latencies)} errors={errors} "
        f"error_rate={rate:.2%} (max {max_error_rate:.2%}) "
        f"p95={p95:.0f} ms ({p95_limit}) -> {verdict}"
    )
    return not breaches


def rate_arg(value: str) -> float:
    rate = float(value)
    if not 0 <= rate <= 1:
        raise argparse.ArgumentTypeError("must be a fraction between 0 and 1, e.g. 0.01")
    return rate


async def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--base-url", default="http://127.0.0.1:8000")
    ap.add_argument("--origin", default="http://localhost:8000")
    ap.add_argument("--stages", default="10,25,50")
    ap.add_argument("--seconds", type=int, default=60)
    ap.add_argument(
        "--max-error-rate",
        type=rate_arg,
        default=0.01,
        help="fail when errors / requests over the whole run exceed this (default 0.01)",
    )
    ap.add_argument(
        "--max-p95-ms",
        type=float,
        default=None,
        help="fail when the p95 latency over every request of the run exceeds this",
    )
    args = ap.parse_args()
    results = []
    for users in (int(s) for s in args.stages.split(",")):
        stats, elapsed = await stage(args.base_url, args.origin, users, args.seconds)
        report(users, stats, elapsed)
        results.append(stats)
    return 0 if gate(results, args.max_error_rate, args.max_p95_ms) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
