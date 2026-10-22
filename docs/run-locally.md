# Run the whole project locally in Docker

Four containers (`infra/docker-compose.yml`, profile `app`):

| Container | URL | What it is |
|---|---|---|
| `postgres` | `localhost:5432` | PostgreSQL 17 + pgvector |
| `backend` | `https://localhost:8000` | FastAPI backend. It migrates the database on start and reads `backend/.env`. |
| `visual` | `https://localhost:8080` | `pbiviz start`, which serves the visual to Power BI's **Developer visual** |
| `adminer` | `http://localhost:8081` | Browser view of the database (only reachable from this PC). Log in with System **PostgreSQL**, Server `postgres`, and the `POSTGRES_*` values. |

You don't need PowerShell 7 or mkcert: both HTTPS servers use one local certificate in `infra/dev-certs/` (gitignored).

## One-time setup

1. **`backend/.env`** filled in, with `uv run python -m app.jobs.check_config` all `[PASS]` (see
   [spikes-runbook.md](spikes-runbook.md) §2).
2. **Local HTTPS certificate.** Skip creating it if `infra/dev-certs/localhost.crt` already exists. Otherwise, from
   the repository root in Git Bash:
   ```bash
   mkdir -p infra/dev-certs
   MSYS_NO_PATHCONV=1 openssl req -x509 -newkey rsa:2048 -nodes -days 365 \
     -keyout infra/dev-certs/localhost.key -out infra/dev-certs/localhost.crt -subj "/CN=localhost" \
     -addext "subjectAltName=DNS:localhost,IP:127.0.0.1" -addext "extendedKeyUsage=serverAuth"
   ```
   Then trust it for your Windows user (Command Prompt, then click **Yes** in the security dialog):
   ```bat
   certutil -user -addstore Root D:\gautam_workspace\Discover-Chat-Bot\infra\dev-certs\localhost.crt
   ```
   Restart the browser (Edge or Chrome).
3. **Entra private key.** The backend container mounts `D:/secrets/chatbot-key.pem`. If yours is somewhere else, set
   `ENTRA_CERT_FILE=<path>` before starting the stack.
4. **Visual configuration** (repeat whenever the API URL or App ID URI changes, then rebuild):
   ```bash
   cd visual
   npm run configure -- --api https://localhost:8000 --app-id-uri https://chatbot-api.kalamandirltd.com --diagnostics on
   ```

## Start / stop

```bash
docker compose -f infra/docker-compose.yml --profile app up -d --build   # start (rebuilds after code changes)
docker compose -f infra/docker-compose.yml --profile app ps              # status: backend should be "healthy"
docker compose -f infra/docker-compose.yml --profile app logs -f backend # follow logs (Ctrl+C to stop following)
docker compose -f infra/docker-compose.yml --profile app down            # stop (the database is kept)
```

Rules:
- After you edit `backend/.env`, recreate the backend:
  `docker compose -f infra/docker-compose.yml --profile app up -d --force-recreate backend`.
- Don't also run `uv run uvicorn ...` on the host: port 8000 is taken by the container.
- `docker compose -f infra/docker-compose.yml up -d` without `--profile app` starts only Postgres. The tests use that.

Checks:
- `https://localhost:8000/api/v1/health` and `https://localhost:8080/assets/status` open in the browser **without a
  certificate warning**.
- Run the config check inside the container:
  `docker exec discover-chatbot-backend-1 python -m app.jobs.check_config`. The `visual ...` lines fail there,
  because the visual's files aren't in the backend image. Run it on the host to check those.

## Admin commands in the container

```bash
docker exec discover-chatbot-backend-1 python -m app.jobs.registry list
docker exec discover-chatbot-backend-1 python -m app.jobs.registry add --dataset-id <id> --workspace-id <id> \
    --workspace-name "<workspace>" --name "<model>" --domain <Sales|HR|...> --enable
```

Phase 2 capture bundles are written to `backend/spikes/captures/` on the host.

## `/summary` reports (Phase 15)

- **Where reports go:** report files are written to `REPORT_STORAGE_DIR` inside the backend container, which is
  backed by the `reports` Docker volume. `docker compose … down` keeps them; `down -v` deletes them, and the database
  with them.
- **When they are deleted:** daily at `REPORT_CLEANUP_TIME` (02:00, `REPORT_TIMEZONE`) and at startup, for reports
  older than `REPORT_RETENTION_DAYS`. To run the deletion by hand:
  ```bash
  docker exec discover-chatbot-backend-1 python -m app.jobs.report_cleanup
  ```
- **Viewing reports:** in Adminer they are in the `summary_reports` table.
- **Pinning KPIs:** a pinned measure is always in that model's report (for users who can see it):
  ```bash
  docker exec discover-chatbot-backend-1 python -m app.jobs.registry kpi add --dataset-id <id> \
      --measure "Sales[Total Net Sales]" --label "Net sales"
  docker exec discover-chatbot-backend-1 python -m app.jobs.registry kpi list --dataset-id <id>
  ```
- **Choosing the `/sales-summary` tables of a model** (ADR 0016): the agent decides which of the four tables
  (`lob_growth`, `subcategory_loss`, `stock_turn`, `price_range`) each model fills. To correct it, pin a table on
  (or `--off`), and `clear` to leave it to the agent again:
  ```bash
  docker exec discover-chatbot-backend-1 python -m app.jobs.registry lens set --dataset-id <id> --lens stock_turn
  docker exec discover-chatbot-backend-1 python -m app.jobs.registry lens set --dataset-id <id> --lens price_range --off
  docker exec discover-chatbot-backend-1 python -m app.jobs.registry lens list --dataset-id <id>
  ```
  This needs migration 0005: rebuild the backend image (`docker compose -f infra/docker-compose.yml up -d --build
  backend`); the container runs the migrations at start.
