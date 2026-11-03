"""Metrics on the internal port (spec 080 §9.7 rev 4.17, §10.3): every refused request counts exactly one check, a
request without a token counts nothing, tool calls count by tool and outcome, and `/metrics` is served on 8084 only."""

import json
import time
from collections.abc import Callable

import pytest
from starlette.testclient import TestClient

from mcp_hub.app import MAX_REQUEST_BODY_BYTES, HubApp
from mcp_hub.metrics import CONTENT_TYPE, REJECTION_CHECKS, TOOL_NAMES
from tests.support.mcp import ACCEPT, MODERN, auth, body, modern
from tests.support.tokens import TokenFactory


def scrape(hub: HubApp) -> dict[str, float]:
    with TestClient(hub.internal) as internal:
        response = internal.get("/metrics")
    assert response.status_code == 200
    return {
        series: float(value)
        for series, value in (line.rsplit(" ", 1) for line in response.text.splitlines() if not line.startswith("#"))
    }


def rejections(hub: HubApp) -> dict[str, float]:
    return {c: scrape(hub)[f'mcp_hub_token_rejections_total{{check="{c}"}}'] for c in REJECTION_CHECKS}


def counted_once(hub: HubApp, request: Callable[[], object], check: str) -> None:
    before = rejections(hub)
    request()
    after = rejections(hub)
    assert {c: after[c] - before[c] for c in REJECTION_CHECKS if after[c] != before[c]} == {check: 1.0}


@pytest.mark.parametrize(
    ("mint", "check"),
    [
        (lambda t: t.mint(header={"typ": "JWT"}), "type"),
        (lambda t: t.mint(header={"typ": None}), "type"),
        (lambda t: t.mint(iss="https://auth.example.org"), "issuer"),
        (lambda t: t.mint(aud=["claude-mcp-hub"]), "audience"),
        (lambda t: t.mint(client_id="furchert-ch"), "client"),
        (lambda t: t.mint(drop=("client_id",)), "required_claims"),  # PyJWT's `require` runs before the client check
        (lambda t: t.mint(exp=int(time.time()) - 90), "time"),
        (lambda t: t.hs256(), "algorithm"),
        (lambda t: t.alg_none(), "algorithm"),
        (lambda t: t.mint(sub="someone-else"), "subject"),
        (lambda t: t.mint(drop=("scope",)), "required_claims"),
        (lambda t: t.mint(scope=42), "scope_format"),
        (lambda t: "not-a-jwt", "malformed"),
    ],
    ids=[
        "typ-JWT",
        "typ-missing",
        "iss",
        "aud",
        "client-id",
        "client-id-missing",
        "expired",
        "hs256",
        "none",
        "sub-unknown",
        "scope-missing",
        "scope-format",
        "malformed",
    ],
)
def test_each_rejected_token_counts_its_check(
    hub: HubApp, client: TestClient, tokens: TokenFactory, mint: Callable[[TokenFactory], str], check: str
) -> None:
    counted_once(hub, lambda: modern(client, mint(tokens), "tools/list"), check)


def test_empty_bearer_value_counts_as_malformed(hub: HubApp, client: TestClient) -> None:
    counted_once(hub, lambda: modern(client, None, "tools/list", headers={"Authorization": "Bearer "}), "malformed")


@pytest.mark.parametrize(
    ("change", "check"),
    [
        ({"scope": ["mail:read"]}, "scope"),
        ({"headers": {"Host": "evil.example.org"}}, "host"),
        ({"headers": {"Origin": "https://evil.example.org"}}, "origin"),
    ],
    ids=["scope-403", "host-421", "origin-403"],
)
def test_refusals_after_authentication_count_their_check(
    hub: HubApp, client: TestClient, tokens: TokenFactory, change: dict[str, object], check: str
) -> None:
    token = tokens.mint(scope=change["scope"]) if "scope" in change else tokens.mint()
    headers = change.get("headers")
    counted_once(hub, lambda: modern(client, token, "tools/list", headers=headers), check)  # type: ignore[arg-type]


def test_body_size_refusal_counts_once(hub: HubApp, client: TestClient, tokens: TokenFactory) -> None:
    headers = {"Accept": ACCEPT, "Content-Type": "application/json", "MCP-Protocol-Version": MODERN}
    headers |= {"Mcp-Method": "tools/list"} | auth(tokens.mint())
    content = b"x" * (MAX_REQUEST_BODY_BYTES + 1)
    counted_once(hub, lambda: client.post("/mcp", headers=headers, content=content), "body_size")


@pytest.mark.parametrize("authorization", [None, "Basic dXNlcjpwYXNz", "bearer"], ids=["none", "basic", "no-space"])
def test_requests_without_a_bearer_token_count_nothing(
    hub: HubApp, client: TestClient, authorization: str | None
) -> None:
    before = rejections(hub)
    headers = {"Authorization": authorization} if authorization is not None else {}
    assert modern(client, None, "tools/list", headers=headers).status_code == 401
    assert rejections(hub) == before


def test_tool_calls_count_by_tool_and_outcome(hub: HubApp, client: TestClient, tokens: TokenFactory) -> None:
    ok = 'mcp_hub_tool_calls_total{tool="list_accounts",outcome="ok"}'
    refused = 'mcp_hub_tool_calls_total{tool="get_message",outcome="invalid_argument"}'
    before = scrape(hub)
    token = tokens.mint()
    assert modern(client, token, "tools/call", {"name": "list_accounts", "arguments": {}}, name="list_accounts")
    called = modern(client, token, "tools/call", {"name": "get_message", "arguments": {}}, name="get_message")
    assert body(called)["result"]["isError"] is True
    after = scrape(hub)
    assert (after[ok] - before[ok], after[refused] - before[refused]) == (1.0, 1.0)
    changed = {
        series for series in after if series.startswith("mcp_hub_tool_calls_total") and after[series] != before[series]
    }
    assert changed == {ok, refused}


def test_tool_enum_matches_the_registered_tools(client: TestClient, tokens: TokenFactory) -> None:
    listed = body(modern(client, tokens.mint(), "tools/list"))["result"]["tools"]
    assert sorted(tool["name"] for tool in listed) == sorted(TOOL_NAMES)


def test_metrics_on_the_internal_port_only(hub: HubApp, client: TestClient) -> None:
    with TestClient(hub.internal) as internal:
        response = internal.get("/metrics")
        assert response.headers["content-type"] == CONTENT_TYPE
        assert response.text.endswith("\n")
        assert internal.post("/metrics").status_code == 405
    assert client.get("/metrics").status_code == 404


def test_status_gauge_is_one_hot_per_account_and_capability(hub: HubApp) -> None:
    series = scrape(hub)
    gauge = {name: value for name, value in series.items() if name.startswith("mcp_hub_account_status")}
    groups: dict[str, list[float]] = {}
    for name, value in gauge.items():
        key = name.split(',status="')[0]
        groups.setdefault(key, []).append(value)
    assert groups
    assert all(sorted(values) == [0.0] * 5 + [1.0] for values in groups.values())
    assert json.dumps(sorted(groups)).count("@") == 0
