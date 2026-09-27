"""`GET /api/ops/status`: one JSON document for external alerting (there is no metrics
backend). Authenticated with `Authorization: Bearer <ops_token>`; without a configured token
the endpoint doesn't exist (404), and it stays out of the OpenAPI schema either way."""

import asyncio
import hmac
import shutil
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Request

from app.api.deps import get_service
from app.db import repo
from app.services.errors import NotFound, Unauthorized
from app.services.ops import WINDOW_MINUTES, read_backup_status

router = APIRouter(prefix="/api/ops", include_in_schema=False)


def require_ops_token(request: Request) -> None:
    secret = request.app.state.settings.ops_token
    expected = secret.get_secret_value().strip() if secret is not None else ""
    if not expected:
        raise NotFound("Нет такого метода API")  # same answer as any unknown API path
    scheme, _, supplied = request.headers.get("authorization", "").partition(" ")
    # compare_digest: the comparison takes as long whatever prefix of the token matches.
    if scheme.lower() != "bearer" or not hmac.compare_digest(
        supplied.strip().encode(), expected.encode()
    ):
        raise Unauthorized("Нужен верный токен: Authorization: Bearer <ops_token>")


def _disk_free_ratio() -> float:
    usage = shutil.disk_usage("/")
    return round(usage.free / usage.total, 4) if usage.total else 0.0


@router.get("/status", dependencies=[Depends(require_ops_token)])
async def ops_status(request: Request) -> dict[str, Any]:
    settings = request.app.state.settings
    now = datetime.now(UTC)
    window = request.app.state.request_metrics.snapshot()
    async with get_service(request).sessionmaker() as db:
        # "Today" is the last 24 hours, the same window as the app-wide chat_limit_per_day.
        messages, tokens = await repo.chat_usage_totals_since(db, now - timedelta(days=1))
    disk_free = await asyncio.to_thread(_disk_free_ratio)
    backup = await asyncio.to_thread(read_backup_status, settings.backup_status_file, now)
    return {
        "window_minutes": WINDOW_MINUTES,
        "requests": window.requests,
        "errors_5xx": window.errors_5xx,
        "error_rate": window.error_rate,
        "p95_ms": window.p95_ms,
        "tokens_today": tokens,
        "chat_messages_today": messages,
        "disk_free_ratio": disk_free,
        "backup": backup,
    }
