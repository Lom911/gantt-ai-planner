"""Privacy-safe request logging: one line per request, no bodies/query/cookies/headers.

``configure()`` wires stdlib logging to stdout and silences uvicorn's own
access log (which would otherwise log the raw request line, including the
query string). ``AccessLogMiddleware`` is pure ASGI — it only wraps
``send``/observes the scope, never buffers the request or response body —
so it is safe to put in front of streaming responses (SSE) too.
"""

import logging
import sys
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from app.services.ops import RequestMetrics

logger = logging.getLogger("app.access")
# Not fed into the ops metrics: uptime probes and the alerting monitor's own polling would
# dilute the error rate and the latency of real traffic.
UNMETERED_PATHS = frozenset({"/healthz", "/api/ops/status"})

Scope = dict[str, Any]
Message = dict[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
App = Callable[[Scope, Receive, Send], Awaitable[None]]


def configure(level: str) -> None:
    """Configure stdlib logging to stdout and disable uvicorn's access log.

    Uvicorn's own access log line includes the raw request target (path +
    query string); we replace it with our redacted ``app.access`` line, so
    it must stay disabled. Uvicorn is invoked with ``--no-access-log`` in
    the Docker CMD as a second, belt-and-suspenders safeguard.
    """
    logging.basicConfig(level=level.upper(), format="%(message)s", stream=sys.stdout, force=True)
    logging.getLogger("uvicorn.access").disabled = True


class AccessLogMiddleware:
    """Pure-ASGI middleware: logs ``method path status duration_ms sid=<...>``.

    Never logs the query string, request/response bodies, cookies, headers,
    prompts, or plan contents/names — only the four fields above. With `metrics`,
    also feeds each request's status and duration into the ops status window.
    """

    def __init__(self, app: App, metrics: RequestMetrics | None = None) -> None:
        self.app = app
        self.metrics = metrics

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        start = time.monotonic()
        status_holder = {"status": 500}  # default if the app raises before responding
        streaming = False

        async def send_wrapper(message: Message) -> None:
            nonlocal streaming
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
                streaming = any(
                    name.lower() == b"content-type" and value.startswith(b"text/event-stream")
                    for name, value in message.get("headers", [])
                )
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            duration_ms = (time.monotonic() - start) * 1000
            if self.metrics is not None and scope.get("path") not in UNMETERED_PATHS:
                # An SSE stream lasts as long as the client stays: counted, but its duration
                # isn't a response time.
                self.metrics.record(status_holder["status"], None if streaming else duration_ms)
            logger.info(
                "%s %s %s %.1f sid=%s",
                scope.get("method", "-"),
                scope.get("path", "-"),
                status_holder["status"],
                duration_ms,
                _session_id_prefix(scope),
            )


def _session_id_prefix(scope: Scope) -> str:
    session_id = scope.get("state", {}).get("session_id")
    if isinstance(session_id, uuid.UUID):
        return str(session_id)[:8]
    return "-"
