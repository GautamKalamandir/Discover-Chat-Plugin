# 0003. Tenant access (Q6) and semantic-model context (Q7)

- Status: Accepted
- Date: 2026-10-06

## Q6. Tenant and admin access

**Context:** The chatbot runs in the **company work tenant**. The project team doesn't have Entra or Power BI admin
rights.

**Decision:** Company IT provides the app registration, admin consent, tenant settings and custom domain. The request is
[docs/entra-setup.md](../entra-setup.md). Development continues with mocked identity and Power BI responses until IT
delivers. The live-tenant spikes (Phase 2) wait on IT.

## Q7. How the visual knows its semantic model

**Context:** The custom-visual API doesn't expose the report ID or semantic-model ID. A visual sees only the fields bound
to it and their filtered data.

**Decision:** Hybrid.
1. **Primary model, set by the report author** in the visual's Format pane. A dropdown lists only models the author can
   access, served by `GET /api/v1/models/accessible`.
2. **Optional bound fields.** Fields dragged into the visual supply slicer/filter context, which is applied to answers
   and stated in them.
3. **Cross-model questions are in v1 (Q7b).** When a question needs other domains (e.g. "Compare Sales with Finance"),
   the agent routes among the **other models the user is authorized for**, queries each separately (one model per
   `ExecuteQuery`), and combines the results.

**Rules:**
- The Format-pane model is a hint. The backend re-authorizes it for every user and every question.
- In a cross-model request, if **any** required model is not authorized, the **whole** request is denied (no partial
  results).
- Combined answers state which model each figure came from, and its refresh time.

**Consequences:** A model router and a multi-model planner join Phase 8. Model "domain" descriptions in the registry
(Phase 4) and per-model summaries in the vector store (Phase 7) are needed for routing. More evaluation cases
(Phase 12).
