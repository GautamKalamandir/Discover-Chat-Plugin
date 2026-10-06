# Discover Chat Bot

A chatbot delivered as a Power BI custom visual (`.pbiviz`). It answers natural-language questions over Power BI semantic
models. A FastAPI agent backend does the work, and users only ever reach the models they are authorized to access.
Their Power BI login is their chatbot login.

See [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) for the architecture, security model and phase plan, and
[docs/adr/](docs/adr/) for recorded decisions.

## Repository layout

| Path | Contents |
|---|---|
| `backend/` | FastAPI backend (Python 3.12, uv) |
| `visual/` | Power BI custom visual (TypeScript, pbiviz) |
| `infra/` | Local infrastructure (PostgreSQL + pgvector via Docker Compose) |
| `docs/` | Architecture decision records and setup guides |

## Prerequisites

- Python 3.12, [uv](https://docs.astral.sh/uv/)
- Node.js LTS and `powerbi-visuals-tools` (`npm i -g powerbi-visuals-tools`)
- Docker Desktop

> If `uv` isn't on your PATH after `pip install --user uv`, either add
> `%APPDATA%\Python\Python312\Scripts` to PATH or use `python -m uv ...`.

## Local development

```bash
# 1. Configuration — every provider (LLM, embeddings, Power BI path) is switched here only
cp .env.example .env

# 2. Database (PostgreSQL 17 + pgvector)
docker compose -f infra/docker-compose.yml up -d --wait

# 3. Backend
cd backend
uv sync
uv run alembic upgrade head
uv run uvicorn app.main:app --reload --port 8000
#   GET http://localhost:8000/api/v1/health        -> liveness
#   GET http://localhost:8000/api/v1/health/ready  -> database connectivity

# 4. Quality checks
uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest

# 5. Visual
cd ../visual
npm install
npx eslint .
npx pbiviz package      # -> visual/dist/*.pbiviz
```

## Switchable providers (`.env`)

| Setting | Values |
|---|---|
| `LLM_PROVIDER` | `groq`, `openai` |
| `EMBEDDING_PROVIDER` | `local`, `openai` (changing it requires a vector re-index) |
| `POWERBI_GATEWAY` / `POWERBI_FALLBACK_GATEWAY` | `fabric_iq_mcp`, `rest` |

## Placeholders to replace before release

- `visual/capabilities.json`: `WebAccess` backend origin and the `AADAuthentication` App ID URI (must be a verified
  custom domain, Phase 0).
- `visual/pbiviz.json`: `author` and `supportUrl`.
