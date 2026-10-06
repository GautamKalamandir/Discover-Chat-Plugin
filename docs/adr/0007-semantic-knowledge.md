# 0007. Semantic knowledge layer (Q10a, Q10b, Q10c)

- Status: Accepted
- Date: 2026-10-06
- Design: [docs/design/phase-7-semantic-knowledge.md](../design/phase-7-semantic-knowledge.md)

## Decision

1. **Source (Q10a):** everything comes from the Power BI semantic model itself: tables, columns, measures,
   descriptions, and "Prep data for AI" (AI instructions, verified answers). There is no CSV/Excel import for now; we
   review real models first.
2. **The index is a recall aid, never an authority.** Every search is filtered by the user's allowed models in SQL
   before ranking. Every result referencing model objects must also appear in the user's own live schema (OLS applied
   by Power BI), and those results are dropped if that schema is unavailable. No data values are ever stored.
3. **Sync (Q10b):**
   - **on use:** when a user's schema is fetched and its version is new, a background sync runs;
   - **admin CLI:** device-code sign-in through a separate public-client app registration;
   - **dev fixture loader.**

   Unchanged documents are not re-embedded. Documents unseen for 7 days are purged.
4. **Embeddings (Q10c):** fastembed `BAAI/bge-small-en-v1.5` (384 dims, ONNX, no PyTorch) or OpenAI
   `text-embedding-3-small` via `.env`. Each provider/model is its own *embedding space* with its own HNSW index, so
   switching needs no migration.
5. **Ranking:** vector similarity + Postgres full-text (for acronyms) fused with Reciprocal Rank Fusion, with small
   boosts for verified answers and exact object names.

## Why

- A shared index can't match every user's view, so security comes from per-request filtering rather than from what
  the index contains. This is also what makes it safe for any user's access to trigger a sync.
- The model's own metadata is maintained by its authors, so it stays current without a second system.

## Open (spike S3)

- The real `GetSemanticModelSchema` payload shape. The normalizer accepts likely variants, and the fixtures are a
  best guess to be replaced by captured payloads.
