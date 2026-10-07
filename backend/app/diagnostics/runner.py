"""Phase 2 spike checks, run as the signed-in user through the production code paths.

S1 sign-in claims · S2 OBO (Power BI + Fabric scopes) · S3 Fabric IQ MCP tools · S4 REST probe and
executeQueries · S6 request Origin. Each step records status, duration and a safe detail; failures
don't stop later steps. Raw formats go to the local capture bundle (see capture.py).
"""

import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from typing import Any

import httpx
import jwt

from app.auth.models import RequestContext
from app.auth.token_broker import TokenBroker
from app.core.config import Settings
from app.core.errors import AppError
from app.diagnostics.capture import CaptureBundle, mask, mask_text_payload
from app.powerbi.fabric_iq import EXECUTE_QUERY, GET_SCHEMA, VALUE_SEARCH, FabricIqMcpGateway
from app.semantic.normalizer import normalize

SAFE_CLAIMS = ("ver", "aud", "iss", "tid", "oid", "scp", "appid", "azp", "exp", "name")
INVALID_DAX = 'EVALUATE ROW("x", [__phase2_no_such_measure__])'
SAMPLE_ROWS = 5


@dataclass
class Step:
    spike: str
    name: str
    status: str = "skipped"  # ok | failed | skipped
    duration_ms: int = 0
    detail: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


def _claims(token: str) -> dict[str, Any]:
    # Signature was already verified by the auth layer (or the token came from Entra via OBO).
    claims = jwt.decode(token, options={"verify_signature": False})
    return {k: claims[k] for k in SAFE_CLAIMS if k in claims}


def _error(exc: Exception) -> str:
    code = getattr(exc, "code", None)
    return f"{type(exc).__name__}{f':{code}' if code else ''}"


class DiagnosticsRunner:
    def __init__(
        self,
        settings: Settings,
        broker: TokenBroker,
        fabric: FabricIqMcpGateway,
        http: httpx.AsyncClient,
    ) -> None:
        self._settings = settings
        self._broker = broker
        self._fabric = fabric
        self._http = http
        self._base = settings.powerbi_api_base_url.rstrip("/")
        self.steps: list[Step] = []

    async def run(
        self,
        ctx: RequestContext,
        bundle: CaptureBundle,
        *,
        model_id: str,
        origin: str | None,
        value_term: str | None = None,
        sample_dax: str | None = None,
    ) -> dict[str, Any]:
        state: dict[str, Any] = {}

        await self._step("S1", "sign_in_claims", lambda s: self._s1(ctx, s))
        await self._step("S2", "obo_powerbi_scope", lambda s: self._s2(ctx, s, state, "pbi", None))
        await self._step(
            "S2",
            "obo_fabric_scope",
            lambda s: self._s2(ctx, s, state, "fabric", self._settings.fabric_iq_token_scope),
        )
        await self._step("S2", "powerbi_list_workspaces", lambda s: self._groups(s, state))
        await self._step("S3", "fabric_iq_tools_list", lambda s: self._tools(s, state, bundle))
        await self._step(
            "S3", "fabric_iq_schema", lambda s: self._schema(s, state, bundle, model_id)
        )
        if value_term:
            await self._step(
                "S3",
                "fabric_iq_value_search",
                lambda s: self._value_search(s, state, bundle, model_id, value_term),
            )
        dax = sample_dax or self._default_dax(state)
        await self._step(
            "S3",
            "fabric_iq_execute_query",
            lambda s: self._execute(s, state, bundle, model_id, dax),
        )
        await self._step(
            "S3", "fabric_iq_invalid_dax", lambda s: self._invalid(s, state, bundle, model_id)
        )
        await self._step(
            "S4", "rest_get_dataset", lambda s: self._probe(s, state, bundle, model_id)
        )
        await self._step(
            "S4",
            "rest_execute_queries",
            lambda s: self._rest_execute(s, state, bundle, model_id, dax),
        )
        self.steps.append(Step("S6", "request_origin", "ok", detail={"origin": origin}))

        summary = {
            "bundle_id": bundle.id,
            "model_id": model_id,
            "sample_dax": dax,
            "steps": [asdict(s) for s in self.steps],
        }
        bundle.write_json("summary.json", summary)
        return summary

    # --- steps ----------------------------------------------------------------------------------

    async def _step(self, spike: str, name: str, body: Callable[[Step], Awaitable[None]]) -> None:
        step = Step(spike, name)
        started = time.perf_counter()
        try:
            await body(step)
            if step.status == "skipped" and not step.detail.get("reason"):
                step.status = "ok"
        except Exception as exc:  # every check reports, none aborts the run
            step.status, step.error = "failed", _error(exc)
        step.duration_ms = int((time.perf_counter() - started) * 1000)
        self.steps.append(step)

    async def _s1(self, ctx: RequestContext, step: Step) -> None:
        step.detail = {"claims": _claims(ctx.access_token), "user_key": ctx.user.key}

    async def _s2(
        self, ctx: RequestContext, step: Step, state: dict[str, Any], key: str, scope: str | None
    ) -> None:
        token = await self._broker.get_token(ctx, scope)
        state[key] = token
        claims = _claims(token)
        step.detail = {"aud": claims.get("aud"), "scp": claims.get("scp"), "ver": claims.get("ver")}

    def _need(self, state: dict[str, Any], key: str, step: Step) -> str | None:
        token = state.get(key)
        if not token:
            step.status, step.detail = "skipped", {"reason": f"no {key} token (S2 failed)"}
        return token

    async def _groups(self, step: Step, state: dict[str, Any]) -> None:
        if not (token := self._need(state, "pbi", step)):
            return
        response = await self._http.get(
            f"{self._base}/groups", headers={"Authorization": f"Bearer {token}"}
        )
        step.detail = {"http_status": response.status_code}
        if response.status_code == 200:
            step.detail["workspace_count"] = len(response.json().get("value", []))
        else:
            step.status = "failed"

    async def _tools(self, step: Step, state: dict[str, Any], bundle: CaptureBundle) -> None:
        if not (token := self._need(state, "fabric", step)):
            return
        tools = await self._fabric.list_tools(token)
        bundle.write_json("fabric_iq_tools.json", tools)
        step.detail = {"tools": [t["name"] for t in tools]}

    async def _schema(
        self, step: Step, state: dict[str, Any], bundle: CaptureBundle, model_id: str
    ) -> None:
        if not (token := self._need(state, "fabric", step)):
            return
        raw = await self._fabric.call_raw(token, GET_SCHEMA, {"artifactId": model_id})
        bundle.write_json("schema_raw_result.json", raw)  # metadata only; local
        payload = (await self._fabric.get_schema(token, model_id)).data
        bundle.write_json("schema_payload.json", payload)
        schema = normalize(payload)
        kinds = [o.kind for o in schema.objects]
        state["first_table"] = next((o.name for o in schema.objects if o.kind == "table"), None)
        step.detail = {
            "is_error": raw.get("is_error"),
            "content_types": [c["type"] for c in raw.get("content", [])],
            "has_structured_content": raw.get("structured_content") is not None,
            "normalized": {
                "tables": kinds.count("table"),
                "columns": kinds.count("column"),
                "measures": kinds.count("measure"),
                "ai_instructions": len(schema.ai_instructions),
                "verified_answers": len(schema.verified_answers),
            },
        }
        if not schema.objects:
            step.status = "failed"
            step.error = "normalizer recognised no objects: payload shape differs (see capture)"

    async def _value_search(
        self, step: Step, state: dict[str, Any], bundle: CaptureBundle, model_id: str, term: str
    ) -> None:
        if not (token := self._need(state, "fabric", step)):
            return
        raw = await self._fabric.call_raw(
            token, VALUE_SEARCH, {"artifactId": model_id, "searchTerms": [term]}
        )
        bundle.write_json("value_search_masked.json", _masked(raw))
        step.detail = {
            "is_error": raw.get("is_error"),
            "content_types": [c["type"] for c in raw.get("content", [])],
        }

    def _default_dax(self, state: dict[str, Any]) -> str:
        table = state.get("first_table") or "Table"
        return f"EVALUATE TOPN({SAMPLE_ROWS}, '{table.replace(chr(39), chr(39) * 2)}')"

    async def _execute(
        self, step: Step, state: dict[str, Any], bundle: CaptureBundle, model_id: str, dax: str
    ) -> None:
        if not (token := self._need(state, "fabric", step)):
            return
        raw = await self._fabric.call_raw(
            token,
            EXECUTE_QUERY,
            {"artifactId": model_id, "daxQueries": [dax], "maxRows": SAMPLE_ROWS},
        )
        bundle.write_json("execute_query_masked.json", _masked(raw))
        step.detail = {
            "is_error": raw.get("is_error"),
            "content_types": [c["type"] for c in raw.get("content", [])],
            "mime_types": [c.get("mime_type") for c in raw.get("content", [])],
            "has_structured_content": raw.get("structured_content") is not None,
        }
        try:
            parsed = await self._fabric.execute_dax(
                token, model_id, dax, max_rows=SAMPLE_ROWS, user_key="diagnostics"
            )
            step.detail["parsed"] = {"columns": parsed.columns, "row_count": parsed.row_count}
        except AppError as exc:
            step.status, step.error = "failed", f"our parser: {_error(exc)}"
        except Exception as exc:
            step.status, step.error = "failed", f"our parser: {_error(exc)}"

    async def _invalid(
        self, step: Step, state: dict[str, Any], bundle: CaptureBundle, model_id: str
    ) -> None:
        if not (token := self._need(state, "fabric", step)):
            return
        raw = await self._fabric.call_raw(
            token, EXECUTE_QUERY, {"artifactId": model_id, "daxQueries": [INVALID_DAX]}
        )
        # Power BI's error text is kept (local only); an unexpected success holds rows: masked.
        bundle.write_json("invalid_dax_error.json", raw if raw.get("is_error") else _masked(raw))
        step.detail = {"is_error": raw.get("is_error"), "classified_as": raw.get("classified_as")}
        if raw.get("classified_as") != "DaxQueryError":
            step.status, step.error = "failed", "error not classified as a DAX error"

    async def _probe(
        self, step: Step, state: dict[str, Any], bundle: CaptureBundle, model_id: str
    ) -> None:
        if not (token := self._need(state, "pbi", step)):
            return
        response = await self._http.get(
            f"{self._base}/datasets/{model_id}", headers={"Authorization": f"Bearer {token}"}
        )
        body = _json(response)
        bundle.write_json(
            "rest_get_dataset.json", {"status": response.status_code, "body": mask(body)}
        )
        step.detail = {
            "http_status": response.status_code,
            "fields": sorted(body) if isinstance(body, dict) else None,
        }

    async def _rest_execute(
        self, step: Step, state: dict[str, Any], bundle: CaptureBundle, model_id: str, dax: str
    ) -> None:
        if not (token := self._need(state, "pbi", step)):
            return
        response = await self._http.post(
            f"{self._base}/datasets/{model_id}/executeQueries",
            headers={"Authorization": f"Bearer {token}"},
            json={"queries": [{"query": dax}], "serializerSettings": {"includeNulls": True}},
        )
        body = _json(response)
        ok = response.status_code == 200
        # Success bodies hold data (masked); error bodies hold Power BI's text (local only).
        bundle.write_json(
            "rest_execute_queries.json",
            {"status": response.status_code, "body": mask(body) if ok else body},
        )
        step.detail = {"http_status": response.status_code}
        if not ok:
            step.status = "failed"
            error = body.get("error", {}) if isinstance(body, dict) else {}
            step.detail["error_code"] = error.get("code") if isinstance(error, dict) else None


def _masked(raw: dict[str, Any]) -> dict[str, Any]:
    out = dict(raw)
    out["content"] = [
        {**c, "text": mask_text_payload(c.get("text"))} for c in raw.get("content", [])
    ]
    out["structured_content"] = mask(raw.get("structured_content"))
    return out


def _json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return {"non_json_length": len(response.content)}
