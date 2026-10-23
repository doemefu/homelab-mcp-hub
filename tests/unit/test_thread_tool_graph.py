"""get_thread through the tool with the real GraphMailbox on a MockTransport (WP13c final review M-4): the wire result,
the error mapping, the provider_call_failed and tool_call order, and no value in any log line."""

import json
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx2
import pytest
from starlette.testclient import TestClient

from mcp_hub.app import create_app
from mcp_hub.config import Settings
from mcp_hub.ids import GraphMessageRef, ThreadKey, encode_message_id, encode_ref, encode_thread_key
from mcp_hub.logging import configure_logging
from mcp_hub.providers import Adapters
from mcp_hub.providers.folders import FolderMapCache
from mcp_hub.providers.graph import Backoff, GraphMailbox
from mcp_hub.registry import Account, load_registry
from tests.support.clock import FakeClock
from tests.support.graph_fixtures import FOLDER_IDS, FakeTokens, body_object, thread_item
from tests.support.logcapture import Capture
from tests.support.logfields import allowed_fields
from tests.support.mcp import body, modern
from tests.support.tokens import TokenFactory

PATH = "/v1.0/me/messages"
FOUND = "AAQkTHREADFOUND="  # a conversation with uniqueBody on the list
FALLBACK = "AAQkTHREADFALLBACK="  # the uniqueBody list answers 400: re-read, then single reads, one failing
EMPTY = "AAQkTHREADEMPTY="  # no member
FORBIDDEN = "AAQkTHREADFORBIDDEN="  # 403 on the list
SLOW_MID = "t9@example.test"  # its lookup answers after the soft stop


def item(n: int, conversation: str, **extra: object) -> dict[str, object]:
    return thread_item(n, conversationId=conversation, subject=f"SUBJECT-{n}", **extra)


ITEMS = [
    item(1, FOUND, uniqueBody=body_object("BODY-ONE")),
    item(2, FOUND, uniqueBody=body_object("BODY-TWO")),
    item(3, FALLBACK),
    item(4, FALLBACK),
    item(9, FOUND),
]


def graph_json(value: object, status: int = 200) -> httpx2.Response:
    return httpx2.Response(status, content=json.dumps(value).encode(), headers={"Content-Type": "application/json"})


class GraphStub:
    """Graph by path and `$filter`; `clock` moves past the soft stop while the slow seed lookup answers."""

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        path = request.url.path
        if path.startswith("/v1.0/me/mailFolders/"):
            return graph_json({"id": FOLDER_IDS[path.rsplit("/", 1)[1]]})
        if path.startswith(PATH + "/"):
            if path.endswith("AAMkMSG0004="):
                return graph_json({"error": {"code": "x"}}, 404)  # moved away during the call
            return graph_json({"uniqueBody": body_object(f"READ-{path.rsplit('/', 1)[1]}")})
        text = request.url.params["$filter"]
        if text.startswith("internetMessageId eq "):
            if SLOW_MID in text:
                self.clock.advance(17.0)  # the lookup completes; the list would start after the soft stop
            return graph_json(
                {"value": [i for i in ITEMS if f"internetMessageId eq '{i['internetMessageId']}'" in text]}
            )
        conversation = text.split("conversationId eq '", 1)[1].rstrip("'")
        unique = request.url.params["$select"].endswith(",uniqueBody")
        if conversation == FORBIDDEN:
            return graph_json({"error": {"code": "x"}}, 403)
        if conversation == FALLBACK and unique:
            return graph_json({"error": {"code": "x"}}, 400)
        members = [i for i in ITEMS if i["conversationId"] == conversation]
        members = [i if unique else {k: v for k, v in i.items() if k != "uniqueBody"} for i in members]
        return graph_json({"value": sorted(members, key=lambda i: str(i["receivedDateTime"]), reverse=True)})


@pytest.fixture
def captured() -> Iterator[Capture]:
    handler = Capture()
    logging.getLogger().addHandler(handler)
    try:
        yield handler
    finally:
        logging.getLogger().removeHandler(handler)


def call(client: TestClient, tokens: TokenFactory, arguments: dict[str, Any]) -> dict[str, Any]:
    params = {"name": "get_thread", "arguments": arguments}
    return body(modern(client, tokens.mint(), "tools/call", params, name="get_thread"))["result"]  # type: ignore[no-any-return]  # JSON-RPC result


def key(conversation: str) -> str:
    value = encode_thread_key(ThreadKey("outlook", "graph", conversation))
    assert value is not None
    return value


def ref(message_id: str) -> str:
    value = encode_ref("outlook", message_id)
    assert value is not None
    return value


def error_of(result: dict[str, Any]) -> tuple[bool, dict[str, str]]:
    return result["isError"], json.loads(result["content"][0]["text"])


def test_graph_adapter_behind_get_thread(
    settings: Settings, secrets_dir: Path, tokens: TokenFactory, captured: Capture
) -> None:
    for name in ("outlook-ms-client-id", "db-username", "db-password", "token-encryption-key"):
        (secrets_dir / name).write_text("placeholder")
    clock = FakeClock()
    stub = GraphStub(clock)
    maps = FolderMapCache()

    def factory(account: Account, _secrets_dir: Path) -> GraphMailbox:
        assert account.id == "outlook"
        return GraphMailbox(
            "outlook",
            FakeTokens(),
            client_factory=lambda timeout: httpx2.Client(transport=httpx2.MockTransport(stub.handle), trust_env=False),
            clock=clock,
            backoff=Backoff(),
            folder_maps=maps,
        )

    configure_logging(settings.log_level)
    logging.getLogger().addHandler(captured)  # configure_logging replaced the root handlers
    hub = create_app(settings, load_registry(secrets_dir / "accounts.json"), adapters=Adapters(mailbox=factory))
    with TestClient(hub.public, base_url="https://mcp.furchert.ch") as client:
        found = call(client, tokens, {"ref": ref("t2@example.test")})
        fallback = call(client, tokens, {"thread_key": key(FALLBACK)})
        empty = call(client, tokens, {"thread_key": key(EMPTY)})
        forbidden = call(client, tokens, {"thread_key": key(FORBIDDEN)})
        slow = call(client, tokens, {"ref": ref(SLOW_MID)})

    assert found["isError"] is False
    content = found["structuredContent"]
    assert (content["account"], content["thread_key"], content["thread_key_source"]) == (
        "outlook",
        key(FOUND),
        "server",
    )
    assert (content["truncated"], content["account_errors"]) == (False, [])
    assert content["summary"]["message_count"] == 3
    assert content["summary"]["message_count_exact"] is True
    messages = content["messages"]
    assert [m["id"] for m in messages] == [
        encode_message_id(GraphMessageRef("outlook", f"AAMkMSG000{n}=")) for n in (1, 2, 9)
    ]
    assert [m["untrusted"]["body"] for m in messages] == ["BODY-ONE", "BODY-TWO", "Unique 9"]
    assert {(m["linked_by"], m["quote_removal"], m["body_original_chars"]) for m in messages} == {
        ("server", "server", None)
    }

    assert fallback["isError"] is False
    content = fallback["structuredContent"]
    assert content["truncated"] is True  # one body not read
    assert content["summary"]["message_count_exact"] is True
    bodies = [(m["untrusted"]["body"], m["body_omitted"]) for m in content["messages"]]
    assert bodies == [("READ-AAMkMSG0003=", False), ("", True)]

    assert error_of(empty) == (True, {"code": "not_found", "message": "Item not found"})
    assert error_of(forbidden) == (True, {"code": "upstream_error", "message": "Provider returned an error"})
    # A soft stop after the seed lookup and before the list (documented, final review M-2 ruling (a)).
    assert error_of(slow) == (True, {"code": "upstream_timeout", "message": "Provider did not answer in time"})

    events = [json.loads(line) for line in captured.lines]
    logged = [e for e in events if e["event"] in {"provider_call_failed", "tool_call"}]
    assert [(e["event"], e.get("outcome"), e.get("exception")) for e in logged] == [
        ("tool_call", "ok", None),
        ("provider_call_failed", "upstream_error", "UnexpectedStatus"),  # the 400 list fallback (SG1), once
        ("provider_call_failed", "not_found", "GraphNotFound"),  # the failed single read, the call goes on
        ("tool_call", "ok", None),
        ("tool_call", "not_found", None),  # absence after a complete list: no provider failure
        ("provider_call_failed", "upstream_error", "Forbidden"),
        ("tool_call", "upstream_error", None),
        ("provider_call_failed", "upstream_timeout", "SoftStop"),
        ("tool_call", "upstream_timeout", None),
    ]
    assert [e["result_count"] for e in logged if e["event"] == "tool_call"] == [3, 2, 0, 0, 0]
    assert all(set(e) <= allowed_fields(e["event"]) for e in events)
    text = "\n".join(captured.lines)
    for forbidden_value in [
        FOUND,
        FALLBACK,
        EMPTY,
        FORBIDDEN,
        "AAQkTHREAD",
        "AAMkMSG",
        "example.test",
        "SUBJECT-",
        "BODY-",
        "READ-",
        "Unique",
        ref("t2@example.test"),
        ref(SLOW_MID),
        *[key(c) for c in (FOUND, FALLBACK, EMPTY, FORBIDDEN)],
        *[m["id"] for m in messages],
        *FOLDER_IDS.values(),
        "AT-SENTINEL",
        "graph.microsoft.com",
    ]:
        assert forbidden_value not in text, forbidden_value
