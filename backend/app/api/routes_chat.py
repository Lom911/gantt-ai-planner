import json
import uuid
from collections.abc import AsyncIterator
from contextlib import aclosing
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import text
from sse_starlette import EventSourceResponse

from app.api.deps import check_origin, client_ip, get_service, require_session
from app.db import repo
from app.services.errors import AgentBusy, RateLimited
from app.services.ratelimit import check_chat_limits

router = APIRouter(prefix="/api/chat")

# Arbitrary constant key for pg_advisory_xact_lock: serializes the check-then-insert
# below across concurrent requests (any session) for the lifetime of the DB
# transaction, so the per-session/global counts check_chat_limits() sees can never be
# stale by the time we reserve a slot with the INSERT. Transaction-scoped (`_xact_`),
# so it's released automatically on commit or rollback — never held past this block.
CHAT_RATE_LIMIT_LOCK_KEY = 0x63686174  # "chat" packed as a bigint, purely for readability


def _sse(event: dict[str, Any]) -> dict[str, str]:
    return {"event": event["type"], "data": json.dumps(event, ensure_ascii=False)}


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)


@router.post("", dependencies=[Depends(check_origin)])
async def chat(
    body: ChatRequest, request: Request, session_id: uuid.UUID = Depends(require_session)
) -> EventSourceResponse:
    service = get_service(request)
    settings = request.app.state.settings
    if service.locks.is_busy(session_id):
        raise AgentBusy()
    per_ip = settings.chat_limit_per_ip_hour
    if not request.app.state.chat_ip_limiter.allow(client_ip(request), per_ip):
        raise RateLimited(f"Лимит: {per_ip} сообщений в час с одного адреса. Попробуйте позже.")
    user_text = body.message.strip()
    turn_id = uuid.uuid4()
    # Check-then-reserve, atomically: without the advisory lock, two concurrent
    # requests could both pass check_chat_limits() (each sees the count *before* the
    # other's insert) and together exceed chat_limit_per_day/-hour. Taking the message
    # slot here (INSERT) means run_turn() below must not insert it again.
    async with service.sessionmaker() as db, db.begin():
        await db.execute(
            text("SELECT pg_advisory_xact_lock(:key)"), {"key": CHAT_RATE_LIMIT_LOCK_KEY}
        )
        await check_chat_limits(
            db,
            session_id,
            per_hour=settings.chat_limit_per_hour,
            per_day=settings.chat_limit_per_day,
        )
        await repo.add_chat_message(
            db, session_id=session_id, role="user", content=user_text, turn_id=turn_id
        )
        await repo.add_chat_usage(db)
    agent = request.app.state.agent

    async def gen() -> AsyncIterator[dict[str, str]]:
        # aclosing() ensures run_turn() is closed deterministically (its busy flag and
        # "agent_status: false" publish included) if the client disconnects mid-stream —
        # a bare `async for ... in agent.run_turn(...): yield` would leave that inner
        # generator to be closed only whenever it happens to be garbage-collected.
        try:
            async with aclosing(agent.run_turn(session_id, user_text, turn_id=turn_id)) as turn:
                async for event in turn:
                    yield _sse(event)
        except AgentBusy as exc:
            # A concurrent send from this session won the race after our is_busy() check
            # (run_turn raises before doing anything). The 200 is already out, so report it
            # in-stream, and drop the reserved user message: it would otherwise stay in the
            # history as a question that never gets an answer.
            async with service.sessionmaker() as db, db.begin():
                await repo.delete_turn_messages(db, session_id, turn_id)
            yield _sse({"type": "error", "code": exc.code, "message": exc.message})

    return EventSourceResponse(gen(), ping=15, sep="\n")


@router.get("/history")
async def history(
    request: Request, session_id: uuid.UUID = Depends(require_session)
) -> list[dict[str, Any]]:
    async with get_service(request).sessionmaker() as db:
        rows = await repo.recent_chat_messages(db, session_id, 200)
    return [
        {
            "id": r.id,
            "role": r.role,
            "content": r.content,
            "created_at": r.created_at.isoformat(),
            "meta": r.meta,
        }
        for r in rows
    ]
