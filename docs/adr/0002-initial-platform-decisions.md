# 0002. Initial platform decisions (Q1–Q4, Q8)

- Status: Accepted
- Date: 2026-10-06

## Q1. Identity and distribution: Power BI login = chatbot login

**Decision:** The visual uses the Power BI custom-visual **Authentication API (SSO)**. It is distributed through
**AppSource** for production and runs as a debug visual during development. There is no separate chatbot login. The
chatbot shows the signed-in Power BI user, and switching account means switching the Power BI account.

**Why:** The goal is that the chatbot identity can never differ from the Power BI identity. Only the Authentication API
returns the token of the user signed into Power BI. Microsoft supports it only for AppSource and debug visuals. A
private visual would need its own sign-in, and the backend couldn't detect a mismatch.

**Consequences:** Multitenant Entra app with a verified custom-domain App ID URI. A backend tenant allow-list. AppSource
certification work in Phase 13. Teams and Power BI Embedded are unsupported by the API.

## Q2. LLM provider: Groq first, then OpenAI, switched via `.env`

**Decision:** An `LLMProvider` interface plus a factory, with the provider selected only by `LLM_PROVIDER` (`groq` |
`openai`) and related `.env` keys. Open-source models via Groq are evaluated first.

## Q2b. Embeddings: local open-source and OpenAI, switched via `.env`

**Decision:** An `EmbeddingProvider` interface plus a factory: `EMBEDDING_PROVIDER` = `local` (sentence-transformers)
or `openai`. Groq offers no embeddings. Changing provider or model requires re-indexing, because dimensions differ.

## Q3. Power BI query path

**Decision:** A `PowerBIGateway` interface with two implementations. **Fabric IQ MCP is primary** (GA, delegated-only,
RLS/OLS enforced, no Build permission needed). **Execute Queries REST is the fallback.** Both are selected via `.env`.
Both always run with the user's On-Behalf-Of token.

## Q4. Vector store

**Decision:** **pgvector** in the same PostgreSQL as the registry and authorization tables.

## Q8. Tooling

**Decision:** uv + Python 3.12 for the backend. Node LTS + powerbi-visuals-tools for the visual. A local git
repository, with the remote still to be decided.
