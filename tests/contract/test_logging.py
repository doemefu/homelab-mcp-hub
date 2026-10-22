import json
import logging
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from mcp_hub.app import create_app
from mcp_hub.config import Settings
from mcp_hub.ids import encode_ref
from mcp_hub.logging import JsonFormatter, configure_logging
from mcp_hub.providers import Adapters
from mcp_hub.providers.imap import ImapMailbox
from mcp_hub.registry import Account, load_registry
from tests.support.imap_fake import FakeFolder, FakeImapServer, FakeMessage
from tests.support.logfields import allowed_fields
from tests.support.mcp import body, initialize, modern
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


# Third-party values of the mail-history test data; each must stay out of every log line (spec 080 §9.7).
HISTORY_SENTINELS = ("Sentinel9x", "sentinel9x", "Archive-Sentinel9x", "Sent Messages", "Deleted Messages", "INBOX")


def _history_message(uid: int, tag: str, minutes_ago: int, flags: tuple[bytes, ...] = ()) -> FakeMessage:
    headers = (
        f"Message-ID: <{tag}-sentinel9x@example.org>\r\n"
        "From: Peer Sentinel9x <peer-sentinel9x@example.org>\r\n"
        "To: owner-sentinel9x@example.org\r\n"
        f"Subject: Sentinel9x {tag}\r\n"
        "In-Reply-To: <root-sentinel9x@example.org>\r\n"
        "References: <root-sentinel9x@example.org>\r\n\r\n"
    ).encode()
    when = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=minutes_ago)
    return FakeMessage(uid, when, flags, headers, b"sentinel9x body")


def _history_server() -> FakeImapServer:
    return FakeImapServer(
        [
            FakeFolder("INBOX", messages={1: _history_message(1, "inbox", 5)}),
            FakeFolder("Sent Messages", (b"\\Sent",), messages={1: _history_message(1, "sent", 6, (b"\\Seen",))}),
            FakeFolder("Archive-Sentinel9x", (b"\\Archive",), messages={1: _history_message(1, "archive", 7)}),
            FakeFolder("Deleted Messages", (b"\\Trash",), messages={1: _history_message(1, "trash", 8)}),
        ]
    )


def _call(client: TestClient, tokens: TokenFactory, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    params = {"name": name, "arguments": arguments}
    result: dict[str, Any] = body(modern(client, tokens.mint(), "tools/call", params, name=name))["result"]
    return result


def test_list_messages_log_fields_allowed(settings: Settings, secrets_dir: Path, tokens: TokenFactory) -> None:
    """Spec 080 §9.7 for the WP13a paths, through the MCP endpoint and the IMAP adapter: list_messages over every
    folder value, a cursor page, get_message by `ref` and by `id`, and refused calls log only allowed fields and
    no Message-ID, reference, ref, id, thread key, cursor, folder name, subject or address."""
    server = _history_server()

    def mailbox(account: Account, _dir: Path) -> ImapMailbox:
        return ImapMailbox(
            account.id,
            host="imap.example.test",
            port=993,
            folder="INBOX",
            username="u",
            password="p",
            client_factory=lambda host, port, context, timeout: server.client(),
        )

    configure_logging(settings.log_level)
    hub = create_app(settings, load_registry(secrets_dir / "accounts.json"), adapters=Adapters(mailbox=mailbox))
    handler = Capture()
    logging.getLogger().addHandler(handler)
    try:
        with TestClient(hub.public, base_url="https://mcp.furchert.ch") as client:
            pages = [
                _call(client, tokens, "list_messages", {"account": "icloud", "folder": folder})["structuredContent"]
                for folder in ("inbox", "sent", "archive", "trash", "all")
            ]
            first = _call(client, tokens, "list_messages", {"account": "icloud", "folder": "all", "limit": 1})
            cursor = first["structuredContent"]["next_cursor"]
            arguments = {"account": "icloud", "folder": "all", "limit": 1, "cursor": cursor}
            pages.append(_call(client, tokens, "list_messages", arguments)["structuredContent"])
            refused = _call(client, tokens, "list_messages", arguments | {"folder": "sent"})
            items = [item for page in pages for item in page["items"]]
            opened = [_call(client, tokens, "get_message", {"ref": items[0]["ref"]})["structuredContent"]]
            opened.append(_call(client, tokens, "get_message", {"id": items[1]["id"]})["structuredContent"])
            absent = _call(client, tokens, "get_message", {"ref": encode_ref("icloud", "absent-sentinel9x@x.test")})
    finally:
        logging.getLogger().removeHandler(handler)

    assert cursor
    assert (refused["isError"], absent["isError"]) == (True, True)
    assert {item["folder"] for item in items} == {"inbox", "sent", "archive", "trash"}
    events = [json.loads(line) for line in handler.lines]
    assert all(set(e) <= allowed_fields(e["event"]) for e in events)
    calls = [(e["tool"], e["outcome"]) for e in events if e["event"] == "tool_call"]
    assert calls == [("list_messages", "ok")] * 7 + [
        ("list_messages", "invalid_cursor"),
        ("get_message", "ok"),
        ("get_message", "ok"),
        ("get_message", "not_found"),
    ]
    forbidden = {cursor, "@", "imap.example.test", *HISTORY_SENTINELS}
    for item in [*items, *opened]:
        forbidden.update(value for value in (item["id"], item["ref"], item["thread_key"]) if value)
    text = "\n".join(handler.lines) + "\n".join(message for _, _, message in handler.raw)
    for value in sorted(forbidden):
        assert value not in text, value
