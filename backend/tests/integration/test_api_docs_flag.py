import httpx
from asgi_lifespan import LifespanManager

from app.main import create_app

from .conftest import TODAY


async def test_api_docs_can_be_turned_off(settings, sessionmaker):
    # Security audit L4: no public Swagger/OpenAPI in production.
    cfg = settings.model_copy(update={"api_docs": False})
    app = create_app(cfg, sessionmaker=sessionmaker, today=lambda: TODAY)
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
            assert (await c.get("/api/docs")).status_code == 404
            assert (await c.get("/api/openapi.json")).status_code == 404
