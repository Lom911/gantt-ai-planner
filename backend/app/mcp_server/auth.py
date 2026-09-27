"""Bearer-token auth for the external `/mcp` HTTP endpoint (spec §8).

Tokens are `mcp_`-prefixed, one per session (issuing a new one revokes the
previous), stored as a sha256 hash + short prefix (never the raw token). The
in-process client (`PlanToolClient` over the in-memory transport) never goes
through this verifier — fastmcp's in-memory transport doesn't run the HTTP
auth middleware, so `get_access_token()` there is always `None` and
`resolve_session_id()` falls back to the `current_session` contextvar.
"""

from datetime import UTC, datetime

from fastmcp.server.auth import AccessToken, TokenVerifier
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import repo
from app.services.plan_service import TOUCH_INTERVAL
from app.services.sessions import hash_token

TOKEN_PREFIX = "mcp_"


class SessionTokenVerifier(TokenVerifier):
    """Looks up an `mcp_...` bearer token and resolves it to its session."""

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        super().__init__()
        self._sessionmaker = sessionmaker

    async def verify_token(self, token: str) -> AccessToken | None:
        if not token.startswith(TOKEN_PREFIX):
            return None
        now = datetime.now(UTC)
        stale = now - TOUCH_INTERVAL
        async with self._sessionmaker() as db, db.begin():
            found = await repo.get_active_mcp_token(db, hash_token(token), now)
            if found is None:
                return None
            row, last_seen = found
            session_id = row.session_id
            # Every MCP request comes through here, but both timestamps only matter at the scale
            # of days (the «последнее использование» hint, the idle-session cleanup): like
            # PlanService.resolve_session, write them at most every TOUCH_INTERVAL instead of an
            # UPDATE (and a WAL write) per request.
            if row.last_used_at is None or row.last_used_at < stale:
                await repo.touch_mcp_token(db, row.id, now)
            # Working only through an MCP client is activity too: without this the idle-session
            # cleanup (session_ttl_days) would delete the plan of an MCP-only user.
            if last_seen < stale:
                await repo.touch_session(db, session_id)
        return AccessToken(
            token=token,
            client_id=str(session_id),
            scopes=[],
            claims={"session_id": str(session_id)},
        )
