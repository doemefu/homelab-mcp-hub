"""A summary header block cut at its 64 KiB cap is read the same way by the list tools, `get_message` and `get_thread`
(#55 item 1; spec 080 §5.4 "IMAP headers", T9-X; MF-T9-4), and a text part that does not come back counts as not
read (#55 item 2)."""

import json
import logging
from collections.abc import Iterator
from datetime import timedelta

import pytest

from mcp_hub.providers.base import MAX_HEADER_BYTES, ListQuery, MailSummary
from mcp_hub.providers.headers import parse_summary_headers, trim_cut
from mcp_hub.providers.imap_session import summary_headers
from tests.support.imap_fake import PLAIN, FakeMessage
from tests.support.imap_threads import NOW, deadline, icloud, mailbox, mid_ref, thread
from tests.support.logcapture import Capture

PAD_TOKEN = "<r{:05d}@example.test>"


def _block(fields: list[tuple[str, str]], last: tuple[str, str], cut_inside: str) -> bytes:
    """Header lines in this order, padded by a `References` field placed before `last` so that the block reaches the
    cap exactly after `cut_inside` (a prefix of `last`'s value)."""
    lines = "".join(f"{name}: {value}\r\n" for name, value in fields)
    tail = f"{last[0]}: {cut_inside}"
    room = MAX_HEADER_BYTES - len(lines) - len(tail) - len("References: \r\n")
    tokens: list[str] = []
    while room - sum(len(t) + 1 for t in tokens) > 60:
        tokens.append(PAD_TOKEN.format(len(tokens)))
    rest = room - sum(len(t) + 1 for t in tokens)
    tokens.append("<p" + "x" * (rest - len("<p@example.test>")) + "@example.test>")
    block = f"{lines}References: {' '.join(tokens)}\r\n{last[0]}: {last[1]}\r\n\r\n".encode()
    assert block[:MAX_HEADER_BYTES].endswith(tail.encode())
    return block


BASE = [("Subject", "cut"), ("Message-ID", "<m1@example.test>")]
CASES = {
    "to cut inside an angle address": _block(
        [("From", "Sender <sender@example.test>"), *BASE],
        ("To", "a@x.example, Bob <bob@example.test>"),
        "a@x.example, Bob <bob@exa",
    ),
    "to cut inside a quoted name with @": _block(
        [("From", "Sender <sender@example.test>"), *BASE],
        ("To", 'a@x.example, "c@evil.example" <real@example.test>'),
        'a@x.example, "c@evil.example" <real@exa',
    ),
    "from cut": _block(
        [("To", "a@x.example"), *BASE],
        ("From", "Sender <sender@example.test>"),
        "Sender <sender@exa",
    ),
    "message-id ends at the cap": _block(
        [("From", "Sender <sender@example.test>"), ("To", "a@x.example"), ("Subject", "cut")],
        ("Message-ID", "<m1@example.test>"),
        "<m1@example.test>",
    ),
}


def _server(block: bytes) -> object:
    return icloud(inbox=[FakeMessage(1, NOW - timedelta(minutes=5), headers=block, text=b"Hello")])


def _list_item(block: bytes) -> MailSummary:
    server = _server(block)
    page = mailbox(server).list_messages(  # type: ignore[arg-type]
        ListQuery("inbox", NOW - timedelta(days=1), False, None, 50),
        deadline(server),  # type: ignore[arg-type]
    )
    [item] = page.items
    return item


def _shape(item: object) -> tuple[object, ...]:
    meta = item.meta  # type: ignore[attr-defined]
    return (
        item.from_address,  # type: ignore[attr-defined]
        tuple(item.to_addresses),  # type: ignore[attr-defined]
        meta.message_id,
        meta.references,
        meta.thread,
    )


@pytest.mark.parametrize("case", list(CASES))
def test_list_and_get_message_read_a_cut_block_alike(case: str) -> None:
    block = CASES[case]
    server = _server(block)
    detail = mailbox(server).get_message(mid_ref(server, "INBOX", 1, 7), deadline(server))  # type: ignore[arg-type]
    item = _list_item(block)
    assert _shape(item) == _shape(detail)
    expected = {"from cut": None, "message-id ends at the cap": "sender@example.test"}
    assert item.from_address == expected.get(case, "sender@example.test")
    assert item.to_addresses == ("a@x.example",)
    assert item.meta.message_id == (None if case == "message-id ends at the cap" else "m1@example.test")


@pytest.mark.parametrize("case", ["to cut inside an angle address", "to cut inside a quoted name with @", "from cut"])
def test_get_thread_reads_a_cut_block_like_the_list(case: str) -> None:
    block = CASES[case]
    server = _server(block)
    page = thread(server, seed=mid_ref(server, "INBOX", 1, 7))  # type: ignore[arg-type]
    [member] = page.members
    assert _shape(member.summary) == _shape(_list_item(block))


def test_cut_fields_keep_only_what_ended_before_the_cut() -> None:
    to = summary_headers(CASES["to cut inside an angle address"][:MAX_HEADER_BYTES])
    assert to.to == ("a@x.example",)  # neither `Bob` nor a shortened `bob@exa`
    quoted = summary_headers(CASES["to cut inside a quoted name with @"][:MAX_HEADER_BYTES])
    assert quoted.to == ("a@x.example",)  # the quoted name is no address
    sender = summary_headers(CASES["from cut"][:MAX_HEADER_BYTES])
    assert (sender.from_address, sender.from_name) == (None, None)
    mid = summary_headers(CASES["message-id ends at the cap"][:MAX_HEADER_BYTES])
    assert (mid.message_id, mid.root) == (None, "r00000@example.test")


def test_message_id_at_the_cap_also_drops_its_own_root() -> None:
    tail = "\r\nMessage-ID: <m1@example.test>"
    head = "From: s@example.test\r\nSubject: "
    block = (head + "s" * (MAX_HEADER_BYTES - len(head) - len(tail)) + tail).encode()
    assert len(block) == MAX_HEADER_BYTES
    assert parse_summary_headers(trim_cut(block)).root is not None  # the id is its own root before the rule
    parsed = summary_headers(block)
    assert (parsed.message_id, parsed.root) == (None, None)


def test_uncut_block_is_parsed_unchanged() -> None:
    block = b"From: Sender <sender@example.test>\r\nTo: a@x.example, root\r\nMessage-ID: <m1@example.test>\r\n\r\n"
    assert summary_headers(block) == parse_summary_headers(block)


@pytest.fixture
def provider_log() -> Iterator[Capture]:
    handler = Capture()
    logger = logging.getLogger("mcp_hub.providers")
    logger.addHandler(handler)
    try:
        yield handler
    finally:
        logger.removeHandler(handler)


BIG_PLAIN = (*PLAIN[:6], 300_000, *PLAIN[7:])  # a text part above the 256 KiB fetch cap


@pytest.mark.parametrize("structure", [PLAIN, BIG_PLAIN], ids=["small", "above-cap"])
def test_get_message_body_not_returned_counts_as_not_read(structure: object, provider_log: Capture) -> None:
    message = FakeMessage(1, NOW - timedelta(minutes=5), headers=b"Subject: s\r\n\r\n", bodystructure=structure)
    message.text = None  # type: ignore[assignment]  # the server answers the text part with NIL
    server = icloud(inbox=[message])
    detail = mailbox(server).get_message(mid_ref(server, "INBOX", 1, 7), deadline(server))  # type: ignore[arg-type]
    assert (detail.body_text, detail.body_source, detail.body_cut) == ("", "none", False)
    lines = [json.loads(line) for line in provider_log.lines]
    assert [(line["event"], line["exception"]) for line in lines] == [("item_degraded", "BodyNotReturned")]


def test_get_message_empty_body_is_read() -> None:
    message = FakeMessage(1, NOW - timedelta(minutes=5), headers=b"Subject: s\r\n\r\n", text=b"")
    server = icloud(inbox=[message])
    detail = mailbox(server).get_message(mid_ref(server, "INBOX", 1, 7), deadline(server))  # type: ignore[arg-type]
    assert (detail.body_text, detail.body_source) == ("", "text/plain")


def test_get_thread_body_not_returned_is_omitted() -> None:
    message = FakeMessage(1, NOW - timedelta(minutes=5), headers=b"Subject: s\r\nMessage-ID: <m1@example.test>\r\n\r\n")
    message.text = None  # type: ignore[assignment]
    server = icloud(inbox=[message])
    page = thread(server, seed=mid_ref(server, "INBOX", 1, 7))  # type: ignore[arg-type]
    [member] = page.members
    assert (member.body.text, member.body.source, member.body.omitted) == ("", "none", True)
    assert page.truncated
