import json
import logging
from datetime import UTC, datetime

import httpx2
import pytest

from mcp_hub.logging import configure_logging
from mcp_hub.providers.base import ProviderError
from mcp_hub.providers.graph import Backoff, GraphMailbox
from tests.support.dav_transport import RecordingTransport
from tests.support.graph_fixtures import FakeTokens, message
from tests.support.logcapture import Capture
from tests.unit.test_graph_adapter import LIST, js

SINCE = datetime(2026, 9, 29, 6, 0, tzinfo=UTC)
SURROGATE = "x\ud800y"


def box(t: RecordingTransport) -> GraphMailbox:
    return GraphMailbox(
        "outlook",
        FakeTokens(),
        client_factory=lambda timeout: httpx2.Client(transport=t.transport(), trust_env=False),
        backoff=Backoff(),
        inbox_ids={},
    )


@pytest.mark.parametrize(
    "bad",
    [
        "not an object",
        {"id": 5},
        {"id": "abc/def"},
        {"id": "A" * 301},
        message(2) | {"receivedDateTime": "garbage"},
        message(2) | {"receivedDateTime": "2026-09-29T07:00:00"},  # no offset
        message(2) | {"from": "not an object", "subject": 123, "bodyPreview": ["x"]},
        message(2) | {"from": {"emailAddress": {"name": 1, "address": {"x": 1}}}},
    ],
)
def test_hostile_items_degrade_alone(bad: object) -> None:
    t = RecordingTransport({LIST: js({"value": [message(1), bad, message(3)]})})
    ids = [getattr(s.ref, "graph_id", None) for s in box(t).list_unread(SINCE, 20).items]
    assert "AAMkMSG0001=" in ids
    assert "AAMkMSG0003=" in ids


def test_lone_surrogates_reach_the_tool_layer_unchanged() -> None:
    hostile = message(1) | {
        "subject": SURROGATE,
        "bodyPreview": SURROGATE,
        "from": {"emailAddress": {"name": SURROGATE, "address": SURROGATE + "@example.test"}},
    }
    t = RecordingTransport({LIST: js({"value": [hostile]})})
    [item] = box(t).list_unread(SINCE, 20).items
    assert item.subject == SURROGATE  # cleaning is the tool layer's job; Task 15 asserts the serialised result


def test_items_already_read_or_too_old_are_dropped() -> None:
    t = RecordingTransport(
        {
            LIST: js(
                {
                    "value": [
                        message(1) | {"isRead": True},
                        message(2) | {"receivedDateTime": "2026-09-28T00:00:00Z"},
                        message(3),
                    ]
                }
            )
        }
    )
    assert [getattr(s.ref, "graph_id", None) for s in box(t).list_unread(SINCE, 20).items] == ["AAMkMSG0003="]


@pytest.mark.parametrize("value", [{"value": "x"}, {"value": None}, {}, {"value": {"a": 1}}])
def test_value_not_a_list_is_account_error(value: dict[str, object]) -> None:
    t = RecordingTransport({LIST: js(value)})
    with pytest.raises(ProviderError) as info:
        box(t).list_unread(SINCE, 20)
    assert (info.value.code, info.value.cause) == ("upstream_error", "MalformedList")


def test_more_than_51_entries_are_not_read() -> None:
    t = RecordingTransport({LIST: js({"value": [message(n) for n in range(1, 500)]})})
    page = box(t).list_unread(SINCE, 50)
    assert len(page.items) == 50
    assert page.more


def test_entries_beyond_51_are_never_inspected() -> None:
    """L7: a hostile entry after the first 51 is not even looked at (no item_degraded line)."""
    configure_logging("DEBUG")
    capture = Capture()
    logging.getLogger().addHandler(capture)
    try:
        values: list[object] = [message(n) for n in range(1, 52)] + ["not an object"]
        page = box(RecordingTransport({LIST: js({"value": values})})).list_unread(SINCE, 50)
    finally:
        logging.getLogger().removeHandler(capture)
    assert len(page.items) == 50
    assert [json.loads(line)["event"] for line in capture.lines].count("item_degraded") == 0


def test_hostile_entry_inside_the_first_51_is_logged_without_an_item_hash() -> None:
    configure_logging("DEBUG")
    capture = Capture()
    logging.getLogger().addHandler(capture)
    try:
        box(RecordingTransport({LIST: js({"value": ["not an object", {"id": "a/b"}]})})).list_unread(SINCE, 20)
    finally:
        logging.getLogger().removeHandler(capture)
    events = [json.loads(line) for line in capture.lines]
    degraded = [e for e in events if e["event"] == "item_degraded"]
    assert [(e["account"], e["capability"], e["exception"]) for e in degraded] == [
        ("outlook", "mail", "MalformedItemError")
    ] * 2
    assert all("item" not in e for e in degraded)
