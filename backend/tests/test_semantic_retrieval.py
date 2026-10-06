"""Index + retrieval against the real test database (pgvector + full-text).

Users (fixtures): user-a sees everything in Sales; user-b has Customer[CreditLimit] hidden by
object-level security (sales-ds.user-b.json). Neither is allowed the Finance model unless stated.
"""

from dataclasses import dataclass

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.authz.messages import GENERIC_DENIAL
from app.core.config import Settings
from app.core.errors import AppError
from app.retention.cleanup import run_cleanup
from app.semantic.indexer import SemanticIndexer, hnsw_index_name
from app.semantic.retriever import SemanticRetriever
from app.semantic.schema_source import DevFixtureSchemaSource
from app.semantic.sync import MetadataSync
from app.semantic.user_schema import UserSchemaService
from tests.authz_helpers import seed_registry
from tests.semantic_helpers import FIXTURES, HashEmbedder, authz_for, fixture

pytestmark = pytest.mark.integration

SETTINGS = Settings(_env_file=None)


@dataclass
class Env:
    sm: async_sessionmaker[AsyncSession]
    embedder: HashEmbedder
    indexer: SemanticIndexer
    sync: MetadataSync
    retriever: SemanticRetriever


def make_env(
    sm: async_sessionmaker[AsyncSession],
    embedder: HashEmbedder,
    *,
    sync_on_use: bool = False,
    fixture_dir: str = str(FIXTURES),
) -> Env:
    indexer = SemanticIndexer(sm, embedder)
    sync = MetadataSync(indexer, sm)
    user_schemas = UserSchemaService(
        DevFixtureSchemaSource(fixture_dir), sync if sync_on_use else None, SETTINGS
    )
    return Env(sm, embedder, indexer, sync, SemanticRetriever(sm, indexer, user_schemas, SETTINGS))


@pytest.fixture
async def env(committed_sessionmaker: async_sessionmaker[AsyncSession]) -> Env:
    await seed_registry(committed_sessionmaker)
    e = make_env(committed_sessionmaker, HashEmbedder())
    for dataset in ("sales-ds", "hr-ds", "finance-ds"):
        await e.sync.sync_payload(dataset, fixture(dataset))
    return e


def titles(docs: list) -> list[str]:  # type: ignore[type-arg]
    return [d.title for d in docs]


# --- scenario 9: only allowed models are searched -----------------------------------------------


async def test_restricted_model_is_never_returned_even_as_best_match(env: Env) -> None:
    authz = await authz_for(env.sm, "user-a", ["sales-ds", "hr-ds"])

    # Worded exactly like the Finance measure description.
    docs = await env.retriever.search(authz, "credit exposure and budget variance against plan")

    assert docs, "allowed models should still produce results"
    assert {d.dataset_id for d in docs} <= {"sales-ds", "hr-ds"}
    assert "[Budget Variance]" not in titles(docs)


async def test_asking_for_a_restricted_model_explicitly_is_denied(env: Env) -> None:
    authz = await authz_for(env.sm, "user-a", ["sales-ds"])

    with pytest.raises(AppError) as excinfo:
        await env.retriever.search(authz, "budget variance", model_ids=["finance-ds"])

    assert excinfo.value.message == GENERIC_DENIAL


async def test_user_with_no_models_gets_nothing(env: Env) -> None:
    authz = await authz_for(env.sm, "user-z", [])

    assert await env.retriever.search(authz, "sales") == []


# --- scenario 16: object-level security ---------------------------------------------------------


async def test_object_hidden_from_the_user_is_not_returned(env: Env) -> None:
    query = "customer credit limit"
    user_a = await authz_for(env.sm, "user-a", ["sales-ds"])
    user_b = await authz_for(env.sm, "user-b", ["sales-ds"])

    assert "Customer[CreditLimit]" in titles(await env.retriever.search(user_a, query))
    assert "Customer[CreditLimit]" not in titles(await env.retriever.search(user_b, query))


async def test_unavailable_user_schema_drops_object_documents(
    committed_sessionmaker: async_sessionmaker[AsyncSession], env: Env, tmp_path: object
) -> None:
    blind = make_env(committed_sessionmaker, env.embedder, fixture_dir=str(tmp_path))  # no files
    authz = await authz_for(env.sm, "user-a", ["sales-ds"])

    docs = await blind.retriever.search(authz, "revenue line of business LOB", k=50)

    assert docs  # model-level documents (instructions, summary) remain
    assert all(not d.referenced_objects for d in docs)


# --- ranking --------------------------------------------------------------------------------------


async def test_acronym_finds_the_column_via_full_text_and_exact_name(env: Env) -> None:
    authz = await authz_for(env.sm, "user-a", ["sales-ds", "hr-ds"])

    docs = await env.retriever.search(authz, "LOB", k=3)

    assert docs[0].title in {"Product[LOB]", "Sales by LOB"}
    assert "Product[LOB]" in titles(docs)


async def test_business_term_reaches_the_measure_and_the_ai_instruction(env: Env) -> None:
    authz = await authz_for(env.sm, "user-a", ["sales-ds"])

    docs = await env.retriever.search(authz, "what is our revenue", k=5)

    assert "[Total Net Sales]" in titles(docs)
    assert any(d.doc_type == "ai_instruction" and "Revenue means" in d.content for d in docs)


async def test_doc_type_filter(env: Env) -> None:
    authz = await authz_for(env.sm, "user-a", ["sales-ds"])

    docs = await env.retriever.search(authz, "sales", doc_types=["measure"])

    assert docs and {d.doc_type for d in docs} == {"measure"}


async def test_routing_picks_relevant_allowed_models_only(env: Env) -> None:
    authz = await authz_for(env.sm, "user-a", ["sales-ds", "hr-ds"])

    routed = await env.retriever.route_models(authz, "compare net sales with headcount and finance")

    assert {r.model.dataset_id for r in routed} == {"sales-ds", "hr-ds"}


# --- sync behaviour -------------------------------------------------------------------------------


async def test_resync_of_unchanged_schema_embeds_nothing(env: Env) -> None:
    env.embedder.embedded_texts.clear()

    stats = await env.sync.sync_payload("sales-ds", fixture("sales-ds"))

    assert stats is not None and stats.embedded == 0 and stats.unchanged > 0
    assert env.embedder.embedded_texts == []


async def test_only_changed_objects_are_re_embedded(env: Env) -> None:
    payload = fixture("sales-ds")
    payload["tables"][1]["columns"][1]["description"] = "Merchandise category"
    env.embedder.embedded_texts.clear()

    stats = await env.sync.sync_payload("sales-ds", payload)

    assert stats is not None and stats.embedded == 1
    assert "Merchandise category" in env.embedder.embedded_texts[0]


async def test_unregistered_model_is_not_indexed(env: Env) -> None:
    assert await env.sync.sync_payload("not-registered", fixture("hr-ds")) is None


async def test_switching_embedding_model_uses_a_fresh_space(env: Env) -> None:
    other = make_env(env.sm, HashEmbedder(model="hash-32", dims=32))
    await other.sync.sync_payload("hr-ds", fixture("hr-ds"))
    authz = await authz_for(env.sm, "user-a", ["sales-ds", "hr-ds"])

    docs = await other.retriever.search(authz, "net sales headcount", k=20)

    assert {d.dataset_id for d in docs} == {"hr-ds"}  # sales not yet re-embedded in this space
    async with env.sm() as session:
        indexes = set(
            await session.scalars(
                text(
                    "SELECT indexname FROM pg_indexes WHERE indexname LIKE 'ix_semdoc_hnsw_space_%'"
                )
            )
        )
    space_a, space_b = await env.indexer.space(), await other.indexer.space()
    assert {hnsw_index_name(space_a.id), hnsw_index_name(space_b.id)} <= indexes


async def test_on_use_sync_runs_in_background_once(
    committed_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    await seed_registry(committed_sessionmaker)
    e = make_env(committed_sessionmaker, HashEmbedder(), sync_on_use=True)
    authz = await authz_for(committed_sessionmaker, "user-a", ["sales-ds"])

    # Nothing indexed yet; the first retrieval fetches the user's schema and schedules a sync.
    await e.retriever.search(authz, "anything")  # no documents yet -> nothing to intersect
    schema = await e.retriever._user_schemas.visible(authz, "sales-ds")
    await e.sync.wait_idle()

    assert schema is not None
    assert "Product[LOB]" in titles(await e.retriever.search(authz, "LOB"))
    e.embedder.embedded_texts.clear()
    e.sync.schedule("sales-ds", schema)  # same version again
    await e.sync.wait_idle()
    assert e.embedder.embedded_texts == []


async def test_documents_not_seen_for_stale_days_are_purged(env: Env) -> None:
    async with env.sm() as session, session.begin():
        await session.execute(
            text(
                "UPDATE semantic_documents SET last_seen_at = now() - interval '8 days' "
                "WHERE doc_key = 'column:Product[Category]'"
            )
        )
        result = await run_cleanup(session, SETTINGS)

    assert result.semantic_documents == 1
    authz = await authz_for(env.sm, "user-a", ["sales-ds"])
    assert "Product[Category]" not in titles(await env.retriever.search(authz, "category", k=50))
