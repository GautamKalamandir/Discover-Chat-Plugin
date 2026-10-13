"""Manage the semantic-model registry (only registered + enabled models can ever be used).

Usage (from backend/):
    uv run python -m app.jobs.registry add --dataset-id <id> --workspace-id <id> \\
        --workspace-name "Sales WS" --name "Sales" --domain Sales [--description "..."] [--enable]
    uv run python -m app.jobs.registry list
    uv run python -m app.jobs.registry enable  --dataset-id <id>
    uv run python -m app.jobs.registry disable --dataset-id <id>

    # KPIs always included in that model's /summary report (ADR 0015), if the user can see them:
    uv run python -m app.jobs.registry kpi add --dataset-id <id> \\
        --measure "Sales[Total Net Sales]" [--label "Net sales"]
    uv run python -m app.jobs.registry kpi list --dataset-id <id>
    uv run python -m app.jobs.registry kpi remove --dataset-id <id> \\
        --measure "Sales[Total Net Sales]"
"""

import argparse
import asyncio
import sys

from app.agent.models import parse_ref
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

    kpi = sub.add_parser("kpi", help="KPIs pinned for a model's /summary report")
    kpi_sub = kpi.add_subparsers(dest="kpi_command", required=True)
    for name, text in (("add", "Pin a measure"), ("list", "List pins"), ("remove", "Unpin")):
        action = kpi_sub.add_parser(name, help=text)
        action.add_argument("--dataset-id", required=True)
        if name != "list":
            action.add_argument("--measure", required=True, help="Table[Measure], as in the model")
        if name == "add":
            action.add_argument("--label", help="Name shown in the report (default: measure)")
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
            elif args.command == "kpi":
                return await _kpi(repo, args)
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


async def _kpi(repo: RegistryRepository, args: argparse.Namespace) -> int:
    model = await repo.get_model_by_pbi_id(args.dataset_id)
    if model is None:
        print(f"error: model {args.dataset_id} is not registered")
        return 1
    if args.kpi_command == "add":
        if parse_ref(args.measure) is None:
            print("error: --measure must look like Table[Measure]")
            return 2
        label = args.label or (parse_ref(args.measure) or ("", args.measure))[1]
        await repo.add_kpi_pin(model, measure=args.measure, label=label, created_by="cli")
        print(f"pinned {args.measure} ({label}) for {model.name}")
    elif args.kpi_command == "remove":
        if not await repo.remove_kpi_pin(model, args.measure):
            print(f"error: {args.measure} is not pinned for {model.name}")
            return 1
        print(f"unpinned {args.measure} for {model.name}")
    else:
        pins = await repo.list_kpi_pins(model.id)
        for pin in pins:
            print(f"{pin.maps_to}  ({pin.term})")
        if not pins:
            print(f"no KPIs pinned for {model.name}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run(sys.argv[1:])))
