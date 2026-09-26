import httpx
from starlette.requests import Request

from app.api.deps import client_ip


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
        "/api/plan/import", data={"project_start": "2026-09-21"}, files=files
    )
    second = await session_client.post(
        "/api/plan/import", data={"project_start": "2026-09-21"}, files=files
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
