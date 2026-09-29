import uuid
from datetime import date

import pytest

from app.agent.llm import Completed, LLMTurnResult, TextDelta
from app.agent.loop import Agent
from app.api.deps import cookie_name
from app.db import repo
from app.domain.errors import ConfirmationRequired
from app.domain.operations import operations_adapter

TODAY = date(2026, 9, 25)


async def send(client, text: str) -> None:
    async with client.stream("POST", "/api/chat", json={"message": text}) as r:
        assert r.status_code == 200
        [chunk async for chunk in r.aiter_text()]


def answer(text: str) -> Completed:
    return Completed(
        LLMTurnResult(
            text=text,
            tool_calls=[],
            stop_reason="end_turn",
            content=[{"type": "text", "text": text}],
        )
    )


async def test_new_conversation_empties_the_chat_and_keeps_the_old_one(session_client):
    await send(session_client, "привет")
    assert len((await session_client.get("/api/chat/history")).json()) == 2

    r = await session_client.post("/api/chat/conversations")
    assert r.status_code == 201
    new_id = r.json()["id"]
    assert (await session_client.get("/api/chat/history")).json() == []

    listed = (await session_client.get("/api/chat/conversations")).json()
    assert len(listed) == 1
    old = listed[0]
    assert old["id"] != new_id
    assert old["title"] == "привет" and old["message_count"] == 2
    messages = (await session_client.get(f"/api/chat/conversations/{old['id']}")).json()
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[0]["content"] == "привет"


async def test_history_lists_earlier_conversations_newest_first(session_client):
    await send(session_client, "первый")
    await session_client.post("/api/chat/conversations")
    await send(session_client, "второй")
    await session_client.post("/api/chat/conversations")
    await send(session_client, "текущий")

    titles = [c["title"] for c in (await session_client.get("/api/chat/conversations")).json()]
    assert titles == ["второй", "первый"]  # the current conversation is the chat itself
    history = (await session_client.get("/api/chat/history")).json()
    assert [m["content"] for m in history if m["role"] == "user"] == ["текущий"]


async def test_empty_conversations_are_not_listed(session_client):
    for _ in range(3):
        assert (await session_client.post("/api/chat/conversations")).status_code == 201
    assert (await session_client.get("/api/chat/conversations")).json() == []


async def test_other_sessions_conversation_is_not_found(app, client, session_client):
    await send(session_client, "секрет")
    await session_client.post("/api/chat/conversations")
    [old] = (await session_client.get("/api/chat/conversations")).json()

    _, other = await app.state.service.create_session()
    async with app.state.service.sessionmaker() as db:
        assert await repo.recent_chat_messages(db, other, 10, uuid.UUID(old["id"])) == []
    r = await session_client.get(f"/api/chat/conversations/{uuid.uuid4()}")
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"


async def test_new_conversation_requires_origin(client, session_client):
    r = await session_client.post(
        "/api/chat/conversations", headers={"origin": "https://evil.example"}
    )
    assert r.status_code == 403


async def test_new_conversation_is_announced_to_other_tabs(app, session_client):
    sid = await _session_id(app, session_client)
    queue = app.state.service.bus.subscribe(sid)
    try:
        await session_client.post("/api/chat/conversations")
        assert queue.get_nowait() == {"type": "chat_reset"}
    finally:
        app.state.service.bus.unsubscribe(sid, queue)


async def test_new_conversation_ends_the_agents_pending_mass_delete(app, session_client):
    # The assistant's «delete 7 tasks?» was asked in the old conversation: a «да» typed into the
    # new one, whose agent never saw the question, must not confirm it.
    sid = await _session_id(app, session_client)
    batch = operations_adapter.validate_python(
        [{"op": "delete_task", "id": i} for i in range(1, 8)]
    )
    with pytest.raises(ConfirmationRequired):
        await app.state.service.apply(sid, batch, source="agent")
    assert (await app.state.service.get_confirmation(sid)) is not None

    await session_client.post("/api/chat/conversations")
    assert await app.state.service.get_confirmation(sid) is None


async def test_chat_limits_still_count_earlier_conversations(app, session_client):
    app.state.settings.chat_limit_per_hour = 1
    await send(session_client, "привет")
    await session_client.post("/api/chat/conversations")
    r = await session_client.post("/api/chat", json={"message": "ещё"})
    assert r.status_code == 429


async def test_import_note_goes_to_the_current_conversation(app):
    _, sid = await app.state.service.create_session()
    async with app.state.service.sessionmaker() as db, db.begin():
        current = await repo.start_conversation(db, sid)
        await repo.add_chat_message(db, session_id=sid, role="system", content="Загружен план")
    async with app.state.service.sessionmaker() as db:
        [note] = await repo.recent_chat_messages(db, sid, 10, current)
    assert note.content == "Загружен план"


async def test_agent_sees_only_the_current_conversation(app):
    class Recording:
        def __init__(self) -> None:
            self.seen: list[list[dict]] = []

        async def stream(self, *, system, tools, messages, max_tokens=None):
            self.seen.append(list(messages))  # the loop appends to it afterwards
            yield answer("ок")

    llm = Recording()
    agent = Agent(llm, app.state.tool_client, app.state.service, today=lambda: TODAY)
    _, sid = await app.state.service.create_session()
    [_ async for _ in agent.run_turn(sid, "старый вопрос")]
    async with app.state.service.sessionmaker() as db, db.begin():
        await repo.start_conversation(db, sid)
    [_ async for _ in agent.run_turn(sid, "новый вопрос")]

    assert llm.seen[-1] == [{"role": "user", "content": "новый вопрос"}]


async def test_reply_stays_with_its_question_when_a_new_conversation_starts_mid_turn(app):
    _, sid = await app.state.service.create_session()
    service = app.state.service

    class StartsNewChat:
        async def stream(self, *, system, tools, messages, max_tokens=None):
            # Another tab reloads while the model is answering.
            async with service.sessionmaker() as db, db.begin():
                await repo.start_conversation(db, sid)
            yield TextDelta("ответ")
            yield answer("ответ")

    agent = Agent(StartsNewChat(), app.state.tool_client, service, today=lambda: TODAY)
    [_ async for _ in agent.run_turn(sid, "вопрос")]
    async with service.sessionmaker() as db:
        question, reply = await repo.recent_chat_messages(db, sid, 10)
        current = await repo.current_conversation_id(db, sid)
    assert (question.role, reply.role) == ("user", "assistant")
    assert reply.conversation_id == question.conversation_id != current


async def _session_id(app, client) -> uuid.UUID:
    token = client.cookies.get(cookie_name(app.state.settings))
    sid = await app.state.service.resolve_session(token)
    assert sid is not None
    return sid
