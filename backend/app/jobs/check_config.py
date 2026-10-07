"""Preflight for live mode (Phase 2): is everything IT delivered configured and reachable?

Usage (from backend/):
    uv run python -m app.jobs.check_config            # --offline skips network checks

Read-only. Never prints secret values (only "present"). Exit code 1 if any check FAILs.
"""

import argparse
import asyncio
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import httpx
from pydantic import SecretStr
from sqlalchemy import text

from app.core.config import AuthProviderName, Settings, get_settings
from app.db.session import create_engine

GUID = re.compile(r"^[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$")
REPO = Path(__file__).resolve().parents[3]


@dataclass
class Check:
    name: str
    status: str  # PASS | WARN | FAIL
    note: str = ""


def _visual() -> tuple[dict[str, object], str, str]:
    """(capabilities, config.ts text, visual guid) — empty values if the visual isn't present."""
    try:
        caps = json.loads((REPO / "visual" / "capabilities.json").read_text(encoding="utf-8"))
        config = (REPO / "visual" / "src" / "config.ts").read_text(encoding="utf-8")
        guid = json.loads((REPO / "visual" / "pbiviz.json").read_text(encoding="utf-8"))["visual"][
            "guid"
        ]
        return caps, config, guid
    except (OSError, KeyError, ValueError):
        return {}, "", ""


def _present(secret: SecretStr | None) -> bool:
    return secret is not None and bool(secret.get_secret_value().strip())


def static_checks(settings: Settings) -> list[Check]:
    checks: list[Check] = []

    def add(name: str, ok: bool, note: str = "", warn: bool = False) -> None:
        checks.append(Check(name, "PASS" if ok else ("WARN" if warn else "FAIL"), note))

    add(
        "AUTH_PROVIDER is entra",
        settings.auth_provider is AuthProviderName.ENTRA,
        f"is {settings.auth_provider}",
    )
    add(
        "ENVIRONMENT is not prod (diagnostics)",
        settings.environment.value != "prod",
        f"is {settings.environment}",
    )
    add("ENTRA_CLIENT_ID is a GUID", bool(GUID.match(settings.entra_client_id or "")))
    uri = settings.entra_app_id_uri or ""
    add(
        "ENTRA_APP_ID_URI is https and not onmicrosoft.com",
        uri.startswith("https://") and "onmicrosoft.com" not in uri,
        uri or "missing",
    )
    tenants = settings.entra_allowed_tenant_ids
    add(
        "ENTRA_ALLOWED_TENANT_IDS are GUIDs",
        bool(tenants) and all(GUID.match(t) for t in tenants),
        f"{len(tenants)} tenant(s)",
    )
    caps, config_ts, guid = _visual()
    scope = settings.entra_required_scope or ""
    add(
        "ENTRA_REQUIRED_SCOPE is <visual guid>_CV_ForPBI",
        bool(guid) and scope == f"{guid}_CV_ForPBI",
        scope or "missing",
    )
    cert = settings.entra_client_certificate_path
    has_cert = bool(cert and settings.entra_client_certificate_thumbprint and Path(cert).is_file())
    add(
        "OBO credential present (certificate or secret)",
        has_cert or _present(settings.entra_client_secret),
        "certificate"
        if has_cert
        else ("secret" if _present(settings.entra_client_secret) else "missing"),
    )
    add(
        "GROQ_API_KEY present (LLM)",
        _present(settings.groq_api_key) or settings.llm_provider.value != "groq",
        "present" if _present(settings.groq_api_key) else "missing",
        warn=True,
    )
    add("DIAGNOSTICS_ENABLED (needed for the spikes)", settings.diagnostics_enabled, warn=True)
    add(
        "POWERBI_GATEWAY is fabric_iq_mcp",
        settings.powerbi_gateway.value == "fabric_iq_mcp",
        f"is {settings.powerbi_gateway}",
        warn=True,
    )

    # The visual must ask Entra for exactly our App ID URI and be allowed to call this backend.
    declared = caps.get("privileges", [])
    privileges = {p.get("name"): p for p in declared} if isinstance(declared, list) else {}
    aad = privileges.get("AADAuthentication", {}).get("parameters", {})
    add(
        "visual AADAuthentication COM == ENTRA_APP_ID_URI",
        isinstance(aad, dict) and aad.get("COM", "").rstrip("/") == uri.rstrip("/"),
        f"visual: {aad.get('COM') if isinstance(aad, dict) else None}",
    )
    match = re.search(r'apiBaseUrl:\s*"([^"]+)"', config_ts)
    api = match.group(1) if match else None
    web = privileges.get("WebAccess", {}).get("parameters", [])
    add(
        "visual apiBaseUrl is allowed by WebAccess",
        api is not None and api in web,
        f"apiBaseUrl={api}",
    )
    add("visual authMode is entra", 'authMode: "entra"' in config_ts, warn=True)
    return checks


async def network_checks(settings: Settings) -> list[Check]:
    checks: list[Check] = []
    async with httpx.AsyncClient(timeout=15) as http:
        try:
            jwks = await http.get(settings.entra_jwks_url)
            keys = len(jwks.json().get("keys", []))
            checks.append(
                Check("Entra signing keys reachable", "PASS" if keys else "FAIL", f"{keys} keys")
            )
        except httpx.HTTPError as exc:
            checks.append(Check("Entra signing keys reachable", "FAIL", type(exc).__name__))
        for tenant in settings.entra_allowed_tenant_ids:
            authority = settings.entra_authority_host.rstrip("/")
            url = f"{authority}/{tenant}/v2.0/.well-known/openid-configuration"
            try:
                found = (await http.get(url)).status_code == 200
                checks.append(Check(f"tenant {tenant[:8]}... exists", "PASS" if found else "FAIL"))
            except httpx.HTTPError as exc:
                checks.append(Check(f"tenant {tenant[:8]}... exists", "FAIL", type(exc).__name__))
        try:
            status = (await http.post(settings.fabric_iq_mcp_url, json={})).status_code
            checks.append(
                Check(
                    "Fabric IQ endpoint reachable (expects 401 unauthenticated)",
                    "PASS" if status == 401 else "WARN",
                    f"HTTP {status}",
                )
            )
        except httpx.HTTPError as exc:
            checks.append(Check("Fabric IQ endpoint reachable", "FAIL", type(exc).__name__))
        if settings.groq_api_key and _present(settings.groq_api_key):
            try:
                status = (
                    await http.get(
                        f"{settings.groq_base_url.rstrip('/')}/models",
                        headers={
                            "Authorization": f"Bearer {settings.groq_api_key.get_secret_value()}"
                        },
                    )
                ).status_code
                checks.append(
                    Check(
                        "GROQ_API_KEY accepted by Groq",
                        "PASS" if status == 200 else "FAIL",
                        f"HTTP {status}",
                    )
                )
            except httpx.HTTPError as exc:
                checks.append(Check("GROQ_API_KEY accepted by Groq", "FAIL", type(exc).__name__))
    checks.append(await _credential_check(settings))
    engine = create_engine(settings)
    try:
        async with engine.connect() as conn:
            version = await conn.scalar(text("SELECT version_num FROM alembic_version"))
            enabled = await conn.scalar(
                text("SELECT count(*) FROM semantic_models WHERE chatbot_enabled")
            )
        checks.append(Check("database reachable + migrated", "PASS", f"revision {version}"))
        checks.append(
            Check(
                "models registered for the chatbot",
                "PASS" if enabled else "WARN",
                f"{enabled} enabled (app.jobs.registry add ...)",
            )
        )
    except Exception as exc:
        checks.append(Check("database reachable + migrated", "FAIL", type(exc).__name__))
    finally:
        await engine.dispose()
    return checks


async def _credential_check(settings: Settings) -> Check:
    """Entra accepts the backend's credential (client-credentials token, discarded)."""
    if not settings.entra_client_id or not settings.entra_allowed_tenant_ids:
        return Check("OBO credential accepted by Entra", "FAIL", "client id / tenant missing")
    from app.auth.token_broker import _build_msal_client  # same construction as OBO

    try:
        client = _build_msal_client(settings, settings.entra_allowed_tenant_ids[0])
        result = await asyncio.to_thread(
            client.acquire_token_for_client,  # type: ignore[attr-defined]
            scopes=[settings.powerbi_scope],
        )
    except Exception as exc:
        return Check("OBO credential accepted by Entra", "FAIL", type(exc).__name__)
    if "access_token" in result:
        return Check("OBO credential accepted by Entra", "PASS")
    return Check(
        "OBO credential accepted by Entra",
        "FAIL",
        f"error={result.get('error')} codes={result.get('error_codes')}",
    )


def report(checks: list[Check]) -> int:
    width = max(len(c.name) for c in checks)
    for check in checks:
        print(f"[{check.status}] {check.name.ljust(width)}  {check.note}")
    failed = sum(c.status == "FAIL" for c in checks)
    warned = sum(c.status == "WARN" for c in checks)
    print(f"\n{len(checks) - failed - warned} passed, {warned} warnings, {failed} failed")
    return 1 if failed else 0


async def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="app.jobs.check_config", description=__doc__)
    parser.add_argument("--offline", action="store_true", help="Skip network/database checks")
    args = parser.parse_args(argv)
    settings = get_settings()
    checks = static_checks(settings)
    if not args.offline:
        checks += await network_checks(settings)
    return report(checks)


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
