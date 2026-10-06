"""Mint a local-development bearer token for AUTH_PROVIDER=dev.

Usage (from backend/):
    uv run python -m scripts.mint_dev_token --oid user-a --tid dev-tenant --name "User A"
"""

import argparse
import time

import jwt

from app.auth.dev import DEV_AUDIENCE, DEV_ISSUER
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

    now = int(time.time())
    claims = {
        "iss": DEV_ISSUER,
        "aud": DEV_AUDIENCE,
        "iat": now,
        "nbf": now,
        "exp": now + args.minutes * 60,
        "oid": args.oid,
        "tid": args.tid,
        "upn": args.upn or f"{args.oid}@dev.local",
        "name": args.name or args.oid,
    }
    print(jwt.encode(claims, settings.dev_auth_secret.get_secret_value(), algorithm="HS256"))


if __name__ == "__main__":
    main()
