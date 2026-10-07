# Phase 2 spikes: runbook (Step 2)

Runs spikes S1 to S6 against the real tenant, through the same code path the product uses: Power BI SSO, then the
backend, then OBO, then Fabric IQ or REST. Every run writes a local **capture bundle** to
`backend/spikes/captures/<UTC time>-<user hash>/`. This folder is gitignored and is never committed. When you are
done, say **"captures ready"**. You don't need to paste anything, because the files are local.

What a bundle contains:

| File | Content | Real data? |
|---|---|---|
| `summary.json` | Each check: status, duration, safe details (claims such as `oid`, `tid`, `aud`, `scp`; counts; error codes) | No tokens, no row values |
| `fabric_iq_tools.json` | Fabric IQ tool names and input schemas | Public contract |
| `schema_raw_result.json`, `schema_payload.json` | The model's schema as Fabric IQ returns it | **Model metadata** (table, column and measure names, descriptions); stays local |
| `execute_query_masked.json`, `value_search_masked.json`, `rest_*.json` | Response **shape** | Values replaced by `<text:N>` / `<number>` |
| `invalid_dax_error.json`, a failed `rest_execute_queries.json` | Power BI's error text | Error wording only; stays local |
| `visual_report.json` | What the visual sees: host, SSO status, saved objects, Context-fields columns and counts, stream timings | No values |

Tokens are never written anywhere. Only an anonymized copy of the schema is committed later, as a test fixture
(`app.jobs.anonymize_capture`).

---

## 1. Install the tools (once)

```powershell
winget install --id Microsoft.PowerShell      # PowerShell 7 (pwsh), needed by pbiviz install-cert
winget install --id FiloSottile.mkcert
```

Open a **new** terminal afterwards so that `pwsh` and `mkcert` are on the PATH.

```bash
# HTTPS certificate for the backend (from the repository root): creates localhost.pem + localhost-key.pem
mkcert -install
mkcert localhost
# HTTPS certificate for the visual dev server
cd visual && npx pbiviz install-cert
```

## 2. Fill `.env` (repository root)

Use the values from IT (docs/entra-setup.md). Secrets go only into `.env`, never into chat.

```dotenv
ENVIRONMENT=dev
AUTH_PROVIDER=entra
ENTRA_CLIENT_ID=<application (client) id>
ENTRA_APP_ID_URI=https://<your custom domain App ID URI>
ENTRA_ALLOWED_TENANT_IDS=["<tenant id>"]
ENTRA_REQUIRED_SCOPE=discoverChatBot09E811F9CAF94C58AD6EEF5D7849A3F7_CV_ForPBI
ENTRA_CLIENT_CERTIFICATE_PATH=<path to .pem>      # or ENTRA_CLIENT_SECRET=<secret>
ENTRA_CLIENT_CERTIFICATE_THUMBPRINT=<thumbprint>
POWERBI_GATEWAY=fabric_iq_mcp
POWERBI_FALLBACK_GATEWAY=rest
GROQ_API_KEY=<your key>
DIAGNOSTICS_ENABLED=true
```

Leave unused lines empty (for example `OPENAI_EMBEDDING_DIMENSIONS=`). Empty values count as "not set".

## 3. Configure the visual, then check everything

```bash
cd visual
npm run configure -- --api https://localhost:8000 --app-id-uri https://<App ID URI> --diagnostics on
cd ../backend
uv run python -m app.jobs.check_config
```

Repeat until every line is `[PASS]`. The `localhost` API URL is expected for these spikes. `check_config` is
read-only and never prints secret values. If a line fails, the note next to it says why. Common causes:

| Failing check | Usual cause |
|---|---|
| `visual AADAuthentication COM == ENTRA_APP_ID_URI` | `--app-id-uri` differs from `ENTRA_APP_ID_URI` (including a trailing `/`) |
| `OBO credential accepted by Entra` (`AADSTS7000215`, `AADSTS700027`) | Wrong secret, or the certificate isn't uploaded to the app registration |
| `tenant ... exists` | Wrong tenant id |
| `Fabric IQ endpoint reachable` | Proxy or private link: use the alternative URL in `.env.example` |

## 4. Register the test models

Get the ids from the model's URL in Power BI Service: `.../groups/<workspace id>/datasets/<dataset id>/...`.

```bash
uv run python -m app.jobs.registry add --dataset-id <dataset id> --workspace-id <workspace id> \
    --workspace-name "<workspace>" --name "<model name>" --domain <Sales|HR|...> --enable
uv run python -m app.jobs.registry list
```

The dev sample models (`sales-ds`, ...) can stay registered. Power BI simply doesn't know them, so nobody gets
access to them.

## 5. Start the backend (HTTPS) and the visual

```bash
# terminal 1, from backend/
uv run uvicorn app.main:create_app --factory --port 8000 --ssl-certfile ../localhost.pem --ssl-keyfile ../localhost-key.pem
# terminal 2, from visual/
npx pbiviz start
```

## 6. Run the checks in Power BI Service

1. Settings → Developer settings → turn **Developer mode** on.
2. Open a report in the test workspace. Edit it, and add the **Developer visual** from the Visualizations pane.
3. In Format pane → Data source, pick the registered model. The dropdown appears once sign-in works.
4. In the **Diagnostics (Phase 2)** panel, optionally enter a value that exists in the model (for example a product
   line) to test ValueSearch. Then click **Run Phase 2 checks**.
5. The table shows each check. A **failed** row is not your mistake: it is exactly what the spikes exist to find.
   Just continue.
6. Drag one slicer column (for example a product-line column) into the visual's **Context fields** well. Set a
   slicer on the page, then run the checks again (S5).

Repeat step 4 signed in as each test user. Use a private browser window per user:

| Run | User | Shows |
|---|---|---|
| a | Normal user with Build permission | The full happy path |
| b | User **without Build** permission on the model | S3 / S4: can Fabric IQ or REST be used without Build? |
| c | **RLS-restricted** user | S3: the rows are filtered (only the shape is saved; compare the row counts in the table) |

## 7. Power BI Desktop (S1, S6)

The developer visual in Desktop needs the packaged diagnostics build:

```bash
cd visual && npx pbiviz package      # still configured with --diagnostics on
```

In Desktop: File → Options → Report settings → turn on **Develop a visual** (if shown). Import
`visual/dist/*.pbiviz` (Visualizations → … → Import a visual from a file). Then sign in and run the checks once.

## 8. Finish

1. Say **"captures ready"**. I analyse the bundles and write `docs/spikes.md` (Step 3).
2. Before you ship anything, configure a normal build again and turn diagnostics off:
   ```bash
   cd visual && npm run configure -- --api https://<backend> --app-id-uri https://<App ID URI>
   ```
   Also set `DIAGNOSTICS_ENABLED=false`. Production refuses to start with it on.
3. You can delete the capture folders once the fixtures are committed.
