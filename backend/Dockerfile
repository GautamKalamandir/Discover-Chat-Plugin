# Backend image (local Docker stack: infra/docker-compose.yml, docs/run-locally.md).
# Configuration comes only from environment variables; .env and secrets are never baked in.
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.12.23 /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Dependencies first, so code changes don't reinstall them.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app
COPY dev-fixtures ./dev-fixtures

RUN useradd --create-home --uid 10001 app \
    && mkdir -p /cache/embeddings /app/spikes/captures \
    && chown -R app /cache /app/spikes
USER app

EXPOSE 8000

# Migrate, then serve over HTTPS (Power BI only calls https:// backends).
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8000 --ssl-certfile /certs/localhost.crt --ssl-keyfile /certs/localhost.key"]
