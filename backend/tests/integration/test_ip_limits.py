from datetime import UTC, datetime, timedelta

import httpx
from asgi_lifespan import LifespanManager
from sqlalchemy import text
from starlette.requests import Request

from app.api.deps import client_ip
from app.main import create_app
from app.services.ratelimit import window_start
from tests.integration.conftest import TODAY


def _client(app, **headers):
    transport = httpx.ASGITransport(app=app)  # peer address is 127.0.0.1
    return httpx.AsyncClient(transport=transport, base_url="http://testserver", headers=headers)


async def test_session_creation_is_limited_per_ip(app):
    app.state.settings.session_limit_per_ip_hour = 2
    statuses = []
    for _ in range(3):
        async with _client(app) as c:
            statuses.append((await c.post("/api/session")).status_code)
    assert statuses == [200, 200, 429]
    async with _client(app) as c:
        r = await c.post("/api/session")
    assert r.json()["error"]["code"] == "rate_limited"
    assert "сесси" in r.json()["error"]["message"]


async def test_reusing_a_valid_session_does_not_count(session_client, app):
    app.state.settings.session_limit_per_ip_hour = 1  # the fixture already used the one slot
    for _ in range(3):
        assert (await session_client.post("/api/session")).status_code == 200


async def test_forwarded_for_is_ignored_unless_proxy_is_trusted(app):
    app.state.settings.session_limit_per_ip_hour = 1
    async with _client(app, **{"x-forwarded-for": "1.1.1.1"}) as c:
        assert (await c.post("/api/session")).status_code == 200
    async with _client(app, **{"x-forwarded-for": "2.2.2.2"}) as c:  # spoofing doesn't help
        assert (await c.post("/api/session")).status_code == 429


async def test_trusted_proxy_uses_the_address_it_appended(app):
    app.state.settings.session_limit_per_ip_hour = 1
    app.state.settings.trust_proxy = True
    # Caddy sets/appends the real peer as the LAST hop; earlier hops are client-supplied.
    async with _client(app, **{"x-forwarded-for": "6.6.6.6, 203.0.113.7"}) as c:
        assert (await c.post("/api/session")).status_code == 200
    async with _client(app, **{"x-forwarded-for": "7.7.7.7, 203.0.113.7"}) as c:
        assert (await c.post("/api/session")).status_code == 429
    async with _client(app, **{"x-forwarded-for": "203.0.113.8"}) as c:
        assert (await c.post("/api/session")).status_code == 200


async def test_chat_is_limited_per_ip_across_sessions(app):
    app.state.settings.chat_limit_per_ip_hour = 1
    async with _client(app) as a, _client(app) as b:
        await a.post("/api/session")
        await b.post("/api/session")
        async with a.stream("POST", "/api/chat", json={"message": "привет"}) as r:
            assert r.status_code == 200
            [chunk async for chunk in r.aiter_text()]
        r = await b.post("/api/chat", json={"message": "привет"})
    assert r.status_code == 429 and r.json()["error"]["code"] == "rate_limited"


async def test_chat_is_limited_per_ip_per_day(app):
    # Security audit: 9 addresses x 60/h could exhaust the app-wide daily quota on their own.
    app.state.settings.chat_limit_per_ip_day = 1
    async with _client(app) as a, _client(app) as b:
        await a.post("/api/session")
        await b.post("/api/session")
        async with a.stream("POST", "/api/chat", json={"message": "привет"}) as r:
            assert r.status_code == 200
            [chunk async for chunk in r.aiter_text()]
        r = await b.post("/api/chat", json={"message": "привет"})
    assert r.status_code == 429 and r.json()["error"]["code"] == "rate_limited"
    assert "сутки" in r.json()["error"]["message"]


async def test_day_blocked_chat_attempts_do_not_use_up_the_hourly_limit(session_client, app):
    app.state.settings.chat_limit_per_ip_day = 0
    app.state.settings.chat_limit_per_ip_hour = 1
    for _ in range(3):
        r = await session_client.post("/api/chat", json={"message": "привет"})
        assert r.status_code == 429 and "сутки" in r.json()["error"]["message"]
    app.state.settings.chat_limit_per_ip_day = 10
    async with session_client.stream("POST", "/api/chat", json={"message": "привет"}) as r:
        assert r.status_code == 200
        [chunk async for chunk in r.aiter_text()]


def test_client_ip_without_peer_falls_back(app):
    request = Request({"type": "http", "headers": [], "client": None, "app": app})
    assert client_ip(request) == "unknown"


def _request(app, peer: str, **headers: str) -> Request:
    raw = [(k.encode(), v.encode()) for k, v in headers.items()]
    return Request({"type": "http", "headers": raw, "client": (peer, 1234), "app": app})


def test_ipv6_clients_are_limited_per_64_prefix(app):
    # Security audit L2: an IPv6 host controls its whole /64, so a per-address limit is moot.
    a = client_ip(_request(app, "2001:db8:1:2::1"))
    b = client_ip(_request(app, "2001:db8:1:2:ffff:ffff:ffff:ffff"))
    c = client_ip(_request(app, "2001:db8:1:3::1"))
    assert a == b != c
    assert client_ip(_request(app, "::ffff:203.0.113.7")) == "203.0.113.7"
    assert client_ip(_request(app, "203.0.113.7")) == "203.0.113.7"


async def test_plan_mutations_are_limited_per_ip(session_client, app):
    # Security audit M2: every mutation stores a full plan snapshot.
    app.state.settings.mutation_limit_per_ip_hour = 2
    op = {"ops": [{"op": "update_task", "id": 1, "duration": 2}]}
    statuses = [
        (await session_client.post("/api/plan/operations", json=op)).status_code for _ in range(2)
    ]
    statuses.append((await session_client.post("/api/plan/undo")).status_code)
    assert statuses == [200, 200, 429]


async def test_imports_are_limited_per_ip(session_client, app):
    app.state.settings.import_limit_per_ip_hour = 1
    files = {"file": ("x.xlsx", b"not an xlsx")}
    first = await session_client.post(
        "/api/plan/import",
        data={"project_start": "2026-09-21", "expected_version": "1"},
        files=files,
    )
    second = await session_client.post(
        "/api/plan/import",
        data={"project_start": "2026-09-21", "expected_version": "1"},
        files=files,
    )
    assert first.status_code == 422 and second.status_code == 429


async def test_daily_chat_quota_survives_deleting_the_session(app):
    # Security audit: the global daily quota counted chat_messages rows, which are
    # cascade-deleted with the session — «new session → chat → DELETE /api/session» reset it.
    app.state.settings.chat_limit_per_day = 1
    async with _client(app) as a:
        await a.post("/api/session")
        async with a.stream("POST", "/api/chat", json={"message": "привет"}) as r:
            assert r.status_code == 200
            [chunk async for chunk in r.aiter_text()]
        assert (await a.delete("/api/session")).status_code == 204
    async with _client(app) as b:
        await b.post("/api/session")
        r = await b.post("/api/chat", json={"message": "привет"})
    assert r.status_code == 429
    assert "Дневной лимит" in r.json()["error"]["message"]


# --- durable (Postgres) limits: sessions, chat per IP, imports ---


async def _counters(app) -> dict[str, int]:
    async with app.state.sessionmaker() as db:
        rows = await db.execute(text("SELECT key, count FROM rate_counters"))
    return {key: count for key, count in rows.all()}


async def test_costly_limits_survive_a_restart(app, settings, sessionmaker):
    # Kept in process memory, these limits reset on every deploy or restart: a client could
    # time its bursts to them. They live in Postgres now.
    app.state.settings.session_limit_per_ip_hour = 1
    async with _client(app) as c:
        assert (await c.post("/api/session")).status_code == 200
    settings.session_limit_per_ip_hour = 1
    restarted = create_app(settings, sessionmaker=sessionmaker, today=lambda: TODAY)
    async with LifespanManager(restarted), _client(restarted) as c:
        r = await c.post("/api/session")
    assert r.status_code == 429 and "сесси" in r.json()["error"]["message"]


async def test_refused_attempts_are_not_counted(app):
    app.state.settings.session_limit_per_ip_hour = 1
    for expected in (200, 429, 429):
        async with _client(app) as c:
            assert (await c.post("/api/session")).status_code == expected
    assert await _counters(app) == {"hour:session:127.0.0.1": 1}


async def test_chat_and_import_counters_use_fixed_windows(session_client, app):
    async with session_client.stream("POST", "/api/chat", json={"message": "привет"}) as r:
        [chunk async for chunk in r.aiter_text()]
    files = {"file": ("x.xlsx", b"not an xlsx")}
    await session_client.post(
        "/api/plan/import",
        data={"project_start": "2026-09-21", "expected_version": "1"},
        files=files,
    )
    counters = await _counters(app)
    assert counters["hour:chat:127.0.0.1"] == 1 and counters["day:chat:127.0.0.1"] == 1
    assert counters["hour:import:127.0.0.1"] == 1
    async with app.state.sessionmaker() as db:
        starts = (await db.execute(text("SELECT DISTINCT window_start FROM rate_counters"))).all()
    now = datetime.now(UTC)
    assert {row[0] for row in starts} <= {window_start("hour", now), window_start("day", now)}


def test_window_start_truncates_to_the_hour_and_the_utc_day():
    moment = datetime(2026, 9, 27, 13, 45, 12, 999, tzinfo=UTC)
    assert window_start("hour", moment) == datetime(2026, 9, 27, 13, tzinfo=UTC)
    assert window_start("day", moment) == datetime(2026, 9, 27, tzinfo=UTC)


async def test_cleanup_prunes_past_windows(app):
    from app.services.cleanup import purge_expired

    now = datetime.now(UTC)
    rows = {
        ("hour:chat:a", window_start("hour", now)): True,  # current windows: kept
        ("day:chat:a", window_start("day", now)): True,
        ("hour:chat:b", window_start("hour", now - timedelta(hours=2))): False,
        ("day:chat:b", window_start("day", now - timedelta(days=2))): False,
    }
    async with app.state.sessionmaker() as db, db.begin():
        for key, start in rows:
            await db.execute(
                text("INSERT INTO rate_counters (key, window_start, count) VALUES (:k, :w, 1)"),
                {"k": key, "w": start},
            )
    await purge_expired(app.state.sessionmaker, ttl_days=14, now=now)
    assert set(await _counters(app)) == {key for (key, _), kept in rows.items() if kept}


# --- chat reserve: abuse can't use up the app-wide daily quota for everyone ---


async def _chat_from(app, ip: str) -> httpx.Response:
    async with _client(app, **{"x-forwarded-for": ip}) as c:
        assert (await c.post("/api/session")).status_code == 200
        async with c.stream("POST", "/api/chat", json={"message": "привет"}) as r:
            await r.aread()
            return r


async def test_the_last_messages_of_the_day_are_kept_for_light_users(app):
    cfg = app.state.settings
    cfg.trust_proxy = True
    cfg.chat_limit_per_day = 5
    cfg.chat_daily_reserve = 3  # the reserve starts once 2 messages were sent app-wide today
    cfg.chat_reserve_per_ip_day = 1
    heavy, light = "203.0.113.1", "203.0.113.2"
    assert (await _chat_from(app, heavy)).status_code == 200
    assert (await _chat_from(app, heavy)).status_code == 200
    refused = await _chat_from(app, heavy)  # already over the reserve's per-address share
    assert refused.status_code == 429
    message = refused.json()["error"]["message"]
    assert "почти исчерпан" in message and "Дневной лимит демо исчерпан" not in message
    assert (await _chat_from(app, light)).status_code == 200  # hasn't written today yet
    assert (await _chat_from(app, light)).status_code == 429
    assert (await _chat_from(app, "203.0.113.3")).status_code == 200
    assert (await _chat_from(app, "203.0.113.4")).status_code == 200
    hard = await _chat_from(app, "203.0.113.5")  # the hard cap holds for everyone
    assert (
        hard.status_code == 429 and "Дневной лимит демо исчерпан" in hard.json()["error"]["message"]
    )


async def test_chat_reserve_settings_defaults(app):
    assert app.state.settings.chat_daily_reserve == 100
    assert app.state.settings.chat_reserve_per_ip_day == 10
