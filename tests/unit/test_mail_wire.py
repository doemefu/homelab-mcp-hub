import json
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from mcp_hub.app import create_app
from mcp_hub.config import Settings
from mcp_hub.ids import MessageRef
from mcp_hub.logging import configure_logging
from mcp_hub.providers import Adapters
from mcp_hub.providers.base import MailDetail, ProviderError, UnreadPage
from mcp_hub.registry import Account, load_registry
from tests.support.mcp import body, modern
from tests.support.tokens import TokenFactory


class Mailbox:
    def check(self) -> None: ...

    def list_unread(self, since: datetime, limit: int) -> UnreadPage:
        return UnreadPage([], False)

    def get_message(self, ref: MessageRef) -> MailDetail:
        raise ProviderError("not_found")


def fake_mailbox(account: Account, secrets_dir: Path) -> Mailbox:
    return Mailbox()


@pytest.fixture
def wired(settings: Settings, secrets_dir: Path) -> Iterator[TestClient]:
    configure_logging(settings.log_level)
    hub = create_app(settings, load_registry(secrets_dir / "accounts.json"), adapters=Adapters(mailbox=fake_mailbox))
    with TestClient(hub.public, base_url="https://mcp.furchert.ch") as client:
        yield client


def test_mail_tools_are_listed_read_only_with_output_schema(wired: TestClient, tokens: TokenFactory) -> None:
    tools = {t["name"]: t for t in body(modern(wired, tokens.mint(), "tools/list"))["result"]["tools"]}
    assert set(tools) == {"list_accounts", "list_unread", "get_message", "get_events"}
    for name in ("list_unread", "get_message"):
        assert tools[name]["annotations"]["readOnlyHint"] is True
        assert tools[name]["annotations"]["destructiveHint"] is False
        assert "outputSchema" in tools[name]
        assert "untrusted" in tools[name]["description"]


def test_list_unread_wire_result_has_output_schema(wired: TestClient, tokens: TokenFactory) -> None:
    params = {"name": "list_unread", "arguments": {}}
    result = body(modern(wired, tokens.mint(), "tools/call", params, name="list_unread"))["result"]
    assert result["isError"] is False
    assert json.loads(result["content"][0]["text"]) == result["structuredContent"]
    assert result["structuredContent"]["items"] == []
    assert result["structuredContent"]["next_cursor"] is None


def test_tool_error_body_is_exact_json(wired: TestClient, tokens: TokenFactory) -> None:
    args = {"name": "get_message", "arguments": {"id": "v1.bad!"}}
    result = body(modern(wired, tokens.mint(), "tools/call", args, name="get_message"))["result"]
    assert result["isError"] is True
    assert json.loads(result["content"][0]["text"]) == {"code": "invalid_argument", "message": "Malformed message id"}
