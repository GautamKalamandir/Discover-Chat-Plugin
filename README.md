# Discover Chat Bot-

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
uv run uvicorn app.main:create_app --factory --reload --port 8000
#   GET http://localhost:8000/api/v1/health        -> liveness
#   GET http://localhost:8000/api/v1/health/ready  -> database connectivity
#   GET http://localhost:8000/api/v1/session       -> signed-in user (needs a bearer token)

# Until IT delivers the Entra app registration, use AUTH_PROVIDER=dev (local only):
uv run python -m scripts.mint_dev_token --oid user-a --name "User A"
#   then: curl -H "Authorization: Bearer <token>" http://localhost:8000/api/v1/session

# 4. Quality checks (database tests use a separate `discover_test` database, created automatically)
uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest

# Register semantic models (only registered + enabled models can ever be used by the chatbot):
uv run python -m app.jobs.registry add --dataset-id <id> --workspace-id <id>     --workspace-name "Sales WS" --name "Sales" --domain Sales --enable
uv run python -m app.jobs.registry list
#   GET /api/v1/models/accessible -> the signed-in user's models (dev mode: DEV_MODEL_ACCESS)

# Semantic index (Phase 7). In dev mode, index the sample schemas:
uv run python -m app.jobs.metadata load-fixture --dataset-id sales-ds --file dev-fixtures/schemas/sales-ds.json
uv run python -m app.jobs.metadata status
#   With Power BI access: models index themselves on first use; admins can force it with
#   uv run python -m app.jobs.metadata sync --all   (device-code sign-in, needs ADMIN_CLI_CLIENT_ID)
#   Tests that download the real embedding model: uv run pytest -m network

# Ask the agent from the terminal (dev mode; set GROQ_API_KEY and POWERBI_GATEWAY=dev_synthetic):
uv run python -m app.jobs.ask --user user-a --model sales-ds "What are GOLD sales this FY?"
uv run python -m app.jobs.ask --user user-a --session <id> "and last year?"
#   Real-LLM smoke test: GROQ_API_KEY=... uv run pytest -m network -k groq

# Chat API (what the visual calls): Server-Sent Events over POST
curl -N -X POST http://localhost:8000/api/v1/chat/stream -H "Authorization: Bearer <token>"      -H "Content-Type: application/json" -d '{"question": "What are GOLD sales this FY?", "primary_model_id": "sales-ds"}'
#   GET/DELETE /api/v1/chat/sessions/{id} restores/deletes a conversation; POST /api/v1/chat/sessions = New chat

# Security (Phase 11): scenario coverage + red-team run with the normal suite; scans:
bash ../scripts/security-scan.sh      # pip-audit, npm audit, gitleaks (history + tree)
#   Real-model red-team: GROQ_API_KEY=... uv run pytest -m network -k redteam
#   docs/security/: threat model, scenario matrix, scan results

# Retention cleanup also runs on its own, e.g. from OS cron / a container or cloud scheduler:
uv run python -m app.jobs.cleanup

# 5. Visual (React + TypeScript)
cd ../visual
npm install
npm test                # vitest: SSE, state, auth, filters, markdown safety, App and Visual
npm run typecheck       # strict TypeScript over src/
npx eslint .            # includes the Power BI certification rules (no innerHTML, ...)
npx pbiviz package      # -> visual/dist/*.pbiviz
```

### Running the visual locally (before IT delivers SSO)

1. Backend in dev mode **over HTTPS** (Power BI Service blocks plain-HTTP calls from visuals):
   ```bash
   mkcert -install && mkcert localhost          # once; creates localhost.pem + localhost-key.pem
   cd backend && uv run uvicorn app.main:create_app --factory --port 8000        --ssl-certfile ../localhost.pem --ssl-keyfile ../localhost-key.pem
   ```
   With `AUTH_PROVIDER=dev`, `ENVIRONMENT=local`, `POWERBI_GATEWAY=dev_synthetic`, `DEV_MODEL_ACCESS` and
   `GROQ_API_KEY` in `.env`, plus the dev fixtures indexed (`app.jobs.metadata load-fixture`).
2. Visual in dev mode: `npm run configure:dev` (sign-in through the backend's local-only `/api/v1/dev/token`).
   Then `npx pbiviz install-cert` (needs PowerShell 7, `pwsh`, on Windows) and `npx pbiviz start`.
3. Power BI Service: Settings → Developer settings → **Developer mode** on. Add the *Developer visual* to a
   report page. In Format pane → Developer, set the dev user (e.g. `user-a`). In Data source, pick the model.
4. Before packaging for real use: `npm run configure -- --api https://<backend> --app-id-uri https://<App ID URI>`
   (production SSO build; the dev sign-in code path is not used).

### Whole stack in Docker

`docker compose -f infra/docker-compose.yml --profile app up -d --build` runs Postgres, the backend (HTTPS :8000)
and the visual dev server (HTTPS :8080). One-time setup: [docs/run-locally.md](docs/run-locally.md).

### Live verification on your tenant (Phase 2 spikes)

Follow [docs/spikes-runbook.md](docs/spikes-runbook.md). In short:
- `uv run python -m app.jobs.check_config` checks `.env`, Entra, the credential, Fabric IQ, Groq and the database
  (read-only, never prints secrets);
- `DIAGNOSTICS_ENABLED=true` plus `npm run configure -- ... --diagnostics on` add a **Diagnostics** panel that runs
  spikes S1–S6 as the signed-in user and saves local, gitignored capture bundles;
- `uv run python -m app.jobs.anonymize_capture <schema_payload.json> <fixture.json>` turns a captured schema into a
  committable test fixture.

## Switchable providers (`.env`)

| Setting | Values |
|---|---|
| `CONVERSATION_RETENTION_HOURS` | hours of inactivity before a conversation is deleted (fallback 12) |
| `AUDIT_RETENTION_HOURS` | hours audit events are kept (fallback 2160 = 90 days) |
| `CLEANUP_SCHEDULER_ENABLED` / `CLEANUP_INTERVAL_MINUTES` | built-in cleanup job (fallback every 15 min) |
| `AUTHZ_ALLOWED_TTL_MINUTES` / `AUTHZ_DENIED_TTL_MINUTES` | how long Power BI access answers are cached (fallback 10 / 2) |
| `AUTH_PROVIDER` | `entra` (real Power BI SSO), `dev` (local only, refused in dev/prod environments) |
| `LLM_PROVIDER` / `LLM_MODEL` | `groq` / `openai`; default model `openai/gpt-oss-120b` |
| `FISCAL_YEAR_START_MONTH` | 4 (April-March) |
| `EMBEDDING_PROVIDER` | `local` (fastembed, BAAI/bge-small-en-v1.5), `openai`; switching re-embeds into a new index space |
| `POWERBI_GATEWAY` / `POWERBI_FALLBACK_GATEWAY` | `fabric_iq_mcp`, `rest` |

## Placeholders to replace before release

- `visual/capabilities.json`: `WebAccess` backend origin and the `AADAuthentication` App ID URI (must be a verified
  custom domain, Phase 0).
- `visual/pbiviz.json`: `author` and `supportUrl`.
