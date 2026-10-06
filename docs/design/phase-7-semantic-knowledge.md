# Phase 7 — Semantic knowledge layer: implementation plan

Status: **APPROVED and IMPLEMENTED 2026-10-06** (see plan Phase 7 for the delivery notes). Decisions applied: Q10a (Power BI semantic model only), Q10b (sync on use + admin CLI), Q10c (fastembed).

## 1. Goal

Given a user's question, find the business meaning the agent needs: which measure "sales" means, what "LOB" stands
for, which model handles "Finance" questions. This must hold three properties:

- Retrieval only ever covers models the user is **authorized** for (security scenario 9).
- Retrieval never returns objects hidden from the user by **object-level security** (scenario 16).
- **No data values** are stored, because row-level security must stay with Power BI.

## 2. Key design principle: the index is a recall aid, not a source of truth

The vector index is shared by all users, so it can't be trusted to match any one user's view of a model. Every
retrieval is therefore **filtered twice**:

1. **Model filter (scenario 9):** SQL `WHERE semantic_model_id IN (<user's allowed models>)` runs *before* ranking.
   A restricted model's documents are never even scored.
2. **User-schema intersection (scenario 16):** each result that references model objects (tables, columns,
   measures) is kept only if **all** of them appear in the user's own live schema. That schema is fetched with the
   user's token via `PowerBIService.get_schema` (OLS applied by Power BI) and cached per user + model for 10 minutes.
   If the user's schema can't be fetched, object-level results are dropped (fail closed).

So even if the index contains something a particular user can't see, it can't reach that user. This also allows a
simple, safe sync design (§5).

## 3. What gets indexed

| Document type | Source | Example content | Used for |
|---|---|---|---|
| `model_summary` | registry (name, domain, description) + table list | "Sales model: net sales, LOB, products, FY…" | **Cross-model routing** (ADR 0003) |
| `table` | schema | "Product — product master incl. Line of Business" | Planning |
| `column` | schema | "Product[LOB] — Line of Business (GOLD, SILVER…)"* | Planning, filters |
| `measure` | schema (name + description; the DAX expression is stored but not embedded) | "[Total Net Sales] — revenue after returns" | Metric resolution |
| `ai_instruction` | Power BI "Prep data for AI": AI instructions | "Revenue means Net Sales unless stated" | Agent guidance |
| `verified_answer` | Power BI "Prep data for AI": verified answers | trigger phrases → fields used | Highest-priority matches |

**Q10a: everything comes from the Power BI semantic model itself:** structure, descriptions, AI instructions and
verified answers (synonyms and definitions are usually written there by model authors). There is **no CSV import
for now**. We review what real models provide and improve later. The `business_glossary` table stays in place for
that.

\* Example values in descriptions are only those written by the **model author**. Column **data values are never
read or indexed**. Specific values such as "GOLD" are resolved at question time with Fabric IQ `ValueSearch` under
the user's token, where RLS applies.

Each document stores `referenced_objects` (e.g. `["measure:Sales[Total Net Sales]"]`), which the user-schema
intersection checks.

## 4. Storage (migration `0003`)

```text
embedding_spaces      id, provider, model, dimensions, created_at
                      one row per (provider, model); switching EMBEDDING_PROVIDER/MODEL in .env creates a new
                      space and the next sync re-embeds into it (dimensions differ: 384 vs 1536)

semantic_documents    id, semantic_model_id (FK, cascade), embedding_space_id (FK, cascade),
                      doc_key (unique per model+space, e.g. "measure:Sales[Total Net Sales]"),
                      doc_type, source, title, content, content_hash,
                      referenced_objects text[],
                      embedding vector            -- dimension fixed per space by the index below
                      search_tsv tsvector GENERATED ALWAYS AS (to_tsvector('simple', title || ' ' || content)) STORED,
                      last_seen_at, created_at, updated_at

Indexes:  GIN(search_tsv); btree(semantic_model_id, embedding_space_id);
          per space, created on first use:
          HNSW ((embedding::vector(<dims>)) vector_cosine_ops) WHERE embedding_space_id = <id>
```

The existing `model_metadata` table receives the normalized objects. `semantic_models.schema_version` holds a hash
of the normalized schema, and `last_synced_at` is set on each sync. The existing `business_glossary` table holds
curated terms.

## 5. Metadata sync

**Normalizer.** It turns a `GetSemanticModelSchema` payload into objects: tables, columns, measures, relationships,
AI instructions and verified answers. Microsoft doesn't document the exact payload shape, so the normalizer accepts
the plausible field names and is pinned by fixtures, which are replaced with real captured payloads in spike S3.

**Upsert, never blind-delete.** A sync upserts documents by `doc_key`. If `content_hash` is unchanged, nothing is
re-embedded, so sync is cheap. Objects not seen by any sync for `METADATA_STALE_DAYS` (default 7) are purged by the
existing cleanup job. Users never see a removed object in the meantime, because of the intersection (§2).

**Triggers (Q10b = A + B, plus D for development):**

| Trigger | How |
|---|---|
| A. **On use** (recommended default) | The user-schema fetch from §2 happens anyway. If its hash differs from the stored `schema_version`, a background task syncs from that payload. The answer isn't delayed, and nobody needs an extra credential |
| B. Admin CLI | `uv run python -m app.jobs.metadata sync --dataset-id X` (or `--all`), signed in as an admin user (device-code login in the terminal). Uses a **separate small public-client app registration** (`ADMIN_CLI_CLIENT_ID`, optional IT item 7), so the API's own registration stays confidential-only. It only sees and indexes what that admin can see; the §2 intersection still protects every user |
| D. Dev fixture | `uv run python -m app.jobs.metadata load-fixture --dataset-id sales-ds --file schema.json`, so retrieval can be developed before IT delivers |

## 6. Retrieval

```python
class SemanticRetriever:
    async def search(self, authz, query, *, model_ids=None, doc_types=None, k=12) -> list[RetrievedDoc]
    async def route_models(self, authz, query, *, k=3) -> list[RoutedModel]   # model_summary docs only
```

1. **Gate:** `model_ids` must be a subset of `authz.allowed`, else generic denial. `None` means all allowed models.
2. **Hybrid ranking:**
   - vector similarity (top `RETRIEVAL_CANDIDATES`, default 50, through the space's HNSW index);
   - Postgres full-text (same candidate count, good for acronyms like *LOB*, *FY*, *YTD* that embeddings handle
     poorly);
   - exact synonym matches.

   These are merged with **Reciprocal Rank Fusion**, with exact synonyms and verified answers boosted.
3. **User-schema intersection** (§2), then top `k`.
4. Results carry `{doc_type, title, content, model dataset_id, referenced_objects, score}`, which become the agent's
   context in Phase 8.

## 7. Embeddings (factory from ADR 0002, switched in `.env`)

| `EMBEDDING_PROVIDER` | Implementation | Default model | Dims |
|---|---|---|---|
| `local` | **fastembed** (ONNX runtime, Apache-2.0, no PyTorch) *(Q10c)* | `BAAI/bge-small-en-v1.5` | 384 |
| `openai` | OpenAI embeddings API | `text-embedding-3-small` | 1536 |

Embedding runs off the event loop (thread) in batches. The local model downloads once (~130 MB) into
`EMBEDDING_CACHE_DIR`.

## 8. Configuration (`.env`)

| Setting | Default |
|---|---|
| `EMBEDDING_PROVIDER` / `EMBEDDING_MODEL` | `local` / `BAAI/bge-small-en-v1.5` |
| `EMBEDDING_CACHE_DIR` | `.cache/embeddings` |
| `OPENAI_EMBEDDING_MODEL` | `text-embedding-3-small` |
| `RETRIEVAL_TOP_K` / `RETRIEVAL_CANDIDATES` | 12 / 50 |
| `USER_SCHEMA_CACHE_MINUTES` | 10 |
| `METADATA_SYNC_ON_USE` | `true` |
| `METADATA_STALE_DAYS` | 7 |

Invalid values fall back to the defaults, as with the other settings.

## 9. Code layout

```text
app/embeddings/   local.py (fastembed), openai.py, factory registration
app/semantic/
  normalizer.py     schema payload -> SchemaObjects (+ hash)
  documents.py      SchemaObjects + glossary -> SemanticDocument list (doc_key, content, referenced_objects)
  indexer.py        upsert/embed/purge into semantic_documents; HNSW per space
  user_schema.py    per-user visible-object cache (via PowerBIService.get_schema)
  sync.py           on-use background sync + CLI entry points
  retriever.py      SemanticRetriever (gate -> hybrid -> intersection)
app/jobs/metadata.py   CLI: sync (admin device-code login) / load-fixture (dev) / status
alembic 0003      embedding_spaces, semantic_documents
```

## 10. Tests

| Test | Proves |
|---|---|
| Restricted model's document is the closest match but never returned | Scenario 9 |
| Object hidden from the user (absent from their schema) is dropped; free-text docs are kept | Scenario 16 |
| User schema unavailable: object-level docs dropped | Fail closed |
| "LOB" / "FY" find the right docs through full-text even when vectors miss | Hybrid ranking |
| "revenue" finds `[Total Net Sales]` via an AI instruction / description in the model | Term resolution |
| `route_models("compare sales with HR")` returns Sales + HR, and never a model outside the allowed set | Cross-model routing |
| Re-sync with unchanged content: zero embedding calls | Cheap sync |
| Provider switch: new space, re-embedded, old space unused | `.env` switching |
| Stale objects purged after N days; data values never appear in documents | Retention / RLS |
| On-use sync runs in the background and doesn't delay the answer; concurrent triggers sync once | On-use sync |
| Admin CLI sync with a mocked device-code login | Admin sync |
| Both embedding providers (local model tiny fixture / OpenAI mocked) | Factory |

## 11. Not in Phase 7

- CSV/Excel glossary import (Q10a: deferred until we see what real models provide).

- The agent calling `search` / `route_models`: Phase 8.
- An admin HTTP endpoint for re-sync (needs an admin-role decision): later. The CLI covers it now.

## 12. Decisions

| # | Decision |
|---|---|
| Q10a | Business meaning comes from the **Power BI semantic model only** (structure, descriptions, AI instructions, verified answers). No CSV import for now |
| Q10b | **Sync on use** (background, users' own access) + **admin CLI** (device-code login via a separate public-client app registration) |
| Q10c | Local embeddings via **fastembed** (`BAAI/bge-small-en-v1.5`, 384 dims). `openai` stays the alternative in `.env` |
