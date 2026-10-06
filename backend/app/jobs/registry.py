"""Manage the semantic-model registry (only registered + enabled models can ever be used).

Usage (from backend/):
    uv run python -m app.jobs.registry add --dataset-id <id> --workspace-id <id> \\
        --workspace-name "Sales WS" --name "Sales" --domain Sales [--description "..."] [--enable]
    uv run python -m app.jobs.registry list
    uv run python -m app.jobs.registry enable  --dataset-id <id>
    uv run python -m app.jobs.registry disable --dataset-id <id>
"""

import argparse
import asyncio
import sys

from app.core.config import get_settings
from app.db.repositories.registry import RegistryRepository
from app.db.session import create_engine, create_sessionmaker


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="app.jobs.registry", description="Model registry")
    sub = parser.add_subparsers(dest="command", required=True)

    add = sub.add_parser("add", help="Register or update a semantic model")
    add.add_argument("--dataset-id", required=True)
    add.add_argument("--workspace-id", required=True)
    add.add_argument("--workspace-name", required=True)
    add.add_argument("--name", required=True)
    add.add_argument("--domain")
    add.add_argument("--description")
    add.add_argument("--tenant-id", help="Defaults to the first ENTRA_ALLOWED_TENANT_IDS entry")
    add.add_argument("--enable", action="store_true", help="Make it available to the chatbot")

    sub.add_parser("list", help="List registered models")
    for name in ("enable", "disable"):
        toggle = sub.add_parser(name, help=f"{name.capitalize()} a model for the chatbot")
        toggle.add_argument("--dataset-id", required=True)
    return parser


async def run(argv: list[str]) -> int:
    args = _parser().parse_args(argv)
    settings = get_settings()
    engine = create_engine(settings)
    try:
        async with create_sessionmaker(engine)() as session, session.begin():
            repo = RegistryRepository(session)
            if args.command == "add":
                tenant_id = args.tenant_id or next(iter(settings.entra_allowed_tenant_ids), None)
                if not tenant_id:
                    print("error: --tenant-id is required (no ENTRA_ALLOWED_TENANT_IDS set)")
                    return 2
                workspace = await repo.upsert_workspace(
                    pbi_workspace_id=args.workspace_id,
                    entra_tenant_id=tenant_id,
                    name=args.workspace_name,
                )
                model = await repo.upsert_semantic_model(
                    pbi_dataset_id=args.dataset_id,
                    workspace=workspace,
                    name=args.name,
                    domain=args.domain,
                    description=args.description,
                    chatbot_enabled=args.enable,
                )
                state = "enabled" if model.chatbot_enabled else "disabled"
                print(f"registered {model.pbi_dataset_id} ({model.name}), {state}")
            elif args.command == "list":
                for model in await repo.list_all_models():
                    state = "enabled " if model.chatbot_enabled else "disabled"
                    domain = model.domain or "-"
                    print(f"{state}  {model.pbi_dataset_id}  {model.name}  [{domain}]")
            else:
                enabled = args.command == "enable"
                if not await repo.set_model_enabled(args.dataset_id, enabled):
                    print(f"error: model {args.dataset_id} is not registered")
                    return 1
                print(f"{args.command}d {args.dataset_id}")
    finally:
        await engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run(sys.argv[1:])))
