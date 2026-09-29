from datetime import date
from io import BytesIO

import httpx
from openpyxl import Workbook

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
TODAY = date(2026, 9, 25)


async def test_session_cookie_and_plan(client):
    r = await client.get("/api/plan")
    assert r.status_code == 401 and r.json()["error"]["code"] == "no_session"
    r = await client.post("/api/session")
    assert r.status_code == 200 and "sid" in r.cookies
    body = (await client.get("/api/plan")).json()
    assert body["version"] == 1 and len(body["plan"]["tasks"]) == 25
    assert body["plan"]["tasks"][0]["start"]
    assert body["can_undo"] is False and body["agent_busy"] is False


async def test_stale_cookie_gets_401_then_new_session(client):
    client.cookies.set("sid", "stale-token")
    assert (await client.get("/api/plan")).status_code == 401
    assert (await client.post("/api/session")).status_code == 200
    assert (await client.get("/api/plan")).status_code == 200


async def test_session_post_is_idempotent(session_client):
    first = session_client.cookies["sid"]
    await session_client.post("/api/session")
    assert session_client.cookies["sid"] == first


async def test_operations_undo_redo(session_client):
    r = await session_client.post(
        "/api/plan/operations", json={"ops": [{"op": "update_task", "id": 1, "duration": 6}]}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["version"] == 2 and body["summary"].startswith("Изменено задач")
    assert any(c["field"] == "duration" for c in body["changes"])
    assert (await session_client.post("/api/plan/undo")).json()["version"] == 1
    assert (await session_client.post("/api/plan/redo")).json()["version"] == 2


async def test_errors_envelope(session_client):
    r = await session_client.post(
        "/api/plan/operations", json={"ops": [{"op": "delete_task", "id": 999}]}
    )
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_operation"
    r = await session_client.post("/api/plan/operations", json={"ops": [{"op": "nope"}]})
    assert r.status_code == 422 and r.json()["error"]["code"] == "validation_error"
    r = await session_client.post("/api/plan/undo")
    assert r.status_code == 409 and r.json()["error"]["message"] == "Нечего отменять"


async def test_oversized_batch_is_rejected_with_russian_message(session_client):
    ops = [{"op": "delete_task", "id": 1}] * 201  # also over the delete-confirmation threshold
    r = await session_client.post("/api/plan/operations", json={"ops": ops})
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "invalid_operation" and "не больше 200 операций" in err["message"]
    assert (await session_client.get("/api/plan")).json()["version"] == 1


async def test_plan_size_cap_comes_from_settings_and_answers_413(settings, sessionmaker):
    from asgi_lifespan import LifespanManager

    from app.domain.seed import build_demo_plan
    from app.main import create_app
    from app.services.plan_service import plan_json_size

    settings.max_plan_json_bytes = plan_json_size(build_demo_plan(TODAY)) + 300
    application = create_app(settings, sessionmaker=sessionmaker, today=lambda: TODAY)
    async with LifespanManager(application):
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
            await c.post("/api/session")
            op = {"op": "update_task", "id": 1, "description": "я" * 300}
            r = await c.post("/api/plan/operations", json={"ops": [op]})
            assert r.status_code == 413
            assert r.json()["error"]["code"] == "plan_too_large"
            assert (await c.get("/api/plan")).json()["version"] == 1


async def test_bad_origin_rejected(session_client):
    r = await session_client.post("/api/plan/reset", headers={"Origin": "https://evil.example"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "bad_origin"


async def test_cross_site_fetch_metadata_rejected_even_without_origin(session_client):
    r = await session_client.post("/api/plan/reset", headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "bad_origin"
    assert (await session_client.get("/api/plan")).json()["version"] == 1
    # Reads stay allowed, and same-origin / user-initiated ("none") writes pass.
    r = await session_client.get("/api/plan", headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 200
    for site in ("same-origin", "none"):
        r = await session_client.post("/api/plan/reset", headers={"Sec-Fetch-Site": site})
        assert r.status_code == 200, site


async def test_reset_and_delete_session(session_client):
    await session_client.post(
        "/api/plan/operations", json={"ops": [{"op": "delete_task", "id": 1}]}
    )
    assert len((await session_client.post("/api/plan/reset")).json()["plan"]["tasks"]) == 25
    assert (await session_client.delete("/api/session")).status_code == 204
    assert (await session_client.get("/api/plan")).status_code == 401


async def test_healthz(client):
    r = await client.get("/healthz")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


async def test_task_history_lists_edits_newest_first(session_client):
    await session_client.post(
        "/api/plan/operations", json={"ops": [{"op": "update_task", "id": 1, "duration": 6}]}
    )
    await session_client.post(
        "/api/plan/operations", json={"ops": [{"op": "update_task", "id": 1, "name": "Новое имя"}]}
    )
    r = await session_client.get("/api/plan/tasks/1/history")
    assert r.status_code == 200
    body = r.json()
    # Newest edits first, then the demo-plan boundary entry the task lineage started from.
    assert len(body) == 3
    assert body[0]["version"] > body[1]["version"] > body[2]["version"]
    assert all(entry["source"] == "user" for entry in body[:2])
    assert all(entry["created_at"] for entry in body)
    assert body[0]["changes"][0]["field"] == "name"
    assert body[1]["changes"][0]["field"] == "duration"
    assert body[2]["source"] == "seed" and body[2]["changes"] == []
    # untouched task: exists, never edited, so only the demo-plan boundary entry shows — not a 404.
    r2 = await session_client.get("/api/plan/tasks/2/history")
    assert r2.status_code == 200
    body2 = r2.json()
    assert len(body2) == 1
    assert body2[0]["source"] == "seed" and body2[0]["changes"] == []


async def test_task_history_stops_at_import_boundary(session_client):
    # Pre-import edit must not leak into the post-import task's history, even though the
    # imported task happens to reuse id 1 — it is not the same task.
    await session_client.post(
        "/api/plan/operations", json={"ops": [{"op": "update_task", "id": 1, "duration": 9}]}
    )
    wb = Workbook()
    for row in [
        ["Задача", "Описание", "Исполнитель", "Длительность", "Предшественники"],
        ["A", "", "Олег", 2, None],
    ]:
        wb.active.append(row)
    buf = BytesIO()
    wb.save(buf)
    r = await session_client.post(
        "/api/plan/import",
        files={"file": ("office.xlsx", buf.getvalue(), XLSX_MIME)},
        data={"project_start": "2026-10-04", "expected_version": "2"},  # after the edit above
    )
    assert r.status_code == 200, r.text
    body = (await session_client.get("/api/plan/tasks/1/history")).json()
    assert len(body) == 1
    assert body[0]["source"] == "import" and body[0]["changes"] == []
    await session_client.post(
        "/api/plan/operations", json={"ops": [{"op": "update_task", "id": 1, "duration": 5}]}
    )
    body2 = (await session_client.get("/api/plan/tasks/1/history")).json()
    assert len(body2) == 2
    assert body2[0]["source"] == "user" and body2[0]["changes"][0]["field"] == "duration"
    assert body2[1]["source"] == "import" and body2[1]["changes"] == []


async def test_task_history_stops_at_reset_boundary(session_client):
    await session_client.post(
        "/api/plan/operations", json={"ops": [{"op": "update_task", "id": 1, "duration": 9}]}
    )
    r = await session_client.post("/api/plan/reset")
    assert r.status_code == 200
    body = (await session_client.get("/api/plan/tasks/1/history")).json()
    assert len(body) == 1
    assert body[0]["source"] == "reset" and body[0]["changes"] == []


async def test_task_history_shows_agent_source(session_client):
    async with session_client.stream(
        "POST", "/api/chat", json={"message": "Перенеси задачу 1 на 1 день"}
    ) as r:
        [_ async for _ in r.aiter_text()]
    r = await session_client.get("/api/plan/tasks/1/history")
    assert r.status_code == 200
    body = r.json()
    assert body and body[0]["source"] == "agent"


async def test_task_history_unknown_task_404(session_client):
    r = await session_client.get("/api/plan/tasks/999/history")
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"


async def test_operations_with_stale_expected_version_get_409(session_client):
    r = await session_client.post(
        "/api/plan/operations", json={"ops": [{"op": "update_task", "id": 1, "duration": 6}]}
    )
    assert r.json()["version"] == 2
    r = await session_client.post(
        "/api/plan/operations",
        json={"ops": [{"op": "update_task", "id": 1, "duration": 7}], "expected_version": 1},
    )
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "version_conflict" and err["details"]["current_version"] == 2
    r = await session_client.post("/api/plan/undo", json={"expected_version": 1})
    assert r.status_code == 409 and r.json()["error"]["code"] == "version_conflict"
    r = await session_client.post("/api/plan/undo", json={"expected_version": 2})
    assert r.status_code == 200 and r.json()["version"] == 1
    r = await session_client.post("/api/plan/redo", json={"expected_version": 1})
    assert r.status_code == 200 and r.json()["version"] == 2


async def test_clamped_bridge_lag_is_reported_in_apply_warnings(session_client):
    # A chain №26 → №27 → №28 with a 365-day lag on each link, appended to the 25-task seed.
    chain = [
        {"op": "add_task", "name": "A", "duration": 1},
        {"op": "add_task", "name": "B", "duration": 1, "predecessors": [{"id": 26, "lag": 365}]},
        {"op": "add_task", "name": "C", "duration": 1, "predecessors": [{"id": 27, "lag": 365}]},
    ]
    r = await session_client.post("/api/plan/operations", json={"ops": chain})
    assert r.status_code == 200 and r.json()["warnings"] == []
    r = await session_client.post(
        "/api/plan/operations", json={"ops": [{"op": "delete_task", "id": 27}]}
    )
    assert r.status_code == 200
    assert r.json()["warnings"] == [
        "Задержка связи №26 → №28 после удаления №27 обрезана до 365 дней (было бы 730)"
    ]
