# 0013. Conversational replies, "why did it change" analysis, grouped cross-model comparison

- Status: Accepted
- Date: 2026-10-08

## Context

Live testing showed two gaps:
- **"hi", "what can I ask?" and "what access do I have?"** received the generic refusal.
- **"Why" questions** (for example "why is gold selling low in 2026-27 compared to 2025-26?") could only compare two
  totals, without showing where the change came from.

Comparisons across two models only worked for single totals.

## Decision

1. **Conversational replies are written by the server, not the LLM.**
   - Greetings and thanks are recognised before any planning: no LLM call, no Power BI call.
   - The planner can return `status: "help"` with a topic: `greeting`, `capabilities`, `data_sources` or
     `out_of_scope`.
   - The reply is built only from the user's own allowed models (G1) and their visible schema (OLS):
     - the model names;
     - what the chatbot can do;
     - three example questions from the user's own measures and columns.
   - A user with no models is told that no data sources are available yet.
   - This keeps Q16: nothing in a reply depends on models outside the user's `AuthorizedContext`. A data question
     whose data is missing still gets the generic denial.
2. **Explain-change analysis (`QueryPlan.change`).**
   - The LLM chooses only:
     - a general measure;
     - the filters;
     - a baseline and a current period;
     - up to 3 breakdowns ("drivers"). The planner picks these; the user can ask for others as a follow-up.
   - The server:
     - validates all of these against the user's schema (G2 on the model);
     - builds one query for the totals and one per breakdown, each with both periods side by side
       (`CALCULATE` per period);
     - computes the total change, and for each item its change and **share of the total change**;
     - lists the biggest decreases and increases.
   - The answer must be grounded in those numbers. It says the data shows *where* the change happened, not outside
     causes.
3. **A period that is still running is never compared silently.** The chatbot asks each time: the same dates in both
   periods (like-for-like), or the full earlier period against the running one so far. The reply to the
   clarification is merged with the original question, with the pending plan stored on the clarification message.
4. **Grouped cross-model comparison.**
   - Two steps (from the same model or different models), each grouped by one column, are matched on the group
     values. The match ignores case and surrounding spaces.
   - Example: sales by store against target by store.
   - Every model must pass G2, as before.

5. **Misspelt values are looked up in the data** (added after live testing, 2026-10-08).
   - When the planner can't place words ("surar", "sivler"), Fabric IQ **ValueSearch** looks them up, as the user,
     in the models already in context.
   - The planner then re-plans once with the matches ("surar" → `'NET SALES MASTER'[Location Name] = "SURAT"`).
6. **"Not found" names the user's own words** (decision 2026-10-08).
   - If words still can't be placed, the reply is: *I couldn't find "salaries" in the data available to you. Check
     the spelling or try another word.*
   - Only words that appear in the user's own question are echoed, so the text never carries LLM-invented terms.
   - The reply is identical whether or not a restricted model has that data, so Q16 still holds.
   - Model-access denials keep the generic message.

## Consequences

- **Clarifications now carry their context.** Replies to any clarification, not only period questions, are merged
  with the original question. Previously the planner saw only the reply.
- **A why-question uses up to `AGENT_MAX_QUERY_STEPS` queries:** 1 for the totals plus up to 3 breakdowns.
- **Tables shown for change analysis** get `[Change]` and `[Change %]` columns.
- **Cross-model matching is by value text.** Models that spell a dimension differently ("HYD" vs "Hyderabad") won't
  match. Mapping tables are future work if needed.
