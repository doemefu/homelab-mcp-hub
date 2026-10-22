"""IMAP `get_message_status` on Gmail (spec 080 §5.2 "Replies and thread, IMAP", §6.5; WP13b T10): the reply and
thread lookups by `X-GM-THRID` in All Mail (Gmail without `\\All`: in INBOX, `\\Trash` and `\\Sent`). A reply is a
`\\Sent` thread member whose re-checked In-Reply-To/References name the message (A-mf5 (b)); the All Mail header
search (a) runs only while GMAIL_REPLY_HEADER_SEARCH is on, which T0 P1 turned off."""

import json
import logging
from collections.abc import Callable, Iterator, Sequence
from datetime import UTC, datetime, timedelta, timezone

import pytest

from mcp_hub.ids import MessageRef, StableRef
from mcp_hub.providers import imap_status
from mcp_hub.providers.base import MessageStatus, ProviderError
from mcp_hub.providers.deadline import SoftStop
from mcp_hub.providers.headers import DESCENDANT_HEADER_BYTES, DESCENDANT_HEADER_ITEM
from mcp_hub.providers.imap_search import MAX_THRID_VALUES, criteria, thrid_or
from mcp_hub.providers.imap_status import LOOKUP_FETCH_UIDS, MAX_LOOKUP_HITS, SOFT_CHECK_ROWS
from tests.support.imap_fake import FakeImapClient, FakeImapServer, FakeMessage
from tests.support.logcapture import Capture
from tests.unit.test_imap_get_message import (
    GMAIL_ALL,
    GMAIL_SENT,
    GMAIL_TRASH,
    INBOX_L,
    MID,
    OWN,
    SENT_L,
    deadline,
    examined,
    gmail,
    hdr,
    mailbox,
    msg,
    names,
    searches,
)
from tests.unit.test_imap_status import (
    A_MID,
    F_MID,
    FOUND,
    HOSTILE_MID,
    OWNER,
    PADDING,
    _block,
    ago,
    copy_flags,
    direction,
    facts,
    m,
    run,
    states,
)

GMAIL = "gmail"
THR = 7001
SENDER = "Sender <sender@example.test>"
FETCH_ITEMS = ["FLAGS", "INTERNALDATE", "X-GM-LABELS", "X-GM-THRID", DESCENDANT_HEADER_ITEM]


def g(message_id: str = MID) -> StableRef:
    return StableRef(GMAIL, message_id)


def x_mail(
    uid: int = 1, minutes: float = 180, *labels: bytes, thrid: int | None = THR, **kwargs: object
) -> FakeMessage:
    """X in All Mail with `\\Inbox`, received three hours ago."""
    return msg(uid, minutes, "x", INBOX_L, *labels, thrid=thrid, message_id=f"<{MID}>", **kwargs)  # type: ignore[arg-type]


def member(
    uid: int,
    minutes: float,
    *labels: bytes,
    message_id: str | None = F_MID,
    to: str | None = MID,
    thrid: int | None = THR,
    sender: str = SENDER,
    flags: tuple[bytes, ...] = (),
) -> FakeMessage:
    """A thread member; with `to` it names that Message-ID in In-Reply-To and References."""
    fields = {"sender": sender}
    if message_id is not None:
        fields["message_id"] = f"<{message_id}>"
    if to is not None:
        fields["in_reply_to"] = fields["references"] = f"<{to}>"
    return msg(uid, minutes, "re", *labels, flags=flags, thrid=thrid, **fields)


def reply(
    uid: int = 5, minutes: float = 60, *labels: bytes, message_id: str | None = A_MID, **kwargs: object
) -> FakeMessage:
    """The owner's reply to X, labelled `\\Sent`."""
    return member(uid, minutes, SENT_L, *labels, message_id=message_id, sender=OWNER, **kwargs)  # type: ignore[arg-type]


def thread_searches(server: FakeImapServer) -> list[bytes]:
    return [key for key in searches(server) if b"X-GM-THRID" in key]  # type: ignore[operator]


def header_lookups(server: FakeImapServer) -> list[bytes]:
    return [key for key in searches(server) if b"In-Reply-To" in key or b"References" in key]  # type: ignore[operator]


def lookups(server: FakeImapServer) -> list[tuple[int, str, str, object]]:
    """(index, command, mailbox selected, argument) of every Gmail lookup SEARCH and member FETCH."""
    out: list[tuple[int, str, str, object]] = []
    current = ""
    for index, (name, argument) in enumerate(server.commands):
        if name == "EXAMINE":
            current = str(argument)
        elif (name == "UID SEARCH" and b"Message-ID" not in argument) or (  # type: ignore[operator]
            name == "UID FETCH" and DESCENDANT_HEADER_ITEM in argument[1]  # type: ignore[index]
        ):
            out.append((index, name, current, argument))
    return out


def lookup_boxes(server: FakeImapServer) -> list[str]:
    return [box for _, name, box, _ in lookups(server) if name == "UID SEARCH"]


def member_fetches(server: FakeImapServer) -> list[tuple[str, list[int]]]:
    return [(box, list(arg[0])) for _, name, box, arg in lookups(server) if name == "UID FETCH"]  # type: ignore[index]


def run_direct(
    server: FakeImapServer, targets: Sequence[StableRef | MessageRef], soft_check: Callable[[], None]
) -> list[MessageStatus]:
    box = mailbox(server, account=GMAIL)
    return box._run(lambda s: imap_status.message_status(s, targets, soft_check=soft_check), deadline(server))


@pytest.fixture
def header_search_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(imap_status, "GMAIL_REPLY_HEADER_SEARCH", True)


class NoHeaderMatchClient(FakeImapClient):
    """Gmail as probed (T0 P1): the reply header search runs but finds nothing."""

    def search(self, criteria: bytes) -> list[int]:
        if b"In-Reply-To" in criteria:
            self._server.run("UID SEARCH", criteria)
            return []
        return super().search(criteria)


class EveryHeaderHitClient(FakeImapClient):
    """The reply header search answers every message of the mailbox (a server that ignores the label key)."""

    def search(self, criteria: bytes) -> list[int]:
        if b"In-Reply-To" in criteria:
            self._server.run("UID SEARCH", criteria)
            return sorted(self._selected("UID SEARCH").messages)
        return super().search(criteria)


class EveryMemberClient(FakeImapClient):
    """X-GM-THRID searches answer every message of the mailbox (an over-matching server)."""

    def search(self, criteria: bytes) -> list[int]:
        if b"X-GM-THRID" in criteria:
            self._server.run("UID SEARCH", criteria)
            return sorted(self._selected("UID SEARCH").messages)
        return super().search(criteria)


def _fail_thread_search(server: FakeImapServer, nth: int) -> None:
    """The `nth` X-GM-THRID SEARCH (from 1) answers NO."""
    original = server.run
    count = {"n": 0}

    def run_(name: str, argument: object = None) -> None:
        if name == "UID SEARCH" and b"X-GM-THRID" in argument:  # type: ignore[operator]
            count["n"] += 1
            if count["n"] == nth:
                server.fail["UID SEARCH"] = server.no("UID SEARCH")
        original(name, argument)

    server.run = run_  # type: ignore[method-assign]


def _fail_examine(server: FakeImapServer, mailbox_name: str, which: frozenset[int] = frozenset({2})) -> None:
    """The EXAMINEs of `mailbox_name` numbered in `which` (from 1) answer NO; by default the second, the lookup's
    (the location opened it first)."""
    original = server.run
    count = {"n": 0}

    def run_(name: str, argument: object = None) -> None:
        if name == "EXAMINE" and argument == mailbox_name:
            count["n"] += 1
            if count["n"] in which:
                server.fail["EXAMINE"] = server.no("EXAMINE")
        original(name, argument)

    server.run = run_  # type: ignore[method-assign]


def _lossy(server: FakeImapServer, uid: int) -> None:
    """Every member FETCH loses the row of `uid` (expunged meanwhile)."""

    class Lossy(FakeImapClient):
        def fetch(self, messages: object, items: object) -> dict[int, dict[bytes, object]]:
            rows = super().fetch(messages, items)  # type: ignore[arg-type]
            if DESCENDANT_HEADER_ITEM in items:  # type: ignore[operator]
                rows.pop(uid, None)
            return rows

    server.client = lambda: Lossy(server)  # type: ignore[method-assign]


# --- search shapes ----------------------------------------------------------------------------------------------


def test_gmail_status_sends_no_header_lookup() -> None:
    """T0 P1: GMAIL_REPLY_HEADER_SEARCH is off; the only HEADER key on Gmail is the location's Message-ID, and the
    reply comes from the `\\Sent` thread member."""
    assert imap_status.GMAIL_REPLY_HEADER_SEARCH is False
    server = gmail(all_mail=[x_mail(), reply(5, 60), member(6, 30)])
    [status] = run(server, [g()])
    assert facts(status) == (ago(60), A_MID, ago(30), True)
    location = b'NOT DRAFT HEADER Message-ID "<' + MID.encode() + b'>"'
    assert searches(server) == [location, location, b"NOT DRAFT X-GM-THRID 7001"]  # Trash, All Mail, thread
    assert header_lookups(server) == []


def test_gmail_header_search_off_uses_thread_members_only() -> None:
    """A-mf5 (b) alone: a reply to the follow-up Y (In-Reply-To Y, References X Y) answers X too; no reply SEARCH."""
    y = "y1@example.test"
    fields = {"message_id": f"<{A_MID}>", "in_reply_to": f"<{y}>", "references": f"<{MID}> <{y}>"}
    answer = msg(5, 60, "re", SENT_L, thrid=THR, sender=OWNER, **fields)
    server = gmail(all_mail=[x_mail(), member(2, 120, INBOX_L, message_id=y), answer])
    x_status, y_status = run(server, [g(), g(y)])
    assert facts(x_status) == (ago(60), A_MID, ago(120), True)
    assert facts(y_status) == (ago(60), A_MID, None, True)
    assert header_lookups(server) == []
    assert thread_searches(server) == [b"NOT DRAFT X-GM-THRID 7001"]  # both refs share the thread: one key


def test_gmail_header_search_on_shape_and_union(header_search_on: None) -> None:
    """Option 17 on: the All Mail search with the `\\Sent` key (never the `sent` list filter); `reply_at` is the
    earliest of the header hits (a) and the `\\Sent` thread members (b)."""
    # (a) finds the reply A in another thread (a changed subject), (b) the later reply B in X's thread
    b_mid = "b1@example.test"
    server = gmail(all_mail=[x_mail(), reply(4, 90, thrid=7002), reply(5, 60, message_id=b_mid)])
    [status] = run(server, [g()])
    assert facts(status) == (ago(90), A_MID, None, True)
    token = b'"<' + MID.encode() + b'>"'
    assert header_lookups(server) == [
        b'NOT DRAFT X-GM-LABELS "\\\\Sent" OR HEADER In-Reply-To ' + token + b" HEADER References " + token
    ]
    assert lookup_boxes(server) == [GMAIL_ALL, GMAIL_ALL]  # reply, then thread, in the All Mail EXAMINE
    reply_fetch = next(arg for _, name, _, arg in lookups(server) if name == "UID FETCH")
    assert list(reply_fetch[1]) == ["FLAGS", "INTERNALDATE", "X-GM-LABELS", DESCENDANT_HEADER_ITEM]  # type: ignore[index]

    # the header search counts only `\\Sent` hits that name X, never a copy of X
    server = gmail(all_mail=[x_mail(), member(4, 90, message_id=A_MID, thrid=7002), reply(5, 80, message_id=MID)])
    assert facts(run(server, [g()])[0]) == (None, None, None, True)

    # MF4: an incoming copy of the reply A found by (a) is not newer, although A is outside X's thread
    alias = "Alias <alias@example.test>"
    server = gmail(all_mail=[x_mail(), reply(4, 90, thrid=7002), member(6, 50, message_id=A_MID, sender=alias)])
    assert facts(run(server, [g()])[0]) == (ago(90), A_MID, None, True)


def test_gmail_header_search_cap_keeps_lowest_uids(header_search_on: None) -> None:
    """C9: more than 500 header hits → the lowest 500 UIDs (the earliest reply) are re-checked, the ref unchecked."""
    replies = [reply(uid, 170 - uid / 10, message_id=f"a{uid}@example.test", thrid=7002) for uid in range(2, 503)]
    server = gmail(all_mail=[x_mail(1, 180), *replies])
    [status] = run(server, [g()])
    assert facts(status) == (ago(170 - 2 / 10), "a2@example.test", None, False)


def test_gmail_header_search_examine_no_marks_unchecked(header_search_on: None) -> None:
    """§6.5: an EXAMINE NO in the reply step leaves the refs unchecked, even when the thread step opens All Mail."""
    server = gmail(inbox=[msg(1, 180, "x", thrid=THR, message_id=f"<{MID}>")], all_mail=[member(6, 30)])
    _fail_examine(server, GMAIL_ALL, frozenset({1, 2}))  # the location's and the reply step's
    [status] = run(server, [m("INBOX", 1, account=GMAIL)])
    assert facts(status) == (None, None, ago(30), False)
    assert lookup_boxes(server) == [GMAIL_ALL]  # the thread SEARCH only


def test_gmail_reply_from_sent_thread_member(header_search_on: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """A-mf5 (b): found although the header search misses it (Gmail does not match References, T0 P1)."""
    server = gmail(all_mail=[x_mail(), reply(5, 60)])
    monkeypatch.setattr(server, "client", lambda: NoHeaderMatchClient(server))
    [status] = run(server, [g()])
    assert facts(status) == (ago(60), A_MID, None, True)
    assert len(header_lookups(server)) == 1

    # a server that ignores the `\\Sent` key: the hub re-checks the label, so an incoming hit is no reply
    server = gmail(all_mail=[x_mail(), member(5, 60, message_id=A_MID, thrid=7002)])
    monkeypatch.setattr(server, "client", lambda: EveryHeaderHitClient(server))
    assert facts(run(server, [g()])[0]) == (None, None, None, True)


def test_gmail_thread_search_shape_and_twenty_keys() -> None:
    """At most 20 X-GM-THRID keys per SEARCH (P5), batched by `thrid_batches`, no SINCE (Option 13); the members are
    fetched with labels, thread id and the 64 KiB header item in chunks of at most 50."""
    xs = [f"x{i}@example.test" for i in range(21)]
    server = gmail(
        all_mail=[msg(i + 1, 200, "x", INBOX_L, thrid=THR + i, message_id=f"<{x}>") for i, x in enumerate(xs)]
    )
    statuses = run(server, [g(x) for x in xs])
    assert [s.thread_checked for s in statuses] == [True] * 21
    first = [THR + i for i in range(MAX_THRID_VALUES)]
    assert thread_searches(server) == [criteria(b"NOT DRAFT", thrid_or(first)), b"NOT DRAFT X-GM-THRID 7021"]
    assert [key.count(b"X-GM-THRID") for key in thread_searches(server)] == [20, 1]
    assert all(b"SINCE" not in key and b"BEFORE" not in key for key in searches(server))  # type: ignore[operator]
    items = {tuple(arg[1]) for _, name, _, arg in lookups(server) if name == "UID FETCH"}  # type: ignore[index]
    assert items == {tuple(FETCH_ITEMS)}
    assert all(len(uids) <= LOOKUP_FETCH_UIDS for _, uids in member_fetches(server))


def test_gmail_one_examine_of_all_mail_for_reply_and_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    """Design A: the location searched Trash, then All Mail; the reply and thread steps run in that EXAMINE."""
    for on in (False, True):
        monkeypatch.setattr(imap_status, "GMAIL_REPLY_HEADER_SEARCH", on)
        server = gmail(all_mail=[x_mail(), reply(5, 60), member(6, 30)], trash=[member(7, 20, message_id="t@x.y")])
        [status] = run(server, [g()])
        assert facts(status) == (ago(60), A_MID, ago(30), True)  # the Trash member is outside All Mail (§5.2)
        assert examined(server) == [GMAIL_TRASH, GMAIL_ALL]
        assert set(lookup_boxes(server)) == {GMAIL_ALL}
        assert names(server).count("LIST") == 1


# --- matching ---------------------------------------------------------------------------------------------------


def test_gmail_reply_needs_sent_label_and_reference() -> None:
    """MF T11: a reply needs the `\\Sent` label and X among its whole In-Reply-To/References tokens; a copy of X
    is never a reply (C8); a `\\Sent` member that does not name X is no reply and never newer incoming. A forward
    that names X (Gmail web keeps it in the thread with `References: <X>`) counts as a reply: the accepted residual
    (A-gr1, MF G5)."""
    forward = msg(5, 60, "Fwd: x", SENT_L, thrid=THR, sender=OWNER, message_id=f"<{A_MID}>", references=f"<{MID}>")
    cases = [
        ([reply(5, 60)], (ago(60), A_MID, None, True)),
        ([forward], (ago(60), A_MID, None, True)),
        ([reply(5, 60, to=None)], (None, None, None, True)),  # threaded by subject, names nothing
        ([reply(5, 60, to="m1@example.testing")], (None, None, None, True)),  # a longer id, never a substring
        ([member(5, 60, message_id=A_MID)], (None, None, ago(60), True)),  # names X, but no `\\Sent`: incoming
        ([reply(5, 60, message_id=MID)], (None, None, None, True)),  # a self-sent copy of X
        ([reply(5, 60, message_id=None)], (ago(60), None, None, True)),  # no valid Message-ID: no reply_ref
    ]
    for members, expected in cases:
        server = gmail(all_mail=[x_mail(), *members])
        assert facts(run(server, [g()])[0]) == expected, members


def test_gmail_newer_incoming_strictly_after() -> None:
    """§5.2: Gmail compares time strictly, as instants: an older or simultaneous member is not newer, whatever its
    offset; the newest later member gives `newer_incoming_at`."""
    x_at = datetime(2026, 10, 21, 6, 0, tzinfo=UTC)

    def at(minutes: float, hours: int) -> datetime:
        return (x_at + timedelta(minutes=minutes)).astimezone(timezone(timedelta(hours=hours)))

    def mail(uid: int, when: datetime, message_id: str, *labels: bytes) -> FakeMessage:
        headers = hdr("re", message_id=f"<{message_id}>", in_reply_to=f"<{MID}>")
        return FakeMessage(uid, when, headers=headers, labels=labels, thrid=THR)

    x = FakeMessage(1, at(0, 2), headers=hdr("x", message_id=f"<{MID}>"), labels=(INBOX_L,), thrid=THR)
    older = mail(2, at(-30, 3), "older@example.test", INBOX_L)  # 08:30+03:00 looks later than 08:00+02:00
    same = mail(3, at(0, -1), "same@example.test")
    later = mail(4, at(30, -2), "later@example.test")  # 04:30-02:00 looks earlier
    server = gmail(all_mail=[x, older, same])
    [status] = run(server, [g()])
    assert facts(status) == (None, None, None, True)
    server = gmail(all_mail=[x, older, same, later])
    [status] = run(server, [g()])
    assert facts(status) == (None, None, x_at + timedelta(minutes=30), True)

    # MF6: an id whose earliest copy is older than X is not newer, although a later copy is
    server = gmail(all_mail=[x, mail(2, at(-30, 0), "dup@example.test"), mail(3, at(60, 0), "dup@example.test")])
    assert facts(run(server, [g()])[0]) == (None, None, None, True)
    # members without a Message-ID count on their own time: the newest later one
    undated = [
        FakeMessage(uid, at(minutes, 0), headers=hdr("re"), thrid=THR)
        for uid, minutes in ((5, -30), (6, 20), (7, 40), (8, 10))
    ]
    server = gmail(all_mail=[x, *undated])
    assert facts(run(server, [g()])[0]) == (None, None, x_at + timedelta(minutes=40), True)
    server = gmail(all_mail=[x, undated[0]])
    assert facts(run(server, [g()])[0]) == (None, None, None, True)


def test_gmail_inbox_and_sent_member_is_not_incoming() -> None:
    """MF4, MF-R1, MF3: a member labelled `\\Inbox` and `\\Sent`, an own From, a copy of the owner's reply (even
    before the `\\Sent` copy in UID order) and a later copy of X are never newer incoming."""
    cases = [
        ([reply(5, 60, INBOX_L)], OWN, (ago(60), A_MID, None, True)),
        ([member(5, 60, INBOX_L, message_id="own@example.test", sender=OWNER)], OWN, (None, None, None, True)),
        (
            [member(5, 60, INBOX_L, message_id="own@example.test", sender=OWNER)],
            frozenset(),
            (None, None, ago(60), True),
        ),
        (
            [member(4, 50, INBOX_L, message_id=A_MID, sender="Alias <alias@example.test>"), reply(5, 60)],
            frozenset(),
            (ago(60), A_MID, None, True),
        ),
        ([member(5, 60, INBOX_L, message_id=MID)], frozenset(), (None, None, None, True)),
    ]
    for members, own, expected in cases:
        server = gmail(all_mail=[x_mail(), *members])
        assert facts(run(server, [g()], own=own)[0]) == expected, members

    # X found in Trash, a later copy of X in All Mail: still a copy, never newer (MF3)
    server = gmail(trash=[x_mail(3, 180)], all_mail=[member(5, 60, INBOX_L, message_id=MID)])
    [status] = run(server, [g()])
    assert (status.found is not None and status.found.meta.folder, facts(status)) == ("trash", (None, None, None, True))


def test_gmail_hand_over_both_sides() -> None:
    """The hand-over on Gmail: X arrives, the owner replies A (Cc a colleague), the colleague answers B with
    `In-Reply-To: <A>` and `References: <X> <A>`. X is replied and continued; A, as a ref of its own, is outgoing,
    not replied (A is no reply to itself) and continued through B; X is older than A, so not newer for A."""
    b = FakeMessage(
        2,
        ago(30),
        headers=_block(
            "From: Colleague <colleague@example.test>",
            "Cc: Owner <owner@example.org>",
            f"Message-ID: <{F_MID}>",
            f"In-Reply-To: <{A_MID}>",
            f"References: <{MID}> <{A_MID}>",
        ),
        labels=(INBOX_L,),
        thrid=THR,
    )
    server = gmail(all_mail=[x_mail(), b, reply(5, 60)])
    x_status, a_status = run(server, [g(), g(A_MID)])
    assert facts(x_status) == (ago(60), A_MID, ago(30), True)
    assert facts(a_status) == (None, None, ago(30), True)
    assert (a_status.found is not None and a_status.found.meta.folder, direction(a_status)) == ("sent", "outgoing")
    assert thread_searches(server) == [b"NOT DRAFT X-GM-THRID 7001"]


def test_gmail_shared_thread_one_key() -> None:
    """Refs of one thread share one X-GM-THRID key; each ref is compared with its own time and Message-ID."""
    x2 = "x2@example.test"
    server = gmail(all_mail=[x_mail(1, 180), member(2, 90, INBOX_L, message_id=x2), member(3, 120, message_id=F_MID)])
    first, second, again = run(server, [g(), g(x2), g()])
    assert facts(first) == (None, None, ago(90), True)  # X2 is a newer incoming member of X's thread
    assert facts(second) == (None, None, None, True)  # F (120 min) is older than X2
    assert facts(again) == facts(first)
    assert thread_searches(server) == [b"NOT DRAFT X-GM-THRID 7001"]


def test_gmail_draft_label_any_case_dropped() -> None:
    """S1: a member with a draft label (any case) or flag is never a reply or newer incoming."""
    drafts = [
        reply(5, 60, b"\\Draft"),
        member(6, 50, b"\\draft", message_id="d1@example.test"),
        member(7, 40, b"\\DRAFTS", message_id="d2@example.test"),
        member(8, 30, message_id="d3@example.test", flags=(b"\\Draft",)),
    ]
    server = gmail(all_mail=[x_mail(), *drafts])
    server.honour_not_draft = False  # the server ignores NOT DRAFT: the hub re-check drops them
    assert facts(run(server, [g()])[0]) == (None, None, None, True)


# --- without All Mail, missing ids --------------------------------------------------------------------------------


def test_gmail_without_all_mail_uses_sent_inbox_trash(monkeypatch: pytest.MonkeyPatch) -> None:
    """E-im3a with T0: X-GM-THRID in `\\Sent` (still selected, no EXAMINE), INBOX and `\\Trash`; a member of the
    `\\Sent` mailbox is sent without the label; `thread_checked` stays true."""
    own_copy = member(3, 60, message_id=A_MID, sender="Alias <alias@example.test>")  # its INBOX copy, no label
    server = gmail(
        with_all=False,
        inbox=[msg(1, 180, "x", thrid=THR, message_id=f"<{MID}>"), own_copy],
        sent=[member(5, 60, message_id=A_MID, sender=OWNER)],
        trash=[member(7, 30)],
    )
    [status] = run(server, [g()])
    assert facts(status) == (ago(60), A_MID, ago(30), True)
    assert examined(server) == ["INBOX", GMAIL_TRASH, GMAIL_SENT, "INBOX", GMAIL_TRASH]
    assert lookup_boxes(server) == [GMAIL_SENT, "INBOX", GMAIL_TRASH]
    assert header_lookups(server) == []

    # with the header search on: in `\\Sent`, without the label key
    monkeypatch.setattr(imap_status, "GMAIL_REPLY_HEADER_SEARCH", True)
    server = gmail(with_all=False, inbox=[msg(1, 180, "x", thrid=THR, message_id=f"<{MID}>")])
    run(server, [g()])
    token = b'"<' + MID.encode() + b'>"'
    assert header_lookups(server) == [b"NOT DRAFT OR HEADER In-Reply-To " + token + b" HEADER References " + token]
    assert lookup_boxes(server) == [GMAIL_SENT, GMAIL_SENT, "INBOX", GMAIL_TRASH]

    # no `\\Sent` mailbox either: replies cannot be read (C5); the thread lookup still runs in INBOX and `\\Trash`
    server = gmail(with_all=False, inbox=[msg(1, 180, "x", thrid=THR, message_id=f"<{MID}>")], trash=[member(7, 30)])
    server.folders = [f for f in server.folders if f.raw_name != GMAIL_SENT]
    assert facts(run(server, [g()])[0]) == (None, None, ago(30), False)
    assert lookup_boxes(server) == [GMAIL_TRASH, "INBOX"]  # Trash still selected after the location


def test_gmail_no_message_id_still_runs_thread_step() -> None:
    """E-mf3: a copy without a valid Message-ID has its reply part unchecked (`replied` from `\\Answered`), but its
    thread is searched; the copy itself is not newer (the same instant)."""
    answered = (b"\\Answered",)
    server = gmail(
        inbox=[msg(1, 180, "no id", flags=answered, thrid=THR)],
        all_mail=[msg(9, 180, "no id", INBOX_L, flags=answered, thrid=THR), reply(5, 60), member(6, 30)],
    )
    [status] = run(server, [m("INBOX", 1, account=GMAIL)])
    assert facts(status) == (None, None, ago(30), False)
    assert copy_flags(status)[0] is True
    assert thread_searches(server) == [b"NOT DRAFT X-GM-THRID 7001"]


def test_gmail_copy_without_thread_id_unchecked() -> None:
    """T0: a found copy without a valid X-GM-THRID is no thread key; `replied` comes from `\\Answered` alone."""
    other = "x2@example.test"
    server = gmail(
        all_mail=[
            x_mail(thrid=None, flags=(b"\\Answered",)),
            reply(5, 60),
            msg(2, 120, "x2", INBOX_L, thrid=7002, message_id=f"<{other}>"),
            member(6, 30, thrid=7002, to=other),
        ]
    )
    first, second = run(server, [g(), g(other)])
    assert facts(first) == (None, None, None, False)
    assert copy_flags(first)[0] is True
    assert facts(second) == (None, None, ago(30), True)
    assert thread_searches(server) == [b"NOT DRAFT X-GM-THRID 7002"]

    # no copy with a thread id: no lookup mailbox is opened (Gmail without `\\All`: Sent stays selected)
    server = gmail(with_all=False, inbox=[msg(1, 180, "x", message_id=f"<{MID}>")], trash=[member(7, 30)])
    assert facts(run(server, [g()])[0]) == (None, None, None, False)
    assert examined(server) == ["INBOX", GMAIL_TRASH, GMAIL_SENT]  # the location's only
    assert lookups(server) == []


def test_gmail_self_sent_x_is_outgoing_and_found() -> None:
    """MF-R2 stays: X labelled `\\Sent` is outgoing; it is no reply to itself."""
    server = gmail(all_mail=[x_mail(1, 180, SENT_L), member(6, 30)])
    [status] = run(server, [g()])
    assert (direction(status), facts(status)) == ("outgoing", (None, None, ago(30), True))


# --- caps, failures, soft stop ------------------------------------------------------------------------------------


def test_gmail_hit_cap_keeps_highest_uids_and_unchecked() -> None:
    """C9: more than 500 members → the highest 500 UIDs are re-checked and the batch is unchecked."""
    members = [member(uid, 100 - uid / 10, message_id=f"f{uid}@example.test") for uid in range(3, MAX_LOOKUP_HITS + 3)]
    server = gmail(all_mail=[x_mail(1, 180), reply(2, 170), *members])
    [status] = run(server, [g()])
    fetched = [uid for _, uids in member_fetches(server) for uid in uids]
    assert (len(fetched), min(fetched), max(fetched)) == (MAX_LOOKUP_HITS, 3, MAX_LOOKUP_HITS + 2)
    assert facts(status) == (None, None, ago(100 - (MAX_LOOKUP_HITS + 2) / 10), False)  # UID 2's reply not read


@pytest.mark.parametrize("case", ["search_no_batch_2_of_2", "fetch_loses_a_row", "examine_no_inbox"])
def test_gmail_lookup_failures_mark_only_their_batch(case: str) -> None:
    """B7: a failed batch marks only its refs; positives read stay; no map rebuild (§6.5)."""
    xs = [f"x{i}@example.test" for i in range(21)]  # thread batches 7001..7020 | 7021
    with_all = case != "examine_no_inbox"
    xs_mail = [msg(i + 1, 200, "x", INBOX_L, thrid=THR + i, message_id=f"<{x}>") for i, x in enumerate(xs)]
    followers = [member(100, 30, thrid=THR, to=xs[0]), member(101, 30, thrid=THR + 20, message_id="f20@x.y", to=xs[20])]
    if with_all:
        server = gmail(all_mail=[*xs_mail, reply(50, 60, thrid=THR + 20, to=xs[20]), *followers])
    else:
        server = gmail(with_all=False, inbox=[*xs_mail, *followers], sent=[reply(50, 60, thrid=THR + 20, to=xs[20])])
    if case == "search_no_batch_2_of_2":
        _fail_thread_search(server, 2)
    elif case == "fetch_loses_a_row":
        _lossy(server, 100)
    else:
        _fail_examine(server, "INBOX")
    statuses = run(server, [g(x) for x in xs])
    assert states(statuses) == [FOUND] * 21
    checked = [s.thread_checked for s in statuses]
    if case == "search_no_batch_2_of_2":
        assert checked == [True] * 20 + [False]
        assert facts(statuses[0]) == (None, None, ago(30), True)
        assert facts(statuses[20]) == (None, None, None, False)
    elif case == "fetch_loses_a_row":
        assert checked == [False] * 20 + [True]
        assert facts(statuses[20]) == (ago(60), A_MID, ago(30), True)
    else:  # the `\\Sent` batches ran first; INBOX refused; Trash still searched
        assert checked == [False] * 21
        assert facts(statuses[20]) == (ago(60), A_MID, None, False)  # the reply read stays
        assert lookup_boxes(server) == [GMAIL_SENT, GMAIL_SENT, GMAIL_TRASH, GMAIL_TRASH]
    assert names(server).count("LIST") == 1


def test_gmail_member_thread_id_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    """A member of another thread (an over-matching server) is ignored; one without X-GM-THRID cannot be attributed
    and leaves its batch unchecked."""
    server = gmail(all_mail=[x_mail(), member(5, 30, thrid=7999), member(6, 20, thrid=7999, message_id="o@x.y")])
    monkeypatch.setattr(server, "client", lambda: EveryMemberClient(server))
    assert facts(run(server, [g()])[0]) == (None, None, None, True)
    server = gmail(all_mail=[x_mail(), member(5, 30, thrid=None)])
    monkeypatch.setattr(server, "client", lambda: EveryMemberClient(server))
    assert facts(run(server, [g()])[0]) == (None, None, None, False)


def test_gmail_missing_header_item_marks_unchecked() -> None:
    server = gmail(all_mail=[x_mail(), FakeMessage(5, ago(30), headers=None, thrid=THR)])
    assert facts(run(server, [g()])[0]) == (None, None, None, False)


def test_gmail_cut_member_rules() -> None:
    """A-sec3, MF-R4: a cut incoming member needs a Message-ID and a From that ended before the cut; a cut `\\Sent`
    member counts as a reply only when X lies before the cut, else the ref is unchecked."""
    sender = f"From: {SENDER}"
    incoming = [
        (_block(sender, f"Message-ID: <{F_MID}>", f"References: {PADDING}"), (None, None, ago(30), True)),
        (_block(f"Message-ID: <{F_MID}>", f"References: {PADDING}", sender), (None, None, None, False)),  # From cut
        (_block(sender, f"References: {PADDING}", f"Message-ID: <{F_MID}>"), (None, None, None, False)),  # id cut
    ]
    for headers, expected in incoming:
        server = gmail(all_mail=[x_mail(), FakeMessage(6, ago(30), headers=headers, thrid=THR)])
        assert facts(run(server, [g()])[0]) == expected

    shown = _block(f"Message-ID: <{A_MID}>", f"References: <{MID}> {PADDING}")
    hidden = _block(f"Message-ID: <{A_MID}>", f"References: {PADDING} <{MID}>")
    server = gmail(all_mail=[x_mail(), FakeMessage(5, ago(60), headers=shown, labels=(SENT_L,), thrid=THR)])
    assert facts(run(server, [g()])[0]) == (ago(60), A_MID, None, True)
    server = gmail(all_mail=[x_mail(), FakeMessage(5, ago(60), headers=hidden, labels=(SENT_L,), thrid=THR)])
    assert facts(run(server, [g()])[0]) == (None, None, None, False)

    # T4-3: an unbracketed References cut inside a longer id that begins with X ("<X>ing") shows no X
    head = f"Message-ID: <{A_MID}>\r\nReferences: "
    filler = "q" * (DESCENDANT_HEADER_BYTES - len(head) - 1 - len(MID) - len("@example.test")) + "@example.test"
    raw = (head + filler + " " + MID + "ing\r\n\r\n").encode()
    assert raw[:DESCENDANT_HEADER_BYTES].endswith(MID.encode())
    server = gmail(all_mail=[x_mail(), FakeMessage(5, ago(60), headers=raw, labels=(SENT_L,), thrid=THR)])
    assert facts(run(server, [g()])[0]) == (None, None, None, False)


def test_gmail_soft_stop_during_lookups_keeps_positives(monkeypatch: pytest.MonkeyPatch) -> None:
    """IR2: a stop inside the second thread batch leaves the first batch's refs checked and the rest unchecked; a
    stop in the header search leaves every ref unchecked. Never an account error."""
    xs = [f"x{i}@example.test" for i in range(21)]

    def build() -> FakeImapServer:
        server = gmail(
            all_mail=[
                *(msg(i + 1, 200, "x", INBOX_L, thrid=THR + i, message_id=f"<{x}>") for i, x in enumerate(xs)),
                reply(50, 60, thrid=THR, to=xs[0]),
                member(51, 30, thrid=THR + 20, to=xs[20]),
            ]
        )
        server.step_seconds = 1.0
        return server

    dry = build()
    run(dry, [g(x) for x in xs], soft=1000.0, hard=2000.0)
    second = [index for index, name, _, _ in lookups(dry) if name == "UID FETCH"][1]  # the members of 7021
    server = build()
    statuses = run(server, [g(x) for x in xs], soft=second - 0.5, hard=2000.0)
    assert server.commands[:-1] == dry.commands[:second]  # nothing sent from the stop on
    assert states(statuses) == [FOUND] * 21
    assert [s.thread_checked for s in statuses] == [True] * 20 + [False]
    assert facts(statuses[0]) == (ago(60), A_MID, None, True)

    monkeypatch.setattr(imap_status, "GMAIL_REPLY_HEADER_SEARCH", True)
    dry = build()
    run(dry, [g(x) for x in xs], soft=1000.0, hard=2000.0)
    second = [index for index, name, _, _ in lookups(dry) if name == "UID SEARCH"][1]  # header batch 2
    server = build()
    statuses = run(server, [g(x) for x in xs], soft=second - 0.5, hard=2000.0)
    assert [s.thread_checked for s in statuses] == [False] * 21
    assert facts(statuses[0]) == (ago(60), A_MID, None, False)  # the header hit read before the stop stays

    # without `\\All`: a ref is checked only when its batch finished in `\\Sent`, INBOX and `\\Trash`
    monkeypatch.setattr(imap_status, "GMAIL_REPLY_HEADER_SEARCH", False)

    def build_without_all() -> FakeImapServer:
        server = gmail(
            with_all=False,
            inbox=[msg(1, 180, "x", thrid=THR, message_id=f"<{MID}>")],
            sent=[member(5, 60, message_id=A_MID, sender=OWNER)],
        )
        server.step_seconds = 1.0
        return server

    dry = build_without_all()
    run(dry, [g()], soft=1000.0, hard=2000.0)
    inbox_search = next(index for index, name, box, _ in lookups(dry) if name == "UID SEARCH" and box == "INBOX")
    server = build_without_all()
    [status] = run(server, [g()], soft=inbox_search - 0.5, hard=2000.0)
    assert facts(status) == (ago(60), A_MID, None, False)


def test_gmail_soft_checks_every_ten_rows_and_per_chunk() -> None:
    """SR2: member parsing calls the soft check every 10 rows and between chunks; a stop there sends nothing more."""
    members = [member(uid, 100 - uid / 10, message_id=f"f{uid}@example.test") for uid in range(2, 252)]

    def build() -> FakeImapServer:
        return gmail(all_mail=[x_mail(1, 180), *members])

    server = build()
    checks: list[str] = []
    [status] = run_direct(server, [g()], lambda: checks.append("check"))
    fetched = member_fetches(server)
    assert [len(uids) for _, uids in fetched] == [50, 50, 50, 50, 50, 1]
    assert len(checks) >= sum((len(uids) - 1) // SOFT_CHECK_ROWS for _, uids in fetched) + len(fetched) - 1
    assert facts(status) == (None, None, ago(100 - 251 / 10), True)

    server = build()

    def stop_in_members() -> None:
        if member_fetches(server):
            raise SoftStop

    [status] = run_direct(server, [g()], stop_in_members)
    assert (status.state, status.thread_checked) == ("found", False)
    assert names(server)[-2:] == ["UID FETCH", "LOGOUT"]


@pytest.mark.parametrize("phase", ["thread_search", "member_fetch"])
def test_gmail_hard_deadline_in_lookup_fails_the_account(phase: str) -> None:
    def build() -> FakeImapServer:
        server = gmail(all_mail=[x_mail(), reply(5, 60), member(6, 30)])
        server.step_seconds = 1.0
        return server

    dry = build()
    run(dry, [g()], soft=1000.0, hard=2000.0)
    command = "UID SEARCH" if phase == "thread_search" else "UID FETCH"
    at = next(index for index, name, _, _ in lookups(dry) if name == command)
    with pytest.raises(ProviderError) as caught:
        run(build(), [g()], soft=1000.0, hard=at - 0.5)
    assert (caught.value.code, caught.value.cause) == ("upstream_timeout", "CallDeadline")


# --- wire and logs ------------------------------------------------------------------------------------------------


def test_no_x_gm_raw_in_any_command(monkeypatch: pytest.MonkeyPatch) -> None:
    """D80: a Message-ID holding `X-GM-RAW` is never sent (header search on or off); the thread lookup still runs by
    its thread id, and no command carries `X-GM-RAW`."""
    raw = "x-gm-raw@example.test"
    for on in (False, True):
        monkeypatch.setattr(imap_status, "GMAIL_REPLY_HEADER_SEARCH", on)
        server = gmail(
            inbox=[msg(1, 180, "x", thrid=THR, message_id=f"<{raw}>")],
            all_mail=[msg(9, 180, "x", INBOX_L, thrid=THR, message_id=f"<{raw}>"), member(6, 30, to=raw)],
        )
        [status] = run(server, [m("INBOX", 1, account=GMAIL)])
        assert status.newer_incoming_at == ago(30)
        assert status.thread_checked is (not on)  # a refused value cannot be header-searched
        assert thread_searches(server) == [b"NOT DRAFT X-GM-THRID 7001"]
        for _, argument in server.commands:
            assert "X-GM-RAW" not in repr(argument).upper()


@pytest.fixture
def hub_log() -> Iterator[Capture]:
    handler = Capture()
    logger = logging.getLogger("mcp_hub")
    logger.addHandler(handler)
    level = logger.level
    logger.setLevel(logging.DEBUG)
    yield handler
    logger.removeHandler(handler)
    logger.setLevel(level)


def test_gmail_logs_carry_no_labels_or_thread_ids(hub_log: Capture) -> None:
    """§9.7: the Gmail lookups log nothing, not even for a member without INTERNALDATE or a failed batch; no label,
    thread id, Message-ID or address reaches a log."""
    thread = 1781234567890123456
    label = b"Leak-Label-Name"
    reply_id = "leak-reply@example.test"

    def build() -> FakeImapServer:
        undated = hdr("undated", message_id="<leak-undated@example.test>", in_reply_to=f"<{HOSTILE_MID}>")
        return gmail(
            all_mail=[
                msg(1, 180, "x", INBOX_L, label, thrid=thread, message_id=f"<{HOSTILE_MID}>"),
                reply(5, 60, label, thrid=thread, to=HOSTILE_MID, message_id=reply_id),
                FakeMessage(6, None, headers=undated, labels=(label,), thrid=thread),
            ]
        )

    [status] = run(build(), [g(HOSTILE_MID)], own=OWN)
    assert facts(status) == (ago(60), reply_id, None, True)
    server = build()
    _fail_thread_search(server, 1)
    [status] = run(server, [g(HOSTILE_MID)], own=OWN)
    assert status.thread_checked is False
    assert {json.loads(line)["event"] for line in hub_log.lines} == {"folder_map_refreshed"}
    text = "\n".join(hub_log.lines) + "\n".join(message for _, _, message in hub_log.raw)
    for value in (str(thread), "Leak", "leak", "owner@example.org"):
        assert value not in text
