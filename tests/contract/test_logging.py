import json
import logging
import time
from collections.abc import Iterator

import pytest
from starlette.testclient import TestClient

from mcp_hub.logging import JsonFormatter
from tests.support.logfields import allowed_fields
from tests.support.mcp import initialize, modern
from tests.support.tokens import TokenFactory

ALLOWED_CHECKS = {
    "malformed",
    "algorithm",
    "type",
    "signature",
    "time",
    "issuer",
    "audience",
    "required_claims",
    "client",
    "scope_format",
    "subject",
    "internal",
    "scope",
    "host",
    "origin",
}


# pytest-httpserver's own request log (the test JWKS server, not the hub).
TEST_INFRASTRUCTURE_LOGGERS = frozenset({"werkzeug"})


class Capture(logging.Handler):
    """Keeps the formatted lines and the raw records, so a regression of the logger pins is visible even though
    JsonFormatter masks third-party message text."""

    def __init__(self) -> None:
        super().__init__()
        self.setFormatter(JsonFormatter())
        self.lines: list[str] = []
        self.raw: list[tuple[str, int, str]] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self.format(record))
        self.raw.append((record.name, record.levelno, record.getMessage()))


@pytest.fixture
def captured(client: TestClient) -> Iterator[Capture]:
    handler = Capture()
    logging.getLogger().addHandler(handler)
    yield handler
    logging.getLogger().removeHandler(handler)


def test_logs_contain_check_names_and_no_forbidden_data(
    client: TestClient, tokens: TokenFactory, captured: Capture
) -> None:
    minted = [
        tokens.mint(),
        tokens.mint(header={"typ": "JWT"}),
        tokens.mint(aud=["x"]),
        tokens.mint(sub="someone-else"),
        tokens.mint(scope=["mail:read"]),
        tokens.mint(exp=int(time.time()) - 300),
        tokens.hs256(),
    ]
    for token in minted:
        modern(client, token, "tools/list")
    modern(client, minted[0], "tools/call", {"name": "list_accounts", "arguments": {}}, name="list_accounts")
    initialize(client, minted[0])
    modern(client, minted[0], "tools/list", headers={"Host": "evil.example.org"})
    modern(client, minted[0], "tools/list", headers={"Origin": "https://evil.example.org"})
    modern(client, None, "tools/list", headers={"Authorization": "Bearer raw-sentinel-token"})

    raw = [(name, level, message) for name, level, message in captured.raw if name not in TEST_INFRASTRUCTURE_LOGGERS]
    raw_text = "\n".join(message for _, _, message in raw)
    # The SDK logs raw Host/Origin values at WARNING; its logger is pinned at ERROR (spec 080 §9.7).
    assert not [r for r in raw if r[0].startswith("mcp.server.transport_security") and r[1] < logging.ERROR]
    assert not [r for r in raw if not r[0].startswith("mcp_hub") and r[1] < logging.WARNING]
    for forbidden in ("raw-sentinel-token", "Bearer", "evil.example.org", "/jwks-7f3a", "HTTP Request", "@"):
        assert forbidden not in raw_text, forbidden
    for token in minted:
        assert token not in raw_text

    text = "\n".join(captured.lines)
    for token in minted:
        assert token not in text
        assert token.split(".")[1] not in text
    for forbidden in (
        "raw-sentinel-token",
        "Bearer",
        "evil.example.org",
        "/jwks-7f3a",
        "HTTP Request",
        "@",
        "icloud-app-password",
        "placeholder",
    ):
        assert forbidden not in text, forbidden

    events = [json.loads(line) for line in captured.lines]
    rejected = [e for e in events if e["event"] == "token_rejected"]
    assert {e["check"] for e in rejected} >= {"type", "audience", "subject", "time", "algorithm"}
    assert all(e["check"] in ALLOWED_CHECKS for e in events if "check" in e)
    requests = [e for e in events if e["event"] == "request"]
    assert {e.get("check") for e in requests} >= {"scope", "host", "origin"}
    assert any(e.get("sub") == "owner-test" and e["status"] == 200 for e in requests)
    calls = [e for e in events if e["event"] == "tool_call"]
    assert calls
    assert calls[0]["tool"] == "list_accounts"
    assert calls[0]["sub"] == "owner-test"
    assert all(set(e) <= allowed_fields(e["event"]) for e in events)
