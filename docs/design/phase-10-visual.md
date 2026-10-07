# Phase 10 — Power BI custom visual (`.pbiviz`): implementation plan

Status: **IMPLEMENTED 2026-10-07** (see plan Phase 10 for the delivery notes). Decision applied: Q14 = **React + TypeScript**.

## 1. Goal

Build the chatbot inside the report. It must:
- sign the user in silently with their Power BI identity (Q1, SSO Authentication API);
- let the report author pick the model (Q7);
- send the report's filter context with each question (Q7);
- stream answers from `POST /api/v1/chat/stream` (Phase 9);
- restore the conversation when Power BI re-renders the visual, and offer "New chat" (Q17).

Everything must pass the AppSource certification rules: no `innerHTML` / `eval`, network only to declared origins,
and the ESLint `powerbi-visuals` rules.

## 2. Screens and states

```text
[header]  Discover Chat Bot · signed in as <name> · [New chat]
[body]    messages: question bubbles / answer bubbles (safe markdown subset) / tables / clarifications /
          notices / status line ("Finding data… Planning… Querying…")
[footer]  textarea (Enter = send, Shift+Enter = newline) · [Send] / [Stop]
```

| State | What the user sees |
|---|---|
| `acquireAADTokenstatus` = NotSupported (Teams, Embedded, RS Service) | "The chatbot isn't available in this view of Power BI." |
| DisabledByAdmin | "Your Power BI administrator hasn't enabled sign-in for custom visuals." |
| NotDeclared / no token / backend 401 after one refresh | "We couldn't sign you in. Please reload the report." |
| No model chosen (author) | Landing hint: "Choose a data source in the Format pane → Data source." |
| Model not available to this viewer (404) | "This data source isn't available to you." (Q16 generic) |
| Streaming | Status line + incremental answer text; **Stop** aborts the fetch, so the backend cancels the turn |
| `error` event / 409 / 429 / 503 | The backend's user-safe message in a notice bubble |

## 3. Components (`visual/src/`)

```text
config.ts               backend base URL + auth mode (generated per build: dev / prod)
auth/tokenProvider.ts   EntraTokenProvider: acquireAADTokenstatus → acquireAADToken, refresh 2 min before
                        expiresOn, one forced refresh on 401
                        DevTokenProvider (dev builds only): gets a token from the backend's local-only
                        dev endpoint (§5)
api/client.ts           JSON calls (session, models, chat sessions) with bearer token + retry-once on 401
api/sse.ts              fetch() + ReadableStream reader → parses SSE frames
                        (EventSource can't send Authorization)
chat/store.ts           pure state machine: messages, streaming turn, errors (unit-tested)
context/reportFilters.ts dataView "Context fields" → [{column:"Table[Column]", values:[…≤50]}]
storage.ts              conversation id per user + model in host.storageV2Service (LocalStorage privilege);
                        falls back to in-memory when the admin hasn't allowed it
ui/*.tsx               React 18 components (App, Header, MessageList, Message, ResultTable, Composer):
                        aria-live answer region, keyboard, high-contrast + theme colours from
                        host.colorPalette. **No dangerouslySetInnerHTML anywhere.**
ui/markdown.ts          safe subset parsed to an AST (paragraphs, bullet lists, **bold**, _italic_), rendered as
                        React elements, so no HTML is ever interpreted
settings.ts             Format pane: Data source card (model dropdown filled from
                        /api/v1/models/accessible), Appearance card (title)
visual.ts               wiring: update() → settings/context; lifecycle; renderingStarted/Finished
```

## 4. capabilities.json

- **Privileges:**
  - `WebAccess` with the backend origin (essential);
  - `AADAuthentication {COM: <App ID URI>}`;
  - `LocalStorage` (non-essential).
- **Data role** `contextFields` (Grouping, optional, up to 5 fields, table mapping). Slicers and filters apply to it,
  so its distinct values describe the current selection. Only columns with ≤ 50 values are sent; more means
  "effectively unfiltered" and is skipped.
- **Objects:** `dataSource.modelId` (enumeration filled dynamically) and `appearance.title`.
- **Placeholders** until IT delivers: backend origin and App ID URI come from the generated `config.ts` and
  `capabilities.json`, written by `npm run configure -- --api https://… --app-id-uri https://…`.

## 5. Development before IT delivers

With no Entra app registration the SSO API returns no token. For local development only:
- **Backend:** `POST /api/v1/dev/token {"oid": "user-a"}`, mounted **only** when `AUTH_PROVIDER=dev` and
  `ENVIRONMENT=local|test`. It mints the same HS256 dev token as `scripts/mint_dev_token.py`.
- **Visual:** `npm run start:dev` builds with `authMode: "dev"` and a dev-user field in the Format pane.
  Production builds don't contain the dev provider at all, because the config is generated at build time.
- **HTTPS:** Power BI Service is HTTPS, so the dev backend must be HTTPS too (uvicorn with a local certificate,
  documented in the README). Otherwise the browser blocks the calls as mixed content.

## 6. Tests

- Vitest + jsdom (+ React Testing Library for the components) on the pure modules:
  - SSE parser (chunk boundaries, CRLF, comments/pings, multi-line data);
  - store transitions (stream → answer/table/clarification/error/done; Stop; retry after 401);
  - report-filter mapping (`Table.Column` queryNames → `Table[Column]`, value caps);
  - markdown renderer (no HTML injection: `<img onerror>` stays text);
  - token provider (status handling, refresh timing);
  - `storage` fallback.
- A view smoke test in jsdom: render a full turn from recorded SSE.
- Lint with the `powerbi-visuals` ESLint rules, and `pbiviz package`, which runs the certification checks.
- Backend: tests for the dev token endpoint (absent outside local/dev auth).

## 7. Verification

1. `npm test`, `npx eslint .`, `npx pbiviz package` all green. Backend suite green.
2. **Manual (needs your Power BI):** `pbiviz start` + Power BI Service *Developer visual* against the local HTTPS
   backend in dev mode. Ask a question, see streaming, Stop, New chat, switch page and come back (restore).
3. **Real SSO:** once IT delivers (spike S1), switch `npm run configure` to the real App ID URI and backend URL.

## 8. Decision

- **Q14: React 18 + TypeScript.** State lives in a pure reducer (`chat/store.ts`) used with `useReducer`, so it is
  unit-tested without the DOM. `visual.ts` mounts one React root and passes settings and report context as props on
  each `update()`.
