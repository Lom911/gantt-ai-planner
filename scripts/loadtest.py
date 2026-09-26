"""Baseline load test for a running stack (roadmap «Нагрузочные тесты»).

Each virtual user: create a session → open the live event stream → load the plan → a few
plan edits (each stores a new version) → one chat turn → Excel export → delete the session.
Stages of concurrent users run for a fixed time; per-endpoint latency percentiles, error
counts and throughput are printed as a Markdown table.

Run against the local e2e stack (fake LLM, raised per-IP limits in docker-compose.yml):
    cd backend && uv run python ../scripts/loadtest.py --base-url http://127.0.0.1:8000 \
        --origin http://localhost:8000 --stages 10,25,50 --seconds 60
Never point it at production: per-IP limits there would (correctly) reject most of it.
"""

import argparse
import asyncio
import statistics
import time
from collections import defaultdict

import httpx


class Stats:
    def __init__(self) -> None:
        self.lat: dict[str, list[float]] = defaultdict(list)
        self.err: dict[str, int] = defaultdict(int)
        self.codes: dict[str, dict[int, int]] = defaultdict(lambda: defaultdict(int))

    def add(self, name: str, ms: float, code: int) -> None:
        self.lat[name].append(ms)
        self.codes[name][code] += 1
        if code >= 400:
            self.err[name] += 1


async def timed(stats: Stats, name: str, coro) -> httpx.Response | None:
    t0 = time.perf_counter()
    try:
        r = await coro
    except Exception:  # noqa: BLE001 - count transport failures as errors
        stats.add(name, (time.perf_counter() - t0) * 1000, 599)
        return None
    stats.add(name, (time.perf_counter() - t0) * 1000, r.status_code)
    return r


async def user(base: str, origin: str, stats: Stats, stop_at: float) -> None:
    while time.monotonic() < stop_at:
        async with httpx.AsyncClient(base_url=base, headers={"Origin": origin}, timeout=30) as c:
            if (r := await timed(stats, "POST /api/session", c.post("/api/session"))) is None or r.status_code != 200:
                await asyncio.sleep(1)
                continue
            events = asyncio.create_task(_hold_events(c))
            await timed(stats, "GET /api/plan", c.get("/api/plan"))
            for i in range(5):
                op = {"ops": [{"op": "update_task", "id": 1 + i, "duration": 2 + i}]}
                await timed(stats, "POST /api/plan/operations", c.post("/api/plan/operations", json=op))
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


async def _chat(c: httpx.AsyncClient) -> httpx.Response:
    async with c.stream("POST", "/api/chat", json={"message": "Сдвинь все задачи Дмитрия на 3 дня"}) as r:
        async for _ in r.aiter_bytes():
            pass
        return r


def pct(values: list[float], q: float) -> float:
    return statistics.quantiles(values, n=100, method="inclusive")[q - 1] if len(values) >= 2 else values[0]


async def stage(base: str, origin: str, users: int, seconds: int) -> tuple[Stats, float]:
    stats = Stats()
    stop_at = time.monotonic() + seconds
    t0 = time.perf_counter()
    await asyncio.gather(*(user(base, origin, stats, stop_at) for _ in range(users)))
    return stats, time.perf_counter() - t0


def report(users: int, stats: Stats, elapsed: float) -> None:
    total = sum(len(v) for v in stats.lat.values())
    errors = sum(stats.err.values())
    print(f"\n### {users} одновременных пользователей — {total} запросов за {elapsed:.0f} с ({total / elapsed:.0f} req/s), ошибок: {errors}\n")
    print("| Запрос | N | p50, мс | p95, мс | p99, мс | ошибки |")
    print("|---|---:|---:|---:|---:|---:|")
    for name, lat in sorted(stats.lat.items()):
        codes = {k: v for k, v in stats.codes[name].items() if k >= 400}
        print(f"| {name} | {len(lat)} | {pct(lat, 50):.0f} | {pct(lat, 95):.0f} | {pct(lat, 99):.0f} | {stats.err[name]} {codes if codes else ''} |")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:8000")
    ap.add_argument("--origin", default="http://localhost:8000")
    ap.add_argument("--stages", default="10,25,50")
    ap.add_argument("--seconds", type=int, default=60)
    args = ap.parse_args()
    for users in (int(s) for s in args.stages.split(",")):
        stats, elapsed = await stage(args.base_url, args.origin, users, args.seconds)
        report(users, stats, elapsed)


if __name__ == "__main__":
    asyncio.run(main())
