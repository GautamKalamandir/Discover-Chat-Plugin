# Phase 2: Spike report

**Tenant:** Kalamandir Jewellers (`0753b5b0-…`). **Model:** KMJL SALES NEW, with 26 tables, 928 columns and 105
measures. **Tested by:** Fena Patel, in Power BI Service (Edge, Developer visual) against the local Docker stack.

Evidence comes from local capture bundles in `backend/spikes/captures/`, which are gitignored. Only the anonymized
schema structure is committed: `backend/tests/fixtures/schemas/captured-fabric-iq-schema.json`.

## Summary

| Spike | Verdict | Evidence (bundle `20261008T073939923265Z`) |
|---|---|---|
| S1 Visual SSO (Service) | ✅ Pass, after two setup fixes (below) | v1 token, `aud` = App ID URI, `scp` = `…_CV_ForPBI`, `appid` = Power BI Service |
| S1 Visual SSO (Desktop) | ⏳ Not run yet | |
| S2 OBO | ✅ Pass, after admin consent | Power BI token (`aud` analysis.windows.net) and Fabric token (`aud` api.fabric.microsoft.com), both with `Dataset.Read.All Item.Execute.All Item.Read.All Workspace.Read.All`. `GET /groups` returns 200. |
| S3 Fabric IQ MCP | ✅ Connects as the user. ⚠️ Three format differences, now fixed. ⏳ Successful ExecuteQuery format not seen yet. | Tools: `ValueSearch, ResolveFabricItem, DiscoverArtifacts, GetReportMetadata, ExecuteQuery, GetSemanticModelSchema` |
| S4 REST fallback | ✅ Probe and error shape as expected | `GET /datasets/{id}` returns 200. `executeQueries` returns 400 `DatasetExecuteQueriesError` with `DetailsMessage`. |
| S5 Visual context | ⏳ Context fields not tested yet | Origin, host and saved-object reports captured |
| S6 Streaming | ✅ Pass | 10 events at 13 → 2718 ms (incremental). Request `Origin: null`, as assumed for CORS. |
| Non-Build / RLS users | ⏳ Not run yet | |

## Findings and the changes they caused

### Setup (S1/S2)

1. **The Developer visual uses scope `<guid>_DEBUG_CV_ForPBI`.** `pbiviz start` serves the visual as `<guid>_DEBUG`,
   so Power BI requests that scope. Without it, Entra issued no token, and the visual showed "sign_in_failed".
   - **Change:** the backend accepts the `_DEBUG` scope outside production only (`app/auth/entra.py`
     `accepted_scopes`, with tests).
   - **Change:** `docs/entra-setup.md` now lists the second scope for dev registrations.
2. **The portal's scope form is limited to 40 characters.** The 57-character scope name must be set through the
   Manifest.
3. **Admin consent produces no request or notification.** OBO failed with `AADSTS65001` until a Global Admin clicked
   *Grant admin consent*. The chatbot's silent OBO never shows Microsoft's consent screen, so the admin consent
   workflow is never triggered.
4. **`AppSource Custom Visuals SSO` must be on.** Without it, `acquireAADTokenstatus` returns `DisabledByAdmin`.

### Visual (found live)

5. **The Format-pane model dropdown stayed empty.** The model list arrives after Power BI builds the Format pane.
   - **Change:** call `host.refreshHostData()` once whenever the list changes (`visual/src/visual.ts`, with test).
6. **The Send button did nothing.** Power BI's sandboxed frame blocks `<form>` submission.
   - **Change:** Send is now a plain button. A regression test checks that the visual contains no `<form>` or submit
     button.

### Fabric IQ formats (S3)

7. **GetSemanticModelSchema** returns the schema as **JSON text**:
   `{"schema": {"Tables": [{"Name", "Columns": [{"Name", "Type", "FormatString"?, "VariationColumns"?}],
   "Measures": [{"Name", "Type", "FormatString"?}]}], "ActiveRelationships": [{"PK", "FK", "UnidirectionalFilter"}],
   "InactiveRelationships": […]}, "semanticModel": {…}}`.
   - The **structured content** only carries an `artifact_citation` (model name and link). Our gateway read that and
     found 0 objects.
   - The real payload has no descriptions, hidden flags, custom instructions or verified answers. Column types are
     `Text`, `Integer`, `Double`, `DateTime` and `Boolean`.
   - **Change:** take the payload from the JSON text and ignore citation-only structured content
     (`app/powerbi/fabric_iq.py`). The normalizer already matched the real field names.
8. **A failed DAX query is not a tool error.** It returns `is_error: false` with the text
   `DAX query syntax error: DAX query execution failed: Query (1, 18) …`. Our code treated that as an unrecognised
   format, so the agent's repair step never ran.
   - **Change:** detect the prefix and raise `DaxQueryError`, which feeds the repair loop.
9. **ValueSearch** returns `{"Results": {"<term>": [{"Table", "Column", "Value", "Score"}]}}`, where `Column` is the
   bare column name. Our matcher expected `Table[Column]` and never matched.
   - **Change:** qualify the column as `'Table'[Column]` (`app/agent/values.py`).
10. **The tool list includes more than documented:** `ResolveFabricItem`, `DiscoverArtifacts` and
    `GetReportMetadata`. They're not used yet.
11. **Successful ExecuteQuery** (bundle `20261008T074831800740Z`) returns
    `{"executionResult": {"tables": [{"columns": [{"name", "type"}], "rows": [[v1, v2, …]]}]}, "semanticModel": {…}}`.
    The rows are **positional lists**, not objects. Our parser didn't recognise this, so every live question fell back
    to REST (about 8 s slower).
    - **Change:** parse the positional rows.
    - **Change:** map Fabric's column names onto the REST spelling (`Table[Column]`, `[Alias]`), using the query's
      own aliases.
    - **Change:** the capture now keeps result column names (metadata) to confirm the exact spelling. Row values stay
      masked.
13. **The access probe now treats HTTP 400 as denied.** Power BI rejects ids that aren't dataset GUIDs. Before, the 5
    sample registry entries were re-probed on every request.

### REST (S4)

12. Error details wrap names in `<oii>…</oii>` markers.
    - **Change:** strip them before the text reaches the repair loop (`app/powerbi/rest.py`).

## Open

- Exact Fabric result column spelling (the next diagnostics run keeps the names).
- S1 in Power BI Desktop. S5 with Context fields and slicers.
- Non-Build and RLS users (S3/S4 access rules).
- **Large models:** `GetSemanticModelSchema` says it returns "an overview if the model is large" and supports JMESPath
  `queries`. KMJL returned the full schema (62 KB); larger models may need the overview path.
