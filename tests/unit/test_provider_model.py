"""Mail-history summary model, list query types and helpers shared by the adapters (spec 080 §5.1, §8.2)."""

import json
import logging
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone

import pytest

from mcp_hub.ids import GraphMessageRef, MessageRef, ThreadKey, location_k
from mcp_hub.providers import Mailbox
from mcp_hub.providers.base import (
    FolderPage,
    FoundMessage,
    MailDetail,
    MailFlags,
    MailMeta,
    MailSummary,
    MessageStatus,
    direction_of,
    log_message_skipped,
    order_key,
    order_key_of,
    placeholder_summary,
    status_unknown,
)
from tests.support.logcapture import Capture

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def summary(ref: MessageRef | GraphMessageRef, received_at: datetime = T0) -> MailSummary:
    return placeholder_summary(ref, received_at, True)


@pytest.fixture
def provider_log() -> Iterator[Capture]:
    handler = Capture()
    logger = logging.getLogger("mcp_hub.providers")
    logger.addHandler(handler)
    yield handler
    logger.removeHandler(handler)


def test_order_key_total_order() -> None:
    a = summary(MessageRef("icloud", "INBOX", 7, 1), T0)
    b = summary(MessageRef("icloud", "INBOX", 7, 2), T0)
    newer = summary(MessageRef("icloud", "INBOX", 7, 3), datetime(2026, 10, 1, 12, 0, 1, tzinfo=UTC))
    assert order_key(a) == (int(T0.timestamp()), location_k(a.ref))
    assert order_key(a) != order_key(b)
    ordered = sorted([a, b, newer], key=order_key, reverse=True)
    assert ordered[0] is newer
    assert [order_key(x) for x in ordered] == sorted([order_key(x) for x in (a, b, newer)], reverse=True)
    # the same instant in another offset gives the same key
    shifted = summary(a.ref, T0.astimezone(timezone(timedelta(hours=5))))
    assert order_key(shifted) == order_key(a)


def test_order_key_of_is_the_order_key_from_its_parts() -> None:
    ref = MessageRef("icloud", "INBOX", 7, 1)
    assert order_key_of(T0, ref) == order_key(summary(ref, T0)) == (int(T0.timestamp()), location_k(ref))


def test_folder_page_frontier_defaults_to_none() -> None:
    page = FolderPage(items=[], more=False, truncated=False, complete=True)
    assert page.frontier is None
    assert list(FolderPage.__dataclass_fields__)[-1] == "frontier"


@pytest.mark.parametrize(
    ("in_sent", "address", "expected"),
    [
        (True, None, "outgoing"),
        (True, "someone@example.org", "outgoing"),
        (False, "me@example.org", "outgoing"),
        (False, "Me+News@Example.org", "outgoing"),
        (False, "other@example.org", "incoming"),
        (False, None, "incoming"),
    ],
)
def test_direction_of(in_sent: bool, address: str | None, expected: str) -> None:
    own = frozenset({"me@example.org"})
    assert direction_of(in_sent=in_sent, from_address=address, own=own) == expected


def test_old_constructors_still_work() -> None:
    ref = MessageRef("icloud", "INBOX", 7, 1)
    item = MailSummary(ref, T0, True, False, "a@example.org", None, "s", "snippet")
    assert item.meta == MailMeta()
    assert item.to_addresses == ()
    assert item.cc_addresses == ()
    assert MailMeta().flags == MailFlags()
    assert (MailMeta().folder, MailMeta().direction) == ("inbox", "incoming")
    detail = MailDetail(ref, T0, True, [], None, None, [], [], None, "", "none", False)
    assert detail.meta == MailMeta()
    meta = MailMeta(thread=ThreadKey("gmail", "gmail", 5), references=("<a@example.org>",))
    assert placeholder_summary(ref, T0, False, meta).meta is meta


def test_log_message_skipped_includes_item_for_an_encodable_location(provider_log: Capture) -> None:
    log_message_skipped(MessageRef("icloud", "INBOX", 7, 1), "mail", ValueError())
    [line] = [json.loads(x) for x in provider_log.lines]
    assert len(line["item"]) == 12


def test_log_message_skipped_omits_item_for_unencodable_location(provider_log: Capture) -> None:
    log_message_skipped(MessageRef("icloud", "bad\x00name", 7, 1), "mail", ValueError())
    log_message_skipped(MessageRef("icloud", "A" * 500, 7, 1), "mail", ValueError())
    lines = [json.loads(x) for x in provider_log.lines]
    assert len(lines) == 2
    assert all("item" not in line for line in lines)
    assert all(line["exception"] == "ValueError" for line in lines)


def _found() -> FoundMessage:
    return FoundMessage(
        location=MessageRef(account="icloud", folder="INBOX", uidvalidity=1, uid=2),
        message_id="a@example.org",
        received_at=T0,
        unread=True,
        meta=MailMeta(),
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"state": "found"},  # found without a copy
        {"state": "gone", "found": "copy"},
        {"state": "unknown", "found": "copy", "reason": "stale_id"},
        {"state": "found", "found": "copy", "reason": "stale_id"},
        {"state": "gone", "reason": "stale_id"},
        {"state": "unknown"},  # unknown without a reason
        {"state": "gone", "reply_at": T0},
        {"state": "unknown", "reason": "stale_id", "reply_message_id": "r@example.org"},
        {"state": "gone", "newer_incoming_at": T0},
        {"state": "gone", "thread_checked": True},
        {"state": "unknown", "reason": "stale_id", "thread_checked": True},
        {"state": "unknown", "reason": "stale_id", "thread_checked": False},
        {"state": "unknown", "reason": "stale_id", "reply_at": T0},
        {"state": "unknown", "reason": "stale_id", "newer_incoming_at": T0},
        {"state": "found", "found": "copy"},  # found needs thread_checked
        {"state": "found", "found": "copy", "thread_checked": True, "reply_message_id": "r@example.org"},
    ],
)
def test_message_status_invariants(kwargs: dict[str, object]) -> None:
    values = dict(kwargs)
    if values.get("found") == "copy":
        values["found"] = _found()
    with pytest.raises(ValueError, match=r"exactly when|need a found|needs reply_at"):
        MessageStatus(**values)  # type: ignore[arg-type]  # deliberately invalid combinations


def test_message_status_valid_shapes() -> None:
    found = MessageStatus("found", found=_found(), reply_at=T0, newer_incoming_at=T0, thread_checked=False)
    assert found.reason is None
    plain = MessageStatus("found", found=_found(), thread_checked=True)
    assert plain.reply_at is None
    assert MessageStatus("found", found=_found(), reply_at=T0, reply_message_id="r@example.org", thread_checked=True)
    no_id = FoundMessage(location=_found().location, message_id=None, received_at=T0, unread=False, meta=MailMeta())
    assert MessageStatus("found", found=no_id, thread_checked=False).found is no_id
    assert MessageStatus("gone").thread_checked is None
    assert MessageStatus("unknown", "incomplete_search").found is None


def test_status_unknown_helper() -> None:
    status = status_unknown("account_error")
    assert (status.state, status.reason) == ("unknown", "account_error")
    assert status.found is None
    assert status.reply_at is None
    assert status.thread_checked is None


def test_mailbox_protocol_lists_message_status() -> None:
    assert "message_status" in Mailbox.__protocol_attrs__  # type: ignore[attr-defined]  # runtime introspection
