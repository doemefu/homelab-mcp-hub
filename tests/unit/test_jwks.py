import socket
from typing import Any

import pytest
from pytest_httpserver import HTTPServer

from mcp_hub.jwks import JwksCache
from tests.support.clock import FakeClock
from tests.support.keys import TestKey, jwks

pytestmark = pytest.mark.anyio

K1, K2 = TestKey("k1"), TestKey("k2")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def fetches(server: HTTPServer) -> int:
    return sum(1 for request, _ in server.log if request.path == "/jwks.json")


async def test_known_kid_is_cached(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/jwks.json").respond_with_json(jwks(K1))
    cache = JwksCache(httpserver.url_for("/jwks.json"), clock=FakeClock())
    assert await cache.get_key("k1") is not None
    assert await cache.get_key("k1") is not None
    assert fetches(httpserver) == 1


async def test_unknown_kid_refetches_at_most_once_per_60_seconds(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/jwks.json").respond_with_json(jwks(K1))
    clock = FakeClock()
    cache = JwksCache(httpserver.url_for("/jwks.json"), clock=clock)
    await cache.get_key("k1")  # t=0: fetch 1
    assert await cache.get_key("unknown") is None  # t=0: throttled
    clock.advance(61)
    assert await cache.get_key("unknown") is None  # t=61: fetch 2
    clock.advance(1)
    assert await cache.get_key("unknown") is None  # t=62: throttled
    assert fetches(httpserver) == 2


async def test_rotated_key_is_found_after_the_throttle_window(httpserver: HTTPServer) -> None:
    httpserver.expect_ordered_request("/jwks.json").respond_with_json(jwks(K1))
    httpserver.expect_ordered_request("/jwks.json").respond_with_json(jwks(K1, K2))
    clock = FakeClock()
    cache = JwksCache(httpserver.url_for("/jwks.json"), clock=clock)
    await cache.get_key("k1")
    clock.advance(60)
    assert await cache.get_key("k2") is not None


@pytest.mark.parametrize(
    "respond",
    [
        lambda s: s.expect_request("/jwks.json").respond_with_data("oops", status=500),
        lambda s: s.expect_request("/jwks.json").respond_with_data("not json", content_type="application/json"),
        lambda s: s.expect_request("/jwks.json").respond_with_json({"keys": "nope"}),
        lambda s: s.expect_request("/jwks.json").respond_with_data("x" * 70000),
    ],
    ids=["status-500", "not-json", "keys-not-a-list", "oversized"],
)
async def test_oversized_or_invalid_jwks_is_ignored(httpserver: HTTPServer, respond: Any) -> None:
    respond(httpserver)
    cache = JwksCache(httpserver.url_for("/jwks.json"), clock=FakeClock())
    assert await cache.get_key("k1") is None


async def test_unreachable_jwks_keeps_previous_keys(httpserver: HTTPServer) -> None:
    httpserver.expect_ordered_request("/jwks.json").respond_with_json(jwks(K1))
    httpserver.expect_ordered_request("/jwks.json").respond_with_data("down", status=503)
    clock = FakeClock()
    cache = JwksCache(httpserver.url_for("/jwks.json"), clock=clock, max_age=100)
    await cache.get_key("k1")
    clock.advance(101)  # stale -> refetch fails -> stale key still served
    assert await cache.get_key("k1") is not None


async def test_refused_connection_is_ignored() -> None:
    with socket.socket() as probe:  # a port that was free a moment ago: nothing listens there
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    cache = JwksCache(f"http://127.0.0.1:{port}/jwks.json", clock=FakeClock(), timeout=1.0)
    assert await cache.get_key("k1") is None


async def test_non_rsa_and_encryption_keys_are_skipped(httpserver: HTTPServer) -> None:
    enc = K2.jwk() | {"use": "enc"}
    httpserver.expect_request("/jwks.json").respond_with_json({"keys": [enc, {"kty": "oct", "kid": "h", "k": "AA"}]})
    cache = JwksCache(httpserver.url_for("/jwks.json"), clock=FakeClock())
    assert await cache.get_key("k2") is None
    assert await cache.get_key("h") is None
