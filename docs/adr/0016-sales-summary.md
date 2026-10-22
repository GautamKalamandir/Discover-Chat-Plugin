# 0016. `/sales-summary`: an MIS sales report built by an investigating agent

- Status: Accepted
- Date: 2026-10-15
- Supersedes: the report content of [ADR 0015](0015-summary-reports.md) §3–§7 and amendment 1. Building on request,
  storage, PDF download, retention, the deletion job and the no-re-check exception (§1, §2, §8–§14) stay as they are.
- Design: [docs/design/phase-16-sales-summary.md](../design/phase-16-sales-summary.md)

## Context

The `/summary` report (ADR 0015) builds the same KPI-change view for every model the user can access. MIS
executives found it generic and hard to act on. They want a **sales** report they can read at first sight:

- **summary bullets**, red where something needs attention and green where it is better;
- **four tables**, each with findings and suggestions:
  1. **LOB growth and degrowth**, with the root cause and a fix for each LOB that degrew;
  2. **sub-category loss**, by store and by month;
  3. **stock turn**;
  4. **sales by price range**, including what isn't selling and why;
- **one prioritised list of actions**.

The rules from earlier work still apply:
- **Nothing is tailored to one model or dashboard.** Users have anywhere from 1 to 40 models with different names.
- **No word lists and no thresholds.** The agent judges; the server verifies (ADR 0014).
- **Every number in the text must match a computed figure** (ADR 0008).
- **Every query runs as the user**, so G1–G3, OLS and RLS apply.

## Decision

Agreed with the product owner one question at a time on 2026-10-15.

1. **Command.** `/sales-summary` replaces `/summary`, and `/summary` stays as an alias. Each report is built when the
   user asks, with the user's own token. There is still no creation cron.
2. **Finding the models.**
   - The agent reads each allowed model's schema and descriptions. It decides which models hold sales data and which
     of the four tables each model can fill.
   - An admin can pin a model to a table, or exclude it: `python -m app.jobs.registry lens …`.
3. **Scope.** Models that aren't sales models are left out of the report. The report lists the models it used.
4. **Periods.**
   - Latest day with data, month to date (MTD) and financial year to date (FYTD), each **against the same period last
     year (LY)**.
   - The financial year starts in `FISCAL_YEAR_START_MONTH`.
   - The sub-category table also shows a column per month.
5. **Root cause of a degrowing LOB.**
   - For the worst `SALES_REPORT_ROOT_CAUSE_LOBS` LOBs, the agent drills down LOB → sub-category → store against
     last year.
   - When the user has a stock model, it also checks **stock availability** for the same LOBs and stores, matched
     by name.
   - A name that has no match in the stock data is reported as not matched, never guessed.
6. **Price ranges.** The model's own price-band column is used when there is one. Otherwise the agent proposes bands
   from the model's price distribution, and the server builds and runs the queries for them.
7. **Layout.**
   - Summary bullets, then the four tables (each with its findings and suggestions), then the action list.
   - The daily trend chart and "no activity now" stay; the other ADR 0015 sections are dropped.
8. **Stock turn.** The model's own stock-turn measure is used when there is one. Otherwise the server computes sales
   ÷ average stock, annualised, by value or by weight as the model offers.
9. **Many models.** Each model gets its own tables. Figures are **never added across models**, which avoids double
   counting when two models overlap.
10. **The agent investigates in a loop.**
    - It runs a query, reads the result, and chooses the next unit, filter or drill-down, until it decides the
      analysis is complete.
    - It chooses the units itself: value, weight or quantity, whichever the model has.
11. **A query budget per table**, `SALES_REPORT_QUERIES_PER_TABLE`, caps the loop. A table whose budget runs out says
    that its analysis was cut short.
12. **Structured steps, no raw DAX.**
    - The agent describes each query: measures, group-by columns, filters, periods and price bands.
    - The server checks it against the user's visible schema (G2) and builds the DAX.
    - The DAX then runs through the existing read-only validator (G3).
13. **Colours are set by the server.**
    - Each bullet and each table cell takes its colour from the facts it cites, using the measure's direction:
      green when better, red when worse.
    - A bullet or suggestion with an unknown fact id, or a number that matches no computed figure, is dropped.

### What the agent knows about the business (research, 2026-10-15)

The shared persona is a senior MIS executive at a jewellery brand. These points become **principles in the prompt,
not thresholds**:

- **Value against volume.** Rupee sales can grow only because gold prices rose, while weight and quantity stay flat.
  The agent compares value, weight and quantity before calling something growth.
- **Same-store against total growth.** New stores can hide weak existing stores.
- **Product mix.** The mix of studded and plain jewellery affects margin. When gold is expensive, customers move to
  lighter, lower-karat pieces.
- **Stock.** Stock turn and stock ageing are key jewellery metrics. Ageing is usually shown in buckets of 0–90,
  91–180, 181–365 and 365+ days.
- **Usual remedies:**
  - move slow stock to stores that sell it;
  - targeted markdowns or offer periods;
  - melt and remake very old stock;
  - buy by category and price point.

Sources (trade press and company results, indicative only):
- GJEPC Solitaire, "Same Stock, Smarter Outcomes";
- Indian Jeweller news 16158;
- Jewel360, "Inventory aging reports";
- Prismanote, "Optimize your inventory rotation";
- Deloitte, "Analytics in the jewellery industry";
- Titan Q2 FY26 coverage (Outlook Business, Business Standard);
- Lalithaa Jewellery Mart IPO analysis.

## Why

- **Generic tables, specific evidence.** The four tables are generic "lenses". Any model whose schema has the needed
  columns can fill them, so nothing depends on one dashboard's names (agent autonomy, ADR 0014).
- **A loop instead of fixed queries.** A drop can only be explained by asking follow-up questions, and the right next
  question depends on what the last one showed. The budget keeps Power BI load and build time predictable.
- **Last year as the comparison.** Jewellery sales are seasonal (Akshaya Tritiya, Diwali, weddings), so the same
  period last year is the fair baseline.
- **Structured steps.** They reuse the validators that already protect chat, so the loop gains no new way to reach
  data the user can't see.

## Consequences

- **Load.** With the default budget of 8 queries per table, one sales model can use up to about 32 queries plus the
  root-cause checks. 10 sales models can reach about 320, while Power BI allows about 120 a minute per user.
  `SALES_REPORT_QUERY_CONCURRENCY` and the existing retry on throttling (429) spread the load. The defaults are tuned
  in the live check (16f).
- **Build time.** Several minutes for users with many sales models. It still fits inside the 30-minute limit and the
  user's cached Power BI token, which lasts about an hour.
- **`report.json` schema 3** (`kind: "sales"`). Reports at schema 2 in the 7-day history still display.
- **Migration 0005** adds `semantic_models.report_lenses` for admin pins.
- **A stock model is optional.** Without one, the root cause can't name stock availability, and the stock-turn table
  says no stock data was found.
- **Server-built DAX is not sent to the LLM repair loop.** If one of these queries fails, the step fails and its
  budget is used.
- **New settings:** `SALES_REPORT_QUERIES_PER_TABLE` (8), `SALES_REPORT_ROOT_CAUSE_LOBS` (3),
  `SALES_REPORT_QUERY_CONCURRENCY` (4).
