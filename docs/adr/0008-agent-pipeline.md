# 0008. Agent: fixed pipeline, grounded answers (Q13, Q13b, Q13c)

- Status: Accepted
- Date: 2026-10-06
- Design: [docs/design/phase-8-agent.md](../design/phase-8-agent.md)

## Decision

1. **Fixed pipeline in plain Python (Q13).** The LLM never calls tools. It produces:
   - a structured `QueryPlan`, validated by pydantic and then by the server;
   - DAX repairs, validated again;
   - the answer wording.

   Everything that touches data runs through server code that has already passed G1–G3.
2. **Server-side checks on everything the LLM produces:**
   - plan models → `assert_allowed`, so the whole request is denied if any is not allowed;
   - plan and DAX objects → must be in the user's own visible schema (OLS);
   - DAX → read-only, one `EVALUATE`;
   - answer numbers → must match computed facts or rows at the precision shown, otherwise a deterministic template
     answer is used.
3. **No partial answers.** If any part of a question isn't covered by the user's allowed models, the reply is the
   one generic message (Q16), never an answer to the covered part only.
4. **Template-first DAX.** `SUMMARIZECOLUMNS` + `TREATAS`/`FILTER` + `TOPN`, with escaping done by code. LLM-written
   DAX only for `custom` steps, and always validated. Repairs on Power BI DAX errors are capped at
   `AGENT_MAX_REPAIRS`.
5. **Model (Q13b):** `openai/gpt-oss-120b` on Groq via its OpenAI-compatible API. OpenAI uses the same code path;
   the provider is switched in `.env`.
6. **Fiscal year (Q13c):** April–March (`FISCAL_YEAR_START_MONTH=4`). Relative periods are turned into explicit
   dates by the server, not the LLM.

## Consequences

- Behaviour is predictable and testable with a scripted LLM, and works with open-source models.
- Questions outside the plan shapes use `custom` DAX. Phase 12 evaluation will show whether more templates are
  needed.
- The answer is checked before it is streamed, so the client receives it in chunks after generation rather than
  token by token.
