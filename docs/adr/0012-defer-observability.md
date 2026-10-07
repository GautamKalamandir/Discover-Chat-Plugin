# 0012. Defer observability, evaluation and load testing (Q11, Q19)

- Status: Accepted
- Date: 2026-10-07

## Decision

- **Phase 12 is deferred.** No OpenTelemetry tracing/metrics, no telemetry exporter or platform, no golden-question
  evaluation set and no load test for now (Q19). The plan stays in
  [docs/design/phase-12-observability-eval.md](../design/phase-12-observability-eval.md) for when it is needed.
- **Sizing (Q11):**
  - ≤ 200 users;
  - ~10 concurrent questions at peak;
  - ≤ 50 semantic models;
  - English only.

  Phase 13 sizes the deployment from this.

## Consequences

- **Diagnosis:** the correlation id links an error message to the Q18-safe logs and to the stored conversation.
  There are no dashboards or alerts.
- **Answer quality:** not measured continuously. The Phase 8 pipeline tests and the Phase 11 red-team suite remain
  the regression net.
- **Latency and LLM cost:** not tracked. The LLM provider's own usage console is the source for cost.
- **Revisit:** before a wider rollout, or when a hosting platform with built-in monitoring is chosen (Phase 13).
