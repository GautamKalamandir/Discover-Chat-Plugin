"""Mint a local-development bearer token for AUTH_PROVIDER=dev.

Usage (from backend/):
    uv run python -m scripts.mint_dev_token --oid user-a --tid dev-tenant --name "User A"
"""

import argparse

from app.auth.dev import mint_dev_token
from app.core.config import AuthProviderName, get_settings


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--oid", required=True, help="User object id")
    parser.add_argument("--tid", default="dev-tenant", help="Tenant id")
    parser.add_argument("--upn", default=None)
    parser.add_argument("--name", default=None)
    parser.add_argument("--minutes", type=int, default=60)
    args = parser.parse_args()

    settings = get_settings()
    if settings.auth_provider is not AuthProviderName.DEV or not settings.dev_auth_secret:
        raise SystemExit("Set AUTH_PROVIDER=dev and DEV_AUTH_SECRET in .env first.")
    print(
        mint_dev_token(
            settings, args.oid, tid=args.tid, name=args.name, upn=args.upn, minutes=args.minutes
        )
    )


if __name__ == "__main__":
    main()
