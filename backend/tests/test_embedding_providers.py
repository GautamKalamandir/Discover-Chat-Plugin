from types import SimpleNamespace
from typing import Any

import pytest

from app.core.config import Settings
from app.embeddings.local import FastEmbedProvider
from app.embeddings.openai import OpenAIEmbeddingProvider


class FakeFastEmbed:
    def __init__(self) -> None:
        self.passages: list[str] = []
        self.queries: list[str] = []

    def passage_embed(self, texts: list[str]) -> Any:
        self.passages.extend(texts)
        return iter([[0.5] * 384 for _ in texts])

    def query_embed(self, text: str) -> Any:
        self.queries.append(text)
        return iter([[0.25] * 384])


async def test_fastembed_provider_loads_lazily_once_and_uses_passage_vs_query() -> None:
    engine = FakeFastEmbed()
    loads: list[tuple[str, str]] = []

    def loader(model: str, cache_dir: str) -> FakeFastEmbed:
        loads.append((model, cache_dir))
        return engine

    provider = FastEmbedProvider(Settings(_env_file=None), loader=loader)
    assert loads == []  # nothing downloaded at construction

    docs = await provider.embed_documents(["a", "b"])
    query = await provider.embed_query("q")

    assert loads == [("BAAI/bge-small-en-v1.5", ".cache/embeddings")]
    assert (len(docs), len(docs[0]), len(query)) == (2, 384, 384)
    assert (engine.passages, engine.queries) == (["a", "b"], ["q"])


class FakeOpenAI:
    def __init__(self) -> None:
        self.batches: list[list[str]] = []
        self.embeddings = SimpleNamespace(create=self._create)

    async def _create(self, model: str, input: list[str]) -> Any:
        self.batches.append(input)
        # Returned out of order on purpose: the provider must sort by index.
        data = [SimpleNamespace(index=i, embedding=[float(i)] * 3) for i in range(len(input))]
        return SimpleNamespace(data=list(reversed(data)))


async def test_openai_provider_batches_and_keeps_order() -> None:
    client = FakeOpenAI()
    provider = OpenAIEmbeddingProvider(Settings(_env_file=None), client=client)

    vectors = await provider.embed_documents([f"t{i}" for i in range(300)])

    assert [len(b) for b in client.batches] == [256, 44]
    assert vectors[0] == [0.0] * 3 and vectors[255] == [255.0] * 3 and vectors[256] == [0.0] * 3


@pytest.mark.network
async def test_real_fastembed_model_produces_meaningful_vectors() -> None:
    """Downloads BAAI/bge-small-en-v1.5 (~70 MB) once. Run with: pytest -m network"""
    provider = FastEmbedProvider(Settings(_env_file=None))

    revenue, invoice_date = await provider.embed_documents(
        ["Measure Total Net Sales: revenue after returns", "Column InvoiceDate: date of invoice"]
    )
    query = await provider.embed_query("what is our revenue")

    def cosine(a: list[float], b: list[float]) -> float:
        return sum(x * y for x, y in zip(a, b, strict=True))

    assert len(query) == 384
    assert cosine(query, revenue) > cosine(query, invoice_date)
