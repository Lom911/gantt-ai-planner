"""`GET /api/meta`: exposes whether the agent is backed by the real Anthropic LLM or
the fake demo mode (e.g. `secrets/anthropic_api_key` not filled in yet on a fresh
deploy), so the UI can show a "Демо-режим без LLM" badge, and the plan's task limit,
so the UI can stop offering «Добавить задачу» at it instead of letting the server refuse
a filled-in form. No session required — this is static server configuration, not
session state.
"""

from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel

from app.agent.llm import resolve_llm_mode, resolve_llm_model
from app.domain.models import MAX_TASKS

router = APIRouter(prefix="/api", tags=["meta"])


class MetaResponse(BaseModel):
    llm_mode: Literal["anthropic", "openrouter", "fake"]
    model: str | None
    max_tasks: int


@router.get("/meta")
async def get_meta(request: Request) -> MetaResponse:
    settings = request.app.state.settings
    mode = resolve_llm_mode(settings)
    model = resolve_llm_model(settings) if mode != "fake" else None
    return MetaResponse(llm_mode=mode, model=model, max_tasks=MAX_TASKS)
