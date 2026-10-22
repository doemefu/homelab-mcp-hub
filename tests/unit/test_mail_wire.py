import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from mcp_hub.app import create_app
from mcp_hub.config import Settings
from mcp_hub.ids import MessageRef
from mcp_hub.logging import configure_logging
from mcp_hub.providers import Adapters
from mcp_hub.providers.base import FolderPage, ListQuery, MailDetail, ProviderError
from mcp_hub.providers.deadline import CallDeadline
from mcp_hub.registry import Account, load_registry
from mcp_hub.tools.mail import LIST_MESSAGES_DESCRIPTION, LIST_UNREAD_DESCRIPTION
from mcp_hub.tools.mail_status import GET_MESSAGE_STATUS_DESCRIPTION
from tests.support.mcp import body, modern
from tests.support.tokens import TokenFactory


class Mailbox:
    def check(self, deadline: CallDeadline) -> None: ...

    def get_message(self, ref: MessageRef, deadline: CallDeadline) -> MailDetail:
        raise ProviderError("not_found")

    def list_messages(self, query: ListQuery, deadline: CallDeadline) -> FolderPage:
        return FolderPage([], more=False, truncated=False, complete=True)


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
    assert set(tools) == {
        "list_accounts", "list_unread", "get_message", "get_events", "list_messages", "get_message_status",
        "get_thread",
    }  # fmt: skip
    for name in ("list_unread", "get_message", "list_messages", "get_thread"):
        assert tools[name]["annotations"]["readOnlyHint"] is True
        assert tools[name]["annotations"]["destructiveHint"] is False
        assert "outputSchema" in tools[name]
        assert "untrusted" in tools[name]["description"]
    assert tools["list_messages"]["description"] == LIST_MESSAGES_DESCRIPTION
    properties = tools["list_messages"]["inputSchema"]["properties"]
    assert set(properties) == {"account", "folder", "since", "unread_only", "direction", "limit", "cursor"}
    assert tools["list_messages"]["inputSchema"].get("required", []) == []
    assert properties["folder"]["enum"] == ["inbox", "sent", "archive", "trash", "all", None]
    assert properties["direction"]["enum"] == ["incoming", "outgoing", None]


def test_get_message_status_schema(wired: TestClient, tokens: TokenFactory) -> None:
    tools = {t["name"]: t for t in body(modern(wired, tokens.mint(), "tools/list"))["result"]["tools"]}
    tool = tools["get_message_status"]
    assert tool["description"] == GET_MESSAGE_STATUS_DESCRIPTION
    assert tool["annotations"] == {
        "title": "Re-check messages",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
    schema = tool["inputSchema"]
    assert set(schema["properties"]) == {"refs"}
    assert schema["required"] == ["refs"]  # C11
    refs = schema["properties"]["refs"]
    assert (refs["type"], refs["items"], refs["minItems"], refs["maxItems"]) == ("array", {"type": "string"}, 1, 50)
    output = tool["outputSchema"]
    assert output["properties"]["next_cursor"] == {"title": "Next Cursor", "type": "null"}
    item = output["$defs"]["StatusItem"]
    assert "input" not in item["required"]  # present only when it differs from `ref`
    assert set(item["required"]) == set(item["properties"]) - {"input"}

    def enum(name: str) -> list[str | None]:
        spec = item["properties"][name]
        return [value for option in spec.get("anyOf", [spec]) for value in option.get("enum", [None])]  # null: None

    assert enum("state") == ["found", "gone", "unknown"]
    assert enum("reason") == [
        "account_error",
        "account_unavailable",
        "id_mismatch",
        "stale_id",
        "incomplete_search",
        None,
    ]
    assert enum("folder") == ["inbox", "sent", "archive", "trash", "other", None]


def test_list_unread_wire_result_has_output_schema(wired: TestClient, tokens: TokenFactory) -> None:
    params = {"name": "list_unread", "arguments": {}}
    result = body(modern(wired, tokens.mint(), "tools/call", params, name="list_unread"))["result"]
    assert result["isError"] is False
    assert json.loads(result["content"][0]["text"]) == result["structuredContent"]
    assert result["structuredContent"]["items"] == []
    assert result["structuredContent"]["next_cursor"] is None


def test_list_unread_input_has_cursor(wired: TestClient, tokens: TokenFactory) -> None:
    tools = {t["name"]: t for t in body(modern(wired, tokens.mint(), "tools/list"))["result"]["tools"]}
    properties = tools["list_unread"]["inputSchema"]["properties"]
    assert set(properties) == {"account", "since", "limit", "cursor"}
    assert tools["list_unread"]["inputSchema"].get("required", []) == []
    assert tools["list_unread"]["description"] == LIST_UNREAD_DESCRIPTION


def test_list_messages_wire_result_matches_its_text(wired: TestClient, tokens: TokenFactory) -> None:
    params = {"name": "list_messages", "arguments": {"folder": "all", "unread_only": True}}
    result = body(modern(wired, tokens.mint(), "tools/call", params, name="list_messages"))["result"]
    assert result["isError"] is False
    assert json.loads(result["content"][0]["text"]) == result["structuredContent"]
    assert result["structuredContent"]["items"] == []
    assert result["structuredContent"]["next_cursor"] is None


def test_list_messages_invalid_folder_is_a_tool_error(wired: TestClient, tokens: TokenFactory) -> None:
    params = {"name": "list_messages", "arguments": {"folder": "junk"}}
    result = body(modern(wired, tokens.mint(), "tools/call", params, name="list_messages"))["result"]
    assert result["isError"] is True
    assert json.loads(result["content"][0]["text"])["code"] == "invalid_argument"


def test_tool_error_body_is_exact_json(wired: TestClient, tokens: TokenFactory) -> None:
    args = {"name": "get_message", "arguments": {"id": "v1.bad!"}}
    result = body(modern(wired, tokens.mint(), "tools/call", args, name="get_message"))["result"]
    assert result["isError"] is True
    assert json.loads(result["content"][0]["text"]) == {"code": "invalid_argument", "message": "Malformed message id"}
