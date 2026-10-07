# 0009. Chat API: SSE streaming, conversations, turn protection (Q17)

- Status: Accepted
- Date: 2026-10-07
- Design: [docs/design/phase-9-chat-api.md](../design/phase-9-chat-api.md)

## Decision

1. **Server-Sent Events over `POST /api/v1/chat/stream`** (sse-starlette). The visual reads it with `fetch()` +
   a stream reader, because `EventSource` can't send the `Authorization` header.
   - Events: `session` (first), `status`, `table`, `token`, `clarification`, `error`, `done` (last).
   - Each event has an increasing id, and a heartbeat ping is sent.
2. **Reject before streaming.** Sign-in, primary-model authorization (generic 404), input validation, the busy
   conversation check (409) and per-user limits (429) are normal JSON errors. Only problems during the turn become
   `error` events.
3. **Conversations (Q17):** one per visual, plus "New chat", restore after re-render, and delete. All are
   owner-only, with the same 404 for missing, expired or someone else's conversation (scenario 22). Restored
   conversations show text only, because result rows are never stored (Q9c).
4. **Turn protection:**
   - one turn per conversation;
   - `CHAT_QUESTIONS_PER_MINUTE` (20) and `CHAT_MAX_CONCURRENT_TURNS` (2) per user;
   - `CHAT_TURN_TIMEOUT_SECONDS` (120).

   On a timeout or client disconnect the agent task is cancelled (in-flight LLM and Power BI calls stop) and no
   assistant message is stored. These limits are per instance; slots expire on their own.

## Consequences

- Closing the visual mid-answer costs nothing more on the backend.
- With several backend instances the per-user limits are per instance until a shared store is added (Phase 13).
