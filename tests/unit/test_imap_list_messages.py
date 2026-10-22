"""IMAP `list_messages` on servers other than Gmail (spec 080 §5.2, §5.4, §5.5, §6.5; WP13a T12)."""

import json
import logging
from collections.abc import Callable, Iterator, Sequence
from datetime import UTC, datetime, timedelta, timezone

import pytest

import mcp_hub.providers.imap as imap
import mcp_hub.providers.imap_session as imap_session
from mcp_hub.cursors import Position
from mcp_hub.ids import MessageRef, ThreadKey, encode_message_id, location_id, location_k
from mcp_hub.providers.base import (
    UNDECODABLE_NOTE,
    FolderPage,
    ListQuery,
    MailFlags,
    MailMeta,
    MailSummary,
    ProviderError,
    item_hash,
    order_key,
    order_key_of,
)
from mcp_hub.providers.deadline import CallDeadline, SoftStop
from mcp_hub.providers.folders import FolderMapCache
from mcp_hub.providers.headers import SUMMARY_HEADER_ITEM, candidate_header_item
from mcp_hub.providers.imap import ImapMailbox
from mcp_hub.providers.imap_guard import MAX_EXAMINE_NAME_CHARS
from mcp_hub.providers.imap_session import FETCH_CHUNK_UIDS, MAX_SEARCH_CANDIDATES
from tests.support.clock import FakeClock
from tests.support.imap_fake import COMMAND_ALLOWLIST, FakeFolder, FakeImapClient, FakeImapServer, FakeMessage
from tests.support.logcapture import Capture

NOW = datetime(2026, 10, 21, 7, 0, tzinfo=UTC)
SINCE = NOW - timedelta(hours=24)  # SEARCH SINCE 19-Oct-2026 (window start date - 1)
OWN = frozenset({"owner@example.org"})
CANDIDATE = ["INTERNALDATE", "FLAGS"]
Commands = list[tuple[str, object]]


def hdr(
    subject: str = "s",
    *,
    sender: str = "Sender <sender@example.test>",
    to: str = "Owner <owner@example.org>",
    cc: str | None = None,
    message_id: str | None = None,
    in_reply_to: str | None = None,
    references: str | None = None,
) -> bytes:
    lines = [f"From: {sender}", f"To: {to}"]
    if cc is not None:
        lines.append(f"Cc: {cc}")
    lines.append(f"Subject: {subject}")
    for name, value in (("Message-ID", message_id), ("In-Reply-To", in_reply_to), ("References", references)):
        if value is not None:
            lines.append(f"{name}: {value}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode()


def msg(
    uid: int, minutes_ago: float, subject: str = "s", *, flags: tuple[bytes, ...] = (), text: bytes = b"Hello", **h: str
) -> FakeMessage:
    return FakeMessage(uid, NOW - timedelta(minutes=minutes_ago), flags=flags, headers=hdr(subject, **h), text=text)


def folder(name: str, attributes: tuple[bytes, ...], messages: Sequence[FakeMessage]) -> FakeFolder:
    return FakeFolder(name, (b"\\HasNoChildren", *attributes), messages={m.uid: m for m in messages})


def icloud(
    *,
    inbox: Sequence[FakeMessage] = (),
    sent: Sequence[FakeMessage] = (),
    archive: Sequence[FakeMessage] | None = (),
    trash: Sequence[FakeMessage] = (),
    sent_name: str = "Sent Messages",
) -> FakeImapServer:
    """iCloud-like LIST: special-use attributes, iCloud's names, junk and drafts mailboxes."""
    folders = [
        folder("INBOX", (), inbox),
        folder(sent_name, (b"\\Sent",), sent),
        *([] if archive is None else [folder("Archive", (b"\\Archive",), archive)]),
        folder("Deleted Messages", (b"\\Trash",), trash),
        folder("Junk", (b"\\Junk",), [msg(90, 1, "junk")]),
        folder("Drafts", (b"\\Drafts",), [msg(91, 1, "draft")]),
        folder("Projects", (), [msg(92, 1, "other")]),
    ]
    return FakeImapServer(folders, clock=FakeClock())


def clock_of(server: FakeImapServer) -> FakeClock:
    assert server.clock is not None
    return server.clock


def box(
    server: FakeImapServer, *, own: frozenset[str] = frozenset(), maps: FolderMapCache | None = None
) -> ImapMailbox:
    return ImapMailbox(
        "icloud",
        host="imap.example.test",
        port=993,
        folder="INBOX",
        username="u",
        password="p",
        client_factory=lambda host, port, context, timeout: server.client(),
        own_addresses=own,
        folder_maps=maps if maps is not None else FolderMapCache(clock=clock_of(server)),
    )


def query(
    folder: str = "inbox",
    *,
    since: datetime = SINCE,
    unread_only: bool = False,
    direction: str | None = None,
    limit: int = 20,
    position: Position | None = None,
) -> ListQuery:
    return ListQuery(folder, since, unread_only, direction, limit, position)  # type: ignore[arg-type]


def deadline(server: FakeImapServer, soft: float = 16.0, hard: float = 20.0) -> CallDeadline:
    clock = clock_of(server)
    return CallDeadline(clock(), soft, hard, clock)


def run(server: FakeImapServer, q: ListQuery, **kwargs: object) -> FolderPage:
    return box(server, **kwargs).list_messages(q, deadline(server))  # type: ignore[arg-type]


def names(server: FakeImapServer) -> list[str]:
    return [name for name, _ in server.commands]


def fetches(server: FakeImapServer) -> list[tuple[list[int], list[str]]]:
    return [(list(a[0]), list(a[1])) for n, a in server.commands if n == "UID FETCH"]  # type: ignore[index]


def searches(server: FakeImapServer) -> list[object]:
    return [a for n, a in server.commands if n == "UID SEARCH"]


def subjects(page: FolderPage) -> list[str | None]:
    return [item.subject for item in page.items]


def position_after(item: MailSummary) -> Position:
    return Position(int(item.received_at.timestamp()), location_k(item.ref))


def pages(server_factory: Callable[[], FakeImapServer], q: ListQuery, **kwargs: object) -> list[FolderPage]:
    """Every page of a listing: each later call repeats the query with the position of the last item."""
    out: list[FolderPage] = []
    position: Position | None = None
    for _ in range(20):
        page = run(
            server_factory(), ListQuery(q.folder, q.since, q.unread_only, q.direction, q.limit, position), **kwargs
        )
        out.append(page)
        if not page.more:
            return out
        position = position_after(page.items[-1])
    raise AssertionError("paging did not end")


@pytest.fixture
def provider_log() -> Iterator[Capture]:
    handler = Capture()
    logger = logging.getLogger("mcp_hub.providers")
    logger.addHandler(handler)
    yield handler
    logger.removeHandler(handler)


# --- folders and searches --------------------------------------------------------------------------------------


def test_inbox_lists_read_and_unread_newest_first() -> None:
    server = icloud(
        inbox=[
            msg(1, 120, "old", flags=(b"\\Seen",)),
            msg(2, 30, "new"),
            msg(3, 60, "mid", flags=(b"\\Seen", b"\\Answered")),
            msg(4, 60 * 30, "outside the window"),
        ]
    )
    page = run(server, query())
    assert subjects(page) == ["new", "mid", "old"]
    assert [item.unread for item in page.items] == [True, False, False]
    assert [item.ref for item in page.items] == [MessageRef("icloud", "INBOX", 7, uid) for uid in (2, 3, 1)]
    assert (page.more, page.truncated, page.complete) == (False, False, True)
    assert all(item.meta.folder == "inbox" and item.meta.folder_name == "INBOX" for item in page.items)


def test_inbox_sends_no_list() -> None:
    server = icloud(inbox=[msg(1, 5, "a"), msg(2, 6, "b")])
    run(server, query())
    assert "LIST" not in names(server)
    assert names(server).count("EXAMINE") == 1  # pass 2 reuses the mailbox examined in pass 1
    assert server.commands[2] == ("EXAMINE", "INBOX")
    assert names(server)[-1] == "LOGOUT"


def test_sent_examines_read_only_and_searches_not_draft() -> None:
    server = icloud(sent=[msg(5, 30, "report", sender="Owner <owner@example.org>")])
    page = run(server, query("sent"))
    assert server.commands[:5] == [
        ("LOGIN", None),
        ("CAPABILITY", None),
        ("LIST", ("", "*")),
        ("EXAMINE", "Sent Messages"),
        ("UID SEARCH", b"NOT DRAFT SINCE 19-Oct-2026"),
    ]
    assert server.commands[5][0] == "UID FETCH"
    [item] = page.items
    assert (item.meta.folder, item.meta.folder_name, item.meta.direction) == ("sent", "Sent Messages", "outgoing")
    assert item.ref == MessageRef("icloud", "Sent Messages", 7, 5)


def test_unread_only_adds_unseen() -> None:
    server = icloud(inbox=[msg(1, 5, "unread"), msg(2, 6, "read", flags=(b"\\Seen",))])
    page = run(server, query(unread_only=True))
    assert searches(server) == [b"NOT DRAFT UNSEEN SINCE 19-Oct-2026"]
    assert subjects(page) == ["unread"]


def test_own_address_makes_inbox_item_outgoing() -> None:
    server = icloud(
        inbox=[msg(1, 5, "mine", sender="Me <Owner+news@Example.org>"), msg(2, 6, "theirs")],
    )
    page = run(server, query(), own=OWN)
    assert [(item.subject, item.meta.direction) for item in page.items] == [
        ("mine", "outgoing"),
        ("theirs", "incoming"),
    ]
    assert all(item.meta.folder == "inbox" for item in page.items)


@pytest.mark.parametrize(
    ("direction", "own", "with_from"),
    [(None, OWN, False), ("incoming", frozenset(), False), ("outgoing", frozenset(), False), ("incoming", OWN, True)],
)
def test_from_fetched_only_with_direction_and_own_addresses(
    direction: str | None, own: frozenset[str], with_from: bool
) -> None:
    server = icloud(inbox=[msg(1, 5, "a")])
    run(server, query(direction=direction), own=own)
    expected = [*CANDIDATE, candidate_header_item(["FROM"])] if with_from else CANDIDATE
    assert fetches(server)[0] == ([1], expected)


def test_direction_filter_applies_before_limit() -> None:
    def server() -> FakeImapServer:
        return icloud(
            inbox=[
                msg(1, 5, "in-1"),
                msg(2, 10, "in-2"),
                msg(3, 15, "in-3"),
                msg(4, 20, "out-1", sender="owner@example.org"),
                msg(5, 25, "out-2", sender="owner@example.org"),
            ]
        )

    outgoing = run(server(), query(direction="outgoing", limit=2), own=OWN)
    assert subjects(outgoing) == ["out-1", "out-2"]
    assert outgoing.more is False
    incoming = run(server(), query(direction="incoming", limit=2), own=OWN)
    assert subjects(incoming) == ["in-1", "in-2"]
    assert incoming.more is True


def test_filter_order() -> None:
    """R3: drafts and items below `since` drop first, then the dedup, the position, the direction and `limit`."""
    draft = (b"\\Draft",)

    def server() -> FakeImapServer:
        srv = icloud(
            inbox=[
                # a draft and an item below `since` never win the dedup: their archive copies are listed
                msg(1, 30, "m1", flags=draft, message_id="<m1@example.test>"),
                msg(2, 24 * 60 + 60, "m2", message_id="<m2@example.test>"),
                # the dedup winner above the position suppresses its archive copy below it
                msg(3, 10, "m3", message_id="<m3@example.test>"),
                # merged with its sent copy: listed from sent, at the sent copy's date (M3, security L1)
                msg(4, 300, "m4", message_id="<m4@example.test>"),
            ],
            archive=[
                msg(11, 40, "m1", message_id="<m1@example.test>"),
                msg(12, 50, "m2", message_id="<m2@example.test>"),
                msg(13, 400, "m3", message_id="<m3@example.test>"),
            ],
            sent=[
                msg(24, 20, "m4", message_id="<m4@example.test>"),
                msg(25, 5, "s5", sender="owner@example.org", message_id="<s5@example.test>"),
                msg(26, 200, "s6", sender="owner@example.org", message_id="<s6@example.test>"),
            ],
        )
        srv.honour_not_draft = False  # the server ignores NOT DRAFT: the hub drops the draft by its FLAGS
        return srv

    first = run(server(), query("all"))
    assert [(i.subject, i.meta.folder) for i in first.items] == [
        ("s5", "sent"),
        ("m3", "inbox"),
        ("m4", "sent"),
        ("m1", "archive"),
        ("m2", "archive"),
        ("s6", "sent"),
    ]
    m4 = first.items[2]
    assert (m4.meta.direction, m4.ref.folder) == ("outgoing", "Sent Messages")

    after_m3 = position_after(first.items[1])
    later = run(server(), query("all", position=after_m3))
    assert ("m3", "archive") not in [(i.subject, i.meta.folder) for i in later.items]
    assert subjects(later) == ["m4", "m1", "m2", "s6"]

    outgoing = run(server(), query("all", direction="outgoing", position=after_m3, limit=1))
    assert [(i.subject, i.meta.folder) for i in outgoing.items] == [("m4", "sent")]
    assert outgoing.more is True  # s6 is outgoing too, below m4


@pytest.mark.parametrize("planted_minutes", [3, 30])  # newer and older than the sent copy
def test_planted_inbox_copy_never_hides_the_sent_copy(planted_minutes: int) -> None:
    """Security L1: an inbox message with the owner's From and the Message-ID of a sent message merges with it, and
    the item shown is the sent copy (its folder, its id, outgoing), never the planted one."""
    mid = "<s@example.test>"

    def server() -> FakeImapServer:
        return icloud(
            inbox=[msg(1, planted_minutes, "planted", sender="owner@example.org", message_id=mid)],
            archive=[msg(11, 40, "archived", sender="owner@example.org", message_id=mid)],
            sent=[msg(21, 10, "sent", sender="owner@example.org", message_id=mid)],
        )

    for own in (OWN, frozenset()):
        page = run(server(), query("all"), own=own)
        assert [(i.subject, i.meta.folder, i.meta.direction) for i in page.items] == [("sent", "sent", "outgoing")]
        assert page.items[0].ref == MessageRef("icloud", "Sent Messages", 7, 21)
    outgoing = run(server(), query("all", direction="outgoing"), own=OWN)
    assert [i.ref for i in outgoing.items] == [MessageRef("icloud", "Sent Messages", 7, 21)]


def test_draft_flag_dropped_per_message() -> None:
    server = icloud(
        inbox=[
            msg(1, 5, "draft", flags=(b"\\Draft",)),
            msg(2, 6, "drafts", flags=(b"\\DRAFTS",)),
            msg(3, 7, "lower", flags=(b"\\draft", b"\\Seen")),
            msg(4, 8, "mail"),
        ]
    )
    server.honour_not_draft = False
    page = run(server, query())
    assert subjects(page) == ["mail"]
    assert searches(server) == [b"NOT DRAFT SINCE 19-Oct-2026"]


def test_all_dedups_by_message_id_and_from() -> None:
    server = icloud(
        inbox=[
            msg(1, 5, "x", message_id="<x@example.test>", sender="A <a@example.test>"),
            msg(2, 6, "y", message_id="<y@example.test>", sender="a@example.test"),
            msg(3, 7, "z"),
        ],
        archive=[
            msg(11, 15, "x", message_id="<x@example.test>", sender="a@example.test"),
            msg(12, 16, "y", message_id="<y@example.test>", sender="b@example.test"),
            msg(13, 17, "z"),
        ],
        sent=[msg(21, 25, "x", message_id="<x@example.test>", sender="A@EXAMPLE.TEST")],
    )
    page = run(server, query("all"))
    found = [(i.subject, i.meta.folder, i.meta.direction) for i in page.items]
    assert found == [
        ("y", "inbox", "incoming"),
        ("z", "inbox", "incoming"),
        ("y", "archive", "incoming"),  # same Message-ID, other From: not merged (S-e)
        ("z", "archive", "incoming"),  # no Message-ID: never merged
        ("x", "sent", "outgoing"),  # one item: one copy is in sent, so the sent copy is shown (M3, security L1)
    ]
    assert page.items[-1].ref == MessageRef("icloud", "Sent Messages", 7, 21)
    assert all(items == [*CANDIDATE, candidate_header_item(["MESSAGE-ID", "FROM"])] for _, items in fetches(server)[:3])


def test_all_dedup_holds_across_pages() -> None:
    """B4: the dedup runs over the whole window before the position, so a later page never shows a dropped copy."""

    def server() -> FakeImapServer:
        return icloud(
            inbox=[msg(1, 10, "kept", message_id="<k@example.test>")],
            archive=[msg(11, 120, "kept", message_id="<k@example.test>"), msg(12, 60, "other")],
        )

    listed = [(i.subject, i.meta.folder) for page in pages(server, query("all", limit=1)) for i in page.items]
    assert listed == [("kept", "inbox"), ("other", "archive")]


def test_equal_internaldate_paging_returns_each_once() -> None:
    same = NOW - timedelta(minutes=30)

    def server() -> FakeImapServer:
        def at(uid: int) -> FakeMessage:
            return FakeMessage(uid, same, headers=hdr(f"m{uid}", message_id=f"<m{uid}@example.test>"))

        return icloud(inbox=[at(uid) for uid in range(1, 5)], sent=[at(uid) for uid in range(21, 24)])

    listed = [i.ref for page in pages(server, query("all", limit=3)) for i in page.items]
    assert len(listed) == 7
    assert len(set(listed)) == 7


def test_later_page_search_bounds() -> None:
    position = Position(int(datetime(2026, 10, 20, 10, 0, tzinfo=UTC).timestamp()), "f" * 16)
    sent = icloud()
    run(sent, query("sent", position=position))
    assert searches(sent) == [b"NOT DRAFT SINCE 19-Oct-2026 BEFORE 22-Oct-2026"]
    every = icloud()
    run(every, query("all", position=position))
    assert searches(every) == [b"NOT DRAFT SINCE 19-Oct-2026"] * 3  # S-a: no BEFORE for `all`


def test_5000_candidate_cap() -> None:
    count = MAX_SEARCH_CANDIDATES + 3
    inbox = [
        FakeMessage(uid, NOW - timedelta(seconds=count - uid + 1), headers=hdr(f"m{uid}"))
        for uid in range(1, count + 1)
    ]
    server = icloud(inbox=inbox)
    page = run(server, query(limit=3))
    assert MAX_SEARCH_CANDIDATES == 5000
    assert [item.ref.uid for item in page.items] == [count, count - 1, count - 2]
    assert (page.more, page.truncated, page.complete) == (True, True, True)
    candidate_uids = [uids for uids, items in fetches(server) if items == CANDIDATE]
    assert len(candidate_uids) == 10
    assert {uid for uids in candidate_uids for uid in uids} == set(range(4, count + 1))


def test_fetch_chunks_of_500_as_int_lists() -> None:
    inbox = [FakeMessage(uid, NOW - timedelta(seconds=2000 - uid), headers=hdr(f"m{uid}")) for uid in range(1, 1202)]
    server = icloud(inbox=inbox)
    page = run(server, query(limit=1))
    assert FETCH_CHUNK_UIDS == 500
    candidate_uids = [uids for uids, items in fetches(server) if items == CANDIDATE]
    assert candidate_uids == [list(range(1, 501)), list(range(501, 1001)), list(range(1001, 1202))]
    assert all(type(uid) is int for uids in candidate_uids for uid in uids)
    assert all(len(uids) <= 500 for uids, _ in fetches(server))
    assert page.truncated is False


# --- degraded data and failures --------------------------------------------------------------------------------


class NoDateClient(FakeImapClient):
    """A server that answers INTERNALDATE with something IMAPClient cannot turn into a date."""

    blank: frozenset[int] = frozenset({2})

    def fetch(self, messages: object, items: object) -> dict[int, dict[bytes, object]]:
        rows = super().fetch(messages, items)  # type: ignore[arg-type]
        for uid in self.blank:
            if uid in rows and b"INTERNALDATE" in rows[uid]:
                rows[uid][b"INTERNALDATE"] = None
        return rows


def test_internaldate_none_dropped_and_degraded(provider_log: Capture, monkeypatch: pytest.MonkeyPatch) -> None:
    server = icloud(inbox=[msg(1, 5, "a"), msg(2, 6, "b"), msg(3, 7, "c")])
    monkeypatch.setattr(server, "client", lambda: NoDateClient(server))
    page = run(server, query())
    assert subjects(page) == ["a", "c"]
    [line] = [json.loads(line) for line in provider_log.lines]
    assert (line["event"], line["exception"]) == ("item_degraded", "MissingInternalDate")
    assert line["item"] == item_hash(encode_message_id(MessageRef("icloud", "INBOX", 7, 2)))


def test_missing_uidvalidity_skips_non_inbox_folder() -> None:
    server = icloud(inbox=[msg(1, 5, "in")], sent=[msg(2, 6, "sent")], archive=[msg(3, 7, "arch")])
    server.folders[1].uidvalidity = None
    page = run(server, query("all"))
    assert subjects(page) == ["in", "arch"]
    assert page.complete is False
    after = names(server)[[a for _, a in server.commands].index("Sent Messages") + 1]
    assert after == "EXAMINE"  # no SEARCH in the skipped folder

    inbox_only = icloud(inbox=[msg(1, 5, "in")])
    inbox_only.folders[0].uidvalidity = None
    with pytest.raises(ProviderError) as info:
        run(inbox_only, query())
    assert (info.value.code, info.value.cause) == ("upstream_error", "MissingUidValidity")


@pytest.mark.parametrize("case", ["search_bad", "examine_no", "search_no", "fetch_no", "abort"])
def test_error_classes(case: str) -> None:
    server = icloud(inbox=[msg(1, 5, "a")], sent=[msg(2, 5, "b")])
    if case == "search_bad":
        server.fail["UID SEARCH"] = server.bad("UID SEARCH")
        with pytest.raises(ProviderError) as info:
            run(server, query())
        assert info.value.code == "upstream_error"
    elif case == "examine_no":
        server.fail["EXAMINE"] = server.no("EXAMINE")
        page = run(server, query("sent"))
        assert names(server) == ["LOGIN", "CAPABILITY", "LIST", "EXAMINE", "LOGOUT"]  # listed in this call already
        assert (page.items, page.complete) == ([], False)
    elif case == "search_no":
        server.fail["UID SEARCH"] = server.no("UID SEARCH")
        page = run(server, query())
        assert (page.items, page.complete, page.truncated) == ([], False, False)
    elif case == "fetch_no":
        server.fail["UID FETCH"] = server.no("UID FETCH")
        page = run(server, query())
        assert (page.items, page.complete) == ([], True)
    else:
        server.fail["UID SEARCH"] = server.abort()
        with pytest.raises(ProviderError) as info:
            run(server, query())
        assert (info.value.code, info.value.cause) == ("upstream_error", "abort")
    assert names(server)[-1] == "LOGOUT"


def test_examine_no_with_a_refused_rebuild_is_incomplete_not_an_error() -> None:
    """A map listed in this call is not listed again; a cached map is rebuilt once per 60 s (Sec16)."""
    clock = FakeClock()
    maps = FolderMapCache(clock=clock)
    calls: list[list[str]] = []
    for _ in range(3):
        server = icloud(sent=[msg(2, 5, "b")])
        server.fail["EXAMINE"] = server.no("EXAMINE")
        page = run(server, query("sent"), maps=maps)
        assert (page.items, page.complete) == ([], False)
        calls.append(names(server))
        clock.advance(10.0)  # inside the 60 s window of the forced rebuild
    assert calls == [
        ["LOGIN", "CAPABILITY", "LIST", "EXAMINE", "LOGOUT"],  # cold: listed in this call, no rebuild
        ["LOGIN", "CAPABILITY", "EXAMINE", "LIST", "LOGOUT"],  # cached map: one rebuild, which opens the window
        ["LOGIN", "CAPABILITY", "EXAMINE", "LOGOUT"],  # inside the window: refused
    ]


def test_fetch_no_in_pass_two_drops_those_items_and_leaves_the_page_incomplete() -> None:
    """S-p drops the UIDs a FETCH NO did not return. "a" is newer than "b": a cursor after "b" would skip "a" for
    good, so the page is incomplete and gets no cursor."""
    server = icloud(inbox=[msg(1, 5, "a")], archive=[msg(2, 6, "b")])
    original = server.run
    count = {"fetch": 0}

    def run_command(command: str, argument: object = None) -> None:
        if command == "UID FETCH" and SUMMARY_HEADER_ITEM in argument[1]:  # type: ignore[index]
            count["fetch"] += 1
            if count["fetch"] == 1:
                server.fail["UID FETCH"] = server.no("UID FETCH")
        original(command, argument)

    server.run = run_command  # type: ignore[method-assign]
    page = run(server, query("all"))
    assert subjects(page) == ["b"]
    assert page.complete is False


# --- soft stop and hard deadline -------------------------------------------------------------------------------


def nth(commands: Commands, match: Callable[[str, object], bool], n: int = 1) -> int:
    hits = [index for index, (name, argument) in enumerate(commands) if match(name, argument)]
    return hits[n - 1]


def soft_stop_before(
    build: Callable[[], FakeImapServer], q: ListQuery, at: Callable[[Commands], int], **kwargs: object
) -> tuple[FakeImapServer, FolderPage]:
    """Run once to find the command index, then again with a soft limit that refuses exactly that command."""
    dry = build()
    run(dry, q, **kwargs)
    index = at(dry.commands)
    server = build()
    server.step_seconds = 1.0
    clock = clock_of(server)
    page = box(server, **kwargs).list_messages(q, CallDeadline(clock(), index - 0.5, index + 100.0, clock))  # type: ignore[arg-type]
    assert server.commands == [*dry.commands[:index], ("LOGOUT", None)]
    return server, page


def three_folders() -> FakeImapServer:
    return icloud(
        inbox=[msg(1, 5, "in-1"), msg(2, 50, "in-2")],
        archive=[msg(11, 10, "arch")],
        sent=[msg(21, 20, "sent")],
    )


def test_soft_stop_returns_partial() -> None:
    """The soft limit passes while pass 2 opens the archive: the inbox items already built are returned, the
    archive FETCH is never sent. (Each pass-2 item checks the limit, so the stop must come after the inbox items.)"""
    server = three_folders()
    clock = clock_of(server)
    original = server.run
    examined = {"Archive": 0}

    def run_command(command: str, argument: object = None) -> None:
        original(command, argument)
        if command == "EXAMINE" and argument == "Archive":
            examined["Archive"] += 1
            if examined["Archive"] == 2:  # pass 2
                clock.advance(20.0)

    server.run = run_command  # type: ignore[method-assign]
    page = box(server).list_messages(query("all"), CallDeadline(clock(), 16.0, 100.0, clock))
    assert subjects(page) == ["in-1", "in-2"]
    assert (page.truncated, page.complete) == (True, False)
    assert names(server)[-2:] == ["EXAMINE", "LOGOUT"]


def test_soft_stop_in_pass_one_returns_an_empty_partial_page() -> None:
    def search_of_sent(commands: Commands) -> int:
        return nth(commands, lambda name, _: name == "UID SEARCH", 3)

    _, page = soft_stop_before(three_folders, query("all"), search_of_sent)
    assert (page.items, page.truncated, page.complete) == ([], True, False)


def test_soft_stop_in_pass_two_gives_no_cursor() -> None:
    """SA13: every SEARCH completed, but the page is incomplete, so the tool issues no cursor."""

    def snippet_fetch(commands: Commands) -> int:
        return nth(commands, lambda name, argument: name == "UID FETCH" and "BODY.PEEK[1]<0.4096>" in argument[1])  # type: ignore[index]

    server, page = soft_stop_before(lambda: icloud(inbox=[msg(1, 5, "a")]), query(), snippet_fetch)
    assert "UID SEARCH" in names(server)
    assert (page.items, page.truncated, page.complete) == ([], True, False)


@pytest.mark.parametrize("command", ["LOGIN", "CAPABILITY"])
def test_soft_stop_at_login_or_capability_is_a_partial_page_not_an_account_error(command: str) -> None:
    """D76/§5.4: a list stopped before the first folder answered is an empty partial page (like Graph's), never
    `upstream_timeout` for the account."""

    def at(commands: Commands) -> int:
        return nth(commands, lambda name, _: name == command)

    server, page = soft_stop_before(three_folders, query("all"), at)
    assert (page.items, page.more, page.truncated, page.complete) == ([], False, True, False)
    assert names(server)[-1] == "LOGOUT"


@pytest.mark.parametrize("soft", [-0.5, 0.5])  # before LOGIN, before the post-login CAPABILITY
def test_soft_stop_at_login_on_get_message_stays_upstream_timeout(soft: float) -> None:
    server = icloud(inbox=[msg(1, 5, "a")])
    server.step_seconds = 1.0
    with pytest.raises(ProviderError) as info:
        box(server).get_message(MessageRef("icloud", "INBOX", 7, 1), deadline(server, soft=soft))
    assert (info.value.code, info.value.cause) == ("upstream_timeout", "SoftStop")


def test_candidate_parse_checks_the_soft_limit_every_100_candidates(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hostile From blocks cost CPU without any IMAP command: pass 1 checks the soft limit every 100 candidates."""
    server = icloud(inbox=[msg(uid, 5 + uid / 100, f"m{uid}") for uid in range(1, 501)])
    clock = clock_of(server)
    original = imap_session.parse_summary_headers
    parsed = {"n": 0}

    def slow_parse(raw: bytes | None) -> object:
        parsed["n"] += 1
        clock.advance(0.1)
        return original(raw)

    monkeypatch.setattr(imap_session, "parse_summary_headers", slow_parse)
    page = box(server, own=OWN).list_messages(query(direction="incoming"), CallDeadline(clock(), 15.0, 100.0, clock))
    assert parsed["n"] == 200  # soft limit passed at 151; stopped at the next check, not after all 500
    assert (page.items, page.truncated, page.complete) == ([], True, False)
    assert names(server)[-2:] == ["UID FETCH", "LOGOUT"]  # no pass-2 command


def test_summary_parse_checks_the_soft_limit_per_item(monkeypatch: pytest.MonkeyPatch) -> None:
    """Security M1: pass 2 parses up to 64 KiB of headers per item without a command; it checks the soft limit per
    item and returns the items built so far as a partial page."""
    server = icloud(inbox=[msg(1, 5, "a"), msg(2, 6, "b"), msg(3, 7, "c")])
    clock = clock_of(server)
    original = imap.parse_summary_headers

    def slow_parse(raw: bytes | None) -> object:
        clock.advance(10.0)
        return original(raw)

    monkeypatch.setattr(imap, "parse_summary_headers", slow_parse)
    page = box(server).list_messages(query(), CallDeadline(clock(), 15.0, 100.0, clock))
    assert subjects(page) == ["a", "b"]
    assert (page.truncated, page.complete) == (True, False)
    assert names(server)[-1] == "LOGOUT"


def test_snippet_decode_checks_the_soft_limit_per_member(monkeypatch: pytest.MonkeyPatch) -> None:
    """The snippet decode of each member checks the soft limit too; the folders built before the stop are kept."""
    server = icloud(inbox=[msg(1, 5, "a")], archive=[msg(11, 10, "b1"), msg(12, 11, "b2")])
    clock = clock_of(server)
    original = imap.decode_part
    decoded = {"n": 0}

    def slow_decode(raw: bytes, part: object) -> str:
        decoded["n"] += 1
        clock.advance(8.0)
        return original(raw, part)  # type: ignore[arg-type]

    monkeypatch.setattr(imap, "decode_part", slow_decode)
    page = box(server).list_messages(query("all"), CallDeadline(clock(), 15.0, 100.0, clock))
    assert decoded["n"] == 2  # a, then b1; stopped before b2
    assert subjects(page) == ["a"]
    assert (page.truncated, page.complete) == (True, False)


def test_abandoned_worker_stops_after_hard_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    server = icloud(inbox=[msg(1, 5, "a")])
    original = server.run

    def slow_search(command: str, argument: object = None) -> None:
        original(command, argument)
        if command == "UID SEARCH":
            clock_of(server).advance(30.0)  # the command returned after the hard deadline

    monkeypatch.setattr(server, "run", slow_search)
    with pytest.raises(ProviderError) as info:
        run(server, query())
    assert (info.value.code, info.value.cause) == ("upstream_timeout", "CallDeadline")
    assert names(server)[names(server).index("UID SEARCH") + 1 :] == ["LOGOUT"]


# --- mailbox names and roles -----------------------------------------------------------------------------------


def test_unsafe_mailbox_name_lists_items_with_null_location_id() -> None:
    name = "Sent " + "x" * 196  # 201 characters: listed, but no id can carry the name (§5.3 rule 9)
    server = icloud(sent=[msg(5, 30, "report")], sent_name=name)
    page = run(server, query("sent"))
    [item] = page.items
    assert len(name) == 201
    assert item.ref == MessageRef("icloud", name, 7, 5)
    assert location_id(item.ref) is None
    assert item.meta.folder_name == name
    assert ("EXAMINE", name) in server.commands
    assert page.complete is True


def test_mailbox_name_over_the_guard_cap_is_skipped_as_incomplete() -> None:
    """The guard bounds EXAMINE at 1,000 characters: such a folder is incomplete, never an account error."""
    server = icloud(sent=[msg(5, 30, "report")], sent_name="S" * (MAX_EXAMINE_NAME_CHARS + 1))
    page = run(server, query("sent"))
    assert (page.items, page.complete) == ([], False)
    assert "EXAMINE" not in names(server)


def validity_changes_in_pass_two(server: FakeImapServer) -> None:
    """The inbox gets a new UIDVALIDITY between its pass-1 EXAMINE and its pass-2 EXAMINE."""
    original = server.run
    examined = {"inbox": 0}

    def run_command(command: str, argument: object = None) -> None:
        if command == "EXAMINE" and argument == "INBOX":
            examined["inbox"] += 1
            if examined["inbox"] == 2:
                server.folders[0].uidvalidity = 8
        original(command, argument)

    server.run = run_command  # type: ignore[method-assign]


def test_uidvalidity_change_between_passes_drops_items() -> None:
    server = three_folders()
    validity_changes_in_pass_two(server)
    page = run(server, query("all"))
    assert subjects(page) == ["arch", "sent"]
    assert (page.truncated, page.complete) == (True, False)


def test_uidvalidity_change_with_a_limit_loses_no_item() -> None:
    """With limit 2 the chosen items are in-1 (inbox) and arch. in-1 is dropped in pass 2; a cursor after arch would
    skip it for good, so the page must be incomplete (no cursor). Repeating the call without a cursor finds it."""
    server = three_folders()
    validity_changes_in_pass_two(server)
    first = run(server, query("all", limit=2))
    assert subjects(first) == ["arch"]
    assert (first.more, first.truncated, first.complete) == (True, True, False)
    assert server.folders[0].uidvalidity == 8  # the server keeps its new UIDVALIDITY
    again = [i.subject for page in pages(lambda: server, query("all", limit=2)) for i in page.items]
    assert sorted(again) == sorted(["in-1", "in-2", "arch", "sent"])


def test_archive_without_role_is_empty_and_complete() -> None:
    server = icloud(inbox=[msg(1, 5, "a")], archive=None)
    page = run(server, query("archive"))
    assert (page.items, page.more, page.truncated, page.complete) == ([], False, False, True)
    assert names(server) == ["LOGIN", "CAPABILITY", "LIST", "LOGOUT"]


def test_summary_fields() -> None:
    zurich = timezone(timedelta(hours=2))
    message = FakeMessage(
        7,
        datetime(2026, 10, 21, 8, 30, tzinfo=zurich),
        flags=(b"\\Seen", b"\\Answered", b"\\Flagged"),
        headers=hdr(
            "Re: Plan",
            sender="Sender Name <sender@example.test>",
            to="Owner <owner@example.org>, other@example.test",
            cc="cc@example.test",
            message_id="<m1@example.test>",
            in_reply_to="<p@example.test>",
            references="<r0@example.test> <p@example.test>",
        ),
        text=b"Hello there",
    )
    [item] = run(icloud(inbox=[message]), query()).items
    assert item == MailSummary(
        ref=MessageRef("icloud", "INBOX", 7, 7),
        received_at=datetime(2026, 10, 21, 8, 30, tzinfo=zurich),
        unread=False,
        has_attachments=False,
        from_address="sender@example.test",
        from_name="Sender Name",
        subject="Re: Plan",
        snippet_text="Hello there",
        to_addresses=("owner@example.org", "other@example.test"),
        cc_addresses=("cc@example.test",),
        meta=MailMeta(
            folder="inbox",
            folder_name="INBOX",
            flags=MailFlags(answered=True, flagged=True),
            direction="incoming",
            message_id="m1@example.test",
            in_reply_to="p@example.test",
            references=("r0@example.test", "p@example.test"),
            thread=ThreadKey("icloud", "headers", "r0@example.test"),
        ),
    )
    assert item.received_at.utcoffset() == timedelta(hours=2)  # normalise_times = False keeps the server offset


# --- Sec3: broad handlers never swallow a stop -----------------------------------------------------------------


def raise_once(original: Callable[..., object]) -> Callable[..., object]:
    calls = {"n": 0}

    def wrapper(*args: object, **kwargs: object) -> object:
        calls["n"] += 1
        if calls["n"] == 1:
            raise SoftStop
        return original(*args, **kwargs)

    return wrapper


def snippet_fetch_stops(monkeypatch: pytest.MonkeyPatch) -> None:
    original = FakeImapClient.fetch

    def fetch(self: FakeImapClient, messages: object, items: object) -> dict[int, dict[bytes, object]]:
        if any(str(item).endswith("<0.4096>") for item in items):  # type: ignore[attr-defined]
            raise SoftStop
        return original(self, messages, items)  # type: ignore[arg-type]

    monkeypatch.setattr(FakeImapClient, "fetch", fetch)


LIST_HANDLERS: dict[str, Callable[[pytest.MonkeyPatch], None]] = {
    "candidate_parse": lambda mp: mp.setattr(
        imap_session, "parse_summary_headers", raise_once(imap_session.parse_summary_headers)
    ),
    "text_part_choice": lambda mp: mp.setattr(imap, "choose_text_part", raise_once(imap.choose_text_part)),
    "snippet_fetch": snippet_fetch_stops,
    "snippet_decode": lambda mp: mp.setattr(imap, "decode_part", raise_once(imap.decode_part)),
    "summary_build": lambda mp: mp.setattr(imap, "attachment_parts", raise_once(imap.attachment_parts)),
}


@pytest.mark.parametrize("handler", list(LIST_HANDLERS))
def test_broad_handlers_pass_soft_stop_through(
    handler: str, monkeypatch: pytest.MonkeyPatch, provider_log: Capture
) -> None:
    LIST_HANDLERS[handler](monkeypatch)
    server = icloud(inbox=[msg(1, 5, "a"), msg(2, 6, "b")])
    page = run(server, query(direction="incoming"), own=OWN)  # with a candidate header, so the parse runs
    assert (page.items, page.truncated, page.complete) == ([], True, False)
    assert provider_log.lines == []  # nothing degraded: the stop was not taken for a broken message


GET_HANDLERS: dict[str, tuple[str, str]] = {
    "structure": ("choose_text_part", "structure"),
    "body": ("decode_part", "body"),
    "headers": ("_sender_and_subject", "headers"),
}


@pytest.mark.parametrize("step", list(GET_HANDLERS))
def test_get_message_steps_pass_soft_stop_through(step: str, monkeypatch: pytest.MonkeyPatch) -> None:
    name, _ = GET_HANDLERS[step]
    monkeypatch.setattr(imap, name, raise_once(getattr(imap, name)))
    server = icloud(inbox=[msg(1, 5, "a")])
    with pytest.raises(ProviderError) as info:
        box(server).get_message(MessageRef("icloud", "INBOX", 7, 1), deadline(server))
    assert (info.value.code, info.value.cause) == ("upstream_timeout", "SoftStop")


def test_address_parse_passes_soft_stop_through() -> None:
    class Message:
        def raw_items(self) -> object:
            raise SoftStop

    with pytest.raises(SoftStop):
        imap._addresses(Message(), "From")  # type: ignore[arg-type]


def test_a_broken_message_still_degrades_to_the_placeholder(
    monkeypatch: pytest.MonkeyPatch, provider_log: Capture
) -> None:
    def exploding(parts: object, text_part: object) -> object:
        raise ValueError("SENTINEL")

    monkeypatch.setattr(imap, "attachment_parts", exploding)
    page = run(icloud(inbox=[msg(1, 5, "a")]), query())
    [item] = page.items
    assert (item.snippet_text, item.subject, item.meta.folder) == (UNDECODABLE_NOTE, None, "inbox")
    assert page.complete is True
    assert [json.loads(line)["exception"] for line in provider_log.lines] == ["ValueError"]
    assert "SENTINEL" not in "\n".join(provider_log.lines)


# --- command surface -------------------------------------------------------------------------------------------


def test_only_allowlisted_commands() -> None:
    server = icloud(inbox=[msg(1, 5, "a")], sent=[msg(2, 6, "b")], archive=[msg(3, 7, "c")], trash=[msg(4, 8, "d")])
    mailbox = box(server, own=OWN)
    for folder_value in ("inbox", "sent", "archive", "trash", "all"):
        mailbox.list_messages(query(folder_value, direction="outgoing", unread_only=True), deadline(server))
    mailbox.list_messages(ListQuery("inbox", SINCE, True, None, 20), deadline(server))
    mailbox.get_message(MessageRef("icloud", "INBOX", 7, 1), deadline(server))
    mailbox.check(deadline(server))
    assert set(names(server)) <= COMMAND_ALLOWLIST
    examined = {argument for name, argument in server.commands if name == "EXAMINE"}
    assert examined == {"INBOX", "Sent Messages", "Archive", "Deleted Messages"}  # never junk, drafts or other


def test_unread_inbox_query_searches_unseen() -> None:
    server = icloud(inbox=[msg(1, 5, "a"), msg(2, 6, "b", flags=(b"\\Seen",)), msg(3, 7, "c")])
    page = box(server).list_messages(ListQuery("inbox", SINCE, True, None, 1), deadline(server))
    assert [item.subject for item in page.items] == ["a"]
    assert page.more is True
    assert searches(server) == [b"NOT DRAFT UNSEEN SINCE 19-Oct-2026"]


def test_fake_lists_raw_bytes_names_like_imapclient() -> None:
    server = FakeImapServer([FakeFolder("Eing&AOQ-nge"), FakeFolder(2026)])
    client = server.client()
    client.folder_encode = False
    assert [name for _, _, name in client.list_folders()] == [b"Eing&AOQ-nge", 2026]


# --- frontier, unparsable messages, log content ----------------------------------------------------------------


def test_frontier_is_the_order_key_of_the_last_returned_item_when_more() -> None:
    def server() -> FakeImapServer:
        return icloud(inbox=[msg(1, 5, "a"), msg(2, 6, "b"), msg(3, 7, "c")])

    page = run(server(), query(limit=2))
    assert page.more is True
    assert page.frontier == order_key(page.items[-1])
    assert run(server(), query(limit=3)).frontier is None  # nothing more: no frontier


def test_frontier_falls_back_to_the_last_chosen_item_when_none_is_returned() -> None:
    """Every chosen item became a draft before pass 2: nothing is returned, yet the window above the last chosen
    item was scanned, so the frontier is that item's order key and the page stays complete (T17 merges on it)."""
    server = icloud(inbox=[msg(1, 5, "a"), msg(2, 6, "b"), msg(3, 7, "c")])
    original = server.run

    def run_command(command: str, argument: object = None) -> None:
        if command == "UID FETCH" and SUMMARY_HEADER_ITEM in argument[1]:  # type: ignore[index]
            for uid in (1, 2):
                server.folders[0].messages[uid].flags = (b"\\Draft",)
        original(command, argument)

    server.run = run_command  # type: ignore[method-assign]
    page = run(server, query(limit=2))
    assert page.items == []
    assert (page.more, page.complete) == (True, True)
    last_chosen = server.folders[0].messages[2]
    assert last_chosen.internaldate is not None
    assert page.frontier == order_key_of(last_chosen.internaldate, MessageRef("icloud", "INBOX", 7, 2))


class DeepStructureClient(FakeImapClient):
    """IMAPClient's recursive response parser raises RecursionError on a BODYSTRUCTURE nested about 1000 deep."""

    def fetch(self, messages: object, items: object) -> dict[int, dict[bytes, object]]:
        if "BODYSTRUCTURE" in items and 1 in messages:  # type: ignore[operator]
            raise RecursionError("maximum recursion depth exceeded")
        return super().fetch(messages, items)  # type: ignore[arg-type]


def test_unparsable_message_keeps_its_read_state(monkeypatch: pytest.MonkeyPatch) -> None:
    server = icloud(inbox=[msg(1, 5, "deep", flags=(b"\\Seen", b"\\Answered")), msg(2, 6, "fine")])
    monkeypatch.setattr(server, "client", lambda: DeepStructureClient(server))
    page = run(server, query())
    deep, fine = page.items
    assert (deep.snippet_text, deep.unread, deep.meta.flags.answered) == (UNDECODABLE_NOTE, False, True)
    assert (fine.subject, fine.unread) == ("fine", True)
    assert ([1], ["FLAGS"]) in fetches(server)  # the FLAGS of the unparsable UID, fetched alone
    assert page.complete is True


def test_merged_all_logs_carry_no_message_id_folder_name_or_address(monkeypatch: pytest.MonkeyPatch) -> None:
    handler = Capture()
    logger = logging.getLogger("mcp_hub")
    logger.addHandler(handler)
    level = logger.level
    logger.setLevel(logging.INFO)  # also the INFO folder_map_refreshed line
    try:
        server = icloud(
            inbox=[msg(1, 5, "Quarterly numbers", message_id="<q1@example.test>")],
            archive=[msg(11, 15, "Quarterly numbers", message_id="<q1@example.test>")],
            sent=[msg(21, 25, "Reply", message_id="<r1@example.test>", sender="owner@example.org"), msg(22, 26, "x")],
        )
        monkeypatch.setattr(NoDateClient, "blank", frozenset({22}))
        monkeypatch.setattr(server, "client", lambda: NoDateClient(server))
        page = run(server, query("all", direction="outgoing"), own=OWN)
    finally:
        logger.removeHandler(handler)
        logger.setLevel(level)
    assert [i.subject for i in page.items] == ["Reply"]
    events = [json.loads(line)["event"] for line in handler.lines]
    assert events == ["folder_map_refreshed", "item_degraded"]
    text = "\n".join(handler.lines) + "\n".join(message for _, _, message in handler.raw)
    for secret in ("q1@example.test", "r1@example.test", "Sent Messages", "Archive", "INBOX", "@example", "Quarterly"):
        assert secret not in text
