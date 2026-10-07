# 0010. Custom visual: React, SSO, build-time configuration (Q14)

- Status: Accepted
- Date: 2026-10-07
- Design: [docs/design/phase-10-visual.md](../design/phase-10-visual.md)

## Decision

1. **React 18 + TypeScript (Q14).**
   - State lives in a pure reducer, so it is unit-tested without a DOM.
   - Answer text is parsed into a small markdown AST and rendered as React elements. There is no
     `innerHTML` / `dangerouslySetInnerHTML`, enforced by the Power BI certification ESLint rules.
2. **Sign-in = Power BI identity (Q1).**
   - Production builds only use `acquireAADTokenService`.
   - Every privilege status (NotSupported, DisabledByAdmin, NotDeclared) has its own user message.
   - Tokens refresh 2 minutes before expiry, and once on a 401.
3. **Backend URL fixed at build time** (`npm run configure`), together with the `WebAccess` and
   `AADAuthentication` privileges. Report authors can't redirect the user's token elsewhere, and non-HTTPS URLs are
   refused.
4. **Local development before IT delivers.**
   - Dev builds get a token from `POST /api/v1/dev/token`, which exists only when `AUTH_PROVIDER=dev` and
     `ENVIRONMENT=local|test`.
   - Production builds are configured with `authMode: "entra"` and never call it.
5. **Context.**
   - The model comes from the Format pane, read from the report objects.
   - Slicer context comes from the optional "Context fields" role (≤ 50 values per field, otherwise treated as
     unfiltered).
   - The conversation id is kept in Power BI local storage when the admin allows it, otherwise in memory.

## Consequences

- The visual works end to end against a local backend before SSO exists.
- Moving to production is a `configure` run, not a code change.
- Real-environment checks (Service, Desktop, Mobile; Teams/Embedded showing "not available") need Power BI with
  Developer mode, and SSO needs IT's app registration (spikes S1/S6).
