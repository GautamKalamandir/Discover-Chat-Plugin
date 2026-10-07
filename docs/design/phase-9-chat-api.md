# Phase 9 — Chat API & streaming: implementation plan

Status: **APPROVED and IMPLEMENTED 2026-10-07** (see plan Phase 9 for the delivery notes). Decision applied: Q17 (one conversation per visual + New chat).

## 1. Context

Phase 8 delivered the agent as an async stream of `AgentEvent`s (`Agent.run`), currently reachable only through the
dev CLI. Phase 9 exposes it to the Power BI visual over HTTPS:
- authenticated (Phase 3);
- authorized per request (Phase 5, gates G1/G2);
- answers streamed as Server-Sent Events (SSE);
- conversations owned by the user (scenario 22);
- protected against overload;
- work stopped as soon as the user goes away.

## 2. Endpoints

| Method | Path | Gates | Purpose |
|---|---|---|---|
| POST | `/api/v1/chat/stream` | G1 + G2 (primary model) | Ask a question; the response is an SSE stream. Creates a conversation if `session_id` is omitted |
| POST | `/api/v1/chat/sessions` | G1 + G2 | Start a new conversation explicitly ("New chat" button) |
| GET | `/api/v1/chat/sessions/{id}` | G1 + owner | Restore a conversation after the visual re-renders (Power BI re-creates visuals on page switch / resize) |
| DELETE | `/api/v1/chat/sessions/{id}` | G1 + owner | The user deletes their own conversation (messages + query records cascade) |

Every path returns the same `404 session_not_found` for missing, expired or someone else's sessions (existing
`ChatRepository.get_owned_session`, scenario 22).

### `POST /api/v1/chat/stream` request

```json
{
  "session_id": "uuid | null",
  "question": "What are GOLD sales this FY?",
  "primary_model_id": "sales-ds",
  "report_filters": [{"column": "Product[LOB]", "values": ["GOLD"]}]
}
```

Validation, done by pydantic before any work:
- `question` 1–`AGENT_MAX_QUESTION_CHARS`;
- at most 20 report filters, each at most 50 values;
- `primary_model_id` optional.

`primary_model_id` is a hint: it goes through `assert_allowed` (G2), and a model the user can't access returns the
same generic 404 as `/models/{id}`. A new value replaces the session's stored primary model (the Format-pane setting
may change).

**Errors before the stream starts** are normal JSON errors with the uniform envelope: 401/403/404/409/422/429. Once
streaming has started, errors arrive as `error` events.

## 3. SSE wire format

`text/event-stream`. Every event carries an increasing `id:` and a JSON `data:` line.

```text
event: session        data: {"session_id": "...", "correlation_id": "..."}      (always first)
event: status         data: {"stage": "understanding|finding_data|planning|querying|analyzing|answering"}
event: table          data: {"model_id", "title", "columns", "rows", "truncated"}
event: token          data: {"text": "..."}
event: clarification  data: {"question": "..."}
event: error          data: {"code": "...", "message": "<user-safe>"}
event: done           data: {"message_id": "...", "query_ids": [...]}             (always last)
: ping                                                                            (every CHAT_HEARTBEAT_SECONDS)
```

- Response headers: `Cache-Control: no-cache`, `X-Accel-Buffering: no` (disables proxy buffering).
- Values are JSON-encoded with dates/decimals as strings.
- The visual reads the stream with `fetch()` + a stream reader, since `EventSource` can't send the `Authorization`
  header (plan §2.4).
- Implemented with **sse-starlette** (`EventSourceResponse`), which is already installed through the MCP SDK and is
  added as a direct dependency. It provides heartbeats and client-disconnect detection. The existing middleware is
  pure ASGI, so nothing buffers the stream.

## 4. Turn lifecycle and protection

| Concern | Behaviour | Setting (`.env`, fallbacks) |
|---|---|---|
| Client disconnects (closed visual, page change) | The agent task is **cancelled**, which stops in-flight LLM and Power BI calls. The user message stays; no assistant message is stored | — |
| Turn takes too long | Cancelled; `error` event `turn_timeout` ("This is taking too long, try a narrower question") | `CHAT_TURN_TIMEOUT_SECONDS=120` |
| Double submit in the same conversation | `409 turn_in_progress` (per-session lock, per instance) | — |
| One user flooding | `429 too_many_questions` | `CHAT_QUESTIONS_PER_MINUTE=20` |
| Too many parallel answers per user | `429 too_many_parallel_questions` | `CHAT_MAX_CONCURRENT_TURNS=2` |
| Idle connection kept alive | SSE comment ping | `CHAT_HEARTBEAT_SECONDS=15` |

The limits are per backend instance, matching the existing REST rate limiter. A shared (Postgres/Redis) limiter is
revisited in Phase 13 if several instances run.

## 5. History endpoint (`GET /chat/sessions/{id}`)

It returns the last `AGENT_HISTORY_TURNS * 2` messages: `{id, role, content, created_at, kind}`, where `kind` is
`answer | clarification | error`.

**Result tables are not returned.** Raw rows are never stored (Q9c), so a restored conversation shows the text
answers only. Re-asking reproduces a table.

## 6. Code layout

```text
app/api/v1/chat.py          routes, request/response models, pre-stream validation
app/chat/streaming.py       AgentEvent -> SSE (serialization, ids, session/done framing)
app/chat/turns.py           TurnGuard: per-session lock, per-user concurrency + rate limit, timeout,
                            cancellation wrapper around Agent.run
app/core/errors.py          + TURN_IN_PROGRESS, TOO_MANY_QUESTIONS, TURN_TIMEOUT codes
app/db/repositories/chat.py + delete_session, update_primary_model (owner-checked)
```

**Reused as is:**
- `CurrentContext` / `AuthorizedDep` / `require_model_access` (`app/auth`, `app/authz/dependencies.py`);
- `AuthorizationService.assert_allowed`;
- `ChatRepository.get_owned_session/create_session/list_messages`;
- `UserRepository.upsert_from_identity`;
- `app.state.agent` (`Agent.run`);
- the error envelope and correlation-ID middleware.

## 7. Tests

**HTTP, with a scripted LLM + `dev_synthetic` + test DB** (the Phase 8 pipeline fixtures):
- happy path: event order `session → status… → table → token… → done`; the `done` message id matches the stored
  message;
- new session vs continued session; follow-up uses the previous plan;
- 401 without a token;
- forbidden / unknown `primary_model_id` → identical 404;
- another user's `session_id` → 404 (scenario 22);
- 422 for an over-long question / too many filters;
- 409 for a second turn while one is running;
- 429 at the per-minute limit and the concurrency limit;
- timeout → `error` event `turn_timeout`, then `done`;
- disconnect → the agent task is cancelled (a blocking fake agent proves it), and no assistant message is stored;
- history returns the conversation, with no rows; delete removes it and a second delete is 404;
- CORS preflight for `POST /chat/stream` from `Origin: null`;
- SSE serialization of dates/decimals.

## 8. Verification

1. ruff, mypy, pytest and `alembic check` all green.
2. Live in dev mode: run uvicorn, then `curl -N` with a minted dev token. Watch events arrive incrementally,
   including the heartbeat, and `Ctrl+C` mid-turn to see the cancellation in the log.
3. With `GROQ_API_KEY`: the same as a real end-to-end answer.

## 9. Decision

- **Q17: one conversation per visual + "New chat".** The visual keeps the current conversation id, restores it with
  `GET /chat/sessions/{id}` after a re-render, starts fresh with `POST /chat/sessions`, and can delete it. There is
  no list of past conversations.
