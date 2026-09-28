import asyncio
import logging
import re
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
    routes_ops,
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
from app.services.iplimit import SlidingWindowLimiter, client_key
from app.services.locks import SessionLocks
from app.services.ops import RequestMetrics
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
    """Wraps the whole app: rate-limits `/mcp` per client IP, guards it from
    cross-origin requests and dodges fastmcp's 307 redirect (verified fact:
    `POST /mcp` w/o a trailing slash redirects to `/mcp/`, which MCP HTTP
    clients don't reliably follow).

    Must be installed as raw ASGI middleware (not a route dependency) so it
    sees the original request path *before* Starlette's router/`Mount` gets
    to rewrite it, and runs before fastmcp's own auth middleware so a bad
    Origin — or a client over its limit — never even reaches the token check.
    """

    def __init__(self, app: ASGIApp, *, settings: Settings) -> None:
        self._app = app
        self._settings = settings
        # Every request counts, authenticated or not (security audit L5): each bearer-token
        # attempt costs a SELECT + UPDATE before fastmcp can accept or refuse it.
        self._limiter = SlidingWindowLimiter(window_seconds=3600)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        path = scope["path"]
        if path != "/mcp" and not path.startswith("/mcp/"):
            await self._app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        peer = scope.get("client")
        key = client_key(
            peer[0] if peer else None,
            headers.get("x-forwarded-for"),
            trust_proxy=self._settings.trust_proxy,
        )
        if not self._limiter.allow(key, self._settings.mcp_limit_per_ip_hour):
            response = error_response(
                "rate_limited", "Слишком много запросов к MCP с вашего адреса. Попробуйте позже."
            )
            await response(scope, receive, send)
            return
        origin = headers.get("origin")
        if origin is not None and origin != self._settings.public_origin:
            response = error_response("bad_origin", "Запрос с чужого источника отклонён")
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
        sm,
        EventBus(),
        SessionLocks(),
        max_versions=cfg.max_versions,
        max_plan_bytes=cfg.max_plan_json_bytes,
        today=today_fn,
    )
    verifier = SessionTokenVerifier(sm)
    mcp = build_mcp(service, today=today_fn, auth=verifier)
    # path="/" + mounting at "/mcp" below is what the McpOriginGate path rewrite targets;
    # stateless_http/json_response: no server-side session state, plain request/response.
    mcp_app = mcp.http_app(path="/", stateless_http=True, json_response=True)
    request_metrics = RequestMetrics()

    @asynccontextmanager
    async def app_lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.settings = cfg
        app.state.sessionmaker = sm
        app.state.service = service
        app.state.mcp = mcp
        app.state.request_metrics = request_metrics
        # Sessions, chat and imports are limited in Postgres (app.services.ratelimit).
        app.state.mutation_ip_limiter = SlidingWindowLimiter(window_seconds=3600)
        cleanup_task = asyncio.create_task(_cleanup_loop(app, cfg))
        try:
            async with PlanToolClient(mcp) as tool_client:
                app.state.tool_client = tool_client
                app.state.agent = Agent(
                    make_llm(cfg),
                    tool_client,
                    service,
                    today=today_fn,
                    turn_token_budget=cfg.llm_turn_token_budget,
                    max_output_tokens=cfg.llm_max_tokens,
                )
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
    # The last one added is the outermost: the access log also records the gate's own 403/429.
    app.add_middleware(McpOriginGate, settings=cfg)
    app.add_middleware(AccessLogMiddleware, metrics=request_metrics)
    install_error_handlers(app)
    app.include_router(routes_session.router)
    app.include_router(routes_plan.router)
    app.include_router(routes_chat.router)
    app.include_router(routes_events.router)
    app.include_router(routes_mcp_token.router)
    app.include_router(routes_meta.router)
    app.include_router(routes_ops.router)

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


# Vite's default asset names: `<name>-<8-char base64url hash>.<ext>`.
_HASHED_NAME = re.compile(r"-[A-Za-z0-9_-]{8}\.[a-z0-9]+$")


def _mount_spa(app: FastAPI, settings: Settings) -> None:
    if not settings.static_dir or not Path(settings.static_dir).is_dir():
        return
    root = Path(settings.static_dir).resolve()
    assets = root / "assets"
    # Without Cache-Control a browser caches index.html heuristically (a share of the time since
    # Last-Modified), so after a deploy it kept loading the previous build. So every file is
    # revalidated on each load, except the ones Vite names by their content hash in assets/
    # (`index-DQn1RFkq.js`), which never change under the same URL. The check is on the file
    # actually served: a missing /assets/… falls back to index.html, which must not be cached.
    revalidate = {"Cache-Control": "no-cache"}
    immutable = {"Cache-Control": "public, max-age=31536000, immutable"}

    # HEAD too: uptime monitors and link checkers probe "/" with it (FileResponse omits the body).
    @app.api_route("/{path:path}", methods=["GET", "HEAD"], include_in_schema=False)
    async def spa(path: str) -> FileResponse:
        candidate = (root / path).resolve()
        if path and candidate.is_file() and candidate.is_relative_to(root):
            hashed = candidate.is_relative_to(assets) and bool(_HASHED_NAME.search(candidate.name))
            return FileResponse(candidate, headers=immutable if hashed else revalidate)
        return FileResponse(root / "index.html", headers=revalidate)
