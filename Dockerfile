# syntax=docker/dockerfile:1.7
# Base images are pinned by tag AND digest (multi-arch index); Dependabot bumps both.
FROM node:26-alpine@sha256:0b36e8c136b94cd4fcf02188228e76c31ad5872eef3fec8cbd2eee500cfd9e80 AS web
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.14-slim@sha256:51dafde81dbdb6ebde285137a295cf18a47ca95234fe388a343719cb97305b3d AS py
COPY --from=ghcr.io/astral-sh/uv:0.11@sha256:77280f2f771df71f90786c314fe1bbc1e023feac652969bbf139c280babf2eb7 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY backend/pyproject.toml backend/uv.lock backend/.python-version ./
RUN uv sync --frozen --no-dev --no-install-project
COPY backend/ ./
RUN uv sync --frozen --no-dev

FROM python:3.14-slim@sha256:51dafde81dbdb6ebde285137a295cf18a47ca95234fe388a343719cb97305b3d
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin app
WORKDIR /app
COPY --from=py /app /app
COPY --from=web /web/dist /app/static
ENV PATH="/app/.venv/bin:$PATH" STATIC_DIR=/app/static PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
# Forwarded headers are trusted only from FORWARDED_ALLOW_IPS (uvicorn default: 127.0.0.1);
# production sets it, and TRUST_PROXY for per-IP limits, in deploy/compose.prod.yml.
USER 10001
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --retries=5 CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)"]
# Keep-alive 180 s (uvicorn's default is 5 s): Caddy keeps idle upstream connections for 2 minutes and
# reuses them; one uvicorn had already closed made a POST (not replayable) fail with a 502. It also
# broke local clients that reuse a socket after a pause ("socket hang up" in e2e).
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--workers", "1", "--no-access-log", "--timeout-keep-alive", "180"]
