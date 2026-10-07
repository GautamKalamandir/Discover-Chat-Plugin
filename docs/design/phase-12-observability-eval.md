# Phase 12 — Observability, evaluation & performance: implementation plan

Status: **DEFERRED (2026-10-07)** by decision. Tracing, metrics, telemetry export, the evaluation set and the load
test are all **not needed for now**. This plan is kept for when they are. Q11 (sizing) is recorded below and used
by Phase 13.

## 1. Goal

Make the running system measurable:
- see where time, cost and errors go for every question (traces + metrics);
- measure answer quality with a scored question set (evaluation);
- prove the backend meets latency targets at the expected load (load test).

All telemetry follows Q18: **ids, codes, counts and timings only, never question/answer text or data values.**

## 2. Workstreams

### A. Tracing (OpenTelemetry)
- One trace per HTTP request, auto-instrumented for FastAPI, SQLAlchemy and httpx.
- Manual spans for each agent stage (`understanding … answering`), every LLM call, every Power BI call, authz
  resolution and retrieval.
- **Span attributes (allow-listed):** correlation id, user key hash, model ids, gateway, status/error codes, row
  counts, token counts, durations.
- A test asserts that no span attribute carries the canary strings used by the Q18 logging test.
- The correlation id is attached to the trace, so a user-reported problem maps to a trace and the stored
  conversation.

### B. Metrics

| Metric | Type | Labels |
|---|---|---|
| `chat_turns_total` | counter | outcome = answer / clarification / denied / error, error_code |
| `chat_turn_duration_seconds` | histogram | outcome |
| `agent_stage_duration_seconds` | histogram | stage |
| `llm_tokens_total` | counter | provider, model, direction = in/out |
| `llm_calls_total` / `llm_call_duration_seconds` | counter / histogram | provider, purpose = plan/repair/answer, result |
| `powerbi_queries_total` / `powerbi_query_duration_seconds` | counter / histogram | gateway, status |
| `authz_decisions_total` | counter | outcome = allow/deny/unverified |
| `authz_cache_lookups_total` | counter | result = hit/miss |
| `chat_rejections_total` | counter | reason = rate_limit / parallel / busy / timeout |
| `answer_grounding_fallbacks_total` | counter | — |
| `http_server_duration` | histogram | route, status (auto) |

- **Estimated LLM cost:** `llm_tokens_total` × a per-model price table set in `.env` (`LLM_PRICE_PER_1M_INPUT`,
  `LLM_PRICE_PER_1M_OUTPUT`).
- **Exporter:** set in `.env` (Q19).

### C. Evaluation (answer quality)
- **Golden set:** `evals/*.json`. Each case has a question, a user, the expected outcome (answer / clarify /
  refuse), and the expected plan essentials: model(s), measure(s), filters, period, top-N.
- **Scorer:**
  - per field: model, measure, filters, period, outcome;
  - refusal correctness (questions about restricted models must be refused);
  - grounding (the answer's numbers come from the results).
- `uv run python -m app.jobs.evaluate --set evals/sales.json` writes a Markdown + JSON report
  (`evals/reports/…`): overall score, per-field accuracy, failures with the plan diff, LLM tokens and cost, p50/p95
  latency.
- **Runs** against the dev fixtures + `dev_synthetic` + the **real LLM** (needs `GROQ_API_KEY`). With real Power
  BI (after S3), cases can add `expected_values` from a reference DAX query, and the scorer then checks the numbers.
- **Seed set:** about 30 questions over the Sales/HR fixtures (direct metrics, filters, FY periods, top-N, follow-ups,
  cross-model, ambiguous, restricted). Replaced or extended with real business questions from your users (see §4).
- A regression gate (`--min-score`) for CI in Phase 13.

### D. Performance / load test
- `scripts/loadtest.py` (asyncio + httpx; no new infrastructure). It drives `POST /chat/stream` with N concurrent
  virtual users and reports throughput, error rate, time-to-first-event and full-turn p50/p95/p99.
- **Backend-overhead mode:** a local-only `LLM_PROVIDER=dev_fake` (fixed plan + fixed answer, refused in prod by the
  Phase 11 guard) plus `dev_synthetic` isolate *our* overhead from LLM and Power BI latency.
- **End-to-end mode:** the same script with the real LLM, at low concurrency (it costs tokens).
- **Latency budget** (to confirm with the Q11 load):

| Stage | Target |
|---|---|
| Auth + authz (cache warm) | p95 < 50 ms |
| Retrieval + context | p95 < 300 ms |
| Backend overhead per turn (fake LLM / fake Power BI) | p95 < 500 ms |
| Time to first event | p95 < 1 s |
| Full answer (real LLM + Power BI) | p95 < 15 s |

### E. Docs
- `docs/operations/observability.md`: what is measured, how to read a trace, how to find a conversation from a
  correlation id.
- `docs/operations/performance.md`: load results vs budget.
- ADR 0012.

## 3. Files

```text
app/observability/    telemetry.py (setup, exporters), metrics.py (instruments), spans.py (helpers +
                      attribute allow-list)
app/llm/fake.py       dev_fake provider (local/test only)
app/jobs/evaluate.py  golden-set runner + scorer + report
evals/                sales.json (seed set), reports/
scripts/loadtest.py
```

The existing agent, LLM, Power BI, authz and chat code gets instrumentation calls only; no behaviour changes.

## 4. Open questions
- **Q11:** expected users, peak concurrent questions, number of semantic models, and languages.
- **Q19:** where should telemetry go?
- Later (not blocking): could your business users give 20–50 real questions with expected answers for the
  golden set?
