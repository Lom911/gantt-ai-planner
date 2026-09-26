import uuid

from fastapi import Request, Response

from app.config import Settings
from app.services.errors import BadOrigin, NoSession, RateLimited
from app.services.iplimit import client_key
from app.services.plan_service import PlanService

UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}


def cookie_name(settings: Settings) -> str:
    return "__Host-sid" if settings.cookie_secure else "sid"


def set_session_cookie(response: Response, settings: Settings, token: str) -> None:
    response.set_cookie(
        cookie_name(settings),
        token,
        max_age=settings.session_ttl_days * 86400,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )


def clear_session_cookie(response: Response, settings: Settings) -> None:
    # Same attributes as when set: browsers ignore a __Host- cookie (deletion included)
    # that comes without Secure.
    response.delete_cookie(
        cookie_name(settings),
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )


def get_service(request: Request) -> PlanService:
    service: PlanService = request.app.state.service
    return service


async def require_session(request: Request) -> uuid.UUID:
    token = request.cookies.get(cookie_name(request.app.state.settings))
    if not token:
        raise NoSession()
    session_id = await get_service(request).resolve_session(token)
    if session_id is None:
        raise NoSession()
    request.state.session_id = session_id  # read by AccessLogMiddleware for sid=... logging
    return session_id


def client_ip(request: Request) -> str:
    """Client address for per-IP limits (trusted-proxy and IPv6 /64 rules: see client_key)."""
    return client_key(
        request.client.host if request.client else None,
        request.headers.get("x-forwarded-for"),
        trust_proxy=request.app.state.settings.trust_proxy,
    )


def limit_mutations(request: Request) -> None:
    settings = request.app.state.settings
    if not request.app.state.mutation_ip_limiter.allow(
        client_ip(request), settings.mutation_limit_per_ip_hour
    ):
        raise RateLimited("Слишком много изменений плана с вашего адреса. Попробуйте позже.")


def limit_imports(request: Request) -> None:
    settings = request.app.state.settings
    if not request.app.state.import_ip_limiter.allow(
        client_ip(request), settings.import_limit_per_ip_hour
    ):
        raise RateLimited("Слишком много загрузок Excel с вашего адреса. Попробуйте позже.")


def check_origin(request: Request) -> None:
    if request.method not in UNSAFE:
        return
    origin = request.headers.get("origin")
    # Fetch metadata as a second signal: a browser marks a request from another site as
    # `Sec-Fetch-Site: cross-site` even where it omits the Origin header.
    cross_site = request.headers.get("sec-fetch-site") == "cross-site"
    if (origin and origin != request.app.state.settings.public_origin) or cross_site:
        raise BadOrigin("Запрос с чужого источника отклонён")
