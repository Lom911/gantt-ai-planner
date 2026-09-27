import os
from functools import lru_cache
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    db_host: str = "localhost"
    db_port: int = 55432
    db_name: str = "planner"
    db_user: str = "planner_app"
    db_password: SecretStr = SecretStr("planner")
    database_url: str | None = None

    public_origin: str = "http://localhost:8000"
    cookie_secure: bool = False

    llm_provider: Literal["anthropic", "openrouter", "fake"] = "fake"
    llm_model: str = "claude-sonnet-5"
    anthropic_api_key: SecretStr | None = None
    openrouter_api_key: SecretStr | None = None
    # None means "use the provider's own default" (api.anthropic.com for the anthropic
    # SDK, or https://openrouter.ai/api when the effective provider is openrouter — see
    # app.agent.llm.resolve_llm_base_url). Only needed to point at a different endpoint
    # (e.g. a self-hosted OpenRouter-compatible gateway).
    llm_base_url: str | None = None
    llm_max_tokens: int = 4096
    # Billed tokens (input + output + cache writes + cache reads) one chat turn may spend over
    # all its LLM calls. A ceiling: a call is sent only if its estimated input plus a minimum
    # answer still fits, with max_tokens capped to the rest; otherwise the turn stops with
    # `turn_budget_exceeded` (app.agent.loop).
    llm_turn_token_budget: int = 300_000

    chat_limit_per_hour: int = 30  # per session, sliding hour
    chat_limit_per_day: int = 500  # app-wide, last 24 hours (chat_usage)
    # Of chat_limit_per_day, the last chat_daily_reserve messages are kept for addresses that
    # have sent fewer than chat_reserve_per_ip_day messages today: a few heavy clients can't
    # use up the quota for everyone else.
    chat_daily_reserve: int = 100
    chat_reserve_per_ip_day: int = 10
    # Per client IP, in Postgres (fixed windows: the clock hour, the UTC day; they survive a
    # restart): new sessions and chat messages across sessions.
    session_limit_per_ip_hour: int = 20
    chat_limit_per_ip_hour: int = 60
    # Per day: without it a handful of addresses at the hourly cap could use up the whole
    # app-wide chat_limit_per_day on their own.
    chat_limit_per_ip_day: int = 150
    # Per client IP: plan mutations (operations/undo/redo/reset, in memory) and Excel imports
    # (in Postgres). Each one stores a full plan snapshot, so unbounded bursts from one client
    # would grow the database.
    mutation_limit_per_ip_hour: int = 1200
    import_limit_per_ip_hour: int = 60
    # Per client IP: every request to the external /mcp endpoint, authenticated or not (each
    # bearer-token attempt costs a database lookup).
    mcp_limit_per_ip_hour: int = 1200
    # Interactive API docs (/api/docs, /api/openapi.json): handy locally, off in production.
    api_docs: bool = True
    # Take the client IP from the last X-Forwarded-For hop (the one our reverse proxy set).
    # Only turn on when every request reaches the app through that proxy (production: Caddy
    # on the internal network); otherwise any client could pick its own "IP".
    trust_proxy: bool = False
    max_upload_mb: int = 2
    session_ttl_days: int = 14
    max_versions: int = 50
    # Largest plan snapshot (compact UTF-8 JSON) a mutation or import may store; each session
    # keeps up to max_versions of them.
    max_plan_json_bytes: int = 1_500_000
    static_dir: str | None = None
    log_level: str = "INFO"

    @property
    def sqlalchemy_url(self) -> str:
        if self.database_url:
            return self.database_url
        return URL.create(
            "postgresql+asyncpg",
            username=self.db_user,
            password=self.db_password.get_secret_value(),
            host=self.db_host,
            port=self.db_port,
            database=self.db_name,
        ).render_as_string(hide_password=False)


@lru_cache
def get_settings() -> Settings:
    return Settings(_secrets_dir=os.environ.get("SECRETS_DIR") or None)
