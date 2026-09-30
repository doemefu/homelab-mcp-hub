import json
from typing import Any

import httpx2
from starlette.testclient import TestClient

MODERN = "2026-07-28"
HANDSHAKE = "2025-11-25"
ACCEPT = "application/json, text/event-stream"
META = {
    "io.modelcontextprotocol/protocolVersion": MODERN,
    "io.modelcontextprotocol/clientCapabilities": {},
    "io.modelcontextprotocol/clientInfo": {"name": "contract-test", "version": "0"},
}


def auth(token: str | None) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"} if token is not None else {}


def modern(
    client: TestClient,
    token: str | None,
    method: str,
    params: dict[str, Any] | None = None,
    name: str | None = None,
    headers: dict[str, str] | None = None,
) -> httpx2.Response:
    """One stateless 2026-07-28 request as claude.ai sends it (research S7)."""
    h = (
        {"Accept": ACCEPT, "Content-Type": "application/json", "MCP-Protocol-Version": MODERN, "Mcp-Method": method}
        | auth(token)
        | (headers or {})
    )
    if name is not None:
        h["Mcp-Name"] = name
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": (params or {}) | {"_meta": META}}
    return client.post("/mcp", headers=h, json=payload)


def initialize(
    client: TestClient, token: str | None, headers: dict[str, str] | None = None
) -> tuple[httpx2.Response, str | None]:
    h = {"Accept": ACCEPT, "Content-Type": "application/json"} | auth(token) | (headers or {})
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": HANDSHAKE,
            "capabilities": {},
            "clientInfo": {"name": "contract-test", "version": "0"},
        },
    }
    response = client.post("/mcp", headers=h, json=payload)
    return response, response.headers.get("mcp-session-id")


def body(response: httpx2.Response) -> dict[str, Any]:
    if response.headers.get("content-type", "").startswith("text/event-stream"):
        for line in response.text.splitlines():
            if line.startswith("data: "):
                return json.loads(line[6:])  # type: ignore[no-any-return]
        raise AssertionError("no data line in SSE response")
    return response.json()  # type: ignore[no-any-return]
