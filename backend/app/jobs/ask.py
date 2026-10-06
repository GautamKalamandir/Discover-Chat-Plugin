"""Ask the agent a question from the terminal (development; the chat API is Phase 9).

Requires AUTH_PROVIDER=dev. Typical local setup in .env:
    POWERBI_GATEWAY=dev_synthetic          (fake rows, labelled as development data)
    DEV_MODEL_ACCESS={"user-a": ["sales-ds", "hr-ds"]}
    GROQ_API_KEY=...                       (real LLM)
and the dev fixtures indexed with app.jobs.metadata load-fixture.

Usage (from backend/):
    uv run python -m app.jobs.ask --user user-a --model sales-ds "What are GOLD sales this FY?"
    uv run python -m app.jobs.ask --user user-a --session <id printed above> "and last year?"
"""

import argparse
import asyncio
import json
import sys
import uuid

from app.agent.events import (
    ClarificationEvent,
    DoneEvent,
    ErrorEvent,
    StatusEvent,
    TableEvent,
    TokenEvent,
)
from app.auth.models import AuthenticatedUser, RequestContext
from app.core.config import AuthProviderName, get_settings
from app.core.errors import AppError
from app.db.repositories.chat import ChatRepository
from app.db.repositories.users import UserRepository
from app.main import create_app


async def run(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="app.jobs.ask", description=__doc__)
    parser.add_argument("question")
    parser.add_argument("--user", required=True, help="Dev user object id (see DEV_MODEL_ACCESS)")
    parser.add_argument("--model", help="Primary semantic model (the visual's Format-pane choice)")
    parser.add_argument("--session", type=uuid.UUID, help="Continue an earlier conversation")
    parser.add_argument("--json", action="store_true", help="Print raw events as JSON lines")
    args = parser.parse_args(argv)

    settings = get_settings()
    if settings.auth_provider is not AuthProviderName.DEV:
        print("app.jobs.ask is for local development: set AUTH_PROVIDER=dev")
        return 2
    app = create_app(settings)
    state = app.state
    try:
        identity = AuthenticatedUser(args.user, "dev-tenant", None, args.user, frozenset(), None)
        authz = await state.authz_service.build_context(
            RequestContext(identity, "cli", access_token="")
        )
        if args.model:
            authz = await state.authz_service.assert_allowed(authz, [args.model])
        async with state.db_sessionmaker() as session, session.begin():
            user = await UserRepository(session).upsert_from_identity(identity)
            chats = ChatRepository(session, retention_hours=settings.conversation_retention_hours)
            chat = (
                await chats.get_owned_session(args.session, user)
                if args.session
                else await chats.create_session(user, report_hint="cli")
            )
        print(f"session {chat.id}  (allowed models: {', '.join(sorted(authz.allowed)) or 'none'})")

        async for event in state.agent.run(authz, chat, args.question, primary_model_id=args.model):
            if args.json:
                print(json.dumps(vars(event), default=str))
            elif isinstance(event, StatusEvent):
                print(f"  ... {event.stage}", flush=True)
            elif isinstance(event, TableEvent):
                print(f"\n[{event.title}]  {', '.join(event.columns)}")
                for row in event.rows[:20]:
                    print("   ", " | ".join(str(v) for v in row.values()))
            elif isinstance(event, TokenEvent):
                print(event.text, end="", flush=True)
            elif isinstance(event, ClarificationEvent):
                print(f"\n? {event.question}")
            elif isinstance(event, ErrorEvent):
                print(f"\n! {event.message}  ({event.code})")
            elif isinstance(event, DoneEvent):
                print("\n")
        return 0
    except AppError as exc:
        print(f"! {exc.message}  ({exc.code})")
        return 1
    finally:
        await state.llm.aclose()
        await state.metadata_sync.aclose()
        await state.powerbi_service.aclose()
        await state.authz_service.aclose()
        await state.db_engine.dispose()


if __name__ == "__main__":
    sys.exit(asyncio.run(run(sys.argv[1:])))
