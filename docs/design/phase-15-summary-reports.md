# Phase 15 — `/summary` reports: design

Status: **APPROVED 2026-10-13.** Built in sub-phases 15a–15e, each implemented only after its own approval.
Decisions: [ADR 0015](../adr/0015-summary-reports.md).

## 1. Flow

```text
visual: user types "/summary"
  └─ POST /api/v1/reports/summary {regenerate:false}            (bearer token, G1)
       ├─ today's ready report exists → {report_id, status:"ready", reused:true}
       ├─ a build is running for this user → {report_id, status:"building", reused:false}
       └─ else: insert summary_reports(status=building) → start background task
                                                          → {report_id, status:"building"}
visual: GET /api/v1/reports/{id}/events (SSE progress) until done
visual: GET /api/v1/reports/{id} → render ReportView
visual: Download PDF → GET /reports/{id}/pdf → downloadService
                       or POST /reports/{id}/download-link → host.launchUrl(GET /reports/download/{token})
```

The background task holds the request's `AuthorizedContext`, so every query runs as the user through
`PowerBIService` (G3, read-only DAX, the user's OBO token).

## 2. Build pipeline (`app/reports/builder.py`)

Models are processed in parallel, `REPORT_MODEL_CONCURRENCY` at a time. Steps per model:

| # | Step | Who | Reuses |
|---|---|---|---|
| 1 | Visible schema + context | server | `UserSchemaService.visible`, `ContextBuilder.build` |
| 2 | KPI plan: date column, ≤ `REPORT_MAX_KPIS` measures (or aggregations), optional target per KPI, ≤ `REPORT_MAX_DRIVERS` breakdowns, assumptions | LLM (`report_kpis.md`) | `complete_json`, admin pins from `business_glossary` |
| 3 | Validate the plan; one re-plan with the problems | server | `validate_plan` (G2 + visible schema) |
| 4 | Latest and previous day with data | server DAX | `dax_builder` helpers |
| 5 | Last refresh time (best effort) | server | new `PowerBIService.last_refresh` |
| 6 | KPI × baseline values in one `ROW(...)` query | server DAX | `CALCULATE(..., FILTER(ALL(date), range))` as in `change.build_steps` |
| 7 | Focus: ≤ `REPORT_MAX_FOCUS` changes to explain, with breakdowns | LLM (`report_focus.md`) | — |
| 8 | Driver analysis per focus | server | `change.build_steps` + `change.analyze_change` |
| 9 | Section text + suggestions | LLM (`report_section.md`) | — |
| 10 | Grounding | server | `answer.ungrounded_numbers`, `format_number` |

After all models: an executive summary (LLM, `report_summary.md`), grounded the same way, then `report.json`, the
PDF, and the row marked `ready`.

**Queries per model:** about 2 (latest day + KPI values), plus 1 total and up to `REPORT_MAX_DRIVERS` per focus.
That is at most ~10 with the defaults. Every query goes through `Executor.run_step`, so DAX validation, the repair
loop and the `query_executions` record (with `report_id`) are the same as in chat.

### 2.1 Periods (latest day D)

| Fact key | Range |
|---|---|
| `day` | D |
| `prev_day` | latest date with data before D |
| `week_ago` | D − 7 days |
| `mtd` | 1st of D's month … D |
| `last_mtd` | 1st of previous month … same day of month (capped at that month's last day) |
| `target_day`, `target_mtd` | the KPI's target measure over `day` / `mtd`, when a target was chosen |

Each KPI fact has the id `k<n>.<baseline>` and holds `value`, `baseline_value`, `change` and `percent_change`.
Driver facts have the id `f<n>.<breakdown>.<item>` and come from `analyze_change` (change, share of total change).

### 2.2 LLM outputs (pydantic, `extra="forbid"`)

```text
ReportKpiPlan  { date_column, kpis:[{measure | aggregation, label, target?}], breakdowns:[col], assumptions:[str] }
ReportFocus    { focus:[{kpi, baseline, breakdowns:[col], why}] }
ReportSection  { headline, narrative, suggestions:[{priority: high|medium|low, title, reason, notes, evidence:[fact id]}] }
ReportSummary  { summary }
```

The prompts give the LLM evidence and principles only. The LLM decides what matters. There are no word lists and no
thresholds.

### 2.3 Grounding rules

- The numbers in every text field must match a fact at the precision shown (`ungrounded_numbers`).
- A suggestion with an unknown evidence id, no evidence, or an ungrounded number is **dropped**.
- A headline or narrative that fails is replaced by a server template.
- The executive summary may only use numbers from the sections' facts.
- When a source is `dev_synthetic`, the report carries the "Development data" note.

### 2.4 Failure handling

| Case | Result |
|---|---|
| A model's schema can't be read, or a query still fails after retries/repairs | Section `status: "failed"` with "Couldn't load <model> data right now." The other models continue |
| The model has no date column or no data | Section `status: "no_data"` |
| Every model failed | Row `failed`, files removed, the user sees an error |
| Timeout (`REPORT_BUILD_TIMEOUT_SECONDS`) or shutdown | Row `failed` |
| Model access denied mid-build (revoked) | That model's section fails with the generic text; nothing about the model is stored |

## 3. Data

### 3.1 `summary_reports` (new)

| Column | Type | Notes |
|---|---|---|
| id | uuid PK | |
| user_id | uuid FK users, CASCADE | |
| status | varchar `building \| ready \| failed` | |
| report_date | date | calendar day in `REPORT_TIMEZONE` at the start of the build |
| models | jsonb | `[{dataset_id, name, data_date, refreshed_at, status}]` |
| headline | varchar(300) | for the history list |
| suggestion_counts | jsonb | `{high, medium, low}` |
| json_path, pdf_path | varchar | relative to `REPORT_STORAGE_DIR` |
| error_code | varchar(64) | |
| created_at, completed_at | timestamptz | |

Indexes:
- `(user_id, created_at)`;
- **unique `(user_id)` WHERE `status = 'building'`**, for one build per user across instances.

### 3.2 `report_download_tokens` (new)

`token_hash` (sha256 hex, PK), `report_id` (FK, CASCADE), `user_id`, `expires_at`, `used_at`.

Consumed atomically with `UPDATE … SET used_at = now() WHERE token_hash = :h AND used_at IS NULL AND expires_at >
now() RETURNING report_id`.

### 3.3 `query_executions` (changed)

`session_id` becomes nullable. A new `report_id` FK to `summary_reports` (CASCADE) is added, with
`CHECK ((session_id IS NULL) <> (report_id IS NULL))`.

### 3.4 Files

`REPORT_STORAGE_DIR/<user id>/<report id>/report.json` and `report.pdf`.

- Writes are atomic: a temporary file, then `os.replace`.
- Resolved paths must stay inside the storage directory.
- `report.json` follows `SummaryReportContent` (`schema_version: 1`):
  - generated time;
  - user display name;
  - executive summary;
  - per-model sections: status, data date, refresh time, KPI table, drivers, headline, narrative, assumptions;
  - suggestions;
  - notes.

### 3.5 Retention

- The daily job at `REPORT_CLEANUP_TIME` (and at startup) runs under its own advisory lock. It:
  - deletes report files, then rows, for reports created before `now − REPORT_RETENTION_DAYS × 24 h`;
  - deletes expired tokens;
  - deletes orphan directories older than the window;
  - fails stuck `building` rows.
- The API hides reports older than the window.
- The conversation cleanup (ADR 0004) keeps users who still have reports.

## 4. API (`app/api/v1/reports.py`)

| Method | Path | Auth | Response |
|---|---|---|---|
| POST | `/api/v1/reports/summary` | user, G1 | `{report_id, status, reused}`. Body `{regenerate: bool}` |
| GET | `/api/v1/reports/{id}/events` | user (owner) | SSE: `progress {stage, model_name?}`, `model_done {model_name, status}`, `done {status}`, `error {code, message}` |
| GET | `/api/v1/reports?days=7` | user | `[{id, report_date, created_at, status, headline, models, suggestion_counts}]` |
| GET | `/api/v1/reports/{id}` | user (owner) | `report.json` content. **No model re-check** (ADR 0015 §11) |
| GET | `/api/v1/reports/{id}/pdf` | user (owner) | `application/pdf`, `Cache-Control: no-store` |
| POST | `/api/v1/reports/{id}/download-link` | user (owner) | `{url_path, expires_in}` |
| GET | `/api/v1/reports/download/{token}` | **token only** | PDF, `Content-Disposition: attachment`, `no-store`. The same 404 for unknown, used or expired tokens |

Missing, expired and other users' reports all return the same `404 report_not_found`.

If the build runs on another backend instance, or has already finished, `/events` sends the current status from the
database and closes. The visual then fetches the report.

Progress stages: `starting`, `reading_model`, `choosing_kpis`, `querying`, `explaining`, `writing`,
`summarising`, `rendering_pdf`.

## 5. PDF (`app/reports/pdf.py`, ReportLab)

1. **Cover:** `REPORT_COMPANY_NAME`, optional logo (`REPORT_LOGO_PATH`; skipped if missing), "Daily summary", the
   user's display name, generated time (`REPORT_TIMEZONE`), and data date / refresh time per model.
2. **Executive summary.**
3. **Per model:**
   - a KPI table: value, then change and % against each baseline, then against target;
   - a horizontal bar chart of the largest driver changes;
   - the narrative and assumptions.
4. **Suggestions** grouped High / Medium / Low, each with title, reason, notes and its evidence figures.
5. **Footer:** page x of y, "Generated by Discover Chat Bot from your Power BI data".

All LLM text is XML-escaped before it goes into ReportLab paragraphs. Fonts are the built-in Helvetica set (no
external font files).

## 6. Visual

- A typed `/summary` (trimmed, any case) is handled by the visual and never sent to `/chat/stream`.
- The report appears as a new message kind `report` (not stored in the conversation), with the stage labels used for
  progress.
- **Download PDF:**
  - If `downloadService.exportStatus()` is `Allowed`, call `exportVisualsContentExtended(base64, "summary-<date>.pdf",
    "base64", "Daily summary")`.
  - Otherwise, call `download-link` and then `host.launchUrl`.
- **Previous reports** opens or downloads earlier reports from the retention window.
- `capabilities.json` gains `{"name": "ExportContent", "essential": false}`.

## 7. Settings

| Key | Default | Meaning |
|---|---|---|
| `REPORTS_ENABLED` | true | Turns the feature (routes, scheduler) on |
| `REPORT_STORAGE_DIR` | `data/reports` | Where report files are written |
| `REPORT_TIMEZONE` | `Asia/Kolkata` | Report day, cleanup time and times shown |
| `REPORT_RETENTION_DAYS` | 7 | Kept for N × 24 h |
| `REPORT_CLEANUP_SCHEDULER_ENABLED` | true | Built-in daily deletion job |
| `REPORT_CLEANUP_TIME` | `02:00` | When it runs |
| `REPORT_BUILD_TIMEOUT_SECONDS` | 600 | Build time limit |
| `REPORT_MODEL_CONCURRENCY` | 2 | Models built in parallel |
| `REPORT_MAX_KPIS` / `REPORT_MAX_DRIVERS` / `REPORT_MAX_FOCUS` / `REPORT_MAX_SUGGESTIONS` | 6 / 3 / 2 / 6 | Size limits per model |
| `REPORT_COMPANY_NAME` | `Discover Chat Bot` | PDF cover |
| `REPORT_LOGO_PATH` | — | Optional PNG/JPG for the cover |
| `REPORT_DOWNLOAD_LINK_SECONDS` | 60 | One-time link lifetime |

## 8. Sub-phases

| Step | Content |
|---|---|
| 15a | Settings, migration, `ReportStore`, repository, deletion job (scheduler + CLI), cleanup change, tests |
| 15b | Report builder, prompts, Executor `report_id`, refresh time, dev gateway dates, KPI pins CLI, tests |
| 15c | PDF renderer, tests |
| 15d | Build manager, routes, progress stream, one-time link, audit events, tests |
| 15e | Visual: `/summary`, ReportView, history, downloads, privilege, help text mentions `/summary`, tests |

## 9. Phase 15.1: report quality (ADR 0015 amendment 1)

### 9.1 Per-model pipeline additions

| Step | What | Queries |
|---|---|---|
| KPI choice | Shared persona prompt (`prompts/report_persona.md`). Each KPI gets `higher_is_better`. A target is paired with its actual, never a KPI on its own | — |
| Values | As before. A KPI with no value on the latest day is moved to `section.missing_kpis` | 1 |
| Trend | One `ROW(...)` query: the main KPI per day for the last `REPORT_TREND_DAYS` days up to D, and for the same days one month earlier | 1 |
| Performers | For the main KPI against the same day last week, one breakdown query per breakdown (`build_steps` + `analyze_change`, top 5 each way) | 1 + breakdowns |
| No activity now | Per breakdown: items with a value in D−7…D−1 and none on D (all items, not just the top movers) | 1 per breakdown |
| Focus | The agent's extra picks, as before (deduplicated against the performers) | 1 + drivers each |
| Text | Persona prompt. FACTS carry formatted values (`display`) and wording (`"down 56.80%"`). Suggestions add `where`, `owner` and `urgency` | — |

### 9.2 Formatting and text clean-up (server)

- `format_indian(value)` gives `1,47,61,530.70`; `compact_indian(value)` gives `1.48 Cr`, `14.76 lakh` or `5,320`.
- The grounding check accepts both formats (`cr` and `lakh` were already understood).
- `clean_text`:
  - replaces non-breaking and other Unicode hyphens and dashes with `-`, and curly quotes and ellipses with
    plain ASCII;
  - removes evidence ids such as `(k2.prev_day)`, `k1.mtd` and `(f1)`.

### 9.3 report.json schema 2 (additions)

- `KpiRow`:
  - `higher_is_better`;
  - `achievement_day` and `achievement_mtd` (actual / target × 100).
- `ModelSection`:
  - `trend {kpi_label, days: [{day, value, earlier_value}]}`;
  - `performers [DriverBreakdown]` (top 5 each way);
  - `inactive [{by, items: [{item, baseline_value}]}]`;
  - `missing_kpis [label]`.
- `Suggestion`: `where`, `owner`, `urgency` (`today` | `this_week` | `this_month`).
- `SummaryReportContent`: `overview` rows `{model, status, kpi_label, value, change_pct, good, alert}`.

### 9.4 Settings

| Key | Default |
|---|---|
| `REPORT_MODEL_CONCURRENCY` | 4 (was 2) |
| `REPORT_BUILD_TIMEOUT_SECONDS` | 1800 (was 600) |
| `REPORT_TREND_DAYS` | 30 |
| `REPORT_INACTIVE_LOOKBACK_DAYS` | 7 |

### 9.5 Sub-steps

| Step | Content |
|---|---|
| 15.1a | Persona and prompts, direction, formatting, text clean-up, trend / performers / inactive queries, target achievement, suggestion fields, overview rows, settings, tests |
| 15.1b | PDF: at-a-glance table, four charts, KPI table with actual values and coloured changes, "No activity now" table, suggestion cards with Where / Who / By when |
| 15.1c | Visual: the same in the chat (SVG charts, no new libraries), progress "(n of N)" |
