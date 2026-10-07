"""Phase 2 spike tooling: diagnostics API, runner, capture bundles, anonymizer, config check."""

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from app.auth.entra import EntraAuthProvider
from app.auth.jwks import StaticKeySource
from app.auth.models import AuthenticatedUser, RequestContext
from app.auth.token_broker import TokenBroker
from app.core.config import Environment, Settings, production_problems
from app.diagnostics.capture import CaptureBundle, mask, mask_text_payload, user_hash
from app.diagnostics.runner import INVALID_DAX, DiagnosticsRunner
from app.jobs import anonymize_capture, check_config
from app.main import create_app
from app.powerbi.fabric_iq import FabricIqMcpGateway
from tests.conftest import KID, TokenFactory
from tests.fake_fabric_iq import FakeFabricIq, running

CANARY = "CANARY-TOKEN-must-never-be-written"
FABRIC_SCOPE = "https://api.fabric.microsoft.com/.default"


# --- routes ---------------------------------------------------------------------------------------


def _app(settings: Settings, signing_key: RSAPrivateKey) -> FastAPI:
    provider = EntraAuthProvider(
        settings, key_source=StaticKeySource({KID: signing_key.public_key()})
    )
    return create_app(settings, auth_provider=provider)


@pytest.fixture
def diag_settings(entra_settings: Settings, tmp_path: Path) -> Settings:
    return entra_settings.model_copy(
        update={"diagnostics_enabled": True, "diagnostics_capture_dir": str(tmp_path)}
    )


@pytest.fixture
async def diag_client(
    diag_settings: Settings, signing_key: RSAPrivateKey
) -> AsyncIterator[AsyncClient]:
    app = _app(diag_settings, signing_key)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def test_diagnostics_routes_are_absent_unless_enabled(
    client: AsyncClient, make_token: TokenFactory
) -> None:
    auth = {"Authorization": f"Bearer {make_token()}"}

    for method, path in [("POST", "/run"), ("GET", "/stream"), ("POST", "/visual")]:
        response = await client.request(method, f"/api/v1/diagnostics{path}", headers=auth, json={})
        assert response.status_code == 404


def test_diagnostics_are_refused_in_production(entra_settings: Settings) -> None:
    prod = entra_settings.model_copy(
        update={"environment": Environment.PROD, "diagnostics_enabled": True}
    )

    assert "DIAGNOSTICS_ENABLED must be false" in production_problems(prod)


async def test_diagnostics_require_a_real_sign_in(diag_client: AsyncClient) -> None:
    for method, path in [("POST", "/run"), ("GET", "/stream"), ("POST", "/visual")]:
        response = await diag_client.request(method, f"/api/v1/diagnostics{path}", json={})
        assert response.status_code == 401


async def test_stream_sends_ten_ticks_then_done(
    diag_client: AsyncClient, make_token: TokenFactory
) -> None:
    async with diag_client.stream(
        "GET",
        "/api/v1/diagnostics/stream",
        headers={"Authorization": f"Bearer {make_token()}"},
    ) as response:
        body = "".join([chunk async for chunk in response.aiter_text()])

    assert body.count("event: tick") == 10
    assert "event: done" in body


async def test_visual_report_is_saved_with_the_origin(
    diag_client: AsyncClient, make_token: TokenFactory, diag_settings: Settings
) -> None:
    response = await diag_client.post(
        "/api/v1/diagnostics/visual",
        headers={"Authorization": f"Bearer {make_token()}", "Origin": "null"},
        json={"report": {"host": "service", "sso": "success"}},
    )

    assert response.status_code == 200
    bundle = Path(diag_settings.diagnostics_capture_dir) / response.json()["bundle_id"]
    saved = json.loads((bundle / "visual_report.json").read_text(encoding="utf-8"))
    assert saved == {"host": "service", "sso": "success", "origin": "null"}


async def test_visual_report_cannot_write_into_another_users_bundle(
    diag_client: AsyncClient, make_token: TokenFactory
) -> None:
    owner = {"Authorization": f"Bearer {make_token()}"}
    other = {"Authorization": f"Bearer {make_token(oid='user-b-oid')}"}
    created = await diag_client.post(
        "/api/v1/diagnostics/visual", headers=owner, json={"report": {}}
    )
    bundle_id = created.json()["bundle_id"]

    stolen = await diag_client.post(
        "/api/v1/diagnostics/visual", headers=other, json={"bundle_id": bundle_id, "report": {}}
    )
    traversal = await diag_client.post(
        "/api/v1/diagnostics/visual", headers=owner, json={"bundle_id": "../x", "report": {}}
    )
    own = await diag_client.post(
        "/api/v1/diagnostics/visual", headers=owner, json={"bundle_id": bundle_id, "report": {}}
    )

    assert stolen.status_code == 404
    assert traversal.status_code == 404
    assert own.json()["bundle_id"] == bundle_id


# --- capture --------------------------------------------------------------------------------------


def test_mask_keeps_structure_and_drops_values() -> None:
    assert mask({"rows": [{"LOB": "GOLD", "Sales": 12.5, "Active": True}]}) == {
        "rows": [{"LOB": "<text:4>", "Sales": "<number>", "Active": "<bool>"}]
    }
    assert mask_text_payload("LOB,Sales\nGOLD,1\nSILVER,2") == {
        "csv_header": "LOB,Sales",
        "csv_rows": 2,
    }


def test_bundle_open_checks_owner_and_format(tmp_path: Path) -> None:
    bundle = CaptureBundle.create(tmp_path, "tenant:alice")

    assert bundle.id.endswith(user_hash("tenant:alice"))
    assert CaptureBundle.open(tmp_path, bundle.id, "tenant:alice") is not None
    assert CaptureBundle.open(tmp_path, bundle.id, "tenant:bob") is None
    assert CaptureBundle.open(tmp_path, f"../{bundle.id}", "tenant:alice") is None


# --- runner ---------------------------------------------------------------------------------------


class StubBroker(TokenBroker):
    """Returns JWT-shaped tokens (the runner reads their claims) that embed the canary."""

    def __init__(self, fail_fabric: bool = False) -> None:
        self.fail_fabric = fail_fabric

    async def get_token(self, ctx: RequestContext, scope: str | None = None) -> str:
        if scope == FABRIC_SCOPE and self.fail_fabric:
            raise RuntimeError("consent missing")
        aud = (
            "https://api.fabric.microsoft.com"
            if scope
            else "https://analysis.windows.net/powerbi/api"
        )
        return jwt.encode({"aud": aud, "scp": "Dataset.Read.All", "jti": CANARY}, "k" * 32)


def _ctx() -> RequestContext:
    user = AuthenticatedUser(
        object_id="user-a-oid",
        tenant_id="tenant-1",
        username="user.a@contoso.com",
        display_name="User A",
        scopes=frozenset({"x_CV_ForPBI"}),
        client_app_id="pbi",
    )
    token = jwt.encode({"ver": "1.0", "oid": "user-a-oid", "jti": CANARY}, "k" * 32)
    return RequestContext(user=user, correlation_id="c-1", access_token=token)


def _powerbi_rest(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path.endswith("/groups"):
        return httpx.Response(200, json={"value": [{"id": "w1"}, {"id": "w2"}]})
    if path.endswith("/executeQueries"):
        return httpx.Response(
            200, json={"results": [{"tables": [{"rows": [{"Product[LOB]": "GOLD"}]}]}]}
        )
    return httpx.Response(200, json={"id": "sales-ds", "name": "Secret Sales Model"})


async def _run(
    tmp_path: Path, broker: TokenBroker, fake: FakeFabricIq
) -> tuple[dict[str, Any], CaptureBundle]:
    settings = Settings(
        _env_file=None,
        fabric_iq_mcp_url="http://fabric-iq.test/mcp",
        fabric_iq_token_scope=FABRIC_SCOPE,
    )
    bundle = CaptureBundle.create(tmp_path, "tenant-1:user-a-oid")
    async with (
        running(fake) as factory,
        httpx.AsyncClient(transport=httpx.MockTransport(_powerbi_rest)) as http,
    ):
        runner = DiagnosticsRunner(
            settings, broker, FabricIqMcpGateway(settings, http_client_factory=factory), http
        )
        summary = await runner.run(
            _ctx(), bundle, model_id="sales-ds", origin="null", value_term="GOLD"
        )
    return summary, bundle


def _steps(summary: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {s["name"]: s for s in summary["steps"]}


async def test_runner_runs_every_spike_step(tmp_path: Path) -> None:
    fake = FakeFabricIq(
        dax_errors={"__phase2_no_such_measure__": "Query (1, 15) The value for '__x' is invalid."}
    )

    summary, _ = await _run(tmp_path, StubBroker(), fake)

    steps = _steps(summary)
    failed = {name: s["error"] for name, s in steps.items() if s["status"] != "ok"}
    assert failed == {}
    assert steps["powerbi_list_workspaces"]["detail"]["workspace_count"] == 2
    assert "ExecuteQuery" in steps["fabric_iq_tools_list"]["detail"]["tools"]
    assert steps["fabric_iq_schema"]["detail"]["normalized"]["tables"] == 1
    assert steps["fabric_iq_execute_query"]["detail"]["parsed"]["row_count"] == 2
    assert steps["fabric_iq_invalid_dax"]["detail"]["classified_as"] == "DaxQueryError"
    assert steps["request_origin"]["detail"] == {"origin": "null"}
    assert summary["sample_dax"] == "EVALUATE TOPN(5, 'Product')"
    assert (
        "ExecuteQuery",
        {"artifactId": "sales-ds", "daxQueries": [INVALID_DAX], "maxRows": 250},
    ) in fake.calls


async def test_runner_continues_after_a_failed_step(tmp_path: Path) -> None:
    summary, _ = await _run(tmp_path, StubBroker(fail_fabric=True), FakeFabricIq())

    steps = _steps(summary)
    assert steps["obo_fabric_scope"]["status"] == "failed"
    assert steps["obo_fabric_scope"]["error"] == "RuntimeError"  # type only, no message
    assert steps["fabric_iq_schema"]["status"] == "skipped"
    assert steps["rest_execute_queries"]["status"] == "ok"  # the REST path still ran


async def test_capture_bundle_has_no_tokens_and_no_row_values(tmp_path: Path) -> None:
    _, bundle = await _run(tmp_path, StubBroker(), FakeFabricIq())

    files = {p.name: p.read_text(encoding="utf-8") for p in bundle.directory.iterdir()}
    assert {"summary.json", "schema_payload.json", "execute_query_masked.json"} <= set(files)
    for name, text in files.items():
        assert CANARY not in text, name
        assert "eyJ" not in text, name  # no JWT of any kind
        if name != "schema_payload.json" and name != "schema_raw_result.json":
            assert "GOLD" not in text, name  # row values are masked
            assert "Secret Sales Model" not in text, name


# --- anonymizer -----------------------------------------------------------------------------------


def test_anonymizer_keeps_structure_and_replaces_every_name() -> None:
    payload = {
        "tables": [
            {
                "name": "Sales",
                "description": "Contoso revenue",
                "columns": [{"name": "LOB", "dataType": "string", "isHidden": False}],
                "measures": [{"name": "Net Sales", "expression": "SUM(Sales[Amount])"}],
            },
            {"name": "Product", "relationships": [{"to": "Sales"}]},
        ],
        "embedded": json.dumps({"table": "Sales"}),
    }

    result, leaks = anonymize_capture.anonymize(payload)

    assert leaks == []
    text = json.dumps(result)
    for original in ("Sales", "Contoso", "LOB", "Net Sales", "Product", "SUM("):
        assert original not in text
    tables = result["tables"]
    assert tables[0]["name"] == tables[1]["relationships"][0]["to"]  # references still line up
    assert tables[0]["columns"][0]["dataType"] == "string"  # format vocabulary kept
    assert tables[0]["columns"][0]["isHidden"] is False
    assert json.loads(result["embedded"])["table"] == tables[0]["name"]


def test_anonymize_cli_writes_the_fixture(tmp_path: Path) -> None:
    source, target = tmp_path / "in.json", tmp_path / "out.json"
    source.write_text(json.dumps({"tables": [{"name": "Finance"}]}), encoding="utf-8")

    assert anonymize_capture.main([str(source), str(target)]) == 0
    assert "Finance" not in target.read_text(encoding="utf-8")


# --- config check ---------------------------------------------------------------------------------


def test_config_check_never_prints_secrets(
    entra_settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = entra_settings.model_copy(
        update={
            "entra_client_secret": SecretStr("SECRET-VALUE-1"),
            "groq_api_key": SecretStr("gsk_SECRET-VALUE-2"),
        }
    )

    check_config.report(check_config.static_checks(settings))

    out = capsys.readouterr().out
    assert "SECRET-VALUE" not in out
    obo = next(line for line in out.splitlines() if "OBO credential present" in line)
    assert obo.startswith("[PASS]") and obo.rstrip().endswith("secret")


def test_config_check_treats_empty_secrets_as_missing(entra_settings: Settings) -> None:
    settings = entra_settings.model_copy(
        update={"entra_client_secret": SecretStr(""), "groq_api_key": SecretStr("")}
    )

    checks = {c.name: c for c in check_config.static_checks(settings)}

    assert checks["OBO credential present (certificate or secret)"].status == "FAIL"
    assert checks["GROQ_API_KEY present (LLM)"].status == "WARN"
