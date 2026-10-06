"""Builds searchable documents from a normalized schema. Never includes data values.

Each document lists the model objects it mentions in `referenced_objects`; retrieval keeps it only
if the requesting user can see all of them (OLS). Documents deliberately mention one object each,
so hiding one column never hides unrelated information.
"""

import hashlib
from dataclasses import dataclass, field
from typing import Any

from app.semantic.normalizer import NormalizedSchema

MAX_INSTRUCTION_CHUNK = 800


@dataclass(frozen=True)
class SemanticDoc:
    doc_key: str
    doc_type: str
    source: str
    title: str
    content: str
    referenced_objects: tuple[str, ...] = ()
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def content_hash(self) -> str:
        text = "\x1f".join([self.title, self.content, *self.referenced_objects])
        return hashlib.sha256(text.encode()).hexdigest()


def build_documents(
    schema: NormalizedSchema, *, model_name: str, domain: str | None, description: str | None
) -> list[SemanticDoc]:
    docs = [
        SemanticDoc(
            doc_key="model_summary",
            doc_type="model_summary",
            source="registry",
            title=f"{model_name} model",
            # Registry fields only: listing tables here would bypass the OLS intersection.
            content=". ".join(
                p for p in (model_name, domain and f"Domain: {domain}", description) if p
            ),
        )
    ]
    for obj in schema.objects:
        if obj.is_hidden:
            continue  # technical objects; still visible for intersection, just not advertised
        if obj.kind == "table":
            title, content = obj.name, f"Table {obj.name}"
        elif obj.kind == "column":
            title = f"{obj.table}[{obj.name}]"
            kind = f" ({obj.data_type})" if obj.data_type else ""
            content = f"Column {obj.name}{kind} in table {obj.table}"
        else:
            title = f"[{obj.name}]"
            content = f"Measure {obj.name} in table {obj.table}"
        if obj.description:
            content = f"{content}: {obj.description}"
        extra = {"expression": obj.expression} if obj.expression else {}
        docs.append(
            SemanticDoc(
                doc_key=obj.key,
                doc_type=obj.kind,
                source="schema",
                title=title,
                content=content,
                referenced_objects=(obj.key,),
                extra=extra,
            )
        )

    for index, chunk in enumerate(_chunks(schema.ai_instructions)):
        docs.append(
            SemanticDoc(
                doc_key=f"ai_instruction:{index}",
                doc_type="ai_instruction",
                source="prep_for_ai",
                title="AI instructions",
                content=chunk,
            )
        )
    for index, answer in enumerate(schema.verified_answers):
        docs.append(
            SemanticDoc(
                doc_key=f"verified_answer:{index}:{answer.title[:80]}",
                doc_type="verified_answer",
                source="prep_for_ai",
                title=answer.title or "Verified answer",
                content="; ".join((answer.title, *answer.phrases)).strip("; "),
                referenced_objects=answer.fields,
            )
        )
    return docs


def _chunks(texts: tuple[str, ...]) -> list[str]:
    chunks: list[str] = []
    for text in texts:
        current = ""
        for paragraph in (p.strip() for p in text.split("\n")):
            if not paragraph:
                continue
            if current and len(current) + len(paragraph) + 1 > MAX_INSTRUCTION_CHUNK:
                chunks.append(current)
                current = ""
            current = f"{current}\n{paragraph}".strip()
        if current:
            chunks.append(current)
    return chunks
