import json
import logging
from collections.abc import Iterator

import httpx2
from starlette.testclient import TestClient

from mcp_hub.app import MAX_REQUEST_BODY_BYTES
from tests.support.logcapture import Capture
from tests.support.mcp import ACCEPT, META, MODERN, assert_challenge, auth, body, modern
from tests.support.tokens import TokenFactory

PAD_MARKER = "pad-sentinel-"


def test_wrong_host_with_valid_token_gets_421(client: TestClient, tokens: TokenFactory) -> None:
    assert modern(client, tokens.mint(), "tools/list", headers={"Host": "evil.example.org"}).status_code == 421


def test_wrong_host_without_token_gets_401(client: TestClient) -> None:
    # Authentication runs before the Host check (spec 080 §4.4): the full challenge, not a 421.
    assert_challenge(modern(client, None, "tools/list", headers={"Host": "evil.example.org"}))


def test_disallowed_origin_gets_403_without_challenge(client: TestClient, tokens: TokenFactory) -> None:
    response = modern(client, tokens.mint(), "tools/list", headers={"Origin": "https://evil.example.org"})
    assert response.status_code == 403
    assert "www-authenticate" not in response.headers


def test_claude_origins_are_allowed(client: TestClient, tokens: TokenFactory) -> None:
    for origin in ("https://claude.ai", "https://claude.com"):
        assert modern(client, tokens.mint(), "tools/list", headers={"Origin": origin}).status_code == 200


def test_public_port_serves_no_health_routes(client: TestClient) -> None:
    assert client.get("/healthz").status_code == 404
    assert client.get("/readyz").status_code == 404


def _padded_tools_list(size: int) -> bytes:
    """A valid 2026-07-28 tools/list body of exactly `size` bytes (the padding is an ignored params field)."""

    def encode(pad: str) -> bytes:
        params = {"_meta": META, "pad": pad}
        return json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": params}).encode()

    encoded = encode(PAD_MARKER + "x" * (size - len(encode(PAD_MARKER))))
    assert len(encoded) == size
    return encoded


def _post(client: TestClient, token: str | None, content: bytes | Iterator[bytes]) -> httpx2.Response:
    headers = {
        "Accept": ACCEPT,
        "Content-Type": "application/json",
        "MCP-Protocol-Version": MODERN,
        "Mcp-Method": "tools/list",
    } | auth(token)
    return client.post("/mcp", headers=headers, content=content)


def test_body_at_cap_is_accepted(client: TestClient, tokens: TokenFactory) -> None:
    response = _post(client, tokens.mint(), _padded_tools_list(MAX_REQUEST_BODY_BYTES))
    assert response.status_code == 200
    assert "tools" in body(response)["result"]


def test_body_above_cap_with_valid_token_gets_413(client: TestClient, tokens: TokenFactory) -> None:
    # Declared by Content-Length: refused before the body is read or parsed.
    response = _post(client, tokens.mint(), _padded_tools_list(MAX_REQUEST_BODY_BYTES + 1))
    assert response.status_code == 413
    assert "www-authenticate" not in response.headers


def test_streamed_body_above_cap_gets_413(client: TestClient, tokens: TokenFactory) -> None:
    # Chunked, no Content-Length: refused once the received bytes pass the cap.
    data = _padded_tools_list(MAX_REQUEST_BODY_BYTES + 1)
    chunks = iter([data[i : i + 16_384] for i in range(0, len(data), 16_384)])
    response = _post(client, tokens.mint(), chunks)
    assert response.status_code == 413


def test_body_above_cap_without_token_gets_401(client: TestClient) -> None:
    # Authentication runs before the body limit (spec 080 §4.4): the full challenge, not a 413.
    assert_challenge(_post(client, None, _padded_tools_list(MAX_REQUEST_BODY_BYTES + 1)))


def test_body_above_cap_logs_status_and_no_body(client: TestClient, tokens: TokenFactory) -> None:
    handler = Capture()
    logging.getLogger().addHandler(handler)
    try:
        _post(client, tokens.mint(), _padded_tools_list(MAX_REQUEST_BODY_BYTES + 1))
    finally:
        logging.getLogger().removeHandler(handler)
    requests = [e for e in map(json.loads, handler.lines) if e["event"] == "request"]
    assert [(e["route"], e["status"], e.get("sub")) for e in requests] == [("/mcp", 413, "owner-test")]
    assert PAD_MARKER not in "\n".join(handler.lines)
    assert PAD_MARKER not in "\n".join(message for _, _, message in handler.raw)
