import json
import logging
from collections.abc import Iterator

import httpx2
from starlette.testclient import TestClient
from starlette.types import Message

from mcp_hub.app import MAX_REQUEST_BODY_BYTES, HubApp
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
    assert "content-length" not in response.request.headers
    assert response.status_code == 413


def test_body_arriving_in_many_chunks_is_refused_once_above_cap(
    hub: HubApp, client: TestClient, tokens: TokenFactory
) -> None:
    # TestClient hands the app the whole body in one message; drive the ASGI app directly so the SDK sees many
    # 16 KiB http.request messages (more_body=True) without a Content-Length, as a chunked upload would arrive.
    chunk = 16 * 1024
    total = 2 * MAX_REQUEST_BODY_BYTES
    headers = {"host": "mcp.furchert.ch", "accept": ACCEPT, "content-type": "application/json"}
    headers |= {"mcp-protocol-version": MODERN, "mcp-method": "tools/list"} | auth(tokens.mint())
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "https",
        "path": "/mcp",
        "raw_path": b"/mcp",
        "root_path": "",
        "query_string": b"",
        "headers": [(name.lower().encode(), value.encode()) for name, value in headers.items()],
        "client": ("127.0.0.1", 50000),
        "server": ("mcp.furchert.ch", 443),
    }
    received = 0
    sent: list[Message] = []

    async def receive() -> Message:
        nonlocal received
        received += 1
        return {"type": "http.request", "body": b"x" * chunk, "more_body": received * chunk < total}

    async def send(message: Message) -> None:
        sent.append(message)

    async def run() -> None:
        await hub.public(scope, receive, send)

    assert client.portal is not None  # the lifespan (and the SDK's session manager) runs on the client's portal
    client.portal.call(run)
    start = next(m for m in sent if m["type"] == "http.response.start")
    assert start["status"] == 413
    # The SDK stops reading at the first chunk that passes the cap; the rest of the body is never received.
    assert received == MAX_REQUEST_BODY_BYTES // chunk + 1


def test_body_above_cap_without_token_gets_401(client: TestClient) -> None:
    # Authentication runs before the body limit (spec 080 §4.4): the full challenge, not a 413.
    assert_challenge(_post(client, None, _padded_tools_list(MAX_REQUEST_BODY_BYTES + 1)))


def test_body_above_cap_logs_status_and_no_body(client: TestClient, tokens: TokenFactory) -> None:
    token = tokens.mint()
    handler = Capture()
    logging.getLogger().addHandler(handler)
    try:
        _post(client, token, _padded_tools_list(MAX_REQUEST_BODY_BYTES + 1))
    finally:
        logging.getLogger().removeHandler(handler)
    requests = [e for e in map(json.loads, handler.lines) if e["event"] == "request"]
    assert [(e["route"], e["status"], e.get("sub"), e.get("check")) for e in requests] == [
        ("/mcp", 413, "owner-test", "body_size")
    ]
    assert PAD_MARKER not in "\n".join(handler.lines)
    assert PAD_MARKER not in "\n".join(message for _, _, message in handler.raw)
    for text in ("\n".join(handler.lines), "\n".join(message for _, _, message in handler.raw)):
        assert token not in text
        assert token.split(".")[1] not in text
