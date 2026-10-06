# 0006. Power BI integration layer

- Status: Accepted
- Date: 2026-10-06

## Decision

1. **One entry point.** `PowerBIService` is the only code that reaches Power BI. It applies gate G3, a read-only DAX
   check, per-user tokens, fallback, retries and error mapping.
2. **Gateways:**
   - **Fabric IQ MCP** is primary, using the official `mcp` SDK over Streamable HTTP. Tool contract version is pinned
     with `X-Variants`, and the tools are checked once via `tools/list`.
   - **Execute Queries REST** is the fallback.

   Both are selected in `.env` (ADR 0002).
3. **Token audiences:** Fabric IQ uses `https://api.fabric.microsoft.com/.default`, as advertised by its OAuth
   protected-resource metadata. REST and the access probe use `https://analysis.windows.net/powerbi/api/.default`.
   Both come from the same Entra resource (Power BI Service) through On-Behalf-Of.
4. **Failure handling:**

| Failure | Behaviour |
|---|---|
| Endpoint refuses / is down / contract mismatch | Try the fallback gateway. Never treated as the user losing access |
| Tool says the user is unauthorized | Re-check live. If gone: revoke and generic denial. If still allowed: next gateway, or on REST a "needs Build permission" message |
| Throttled / timeout | Retry up to `POWERBI_MAX_RETRIES` with backoff (Retry-After honoured, ≤ 10 s), then 429 / 504 |
| DAX error | No fallback. The error text goes to the agent's repair loop (Phase 8); the user sees a generic message |

5. **Limits:** `maxRows` defaults to 250 and is capped at 1,000 (Fabric IQ maximum). REST is held under 120
   queries/min/user per instance.

## Why

- A single entry point means no future code can query Power BI without passing G3.
- Separating "endpoint unavailable" from "user denied" prevents wrongly revoking a user's access when, for example,
  a tenant setting is off or a region is unsupported.

## Open (spike S3)

- The real output shapes and error wording of the Fabric IQ tools.
- End-to-end acceptance of the Fabric-scope OBO token.
