# 0015. `/summary` daily business reports, PDF download and 7-day deletion

- Status: Accepted; report content partly superseded by [ADR 0016](0016-sales-summary.md) (2026-10-15)
- Date: 2026-10-13
- Design: [docs/design/phase-15-summary-reports.md](../design/phase-15-summary-reports.md)

## Context

Managers want a daily report on their Power BI data, with what changed and prioritised suggestions (High / Medium /
Low, each with a reason and notes) to act on. The first idea was a job that builds every user's report every day at
8 o'clock, stores it, and a 7-day deletion job.

A scheduled build has nobody signed in. Every Power BI query here runs with the **user's own delegated token**
(On-Behalf-Of, ADR 0002), so Power BI applies their row- and object-level security. The alternatives don't work:
- Fabric IQ, the primary query path, accepts only delegated tokens.
- Execute Queries REST refuses service principals on any model with row-level security.
- A shared identity would break the core rule in IMPLEMENTATION_PLAN §1.

## Decision

1. **Built when the user types `/summary`**, with their live token. **There is no creation cron.** No refresh tokens
   are stored and no service principal is used.
2. **Reuse.**
   - The first `/summary` of the calendar day builds the report. The day is counted in `REPORT_TIMEZONE`, default
     Asia/Kolkata.
   - Later `/summary` calls the same day show that report.
   - **Regenerate** builds a new one. Every build is kept as its own report.
3. **Scope:** every chatbot-enabled model the user is authorized for (gate G1), one section per model.
4. **Period:** each model's **latest day that has data**, found by a query run as the user. It is shown with that date
   and the model's last refresh time when Power BI provides it.
5. **What is measured.**
   - The agent picks the KPIs, a date column and up to 3 breakdowns from the user's visible schema.
   - Admins can **pin** must-have KPIs per model (`business_glossary`, type `kpi_definition`). Pins are always
     included if the user can see them.
   - The server validates everything like a chat plan (G2, visible schema, read-only DAX).
6. **Baselines** are a fixed set computed by the server:
   - the previous day with data;
   - the same weekday a week earlier;
   - month to date against last month up to the same day;
   - target, where a target measure exists.

   The agent picks which changes to explain. The server works out where each change came from with the existing
   explain-change analysis (ADR 0013).
7. **Suggestions.**
   - The agent sets the priority and writes a title, reason and notes. No fixed thresholds decide priority.
   - Each suggestion must cite computed facts by id, and every number in it must match a computed figure (the ADR
     0008 grounding check). Suggestions failing either check are dropped.
   - Suggestions are read-only: nothing is tracked afterwards.
8. **In the chat:**
   - progress while the report builds, then the report;
   - **Download PDF** and **Regenerate** buttons;
   - a **Previous reports** list for the retention window.
9. **PDF download.**
   - First choice: Power BI's file download API (`ExportContent` privilege, admin switch "Allow downloads from custom
     visuals", the user confirms each download).
   - Otherwise: a **one-time link valid for `REPORT_DOWNLOAD_LINK_SECONDS` (60 s)**, opened with `host.launchUrl`. The
     link is random, stored only as a hash, and works once.
10. **Storage.**
    - `report.json` and `report.pdf` are written under `REPORT_STORAGE_DIR/<user id>/<report id>/` through a
      `ReportStore` interface. It is local disk now; cloud storage can be added in Phase 13.
    - Postgres (`summary_reports`) holds the paths and metadata.
    - Report queries are recorded in `query_executions`, with DAX and result metadata only.
11. **No re-check when a stored report is opened (accepted exception to §1).**
    - A report shows the figures the user was allowed to see when it was built, for as long as it is kept.
    - If their access to a model is removed in that time, they can still open that report.
    - Opening one still requires sign-in, and users can only ever reach their own reports.
12. **Deletion job.**
    - Runs daily at `REPORT_CLEANUP_TIME` (02:00) in `REPORT_TIMEZONE`, and once at startup.
    - Deletes reports created more than `REPORT_RETENTION_DAYS` (7) × 24 hours ago: **files first, then rows**. A file
      that can't be deleted keeps its row, so the next run retries it.
    - It also removes expired download links and orphaned files, and marks builds stuck past the time limit as
      failed.
    - It runs as a built-in scheduler with a Postgres advisory lock, and as `python -m app.jobs.report_cleanup` for OS
      cron (the ADR 0004 pattern).
    - Expired reports are hidden by the API even before the job runs.
13. **Failures.** A model whose queries still fail after the usual retries gets a "couldn't load" section, and the
    rest of the report is saved. If every model fails, nothing is saved and the user sees an error.
14. **Background build.**
    - One build at a time per user, enforced by the database, so it holds across backend instances.
    - The build streams progress and continues if the visual is closed.
    - It is stopped after `REPORT_BUILD_TIMEOUT_SECONDS` (600).

## Why

- Building on demand is the only way to have a ready-made report and still run every query as the user, with no
  stored credentials.
- Reusing the day's report keeps cost down: one build makes several Power BI queries and four LLM calls per model.
  Regenerate covers late data refreshes.
- "Latest day with data" works when a refresh is late, and gives each model an honest date.
- Grounded, evidence-linked suggestions keep the agent free to judge what matters ([ADR 0014](0014-agent-clarification-with-options.md):
  the agent decides, the server verifies) without inventing numbers.
- The no-re-check exception was chosen by the product owner so a report stays a stable record of its day.

## Consequences

- **No report is ready before the user asks.** The first `/summary` of the day takes about 1–2 minutes.
- **Token lifetime.**
  - A background build uses the user's cached Power BI token (about 1 hour).
  - The 600-second limit keeps a build inside that.
  - A build started near the end of the user's sign-in can fail with an authentication error; the user simply runs
    `/summary` again.
- **ADR 0004 amended:** summary reports are kept for `REPORT_RETENTION_DAYS`, longer than conversations (12 h). They
  hold totals, changes and LLM text, **never raw result rows**. Users who still have reports aren't removed by the
  conversation cleanup.
- **`query_executions.session_id` becomes optional:** a row belongs to a chat session or to a report.
- **IT asks:** enable "Allow downloads from custom visuals" (until then, Download PDF uses the one-time link), and
  accept a visual update that adds the `ExportContent` privilege.
- **Disk and backup:** report files contain business figures; the storage volume needs the same protection as the
  database (Phase 13).
- New settings: `REPORTS_ENABLED`, `REPORT_STORAGE_DIR`, `REPORT_TIMEZONE`, `REPORT_RETENTION_DAYS`,
  `REPORT_CLEANUP_SCHEDULER_ENABLED`, `REPORT_CLEANUP_TIME`, `REPORT_BUILD_TIMEOUT_SECONDS`,
  `REPORT_MODEL_CONCURRENCY`, `REPORT_MAX_KPIS`, `REPORT_MAX_DRIVERS`, `REPORT_MAX_FOCUS`, `REPORT_MAX_SUGGESTIONS`,
  `REPORT_COMPANY_NAME`, `REPORT_LOGO_PATH`, `REPORT_DOWNLOAD_LINK_SECONDS`.

## Amendment 1 (2026-10-13): report quality, after the first live report

The first live PDF showed:
- raw numbers and fact ids in the text;
- unsupported characters;
- `n/a` KPI rows;
- a target measure treated as the main sales figure;
- no charts, because charts only appeared when the agent chose a change to explain.

Users have anywhere from 1 to 40 models. **Nothing is tailored to one model**: every rule works from each model's own
schema, descriptions and values.

1. **Point of view.** Every report prompt starts from one shared persona: a senior MIS executive at a jewellery
   brand, writing for MIS executives and upper management. The agent decides, per model, which measures are actuals
   and which are targets. A target is never a KPI on its own; it is paired with the actual it belongs to.
2. **Direction.** The agent marks each KPI `higher_is_better` (true or false). The server colours every change from
   that: green is better, red needs attention. There are no word lists.
3. **Numbers.** Indian format, produced only by the server:
   - `1,47,61,530.70` in tables;
   - `1.48 Cr` or `14.76 lakh` in charts and text.

   The agent receives ready-formatted values and changes ("down 56.80%"), so it never formats a number itself. The
   grounding check still applies.
4. **Charts in every section** (PDF and chat):
   - a daily trend of the main KPI over `REPORT_TREND_DAYS` (30), with the same days a month earlier;
   - a KPI change overview;
   - top and bottom performers per breakdown against the same day last week, always computed, no longer only when
     the agent picks a change;
   - target achievement where a target exists.
5. **"No activity now" alerts.** For every breakdown, items that had a value in the previous 7 days but none on the
   latest day: stores, categories, items, and stock measures that hit zero.
6. **KPIs without a value on the latest day** are listed in a note instead of `n/a` rows.
7. **Suggestions** add `where` (store, region, category or item, from the data), `owner` (the function that should
   act, named by the agent) and `urgency` (today, this week or this month). The existing grounding and evidence
   rules still apply.
8. **Many models.** An "All data sources at a glance" table comes first, then full sections for every model.
   - Defaults change to `REPORT_MODEL_CONCURRENCY=4` and `REPORT_BUILD_TIMEOUT_SECONDS=1800`.
   - Progress shows "Sales (12 of 38)".
   - A 30-minute build still fits inside the user's cached Power BI token, which lasts about 1 hour.
9. **Text clean-up by the server.** Before text reaches the PDF or the chat, characters the PDF font lacks are
   replaced with plain equivalents, and evidence ids such as `(k2.prev_day)` are removed.

**Consequences:**
- About 10 queries per model instead of 2–6: a 40-model report runs about 400 Power BI queries.
- The first `/summary` of the day for a user with many models takes several minutes.
- Report JSON `schema_version` becomes 2. Version-1 reports still display; they simply have no charts.
