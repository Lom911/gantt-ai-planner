import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from datetime import UTC, date, datetime
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastmcp.utilities.lifespan import combine_lifespans
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Receive, Scope, Send

from app.agent.llm import make_llm
from app.agent.loop import Agent
from app.api import (
    routes_chat,
    routes_events,
    routes_mcp_token,
    routes_meta,
    routes_plan,
    routes_session,
)
from app.api.errors import error_response, install_error_handlers
from app.config import Settings, get_settings
from app.db.engine import make_engine, make_sessionmaker
from app.logging_setup import AccessLogMiddleware
from app.logging_setup import configure as configure_logging
from app.mcp_server.auth import SessionTokenVerifier
from app.mcp_server.client import PlanToolClient
from app.mcp_server.server import build_mcp
from app.services.cleanup import run_cleanup_cycle
from app.services.events import EventBus
from app.services.iplimit import SlidingWindowLimiter
from app.services.locks import SessionLocks
from app.services.plan_service import PlanService

logger = logging.getLogger("app.cleanup")

CLEANUP_INTERVAL_SECONDS = 3600
CLEANUP_FIRST_RUN_DELAY_SECONDS = 30


async def _cleanup_loop(app: FastAPI, cfg: Settings) -> None:
    """Purge expired sessions hourly; first run shortly after startup.

    Failures are logged and swallowed so a transient DB hiccup never crashes
    the loop or the app (no crash loop) — the next hourly tick tries again.
    """
    await asyncio.sleep(CLEANUP_FIRST_RUN_DELAY_SECONDS)
    while True:
        try:
            await run_cleanup_cycle(
                app.state.sessionmaker,
                app.state.service.locks,
                app.state.service.bus,
                ttl_days=cfg.session_ttl_days,
                now=datetime.now(UTC),
            )
        except Exception:
            logger.exception("session cleanup cycle failed")
        await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)


class McpOriginGate:
    """Wraps the whole app: guards `/mcp` from cross-origin requests and dodges
    fastmcp's 307 redirect (verified fact: `POST /mcp` w/o a trailing slash
    redirects to `/mcp/`, which MCP HTTP clients don't reliably follow).

    Must be installed as raw ASGI middleware (not a route dependency) so it
    sees the original request path *before* Starlette's router/`Mount` gets
    to rewrite it, and runs before fastmcp's own auth middleware so a bad
    Origin never even reaches the token check.
    """

    def __init__(self, app: ASGIApp, *, public_origin: str) -> None:
        self._app = app
        self._public_origin = public_origin

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        path = scope["path"]
        if path != "/mcp" and not path.startswith("/mcp/"):
            await self._app(scope, receive, send)
            return
        origin = Headers(scope=scope).get("origin")
        if origin is not None and origin != self._public_origin:
            response = JSONResponse(
                {"error": {"code": "bad_origin", "message": "Запрос с чужого источника отклонён"}},
                status_code=403,
            )
            await response(scope, receive, send)
            return
        if scope["path"] == "/mcp":
            scope = {**scope, "path": "/mcp/"}
        await self._app(scope, receive, send)


def create_app(
    settings: Settings | None = None,
    *,
    sessionmaker: async_sessionmaker[AsyncSession] | None = None,
    today: Callable[[], date] | None = None,
) -> FastAPI:
    cfg = settings or get_settings()
    today_fn = today or date.today

    engine = None
    sm = sessionmaker
    if sm is None:
        engine = make_engine(cfg)
        sm = make_sessionmaker(engine)

    service = PlanService(
        sm, EventBus(), SessionLocks(), max_versions=cfg.max_versions, today=today_fn
    )
    verifier = SessionTokenVerifier(sm)
    mcp = build_mcp(service, today=today_fn, auth=verifier)
    # path="/" + mounting at "/mcp" below is what the McpOriginGate path rewrite targets;
    # stateless_http/json_response: no server-side session state, plain request/response.
    mcp_app = mcp.http_app(path="/", stateless_http=True, json_response=True)

    @asynccontextmanager
    async def app_lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.settings = cfg
        app.state.sessionmaker = sm
        app.state.service = service
        app.state.mcp = mcp
        app.state.session_ip_limiter = SlidingWindowLimiter(window_seconds=3600)
        app.state.chat_ip_limiter = SlidingWindowLimiter(window_seconds=3600)
        app.state.mutation_ip_limiter = SlidingWindowLimiter(window_seconds=3600)
        app.state.import_ip_limiter = SlidingWindowLimiter(window_seconds=3600)
        cleanup_task = asyncio.create_task(_cleanup_loop(app, cfg))
        try:
            async with PlanToolClient(mcp) as tool_client:
                app.state.tool_client = tool_client
                app.state.agent = Agent(make_llm(cfg), tool_client, service, today=today_fn)
                yield
        finally:
            cleanup_task.cancel()
            with suppress(asyncio.CancelledError):
                await cleanup_task
            if engine is not None:
                await engine.dispose()

    configure_logging(cfg.log_level)

    app = FastAPI(
        title="Gantt AI Planner",
        lifespan=combine_lifespans(app_lifespan, mcp_app.lifespan),
        docs_url="/api/docs" if cfg.api_docs else None,
        openapi_url="/api/openapi.json" if cfg.api_docs else None,
        redoc_url=None,
    )
    app.add_middleware(AccessLogMiddleware)
    app.add_middleware(McpOriginGate, public_origin=cfg.public_origin)
    install_error_handlers(app)
    app.include_router(routes_session.router)
    app.include_router(routes_plan.router)
    app.include_router(routes_chat.router)
    app.include_router(routes_events.router)
    app.include_router(routes_mcp_token.router)
    app.include_router(routes_meta.router)

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        async with app.state.sessionmaker() as db:
            await db.execute(text("SELECT 1"))
        return JSONResponse({"status": "ok"})

    # Unknown API paths answer with the JSON error envelope, for every method: otherwise a
    # GET falls through to the SPA catch-all (index.html, 200) and other methods to a 405.
    # A wrong method on an existing API path also lands here (404 instead of 405).
    @app.api_route(
        "/api/{rest:path}",
        methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"],
        include_in_schema=False,
    )
    @app.api_route(
        "/api",
        methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"],
        include_in_schema=False,
    )
    async def unknown_api(rest: str = "") -> JSONResponse:
        return error_response("not_found", "Нет такого метода API", status=404)

    app.mount("/mcp", mcp_app)

    _mount_spa(app, cfg)  # must stay the LAST registration: catch-all route
    return app


def _mount_spa(app: FastAPI, settings: Settings) -> None:
    if not settings.static_dir or not Path(settings.static_dir).is_dir():
        return
    root = Path(settings.static_dir).resolve()

    # HEAD too: uptime monitors and link checkers probe "/" with it (FileResponse omits the body).
    @app.api_route("/{path:path}", methods=["GET", "HEAD"], include_in_schema=False)
    async def spa(path: str) -> FileResponse:
        candidate = (root / path).resolve()
        if path and candidate.is_file() and candidate.is_relative_to(root):
            return FileResponse(candidate)
        return FileResponse(root / "index.html")
