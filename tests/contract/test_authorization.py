"""Gate G6: spec 080 §10.3 rows, one test (or parameter set) per row."""

import time
from collections.abc import Callable
from pathlib import Path

import httpx2
import pytest
from pytest_httpserver import HTTPServer
from starlette.testclient import TestClient

from mcp_hub.app import HubApp
from tests.support.clock import FakeClock
from tests.support.keys import TestKey, jwks
from tests.support.mcp import HANDSHAKE, body, initialize, modern
from tests.support.tokens import TokenFactory

METADATA_URL = "https://mcp.furchert.ch/.well-known/oauth-protected-resource/mcp"
SCOPE_PARAM = 'scope="mail:read calendar:read"'


def assert_challenge(response: httpx2.Response, status: int = 401) -> None:
    assert response.status_code == status
    challenge = response.headers["www-authenticate"]
    assert challenge.startswith("Bearer ")
    assert f'resource_metadata="{METADATA_URL}"' in challenge
    assert challenge.count(SCOPE_PARAM) == 1
    if status == 401:
        assert 'error="invalid_token"' in challenge
        assert 'error_description="Authentication required"' in challenge


def test_no_token_returns_401_with_challenge(client: TestClient) -> None:
    assert_challenge(modern(client, None, "tools/list"))


def test_no_token_handshake_probe_returns_same_challenge(client: TestClient) -> None:
    response, _ = initialize(client, None, headers={"MCP-Protocol-Version": HANDSHAKE})
    assert_challenge(response)


def test_protected_resource_metadata(client: TestClient) -> None:
    response = client.get("/.well-known/oauth-protected-resource/mcp")
    assert response.status_code == 200
    data = response.json()
    assert data["resource"] == "https://mcp.furchert.ch/mcp"
    assert data["authorization_servers"] == ["https://auth.furchert.ch"]
    assert data["scopes_supported"] == ["mail:read", "calendar:read"]


def test_root_metadata_path_is_not_served(client: TestClient) -> None:
    assert client.get("/.well-known/oauth-protected-resource").status_code == 404


def test_valid_token_modern_protocol_list_and_call(client: TestClient, tokens: TokenFactory) -> None:
    token = tokens.mint()
    listed = modern(client, token, "tools/list")
    assert listed.status_code == 200
    assert [t["name"] for t in body(listed)["result"]["tools"]] == ["list_accounts"]
    called = modern(client, token, "tools/call", {"name": "list_accounts", "arguments": {}}, name="list_accounts")
    assert called.status_code == 200
    assert body(called)["result"]["isError"] is False


def test_valid_token_handshake_protocol_initialize_then_list(client: TestClient, tokens: TokenFactory) -> None:
    token = tokens.mint()
    response, session = initialize(client, token)
    assert response.status_code == 200
    assert session
    assert body(response)["result"]["protocolVersion"] == HANDSHAKE
    headers = {
        "Authorization": f"Bearer {token}",
        "Mcp-Session-Id": session,
        "MCP-Protocol-Version": HANDSHAKE,
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    listed = client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert listed.status_code == 200
    assert [t["name"] for t in body(listed)["result"]["tools"]] == ["list_accounts"]


@pytest.mark.parametrize("typ", ["application/at+jwt", "AT+JWT", "Application/At+Jwt", "at+jwt"])
def test_typ_variants_accepted(client: TestClient, tokens: TokenFactory, typ: str) -> None:
    assert modern(client, tokens.mint(header={"typ": typ}), "tools/list").status_code == 200


def test_scope_as_space_delimited_string(client: TestClient, tokens: TokenFactory) -> None:
    assert modern(client, tokens.mint(scope="mail:read calendar:read"), "tools/list").status_code == 200


def test_aud_as_plain_string(client: TestClient, tokens: TokenFactory) -> None:
    assert modern(client, tokens.mint(aud="https://mcp.furchert.ch/mcp"), "tools/list").status_code == 200


def test_role_claim_is_ignored(client: TestClient, tokens: TokenFactory) -> None:
    assert modern(client, tokens.mint(role="ADMIN"), "tools/list").status_code == 200


def test_expired_within_leeway_is_accepted(client: TestClient, tokens: TokenFactory) -> None:
    assert modern(client, tokens.mint(exp=int(time.time()) - 30), "tools/list").status_code == 200


@pytest.mark.parametrize(
    "mint",
    [
        lambda t: t.mint(header={"typ": "JWT"}),
        lambda t: t.mint(header={"typ": None}),
        lambda t: t.mint(iss="https://auth.example.org"),
        lambda t: t.mint(aud=["claude-mcp-hub"]),
        lambda t: t.mint(aud=["https://mcp.furchert.ch/mcp/"]),
        lambda t: t.mint(client_id="furchert-ch"),
        lambda t: t.mint(drop=("client_id",)),
        lambda t: t.mint(exp=int(time.time()) - 90),  # spec §4.3 row 6
        lambda t: t.mint(nbf=int(time.time()) + 120),
        lambda t: t.hs256(),
        lambda t: t.alg_none(),
        lambda t: t.mint(sub="someone-else"),
        lambda t: t.mint(sub="Owner-Test"),
        lambda t: t.mint(drop=("scope",)),
    ],
    ids=[
        "typ-JWT",
        "typ-missing",
        "iss",
        "aud-client",
        "aud-slash",
        "client-id",
        "client-id-missing",
        "expired",
        "nbf-future",
        "hs256",
        "none",
        "sub-unknown",
        "sub-case",
        "scope-missing",
    ],
)
def test_rejected_tokens_get_401(client: TestClient, tokens: TokenFactory, mint: Callable[[TokenFactory], str]) -> None:
    assert_challenge(modern(client, mint(tokens), "tools/list"))


@pytest.mark.parametrize("content", [None, ""], ids=["missing", "empty"])
def test_allowlist_missing_or_empty_rejects(
    client: TestClient, tokens: TokenFactory, secrets_dir: Path, clock: FakeClock, content: str | None
) -> None:
    path = secrets_dir / "allowed-subjects"
    if content is None:
        path.unlink()
    else:
        path.write_text(content)
    clock.advance(60)
    assert_challenge(modern(client, tokens.mint(), "tools/list"))


def test_allowlist_change_takes_effect_within_60_seconds(
    client: TestClient, tokens: TokenFactory, secrets_dir: Path, clock: FakeClock
) -> None:
    assert modern(client, tokens.mint(), "tools/list").status_code == 200
    (secrets_dir / "allowed-subjects").write_text("")
    clock.advance(60)
    assert_challenge(modern(client, tokens.mint(), "tools/list"))
    (secrets_dir / "allowed-subjects").write_text("owner-test\n")
    clock.advance(60)
    assert modern(client, tokens.mint(), "tools/list").status_code == 200


@pytest.mark.parametrize(
    "scope", [["mail:read"], ["calendar:read"], ["openid"], []], ids=["mail-only", "calendar-only", "neither", "empty"]
)
def test_insufficient_scope_gets_403_with_scope_param(
    client: TestClient, tokens: TokenFactory, scope: list[str]
) -> None:
    response = modern(client, tokens.mint(scope=scope), "tools/list")
    assert_challenge(response, status=403)
    assert 'error="insufficient_scope"' in response.headers["www-authenticate"]


def test_unknown_kid_refetch_throttled(
    client: TestClient, tokens: TokenFactory, jwks_server: HTTPServer, clock: FakeClock
) -> None:
    assert modern(client, tokens.mint(), "tools/list").status_code == 200
    stranger = TokenFactory(TestKey("rotated-kid"))
    clock.advance(61)
    for _ in range(5):
        assert_challenge(modern(client, stranger.mint(), "tools/list"))
    assert sum(1 for request, _ in jwks_server.log if request.path.startswith("/jwks-")) == 2


@pytest.mark.parametrize(
    "value",
    [
        "Bearer ",
        "bearer",
        "Bearer  x.y.z",
        "Bearer abc.def.ghi",
        "Bearer W10.e30.",
        "Basic dXNlcjpwYXNz",
        "Bearer " + "a" * 100_000,
    ],
    ids=["empty", "no-token", "double-space", "not-base64", "header-array", "basic", "huge"],
)
def test_malformed_bearer_values_get_401(client: TestClient, value: str) -> None:
    # In-process (no HTTP server): "huge" proves the verifier path. Through uvicorn, headers above its ~16 KiB limit
    # get 400 from the HTTP server before the app runs; scripts/smoke_image.sh covers 8 KiB -> 401 and 32 KiB -> 400.
    response = modern(client, None, "tools/list", headers={"Authorization": value})
    assert_challenge(response)


def test_jwks_outage_gives_401_and_recovers(
    hub: HubApp, tokens: TokenFactory, jwks_server: HTTPServer, clock: FakeClock, key: TestKey
) -> None:
    jwks_server.clear()
    jwks_server.expect_request("/jwks-7f3a.json").respond_with_data("down", status=503)
    # One client, one lifespan run: the SDK session manager refuses a second run() (research U9). The
    # dispatcher routes the absolute 127.0.0.1:8084 URL to the internal app (scope["server"] from the request URL).
    with TestClient(hub.asgi, base_url="https://mcp.furchert.ch") as c:
        assert_challenge(modern(c, tokens.mint(), "tools/list"))
        assert c.get("http://127.0.0.1:8084/readyz").status_code == 200
        jwks_server.clear()
        jwks_server.expect_request("/jwks-7f3a.json").respond_with_json(jwks(key))
        clock.advance(60)
        assert modern(c, tokens.mint(), "tools/list").status_code == 200


def test_list_accounts_output_and_annotations(client: TestClient, tokens: TokenFactory) -> None:
    token = tokens.mint()
    tool = body(modern(client, token, "tools/list"))["result"]["tools"][0]
    assert tool["annotations"] == {
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
        "title": "List accounts",
    }
    result = body(
        modern(client, token, "tools/call", {"name": "list_accounts", "arguments": {}}, name="list_accounts")
    )["result"]
    assert [a["id"] for a in result["structuredContent"]["accounts"]] == ["icloud", "gmail", "outlook", "uzh"]
