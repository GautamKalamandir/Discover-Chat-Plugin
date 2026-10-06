import json
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from app.auth.jwks import JwksKeySource

JWKS_URL = "https://login.example.com/keys"


def jwk(kid: str) -> dict[str, Any]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048).public_key()
    data: dict[str, Any] = json.loads(RSAAlgorithm.to_jwk(key))
    data["kid"] = kid
    return data


class JwksEndpoint:
    def __init__(self, *kids: str) -> None:
        self.keys = [jwk(k) for k in kids]
        self.requests = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests += 1
        return httpx.Response(200, json={"keys": self.keys})

    def source(self, **kwargs: Any) -> JwksKeySource:
        client = httpx.AsyncClient(transport=httpx.MockTransport(self.handler))
        return JwksKeySource(JWKS_URL, http_client=client, **kwargs)


async def test_keys_are_fetched_once_and_cached() -> None:
    endpoint = JwksEndpoint("k1", "k2")
    source = endpoint.source()

    await source.get_signing_key("k1")
    await source.get_signing_key("k2")

    assert endpoint.requests == 1


async def test_rolled_over_key_is_picked_up_by_refetch() -> None:
    endpoint = JwksEndpoint("k1")
    source = endpoint.source(min_refresh_interval=0)
    await source.get_signing_key("k1")

    endpoint.keys.append(jwk("k2"))

    assert await source.get_signing_key("k2") is not None
    assert endpoint.requests == 2


async def test_unknown_kids_cannot_force_repeated_refetches() -> None:
    endpoint = JwksEndpoint("k1")
    source = endpoint.source(min_refresh_interval=300)
    await source.get_signing_key("k1")

    for forged in ("x1", "x2", "x3"):
        with pytest.raises(KeyError):
            await source.get_signing_key(forged)

    assert endpoint.requests == 1


async def test_cache_expiry_forces_refetch() -> None:
    endpoint = JwksEndpoint("k1")
    source = endpoint.source(ttl_seconds=-1)  # always expired; avoids clock resolution

    await source.get_signing_key("k1")
    await source.get_signing_key("k1")

    assert endpoint.requests == 2
