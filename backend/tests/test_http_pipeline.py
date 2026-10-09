from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from tests.conftest import TokenFactory


async def test_correlation_id_is_generated_and_echoed(client: AsyncClient) -> None:
    response = await client.get("/api/v1/health")

    assert len(response.headers["x-correlation-id"]) == 32


async def test_well_formed_inbound_correlation_id_is_kept(client: AsyncClient) -> None:
    response = await client.get("/api/v1/health", headers={"X-Correlation-ID": "visual-req-42"})

    assert response.headers["x-correlation-id"] == "visual-req-42"


async def test_malformed_correlation_id_is_replaced(client: AsyncClient) -> None:
    response = await client.get(
        "/api/v1/health", headers={"X-Correlation-ID": "bad id\twith<script>"}
    )

    assert response.headers["x-correlation-id"] != "bad id\twith<script>"


async def test_errors_use_the_uniform_envelope_with_correlation_id(client: AsyncClient) -> None:
    response = await client.get("/api/v1/session", headers={"X-Correlation-ID": "abc-123"})

    assert response.json() == {
        "error": {
            "code": "missing_token",
            "message": "Sign-in is required.",
            "correlation_id": "abc-123",
        }
    }


async def test_unknown_route_uses_the_uniform_envelope(client: AsyncClient) -> None:
    response = await client.get("/api/v1/does-not-exist")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


async def test_security_headers_are_set(client: AsyncClient) -> None:
    response = await client.get("/api/v1/health")

    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["cache-control"] == "no-store"
    assert "strict-transport-security" in response.headers  # ENVIRONMENT=test, not local


async def test_oversized_request_body_is_rejected(client: AsyncClient) -> None:
    response = await client.post("/api/v1/session", content=b"x" * (64 * 1024 + 1))

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "payload_too_large"


async def test_cors_preflight_from_sandboxed_visual_is_allowed(client: AsyncClient) -> None:
    response = await client.options(
        "/api/v1/session",
        headers={
            "Origin": "null",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "null"
    assert "access-control-allow-credentials" not in response.headers


async def test_cors_preflight_allows_the_visuals_delete_chat(client: AsyncClient) -> None:
    # "Delete chat" sends DELETE cross-origin; a refused preflight failed silently in the visual.
    response = await client.options(
        "/api/v1/chat/sessions/00000000-0000-0000-0000-000000000000",
        headers={
            "Origin": "null",
            "Access-Control-Request-Method": "DELETE",
            "Access-Control-Request-Headers": "authorization",
        },
    )

    assert response.status_code == 200
    assert "DELETE" in response.headers["access-control-allow-methods"]


async def test_cors_preflight_from_unknown_origin_is_not_allowed(client: AsyncClient) -> None:
    response = await client.options(
        "/api/v1/session",
        headers={"Origin": "https://evil.example.com", "Access-Control-Request-Method": "GET"},
    )

    assert "access-control-allow-origin" not in response.headers


async def test_authenticated_response_carries_cors_header(
    entra_app: FastAPI, make_token: TokenFactory
) -> None:
    async with AsyncClient(transport=ASGITransport(app=entra_app), base_url="http://t") as client:
        response = await client.get(
            "/api/v1/session",
            headers={"Origin": "null", "Authorization": f"Bearer {make_token()}"},
        )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "null"
