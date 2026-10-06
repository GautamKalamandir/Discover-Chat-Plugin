# 0005. Authorization gates, cache and denials (Q15, Q15b, Q16)

- Status: Accepted
- Date: 2026-10-06
- Design: [docs/design/phase-5-authorization.md](../design/phase-5-authorization.md)

## Decision

1. **Check before moving further, at three gates.** A global HTTP middleware can't know which models a chat question
   needs, because the agent decides that after reading it. So the check runs at three points:
   - **G1:** a per-route FastAPI dependency resolves the user's allowed models.
   - **G2:** before the handler or agent, the requested models must be in that set.
   - **G3:** every agent tool re-checks its model.

   Power BI, queried with the user's own token, is the final gate.
2. **Source of truth = Power BI**, asked with `GET /v1.0/myorg/datasets/{id}` as the user. A failed check counts as
   not allowed (fail closed).
3. **Cache:** allowed decisions for **10 min** (Q15), denied for **2 min** (Q15b), in `user_model_access`, shared
   across backend instances. A cached denial for a model the user explicitly asks about is re-checked live first. A
   Power BI rejection of a real query revokes immediately.
4. **Generic denials (Q16):** one message, never naming the model. Unknown, disabled and forbidden models are
   indistinguishable. A cross-model request with any denied model is denied as a whole. Details go only to the audit
   log.

## Consequences

- New grants work immediately when asked for, and appear in the model list within 2 minutes.
- Revocations take effect at the latest on the next real query, or within 10 minutes for the list.
- Each cache miss costs one Power BI call per registered model (parallel, capped by `AUTHZ_PROBE_CONCURRENCY`).
