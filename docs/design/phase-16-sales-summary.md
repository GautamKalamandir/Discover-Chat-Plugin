# Phase 16: `/sales-summary` design

Status: **APPROVED 2026-10-15.** Built in sub-phases 16a–16f, each implemented only after its own approval.
Decisions: [ADR 0016](../adr/0016-sales-summary.md). Reused from Phase 15: build manager, SSE progress, `ReportStore`,
repository, PDF download (download API or one-time link), the 7-day deletion job
([phase-15 design](phase-15-summary-reports.md) §1, §3.2–§3.5, §4, §6).

## 1. Flow

```text
visual: "/sales-summary" (or "/summary")
  └─ POST /api/v1/reports/summary {regenerate}            unchanged route; builds a sales report (schema 3)
       └─ background build, as the user:
            1. discovery     which allowed models are sales models, and which lenses each can fill   (LLM, batched)
            2. per (model, lens), REPORT_MODEL_CONCURRENCY at a time:
                 seed step ─► loop { LLM: next LensStep | done } ─► validate ─► DAX ─► Executor ─► facts
                 ─► table rows (server) ─► findings + suggestions (LLM, grounded)
            3. trend + "no activity now" for each model's main sales measure      (reused from Phase 15)
            4. summary bullets (LLM; colour by the server) + action list
            5. report.json (schema 3) ─► PDF ─► row ready
```

## 2. Lenses

A **lens** is one of the four tables. It is defined by its goal and the columns it needs, never by a model or
dashboard name.

| Lens | Goal (for the prompt) | Needs (from discovery) | Seed step (always run first) |
|---|---|---|---|
| `lob_growth` | Which LOBs grew and which degrew against last year; for the worst degrowing LOBs, why (sub-category, store, stock availability) and what to do | date column, sales measure(s), LOB column; optional sub-category, store, weight and quantity measures | LOB × every sales unit × `day, day_ly, mtd, mtd_ly, fytd, fytd_ly` |
| `subcategory_loss` | Which sub-categories lost sales, in which stores and months, and what to do | date, sales measure, sub-category column; store column | sub-category × sales × `mtd, mtd_ly, fytd, fytd_ly` |
| `stock_turn` | How fast stock sells, by LOB or category and by store; slow and non-moving stock | a stock-turn measure, **or** sales and stock (closing stock) measures plus a date column | the turn measure × LOB, **or** sales and stock × month (FYTD) |
| `price_range` | Sales by price range against last year; which ranges or items aren't selling and why | a band column, **or** a price/value-per-item column for agent bands | band × sales × `mtd, mtd_ly, fytd, fytd_ly` |

A model can fill one or more lenses. A stock model usually fills only `stock_turn`; it is also the source for
the stock check in `lob_growth` (§5.3).

## 3. Discovery (`app/reports/sales/discovery.py`)

- **Input:** every allowed model's visible schema, from `UserSchemaService.visible` (OLS applied). Each model is sent
  as a compact list (tables, columns with types, measures, descriptions), with up to 10 models per LLM call.
- **Output** (pydantic, `extra="forbid"`), per model:

```text
ModelRoles { dataset_id, is_sales: bool, lenses: [lens], date_column?,
             columns: { lob?, subcategory?, store?, item?, price_band?, price? },
             measures: { sales_value?, sales_weight?, sales_qty?, stock_value?, stock_weight?, stock_turn?,
                         higher_is_better: {measure: bool} },
             reason }
```

- **Server checks:**
  - every column and measure must be visible to the user and of the right kind (dates for the date column);
  - unknown references are dropped, with one re-ask that lists the problems (the same pattern as `_choose_kpis`).
- **Admin pins** (`semantic_models.report_lenses`, migration 0005) override the agent's choice: `{lens: true}`
  forces a lens on, `{lens: false}` turns it off. A pin on a model the user can't access has no effect (G1).
- **Result:** the report's `models_used` lists the models included and what each one was used for.

## 4. `LensStep`: the agent's query (`app/reports/sales/steps.py`)

```text
LensStep {
  model_id,                               must be an allowed model (G1)
  measures: [Table[Measure]] ≤ 4 | aggregations: [Aggregation] ≤ 4,
  group_by: [Table[Column]] ≤ 3,
  filters: [Filter] ≤ 10,                 the existing Filter shape (=, in, <>, between, >, <, ...)
  periods: [day | day_ly | mtd | mtd_ly | fytd | fytd_ly | months_fy | months_fy_ly] ≥ 1,
  bands: [{label, min, max}] ≤ 12 + band_column,     only for price ranges with no band column
  top_n ≤ 200, order: value_desc | value_asc | group_asc,
  why: str ≤ 160                           logged, never shown
}
```

**Validation** reuses `plan_validator.check_step` (made public), `resolve_measure`, `is_date_column` and the
visible-key check. In addition:
- bands must be ascending and must not overlap;
- `band_column` must be a numeric column;
- a step identical to one already run is refused.

### 4.1 Periods (latest day with data D, FY start month F)

| Key | Range |
|---|---|
| `day` | D |
| `mtd` | 1st of D's month … D |
| `fytd` | 1st of F in D's financial year … D |
| `*_ly` | the same range one year earlier (`shift_year`, 29 Feb → 28 Feb) |
| `months_fy` | one range per month of the financial year up to D; the last month is MTD |
| `months_fy_ly` | the same months one year earlier, each cut at the same day of the month |
| `month_ends_fy` | the last day of each of those months (D for the current one): closing stock for the derived stock turn (added in 16c) |

D is found per model with the existing `facts.latest_dates_dax`.

### 4.2 DAX (`app/reports/sales/dax.py`)

```text
EVALUATE
TOPN(<top_n>,                                   only when top_n is given
  SUMMARIZECOLUMNS(<group_by…>,
     <filters as tables>,                        dax_builder._filter_table
     "m1|mtd",    CALCULATE(<m1>, <in_days(date, mtd)>),
     "m1|mtd_ly", CALCULATE(<m1>, <in_days(date, mtd_ly)>),
     ...),
  [m1|<first period>], DESC)
```

- Bands: one column per band, `"m1|mtd|b2", CALCULATE(<m1>, <period>, FILTER(ALL(<band col>), <band col> >= min && <
  max))`.
- Built only from the existing helpers `column`, `quote_name`, `literal`, `_filter_table` and `facts.in_days`.
- Run with `Executor.run_step(..., report_id, repair=False)`. The read-only `validate_dax` still applies.
  Server-built DAX is never sent to the LLM repair loop.

### 4.3 Facts

Each result cell becomes a fact. Its id is `<lens>.<step>.<row>.<measure>|<period>[|<band>]`, e.g.
`lob.2.r3.m1|mtd`. Each fact holds:
- `value`, plus `ly_value`, `change` and `pct` for the `*_ly` pair;
- `good`, from the measure's `higher_is_better`;
- `display` (`format_indian`), `compact` (`compact_indian`) and `words` (`change_words`).

The loop history sent to the LLM holds facts only, never raw rows: the 10 largest and 10 smallest rows by change for
each step, with the total row count.

## 5. The investigation loop (`app/reports/sales/loop.py`)

### 5.1 Protocol

Each turn the LLM receives:
- the shared persona and the lens goal;
- the model's roles from discovery;
- the steps run so far, each with its `why` and its compact facts;
- the budget left.

It returns:

```text
LoopTurn { action: "query", step: LensStep } | { action: "done", note? }
```

### 5.2 Guardrails (safety only, not judgement)

| Rule | Behaviour |
|---|---|
| Budget | `SALES_REPORT_QUERIES_PER_TABLE` queries per (model, lens), including the seed. When it is used up, the table gets `cut_short: true` |
| Invalid step | One re-ask listing the problems. A second invalid step uses one query from the budget |
| Duplicate step | Refused and counted as invalid |
| Query fails after retries | The step's facts are empty and the loop continues; the error code is logged |
| `MODEL_ACCESS_DENIED` | Stops this model; its tables fail with the generic message |
| Concurrency | `SALES_REPORT_QUERY_CONCURRENCY` queries at once across the whole build (one semaphore) |

### 5.3 Cross-model stock check (`lob_growth`)

For the `SALES_REPORT_ROOT_CAUSE_LOBS` worst degrowing LOBs, the agent may run steps on the **stock model**, either
the one discovery found or the one an admin pinned to `stock_turn`.
- It filters with `in` on the LOB and store **values taken from the sales results**.
- Values with no row in the stock result are listed as "not matched in stock data". Names are never fuzzy-matched
  or guessed.
- Stock figures and sales figures stay in separate facts. They are never added together.

## 6. Tables (`app/reports/sales/tables.py`)

The server builds every table row from facts. The LLM only writes text.

| Lens | Columns | Colours |
|---|---|---|
| `lob_growth` | LOB · value MTD · vs LY % · value FYTD · vs LY % · weight/qty vs LY % (when present) · status (growth/degrowth) | each % by `good` |
| `subcategory_loss` | sub-category · store (optional) · month columns (value, vs LY %) · loss vs LY | loss red, gain green |
| `stock_turn` | LOB or category · store (optional) · stock turn · stock · sales · non-moving flag | turn compared within the table only, no fixed threshold |
| `price_range` | band · sales MTD · vs LY % · sales FYTD · vs LY % · share of sales | by `good` |

**Derived stock turn (D8):** sales ÷ the average of the month-end stock values in the range, × 365 ÷ the days in the
range. The table notes that the server computed it.

Each table gets:
- `findings`: a few lines on what happened and why (the root cause for degrowth);
- `suggestions`: `SuggestionText` with priority, title, where, owner, urgency, reason, notes and evidence;
- `cut_short` and `notes`.

Grounding is the same as Phase 15, with the gap closed: no blanket day or month numbers are allowed. Dates in the
text are already removed by `ungrounded_numbers`.

## 7. Summary bullets and actions (`app/reports/sales/writer.py`)

- **The LLM returns** `bullets: [{text ≤ 240, evidence: [fact id] ≥ 1, emphasis: [phrase] ≤ 2}] ≤ 8`, with the
  most important bullets first.
- **The server checks** each bullet's evidence ids and numbers; a bullet that fails is dropped.
- **The server sets the colour** from the cited facts: every cited fact `good` → green; any cited fact not good →
  red; otherwise neutral. `emphasis` phrases are shown in bold, and only if they appear word for word in the text.
- **The action list** is every table suggestion, sorted by priority (high → low) and urgency (today → this month).
  Each action names its model and table.

## 8. `report.json` schema 3

```text
SummaryReportContent {
  schema_version: 3, kind: "sales", report_id, generated_at, timezone, user_name,
  bullets: [{text, tone: good|bad|neutral, emphasis, evidence}],
  models_used: [{dataset_id, name, lenses, reason}],
  sales_sections: [{dataset_id, name, status, data_date, refreshed_at,
                    tables: [{lens, title, columns, rows, findings, suggestions, cut_short, notes, queries_used}],
                    trend?, inactive}],
  actions: [Suggestion + lens],
  notes
}
```

Schema-2 reports (`kind` missing) still display, using the Phase 15 view, until they age out of the 7-day history.

Built in 16d: `summary` holds the bullets as plain text (the history line is its first bullet); `sections` and
`overview` stay empty so an older visual still opens the file; the facts behind each table are kept in memory for
the bullets and never written to `report.json`. `parse_report` reads either schema.

## 9. PDF (`app/reports/pdf.py`, new `render_sales_pdf`)

1. **Cover:** company, "Daily sales MIS summary", user, generated time, the models used.
2. **Summary bullets:** green or red text with the emphasised phrases in bold. Each bullet also starts with ▲, ▼ or
   •, so the meaning never depends on colour alone.
3. **For each model, the four tables in order.** Each table shows its rows with coloured % cells, then its findings,
   then its suggestions as cards (where / who / by when), plus "analysis cut short" when it applies.
4. **The trend chart and "no activity now"** for each model (existing renderers).
5. **The action list** grouped High / Medium / Low.
6. **Footer** as before.

All text is escaped and cleaned (`_escape` / `clean_text`) as in Phase 15.

## 10. Visual

- `/sales-summary` and `/summary` (trimmed, any case) both start the report.
- Progress labels per stage and lens, e.g. "Stock turn: Sales (3 of 12)".
- `ReportView` renders schema 3: coloured bullets, collapsible models with their four tables, and the action list.
  Schema-2 reports keep the current view.
- The help text mentions `/sales-summary`.

## 11. Settings

| Key | Default | Meaning |
|---|---|---|
| `SALES_REPORT_QUERIES_PER_TABLE` | 8 | Query budget per (model, lens), seed included |
| `SALES_REPORT_ROOT_CAUSE_LOBS` | 3 | How many degrowing LOBs get the root-cause drill-down |
| `SALES_REPORT_QUERY_CONCURRENCY` | 4 | Power BI queries at once per build |

The existing `REPORT_*` settings (timeout, model concurrency, retention, storage and so on) still apply.

## 12. Failure handling

| Case | Result |
|---|---|
| No sales model found | The build fails with "None of your data sources hold sales data the report can use." |
| A lens has no model that can fill it | The report shows that table once, saying no data source has the needed fields |
| A seed query fails | That table fails with "Couldn't load <model> data right now."; the other tables continue |
| Every table of every model fails | The row is marked failed and the user sees an error (as in Phase 15) |
| Budget used up | The table is shown with what was found, marked "analysis cut short" |

## 13. Sub-phases

| Step | Content |
|---|---|
| 16.0 | This design, ADR 0016, plan section, checklist rows S1–S12 |
| 16a | Settings; migration 0005 and the `lens` CLI; discovery (batched LLM plus pins); `/summary` alias; carry-over fixes from the Phase 15 review (grounding gap; a missing file returns 404; `/events` race; `mark_ready` row count; cached query embeddings; merged schema fetches); tests |
| 16b | `LensStep`, validation, the DAX compiler (periods, LY, FY months, bands), facts, the loop with budget and concurrency, `repair=False`, dev-gateway support; tests |
| 16c | Seeds, persona and lens prompts (research principles), cross-model stock check, table builders, findings and suggestions with grounding; tests |
| 16d | Bullets and actions, schema 3, `render_sales_pdf`; tests |
| 16e | Visual: command, progress, schema-3 view, help text; tests |
| 16f | Live check (checklist S1–S12); tune the default budgets from measured query counts |
