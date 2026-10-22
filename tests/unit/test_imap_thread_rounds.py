"""IMAP `get_thread` on servers other than Gmail: header round (1), round (2) with the upward closure, the re-check of
every hit and what leaves the member list partial or inexact (spec 080 §6.5, §5.2 `get_thread`; WP13c T8, Option 3a,
A-mf1/2/4/4a, E-mf4, G2, G4, IR2, IR3, P7, P11, P12, Q9, T4-1, T7-3)."""

import json
import logging
import re
import time
from collections.abc import Iterator

import pytest

import mcp_hub.providers.imap as imap
from mcp_hub.ids import MessageRef, ThreadKey
from mcp_hub.providers.base import ProviderError, ThreadPage
from mcp_hub.providers.deadline import CallDeadline
from mcp_hub.providers.headers import SUMMARY_HEADER_ITEM, ParsedHeaders, parse_summary_headers
from mcp_hub.providers.imap_session import LOOKUP_FETCH_UIDS, MAX_LOOKUP_HITS, SOFT_CHECK_ROWS
from mcp_hub.providers.imap_thread import MAX_ROUND_ITERATIONS, seed_round_ids
from tests.support.imap_fake import FakeImapClient, FakeImapServer, FakeMessage
from tests.support.imap_threads import (
    NOW,
    clock_of,
    examined,
    folder,
    icloud,
    mailbox,
    mid_ref,
    msg,
    ref,
    thread,
)
from tests.support.logcapture import Capture

OWNER = "Owner <owner@example.org>"
CORE = {"INBOX", "Sent Messages", "Archive", "Deleted Messages"}
KEY = re.compile(rb'HEADER (Message-ID|In-Reply-To|References) "<([^"<>]*)>"')
CAP = 65_536


@pytest.fixture
def provider_log() -> Iterator[Capture]:
    handler = Capture()
    logger = logging.getLogger("mcp_hub.providers")
    logger.addHandler(handler)
    level = logger.level
    logger.setLevel(logging.WARNING)
    yield handler
    logger.removeHandler(handler)
    logger.setLevel(level)


def subjects(page: ThreadPage) -> list[str | None]:
    return [member.summary.subject for member in page.members]


def exact(page: ThreadPage) -> bool:
    assert page.overview is not None
    return page.overview.message_count_exact


def count(page: ThreadPage) -> int:
    assert page.overview is not None
    return page.overview.message_count


def keys(criteria: bytes) -> list[tuple[str, str]]:
    return [(field.decode(), value.decode()) for field, value in KEY.findall(criteria)]


def searched(server: FakeImapServer) -> list[tuple[object, bytes]]:
    """(mailbox selected by the last EXAMINE, criteria) of every UID SEARCH."""
    out: list[tuple[object, bytes]] = []
    current: object = None
    for name, argument in server.commands:
        if name == "EXAMINE":
            current = argument
        elif name == "UID SEARCH":
            out.append((current, argument))
    return out


def round_one(server: FakeImapServer) -> list[tuple[object, bytes]]:
    return [(box, c) for box, c in searched(server) if b"HEADER References" in c]


def round_two(server: FakeImapServer) -> list[tuple[object, bytes]]:
    """Round-(2) searches: after the first round-(1) search, without `References` keys."""
    out: list[tuple[object, bytes]] = []
    started = False
    for box, criteria in searched(server):
        if b"HEADER References" in criteria:
            started = True
        elif started:
            out.append((box, criteria))
    return out


def summary_fetches(server: FakeImapServer, box: str) -> list[list[int]]:
    """UIDs of the summary-header FETCHes in `box`."""
    out: list[list[int]] = []
    current: object = None
    for name, argument in server.commands:
        if name == "EXAMINE":
            current = argument
        elif name == "UID FETCH" and current == box:
            uids, items = argument  # type: ignore[misc]
            if SUMMARY_HEADER_ITEM in items:
                out.append(sorted(uids))
    return out


def fail_in(server: FakeImapServer, box: str, command: str) -> None:
    """Answer the first `command` sent while `box` is selected (or the EXAMINE of `box`) with NO."""
    original = server.run
    state = {"box": None, "done": False}

    def run(name: str, argument: object = None) -> None:
        if name == "EXAMINE":
            state["box"] = argument
        if not state["done"] and name == command and state["box"] == box:
            state["done"] = True
            server.commands.append((name, argument))
            raise server.no(name)
        original(name, argument)

    server.run = run  # type: ignore[method-assign]


def seed_in_inbox(server: FakeImapServer, uid: int = 1) -> MessageRef:
    return mid_ref(server, "INBOX", uid, 7)


# --- round (1) -------------------------------------------------------------------------------------------------


def test_seed_round_ids_order_and_cap() -> None:
    """A-mf1: the root, then In-Reply-To, then the newest References entries until 11; the own id only alone."""
    refs = " ".join(f"<r{i}@example.test>" for i in range(40))
    parsed = parse_summary_headers(
        f"Message-ID: <s@example.test>\r\nIn-Reply-To: <p@example.test>\r\nReferences: {refs}\r\n\r\n".encode()
    )
    assert seed_round_ids(parsed) == [
        "r0@example.test",
        "p@example.test",
        *(f"r{i}@example.test" for i in range(39, 30, -1)),
    ]
    alone = parse_summary_headers(b"Message-ID: <s@example.test>\r\n\r\n")
    assert seed_round_ids(alone) == ["s@example.test"]
    nothing = ParsedHeaders(None, None, (), (), None, None, None, (), None)
    assert seed_round_ids(nothing) == []


def test_round_one_keeps_root_of_forty_references() -> None:
    refs = " ".join(f"<r{i}@example.test>" for i in range(40))
    server = icloud(
        inbox=[msg(1, 10, "seed", message_id="<s@example.test>", in_reply_to="<p@example.test>", references=refs)],
        archive=[msg(2, 300, "root", message_id="<r0@example.test>")],
    )
    page = thread(server, seed=seed_in_inbox(server))
    assert subjects(page) == ["root", "seed"]
    first = round_one(server)
    assert [box for box, _ in first] == ["INBOX", "Sent Messages", "Archive", "Deleted Messages"]  # one per folder
    wire = keys(first[0][1])
    assert len(wire) == 22
    assert list(dict.fromkeys(value for _, value in wire)) == [
        "r0@example.test",
        "p@example.test",
        *(f"r{i}@example.test" for i in range(39, 30, -1)),
    ]
    assert {field for field, _ in wire} == {"Message-ID", "References"}
    assert all(criteria.startswith(b"NOT DRAFT ") for _, criteria in searched(server))


def test_round_one_own_id_when_no_headers() -> None:
    server = icloud(inbox=[msg(1, 10, "seed", message_id="<s@example.test>")])
    page = thread(server, seed=seed_in_inbox(server))
    assert subjects(page) == ["seed"]
    assert keys(round_one(server)[0][1]) == [("Message-ID", "s@example.test"), ("References", "s@example.test")]
    assert exact(page) is True


def test_headers_key_round_one_root_only() -> None:
    server = icloud(
        inbox=[msg(1, 60, "root", message_id="<r0@example.test>")],
        sent=[
            msg(
                2,
                30,
                "reply",
                sender=OWNER,
                message_id="<a@example.test>",
                in_reply_to="<r0@example.test>",
                references="<r0@example.test>",
            ),
        ],
    )
    key = ThreadKey("icloud", "headers", "r0@example.test")
    page = thread(server, key=key)
    assert page.thread == key
    assert subjects(page) == ["root", "reply"]
    assert [member.linked_by for member in page.members] == ["headers", "headers"]
    assert keys(round_one(server)[0][1]) == [("Message-ID", "r0@example.test"), ("References", "r0@example.test")]
    assert exact(page) is True
    assert page.truncated is False


def _cut_seed(*, bracketed: bool) -> tuple[bytes, list[str]]:
    """A seed header whose References run past the 64 KiB item and are cut inside a token; In-Reply-To comes first,
    the Message-ID after the cut. Returns the header and the References ids complete before the cut, in order."""
    head = (
        "From: Sender <sender@example.test>\r\nTo: Owner <owner@example.org>\r\nSubject: seed\r\n"
        "In-Reply-To: <p@example.test>\r\nReferences: "
    )
    for filler in range(40):
        ids = [f"r{i}@example.test" for i in range(4000)]
        tokens = [f"<{v}>" if bracketed else v for v in ids]
        tokens[0] = tokens[0].replace("r0@", "r0" + "f" * filler + "@")
        ids[0] = ids[0].replace("r0@", "r0" + "f" * filler + "@")
        raw = (head + " ".join(tokens) + "\r\nMessage-ID: <s@example.test>\r\n\r\n").encode()
        tail = raw[:CAP].rsplit(b" ", 1)[1]
        if raw[CAP - 1 : CAP + 1].isalnum() and b"@" in tail:  # the cut falls inside an id, after its `@`
            visible = raw[:CAP].decode()
            complete = [v for v in ids if (f"<{v}>" if bracketed else f"{v} ") in visible]
            return raw, complete
    raise AssertionError("no filler puts the cut inside a token")


@pytest.mark.parametrize("bracketed", [True, False])
def test_cut_seed_header_still_rounds_from_root(bracketed: bool) -> None:
    """IR2, MF-P2, P12, T7-3: a cut seed block still feeds round (1) with the root, In-Reply-To and the newest ids
    complete before the cut (never a shortened one), and the count is not exact. A `ref` seed keeps its Message-ID;
    an `id` seed whose Message-ID lay beyond the cut has none."""
    raw, complete = _cut_seed(bracketed=bracketed)
    root = complete[0]
    server = icloud(
        inbox=[FakeMessage(1, NOW, headers=raw)],
        archive=[msg(2, 300, "root", message_id=f"<{root}>")],
    )
    seed = ref("s@example.test", server) if bracketed else seed_in_inbox(server)
    page = thread(server, seed=seed)
    assert subjects(page) == ["root", "seed"]
    wire = list(dict.fromkeys(value for _, value in keys(round_one(server)[0][1])))
    assert wire == [root, "p@example.test", *reversed(complete[-9:])]
    assert exact(page) is False
    assert page.truncated is False
    assert page.members[1].summary.meta.message_id == ("s@example.test" if bracketed else None)


def test_refused_id_skipped_and_inexact() -> None:
    """T4-1: an id the search builder refuses is filtered out before batching; the others are searched and the
    count is not exact."""
    server = icloud(
        inbox=[
            msg(
                1,
                10,
                "seed",
                message_id="<s@example.test>",
                references="<r0@example.test> <X-GM-RAW@example.test> <r2@example.test>",
            ),
            msg(2, 5, "reply", message_id="<c@example.test>", references="<r2@example.test>"),
        ]
    )
    page = thread(server, seed=seed_in_inbox(server))
    assert subjects(page) == ["seed", "reply"]
    values = {value for _, value in keys(round_one(server)[0][1])}
    assert values == {"r0@example.test", "r2@example.test"}
    assert all(b"X-GM-RAW" not in criteria.upper() for _, criteria in searched(server))
    assert exact(page) is False
    assert page.truncated is False


def test_seed_linked_by_headers() -> None:
    """MF-P1: the seed of a `ref` and every member of a `headers` key are linked by "headers"."""

    def build() -> FakeImapServer:
        return icloud(
            inbox=[
                msg(1, 60, "root", message_id="<r@example.test>"),
                msg(
                    2,
                    30,
                    "reply",
                    message_id="<s@example.test>",
                    in_reply_to="<r@example.test>",
                    references="<r@example.test>",
                ),
            ]
        )

    server = build()
    page = thread(server, seed=ref("s@example.test", server))
    assert [(m.summary.subject, m.linked_by) for m in page.members] == [("root", "headers"), ("reply", "headers")]
    server = build()
    page = thread(server, key=ThreadKey("icloud", "headers", "r@example.test"))
    assert [(m.summary.subject, m.linked_by) for m in page.members] == [("root", "headers"), ("reply", "headers")]


@pytest.mark.parametrize("with_id", [True, False])
def test_same_row_from_two_rounds_counted_once(with_id: bool) -> None:
    """P7: a row hit by round (1) (`References` = the root) and again by round (2) (`In-Reply-To` = the seed) is
    read and counted once, with or without a Message-ID."""
    server = icloud(
        inbox=[
            msg(
                1,
                30,
                "seed",
                message_id="<s@example.test>",
                in_reply_to="<r@example.test>",
                references="<r@example.test>",
            ),
            msg(
                2,
                10,
                "reply",
                in_reply_to="<s@example.test>",
                references="<r@example.test>",
                **({"message_id": "<c@example.test>"} if with_id else {}),
            ),
        ]
    )
    page = thread(server, seed=seed_in_inbox(server))
    assert subjects(page) == ["seed", "reply"]
    assert count(page) == 2
    assert exact(page) is True
    assert sum(2 in uids for uids in summary_fetches(server, "INBOX")) == 1  # the second hit is not fetched again
    downward = [c for box, c in round_two(server) if box == "INBOX" and b"In-Reply-To" in c]
    assert ("In-Reply-To", "s@example.test") in keys(downward[0])


def test_complete_headers_key_without_member_not_found() -> None:
    """P12, IR3: a headers key that a complete search finds nowhere is `not_found`; an incomplete one is
    `upstream_timeout`."""
    key = ThreadKey("icloud", "headers", "gone@example.test")
    server = icloud(inbox=[msg(1, 10, "other", message_id="<o@example.test>")])
    with pytest.raises(ProviderError) as caught:
        thread(server, key=key)
    assert caught.value.code == "not_found"
    assert len(round_one(server)) == 4

    server = icloud(inbox=[msg(1, 10, "other", message_id="<o@example.test>")])
    fail_in(server, "Archive", "UID SEARCH")
    with pytest.raises(ProviderError) as caught:
        thread(server, key=key)
    assert caught.value.code == "upstream_timeout"


# --- round (2) -------------------------------------------------------------------------------------------------


def test_iteration_one_includes_seed_id() -> None:
    """A-mf2: a reply whose In-Reply-To names the seed and whose References lack the root is found by the seed's
    own id in iteration 1."""
    server = icloud(
        inbox=[
            msg(1, 90, "root", message_id="<r@example.test>"),
            msg(
                2,
                60,
                "seed",
                message_id="<s@example.test>",
                in_reply_to="<r@example.test>",
                references="<r@example.test>",
            ),
            msg(
                3,
                30,
                "reply",
                message_id="<c@example.test>",
                in_reply_to="<s@example.test>",
                references="<x@example.test>",
            ),
        ]
    )
    page = thread(server, seed=seed_in_inbox(server, 2))
    assert subjects(page) == ["root", "seed", "reply"]
    first = keys(round_two(server)[0][1])
    assert ("In-Reply-To", "s@example.test") in first
    assert exact(page) is True


def test_outlook_follow_up_finds_request_upward() -> None:
    """E-mf4: from the follow-up B (In-Reply-To and References name only A), the owner's reply A (in sent, its
    References name R) and the request R are found: R through an upward Message-ID key."""
    server = icloud(
        inbox=[
            msg(1, 120, "request", message_id="<r@example.test>"),
            msg(
                3,
                30,
                "follow-up",
                message_id="<b@example.test>",
                in_reply_to="<a@example.test>",
                references="<a@example.test>",
            ),
        ],
        sent=[
            msg(
                2,
                60,
                "answer",
                sender=OWNER,
                message_id="<a@example.test>",
                in_reply_to="<r@example.test>",
                references="<r@example.test>",
            ),
        ],
    )
    page = thread(server, seed=seed_in_inbox(server, 3))
    assert subjects(page) == ["request", "answer", "follow-up"]
    assert [m.summary.meta.direction for m in page.members] == ["incoming", "outgoing", "incoming"]
    upward = [c for _, c in round_two(server) if ("Message-ID", "r@example.test") in keys(c)]
    assert upward
    assert exact(page) is True


def test_overflow_keys_go_to_further_searches() -> None:
    """A-mf4a: 25 keys in one iteration go out as two SEARCHes (20 + 5) in the same folder; none is dropped."""
    replies = [
        msg(
            i,
            100 - i,
            f"reply {i}",
            message_id=f"<m{i}@example.test>",
            in_reply_to="<r@example.test>",
            references="<r@example.test>",
        )
        for i in range(2, 26)
    ]
    server = icloud(inbox=[msg(1, 200, "root", message_id="<r@example.test>"), *replies])
    page = thread(server, seed=seed_in_inbox(server))
    assert count(page) == 25
    assert exact(page) is True
    inbox = [keys(c) for box, c in round_two(server) if box == "INBOX"]
    assert [len(k) for k in inbox] == [20, 5]
    assert {field for k in inbox for field, _ in k} == {"In-Reply-To"}
    assert {value for k in inbox for _, value in k} == {"r@example.test", *(f"m{i}@example.test" for i in range(2, 26))}


def test_downward_keys_fill_the_first_search() -> None:
    """A-mf4/4a: with more than 20 keys in one iteration the downward (In-Reply-To) keys go first, so the first
    SEARCH holds only them; the upward keys (each new member's In-Reply-To and root, at most 2) follow in the next."""
    replies = [
        msg(
            i,
            100 - i,
            f"reply {i}",
            message_id=f"<m{i}@example.test>",
            in_reply_to=f"<u{i}@example.test>",
            references="<r@example.test>",
        )
        for i in range(2, 21)
    ]
    server = icloud(
        inbox=[msg(1, 200, "root", message_id="<r@example.test>"), *replies],
        archive=[msg(30, 150, "parent", message_id="<u2@example.test>")],
    )
    page = thread(server, seed=seed_in_inbox(server))
    assert count(page) == 21  # the root, 19 replies and the parent found upward
    assert "parent" in subjects(page)
    assert exact(page) is True
    first, second = [keys(c) for box, c in round_two(server) if box == "INBOX"][:2]
    assert len(first) == 20
    assert {field for field, _ in first} == {"In-Reply-To"}
    assert {field for field, _ in second} == {"Message-ID"}
    assert {value for _, value in second} == {f"u{i}@example.test" for i in range(2, 21)}


def _chain(length: int) -> list[FakeMessage]:
    """A seed and replies that each name only their parent in In-Reply-To (no References)."""
    out = [msg(1, 200, "m0", message_id="<m0@example.test>")]
    for i in range(1, length):
        out.append(
            msg(i + 1, 200 - i, f"m{i}", message_id=f"<m{i}@example.test>", in_reply_to=f"<m{i - 1}@example.test>")
        )
    return out


def test_three_iterations_cap_marks_inexact() -> None:
    """At most 3 iterations: keys left after the third are never searched, so the count is not exact (P11: not
    truncated)."""
    server = icloud(inbox=_chain(6))
    page = thread(server, seed=seed_in_inbox(server))
    assert subjects(page) == ["m0", "m1", "m2", "m3"]
    assert len([c for box, c in round_two(server) if box == "INBOX"]) == MAX_ROUND_ITERATIONS
    assert exact(page) is False
    assert page.truncated is False

    server = icloud(inbox=_chain(3))  # the third iteration finds nothing: complete
    page = thread(server, seed=seed_in_inbox(server))
    assert subjects(page) == ["m0", "m1", "m2"]
    assert exact(page) is True


def test_reserve_spent_stops_new_iteration() -> None:
    """G2: no round starts once the 12 s reserve is spent; the keys left make the list partial (P11)."""
    server = icloud(inbox=_chain(3))
    original = server.run

    def run(name: str, argument: object = None) -> None:
        original(name, argument)
        if name == "UID SEARCH" and not round_one(server)[1:]:
            clock_of(server).advance(12.5)  # once, in the first round-(1) search: past the reserve, not the soft stop

    server.run = run  # type: ignore[method-assign]
    page = thread(server, seed=seed_in_inbox(server))
    assert subjects(page) == ["m0"]
    assert len(round_one(server)) == 4  # a round that started finishes
    assert round_two(server) == []
    assert page.truncated is True
    assert exact(page) is False

    server = icloud(inbox=_chain(3))
    page = thread(server, seed=seed_in_inbox(server), call=_spent(server, 13.0))
    assert subjects(page) == ["m0"]
    assert searched(server) == []
    assert page.truncated is True
    assert exact(page) is False


def _spent(server: FakeImapServer, seconds: float) -> CallDeadline:
    clock = clock_of(server)
    return CallDeadline(clock() - seconds, 16.0, 20.0, clock)


# --- the re-check ----------------------------------------------------------------------------------------------


def test_substring_and_case_hits_are_not_members() -> None:
    """A hit is a member only by whole msg-id tokens compared exactly: substring, case, quoted-string and comment
    hits of a substring server are not."""
    server = icloud(
        inbox=[
            msg(1, 60, "seed", message_id="<s@example.test>"),
            msg(2, 50, "substring", message_id="<x2@example.test>", references="<a<s@example.test>"),
            msg(3, 40, "case", message_id="<x3@example.test>", in_reply_to="<S@EXAMPLE.TEST>"),
            msg(4, 30, "quoted", message_id="<x4@example.test>", references='"<s@example.test>"'),
            msg(5, 20, "comment", message_id="<x5@example.test>", references="(<s@example.test>)"),
            msg(6, 10, "own id case", message_id="<S@Example.Test>"),
        ]
    )
    page = thread(server, seed=seed_in_inbox(server))
    assert subjects(page) == ["seed"]
    assert exact(page) is True
    assert {uid for uids in summary_fetches(server, "INBOX") for uid in uids} >= {2, 3, 4, 5, 6}  # all re-checked


def _long_references(*, wanted_first: bool) -> bytes:
    pad = " ".join(f"<p{i}@example.test>" for i in range(5000))
    refs = f"<s@example.test> {pad}" if wanted_first else f"{pad} <s@example.test>"
    return refs.encode()


def test_cut_block_without_message_id_marks_inexact() -> None:
    """A-sec3: a cut block whose Message-ID did not end before the cut is never a member; the count is not exact."""
    raw = b"From: Sender <sender@example.test>\r\nReferences: " + _long_references(wanted_first=True)
    raw += b"\r\nMessage-ID: <h@example.test>\r\n\r\n"
    server = icloud(
        inbox=[msg(1, 60, "seed", message_id="<s@example.test>")], archive=[FakeMessage(2, NOW, headers=raw)]
    )
    page = thread(server, seed=seed_in_inbox(server))
    assert subjects(page) == ["seed"]
    assert exact(page) is False
    assert page.truncated is False


def test_cut_block_without_wanted_id_marks_inexact() -> None:
    """A cut block that shows no wanted id may hide it beyond the cut: not a member, and the count is not exact."""
    raw = b"Message-ID: <h@example.test>\r\nReferences: " + _long_references(wanted_first=False) + b"\r\n\r\n"
    server = icloud(
        inbox=[msg(1, 60, "seed", message_id="<s@example.test>")], archive=[FakeMessage(2, NOW, headers=raw)]
    )
    page = thread(server, seed=seed_in_inbox(server))
    assert subjects(page) == ["seed"]
    assert exact(page) is False
    assert page.truncated is False


def test_cut_from_gives_direction_by_folder() -> None:
    """A member whose From runs past the cut gets no sender; its direction follows the folder."""
    raw = (
        b"Message-ID: <m@example.test>\r\nIn-Reply-To: <s@example.test>\r\nSubject: cut\r\n"
        b"From: Owner <owner@example.org>,\r\n " + b"x" * 70_000 + b"\r\n\r\n"
    )
    server = icloud(
        inbox=[msg(1, 60, "seed", message_id="<s@example.test>"), FakeMessage(2, NOW, headers=raw)],
    )
    page = thread(server, seed=seed_in_inbox(server))
    assert subjects(page) == ["seed", "cut"]
    member = page.members[1]
    assert member.summary.from_address is None
    assert member.summary.meta.direction == "incoming"  # inbox; the visible From would have said outgoing
    assert member.summary.meta.message_id == "m@example.test"


def test_copies_merge_once_outgoing_if_any_in_sent() -> None:
    """A-mf3, MF-R2: the owner's self-cc in sent and inbox is one outgoing member; an archive and a trash copy of a
    reply are one member."""
    first = {
        "subject": "first",
        "sender": OWNER,
        "to": "Peer <peer@example.test>",
        "cc": OWNER,
        "message_id": "<m0@example.test>",
    }
    reply = {
        "subject": "re",
        "message_id": "<m2@example.test>",
        "in_reply_to": "<m1@example.test>",
        "references": "<m0@example.test> <m1@example.test>",
    }
    server = icloud(
        inbox=[
            msg(1, 90, **first),
            msg(
                2,
                60,
                "seed",
                message_id="<m1@example.test>",
                in_reply_to="<m0@example.test>",
                references="<m0@example.test>",
            ),
        ],
        sent=[msg(1, 90, **first)],
        archive=[msg(3, 30, **reply)],
        trash=[msg(4, 30, **reply)],
    )
    page = thread(server, seed=seed_in_inbox(server, 2))
    assert subjects(page) == ["first", "seed", "re"]
    assert count(page) == 3
    assert [m.summary.meta.direction for m in page.members] == ["outgoing", "incoming", "incoming"]
    assert page.members[0].summary.ref == MessageRef("icloud", "INBOX", 7, 1)  # S-e: inbox before sent on a tie
    assert page.members[2].summary.meta.folder == "archive"  # archive before trash
    assert exact(page) is True


# --- partial and inexact (P11, G4, Q9) -------------------------------------------------------------------------


def _two_members() -> FakeImapServer:
    return icloud(
        inbox=[msg(1, 60, "seed", message_id="<s@example.test>")],
        sent=[
            msg(
                2,
                30,
                "answer",
                sender=OWNER,
                message_id="<a@example.test>",
                in_reply_to="<s@example.test>",
                references="<s@example.test>",
            )
        ],
        archive=[
            msg(
                3,
                10,
                "later",
                message_id="<l@example.test>",
                in_reply_to="<a@example.test>",
                references="<s@example.test> <a@example.test>",
            )
        ],
    )


def test_search_no_marks_inexact_not_error() -> None:
    """G4, P11: a SEARCH answered NO in a member round leaves that folder's part missing: the members found are
    returned, `truncated` and not exact; never a tool error."""
    server = _two_members()
    fail_in(server, "Archive", "UID SEARCH")
    page = thread(server, seed=seed_in_inbox(server))
    assert page.outcome == "ok"
    assert subjects(page) == ["seed", "answer", "later"]  # round (2) searches Archive again and finds it
    assert page.truncated is True
    assert exact(page) is False


def test_examine_no_marks_inexact_not_error() -> None:
    server = _two_members()
    fail_in(server, "Archive", "EXAMINE")
    page = thread(server, seed=seed_in_inbox(server))
    assert page.outcome == "ok"
    assert subjects(page) == ["seed", "answer", "later"]
    assert page.truncated is True
    assert exact(page) is False


class AllHitsClient(FakeImapClient):
    """Every round search in Archive answers every message there, so each is re-checked."""

    def search(self, criteria: bytes) -> list[int]:
        selected = self._selected("UID SEARCH")
        if selected.raw_name == "Archive":
            self._server.run("UID SEARCH", criteria)
            return sorted(selected.messages)
        return super().search(criteria)


def test_hit_cap_marks_inexact(monkeypatch: pytest.MonkeyPatch) -> None:
    """P11: more than 500 hits in one SEARCH: the newest 500 are re-checked; the count is not exact, not truncated."""
    noise = [msg(100 + i, 50, "noise", message_id=f"<n{i}@example.test>") for i in range(MAX_LOOKUP_HITS + 2)]
    server = icloud(inbox=[msg(1, 60, "seed", message_id="<s@example.test>")], archive=noise)
    monkeypatch.setattr(server, "client", lambda: AllHitsClient(server))
    page = thread(server, seed=seed_in_inbox(server))
    assert subjects(page) == ["seed"]
    assert exact(page) is False
    assert page.truncated is False
    fetched = [uid for uids in summary_fetches(server, "Archive") for uid in uids]
    assert min(fetched) == 102  # the newest 500 UIDs
    assert all(len(uids) <= LOOKUP_FETCH_UIDS for uids in summary_fetches(server, "Archive"))


def test_row_without_internaldate_dropped_inexact(provider_log: Capture) -> None:
    """Q9: a member row without INTERNALDATE is dropped with one item_degraded line; the count is not exact."""
    server = icloud(
        inbox=[
            msg(1, 60, "seed", message_id="<s@example.test>"),
            msg(2, None, "undated", message_id="<u@example.test>", in_reply_to="<s@example.test>"),
        ]
    )
    page = thread(server, seed=seed_in_inbox(server))
    assert subjects(page) == ["seed"]
    assert exact(page) is False
    assert page.truncated is False
    assert [json.loads(line)["exception"] for line in provider_log.lines] == ["MissingInternalDate"]


# --- folders ---------------------------------------------------------------------------------------------------


def test_trash_skipped_without_include_trash_seed_kept() -> None:
    """Without `include_trash` the rounds never search trash; a seed read there is kept (SG3, Q1)."""
    server = icloud(
        inbox=[msg(2, 30, "reply", message_id="<a@example.test>", references="<s@example.test>")],
        trash=[
            msg(1, 60, "seed", message_id="<s@example.test>"),
            msg(3, 10, "binned", message_id="<b@example.test>", references="<s@example.test>"),
        ],
    )
    page = thread(server, seed=mid_ref(server, "Deleted Messages", 1, 7), include_trash=False)
    assert subjects(page) == ["seed", "reply"]
    assert all(box != "Deleted Messages" for box, _ in searched(server))
    assert exact(page) is True


def test_never_other_junk_or_drafts() -> None:
    """Only the core folders are searched; a draft hit of a server that ignores `NOT DRAFT` is dropped (S1)."""
    server = icloud(
        inbox=[
            msg(1, 60, "seed", message_id="<s@example.test>"),
            msg(2, 40, "draft", flags=(b"\\Draft",), message_id="<d@example.test>", in_reply_to="<s@example.test>"),
        ],
        other=[
            folder("Projects", (), [msg(5, 30, "filed", message_id="<f@example.test>", in_reply_to="<s@example.test>")])
        ],
    )
    server.honour_not_draft = False
    page = thread(server, seed=seed_in_inbox(server))
    assert subjects(page) == ["seed"]
    assert any(2 in uids for uids in summary_fetches(server, "INBOX"))  # hit and re-checked, then dropped
    assert set(examined(server)) <= CORE
    assert {box for box, _ in searched(server)} <= CORE


# --- CPU -------------------------------------------------------------------------------------------------------

HOSTILE_UNITS = ("<x@y> ", "<x@y>", "(((( ", "<x@y>\r\n ", "a@x.y ", '"<x@y>" ')


def _hostile(uid: int, unit: str) -> FakeMessage:
    head = f"From: Sender <sender@example.test>\r\nMessage-ID: <h{uid}@example.test>\r\nReferences: "
    return FakeMessage(uid, NOW, headers=(head + unit * (CAP // len(unit) + 1)).encode())


def _hostile_server(monkeypatch: pytest.MonkeyPatch) -> FakeImapServer:
    blocks = [_hostile(uid, HOSTILE_UNITS[uid % len(HOSTILE_UNITS)]) for uid in range(2, MAX_LOOKUP_HITS + 2)]
    server = icloud(inbox=[msg(1, 60, "seed", message_id="<s@example.test>")], archive=blocks)
    monkeypatch.setattr(server, "client", lambda: AllHitsClient(server))
    return server


def test_round_parse_cpu_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    """sec-G5, SR2: 500 hostile 64 KiB blocks per round search are parsed with a soft check at least every 10 rows,
    and on the real clock the call ends within 3 s, or within 1 s of the soft stop."""
    server = _hostile_server(monkeypatch)
    checks: list[None] = []
    original = imap._check_soft

    def counted(deadline: CallDeadline) -> None:
        checks.append(None)
        original(deadline)

    monkeypatch.setattr(imap, "_check_soft", counted)
    page = thread(server, seed=seed_in_inbox(server))
    assert subjects(page) == ["seed"]
    assert exact(page) is False  # every cut block may hide a wanted id
    fetched = summary_fetches(server, "Archive")
    assert all(len(uids) <= LOOKUP_FETCH_UIDS for uids in fetched)
    assert len(checks) >= sum((len(uids) - 1) // SOFT_CHECK_ROWS for uids in fetched) > 0

    server = _hostile_server(monkeypatch)
    soft = 1.0
    box = mailbox(server)
    started = time.perf_counter()
    page = thread(
        server, seed=seed_in_inbox(server), box=box, call=CallDeadline(started, soft, 20.0, time.perf_counter)
    )
    elapsed = time.perf_counter() - started
    assert page.outcome == "ok"
    assert elapsed <= min(3.0, soft + 1.0), elapsed
