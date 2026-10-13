# 0014. Agent-led clarification with options, and value resolution from evidence

- Status: Accepted
- Date: 2026-10-13

## Context

Live test: *"let me know all sales of kismaba till now"* answered *"No data was found for kismaba sales…"*.
- The planner filtered `Location Name = "kismaba"`. Value resolution only accepted a stored value **equal ignoring
  case**, so ValueSearch's `STR - KOSAMBA` was discarded and the query returned no rows. The correct spelling
  "kosamba" failed the same way, because stored names carry a `STR - ` prefix.
- Typo recovery (ADR 0013 §5) only ran when the planner flagged a word as unresolved.
- A clarification was plain text. The user typed a reply and the LLM merged it with the earlier question; nothing
  tied the reply to what was asked.
- "till now" was silently read as this financial year. Fixed word meanings in prompts can't cover such phrases.

## Decision

**The agent decides; the server verifies.** The LLM decides whether anything is ambiguous, and whether to proceed,
ask a question with options, or say it can't answer. No word lists and no score thresholds decide for it. The
server guarantees that every option is executable and uses only models, objects and values the user may see.

1. **Value evidence before running a plan.**
   - Every string filter value (`=`, `in`, `<>`) and every unresolved word is looked up **as the user**: Fabric IQ
     ValueSearch scoped to the filter's column, then model-wide for words not found there.
   - If ValueSearch is unavailable, the column's distinct values are read with a bounded DAX query (as the user,
     cached briefly per user) and ranked locally. (D5)
   - Values that exist exactly (ignoring case) are switched to their stored spelling with no extra LLM call.
   - Otherwise the evidence (closest stored values with match scores, or "nothing found") goes back to the LLM,
     which decides once per turn.
2. **Clarification with options.**
   - `QueryPlan.options`: each option is a complete runnable plan with a short label.
   - The server validates every option like any plan (G2 and the user's visible schema), and every filter value
     in it must exist in the user's data. Invalid options are dropped; with none left the planner gets one re-plan,
     and then the question is asked as plain text.
   - A forbidden model in an option still denies the whole request (scenario 7).
   - The pending question, its options and their validated plans are stored on the clarification message, with
     opaque server-generated ids.
3. **A click runs exactly the validated option.** `POST /chat/stream` accepts `choice {clarification_id,
   option_id}`. Only an option of the latest clarification in the same conversation is accepted. G2 and schema
   validation run again, then the query runs without another planning call. A typed reply still works: the planner
   sees the question and the option labels and says whether the message answers it.
4. **Running periods** (ADR 0013 §3) are asked with two options, like-for-like and full period.
5. **Buttons in the visual** (D2), plus "None of these", which hands over to typing.
6. **Assumptions** (D7): the planner may record interpretations it made (`assumptions`). The answer mentions them
   when the agent thinks they matter; the server doesn't enforce it.
7. **Loop guard** (D9): at most `AGENT_MAX_CLARIFICATIONS` (3) questions for one question chain. After that the
   agent must proceed or say it can't answer.
8. **Prompts** (D6): the rule "this year / last year mean the financial year" is removed; the prompt keeps only the
   fact of when the financial year starts. The synonym list, the "why" breakdown hints and the greeting shortcut
   stay (D8).

## Consequences

- One extra LLM round trip when a value isn't stored exactly. A click on an option skips planning, so it is faster.
- Options never reveal values the user can't see: every value comes from a search or query run as the user (RLS).
  Q16 and Q18 hold: option values are stored with the conversation (12 h retention) and never logged.
- Older visuals keep working: options are extra fields on the existing `clarification` event.
- New settings: `AGENT_MAX_CLARIFICATIONS`, `AGENT_MAX_OPTIONS`, `VALUE_FALLBACK_MAX_VALUES`, `VALUE_CACHE_MINUTES`.
