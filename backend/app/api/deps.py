import ipaddress
import uuid

from fastapi import Request, Response

from app.config import Settings
from app.services.errors import BadOrigin, NoSession, RateLimited
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
    """Client address for per-IP limits. Behind a trusted proxy it is the LAST
    X-Forwarded-For hop: Caddy appends the address that connected to it (and by default
    drops forwarded headers from untrusted clients), while earlier hops are whatever the
    client sent. Without trust_proxy the header is ignored and the TCP peer is used."""
    if request.app.state.settings.trust_proxy:
        hops = [h.strip() for h in request.headers.get("x-forwarded-for", "").split(",")]
        hops = [h for h in hops if h]
        if hops:
            return _limit_key(hops[-1])
    return _limit_key(request.client.host) if request.client else "unknown"


def _limit_key(host: str) -> str:
    """One limit bucket per IPv4 address, but per /64 for IPv6: a single IPv6 host usually
    controls its whole /64, so per-address limits would be trivially bypassed."""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return host
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return str(ip.ipv4_mapped)
        return str(ipaddress.ip_network(f"{ip}/64", strict=False))
    return host


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
    origin = request.headers.get("origin")
    if request.method in UNSAFE and origin and origin != request.app.state.settings.public_origin:
        raise BadOrigin("Запрос с чужого источника отклонён")
