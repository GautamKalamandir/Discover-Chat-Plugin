"""Semantic-index administration (ADR 0007).

Usage (from backend/):
    # Admin sync from Power BI, signed in as yourself (device-code login in this terminal).
    # Needs ADMIN_CLI_CLIENT_ID (docs/entra-setup.md §4). Indexes only what you can see.
    uv run python -m app.jobs.metadata sync --dataset-id <id>      (or --all)

    # Development before IT delivers: index a schema payload from a JSON file.
    uv run python -m app.jobs.metadata load-fixture --dataset-id sales-ds --file schema.json

    uv run python -m app.jobs.metadata status
"""

import argparse
import asyncio
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from sqlalchemy import func, select

from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.db.models import EmbeddingSpace, SemanticDocument, SemanticModel
from app.db.repositories.registry import RegistryRepository
from app.db.session import create_engine, create_sessionmaker
from app.embeddings.factory import create_embedding_provider
from app.powerbi.fabric_iq import FabricIqMcpGateway
from app.semantic.indexer import SemanticIndexer
from app.semantic.sync import MetadataSync

TokenAcquirer = Callable[[Settings], str]


def acquire_admin_token(settings: Settings) -> str:
    """Device-code sign-in for the Fabric scope via the admin CLI app registration."""
    import msal

    if not settings.admin_cli_client_id or not settings.entra_allowed_tenant_ids:
        raise SystemExit("Set ADMIN_CLI_CLIENT_ID and ENTRA_ALLOWED_TENANT_IDS first.")
    app = msal.PublicClientApplication(
        settings.admin_cli_client_id,
        authority=f"{settings.entra_authority_host.rstrip('/')}/{settings.entra_allowed_tenant_ids[0]}",
    )
    flow = app.initiate_device_flow(scopes=[settings.fabric_iq_token_scope])
    if "user_code" not in flow:
        raise SystemExit(f"Could not start sign-in: {flow.get('error_description', flow)}")
    print(flow["message"], flush=True)
    result: dict[str, Any] = app.acquire_token_by_device_flow(flow)
    if "access_token" not in result:
        raise SystemExit(f"Sign-in failed: {result.get('error_description', result.get('error'))}")
    return str(result["access_token"])


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="app.jobs.metadata", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sync = sub.add_parser("sync", help="Sync from Power BI as the signed-in admin")
    target = sync.add_mutually_exclusive_group(required=True)
    target.add_argument("--dataset-id")
    target.add_argument("--all", action="store_true", help="All chatbot-enabled models")
    fixture = sub.add_parser("load-fixture", help="Index a schema payload from a JSON file")
    fixture.add_argument("--dataset-id", required=True)
    fixture.add_argument("--file", required=True, type=Path)
    sub.add_parser("status", help="Documents per model and embedding space")
    return parser


async def run(argv: list[str], *, acquire_token: TokenAcquirer = acquire_admin_token) -> int:
    args = _parser().parse_args(argv)
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)
    engine = create_engine(settings)
    sessionmaker = create_sessionmaker(engine)
    try:
        if args.command == "status":
            return await _status(sessionmaker)
        indexer = SemanticIndexer(sessionmaker, create_embedding_provider(settings))
        sync = MetadataSync(indexer, sessionmaker)
        if args.command == "load-fixture":
            payload = json.loads(args.file.read_text(encoding="utf-8"))
            stats = await sync.sync_payload(args.dataset_id, payload)
            if stats is None:
                print(f"error: {args.dataset_id} is not registered (see app.jobs.registry)")
                return 1
            print(f"{args.dataset_id}: {stats.embedded} embedded, {stats.unchanged} unchanged")
            return 0

        token = acquire_token(settings)
        if args.all:
            async with sessionmaker() as session:
                datasets = [
                    m.pbi_dataset_id
                    for m in await RegistryRepository(session).list_chatbot_models()
                ]
        else:
            datasets = [args.dataset_id]
        gateway = FabricIqMcpGateway(settings)
        failures = 0
        for dataset_id in datasets:
            try:
                payload = (await gateway.get_schema(token, dataset_id)).data
                stats = await sync.sync_payload(dataset_id, payload)
            except Exception as exc:
                failures += 1
                print(f"{dataset_id}: FAILED ({type(exc).__name__}: {exc})")
                continue
            if stats is None:
                failures += 1
                print(f"{dataset_id}: not registered")
            else:
                print(f"{dataset_id}: {stats.embedded} embedded, {stats.unchanged} unchanged")
        await gateway.aclose()
        return 1 if failures else 0
    finally:
        await engine.dispose()


async def _status(sessionmaker: Any) -> int:
    async with sessionmaker() as session:
        rows = (
            await session.execute(
                select(
                    SemanticModel.pbi_dataset_id,
                    EmbeddingSpace.provider,
                    EmbeddingSpace.model,
                    func.count(SemanticDocument.id),
                    func.max(SemanticDocument.last_seen_at),
                )
                .join(SemanticDocument, SemanticDocument.semantic_model_id == SemanticModel.id)
                .join(EmbeddingSpace, EmbeddingSpace.id == SemanticDocument.embedding_space_id)
                .group_by(
                    SemanticModel.pbi_dataset_id, EmbeddingSpace.provider, EmbeddingSpace.model
                )
                .order_by(SemanticModel.pbi_dataset_id)
            )
        ).all()
    if not rows:
        print("no documents indexed yet")
    for dataset_id, provider, model, count, last_seen in rows:
        print(
            f"{dataset_id}  {provider}/{model}  {count} docs  last seen {last_seen:%Y-%m-%d %H:%M}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run(sys.argv[1:])))
