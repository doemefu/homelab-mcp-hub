"""The IMAP location step as `get_message_status` uses it (spec 080 §5.2 "Location, IMAP", §5.4; WP13b T7): the data
of the copies found, seeds, the rank tie rule, the reply mailbox searched last, and the chunked re-check fetch."""

from collections.abc import Callable, Mapping, Sequence
from datetime import timedelta

import pytest

from mcp_hub.providers.deadline import SoftStop
from mcp_hub.providers.folders import FolderEntry, FolderMapCache, inbox_entry
from mcp_hub.providers.imap_guard import GuardedImapClient
from mcp_hub.providers.imap_session import (
    FETCH_CHUNK_UIDS,
    SOFT_CHECK_EVERY,
    FoundCopy,
    ImapSession,
    LocateResult,
    _field_complete,
    locate_items,
)
from tests.support.imap_fake import FakeImapServer, FakeMessage
from tests.unit.test_imap_get_message import (
    GMAIL_ALL,
    GMAIL_SENT,
    GMAIL_TRASH,
    INBOX_L,
    MID,
    NOW,
    OWN,
    SENT_L,
    THREAD,
    OverMatchingClient,
    deadline,
    examined,
    fail_search_in,
    fetch_items,
    folder,
    gmail,
    icloud,
    mailbox,
    msg,
)

ID_ITEM = "BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)]<0.2048>"
FROM_ITEM = "BODY.PEEK[HEADER.FIELDS (MESSAGE-ID FROM)]<0.8192>"
PROJECTS = FolderEntry(raw_name="Projects", display_name="Projects", attributes=frozenset(), role="other")


def run_locate(
    server: FakeImapServer,
    ids: Sequence[str],
    *,
    own: frozenset[str] = frozenset(),
    seeds: Mapping[str, FoundCopy] | None = None,
    soft: float = 16.0,
    soft_check: Callable[[], None] | None = None,
) -> LocateResult:
    box = mailbox(server, account="gmail" if b"X-GM-EXT-1" in server.capabilities else "icloud", own=own)
    check = box._soft_check if soft_check is None else soft_check
    if seeds is None:
        return box._run(lambda session: session.locate(ids, soft_check=check), deadline(server, soft))
    return box._run(lambda session: session.locate(ids, seeds=seeds, soft_check=check), deadline(server, soft))


def raw(uid: int, minutes_ago: float, header: str) -> FakeMessage:
    return FakeMessage(uid, NOW - timedelta(minutes=minutes_ago), headers=(header + "\r\n\r\n").encode())


def seed(entry: FolderEntry, uid: int, minutes_ago: float, **kwargs: object) -> FoundCopy:
    folder_name = "other" if entry.role == "other" else entry.role
    return FoundCopy(entry, 7, uid, NOW - timedelta(minutes=minutes_ago), folder_name, **kwargs)  # type: ignore[arg-type]


def where(result: LocateResult, message_id: str = MID) -> tuple[str, int]:
    copy = result.found[message_id]
    return copy.entry.raw_name, copy.uid


# --- the data of a located copy -------------------------------------------------------------------------------


def test_found_copy_carries_folder_flags_labels_from_thread() -> None:
    server = icloud(
        archive=[
            msg(4, 60, "a", flags=(b"\\Seen", b"\\Answered"), sender="Owner <owner@example.org>", message_id=f"<{MID}>")
        ]
    )
    copy = run_locate(server, [MID], own=OWN).found[MID]
    assert (copy.entry.raw_name, copy.uidvalidity, copy.uid, copy.received_at) == (
        "Archive",
        7,
        4,
        NOW - timedelta(minutes=60),
    )
    assert (copy.folder, copy.flags, copy.labels) == ("archive", frozenset({b"\\seen", b"\\answered"}), frozenset())
    assert (copy.from_address, copy.thread_id) == ("owner@example.org", None)

    server = gmail(
        all_mail=[msg(1, 60, "g", INBOX_L, SENT_L, flags=(b"\\Flagged",), thrid=THREAD, message_id=f"<{MID}>")]
    )
    copy = run_locate(server, [MID], own=OWN).found[MID]
    assert (copy.entry.raw_name, copy.folder) == (GMAIL_ALL, "inbox")  # All Mail decides per message by its labels
    assert (copy.flags, copy.labels) == (frozenset({b"\\flagged"}), frozenset({b"\\inbox", b"\\sent"}))
    assert (copy.from_address, copy.thread_id) == ("sender@example.test", THREAD)


def test_from_fetched_only_with_own_addresses_and_8_kib_partial() -> None:
    assert locate_items(gmail=False, with_from=False) == ["FLAGS", "INTERNALDATE", ID_ITEM]
    assert locate_items(gmail=False, with_from=True) == ["FLAGS", "INTERNALDATE", FROM_ITEM]
    assert locate_items(gmail=True, with_from=True) == ["FLAGS", "INTERNALDATE", FROM_ITEM, "X-GM-LABELS", "X-GM-THRID"]

    server = icloud(inbox=[msg(1, 5, "a", message_id=f"<{MID}>")])
    assert run_locate(server, [MID]).found[MID].from_address is None  # no FROM without own addresses
    assert fetch_items(server) == [["FLAGS", "INTERNALDATE", ID_ITEM]]

    server = icloud(inbox=[msg(1, 5, "a", message_id=f"<{MID}>")])
    assert run_locate(server, [MID], own=OWN).found[MID].from_address == "sender@example.test"
    assert fetch_items(server) == [["FLAGS", "INTERNALDATE", FROM_ITEM]]


def test_gmail_locate_fetches_thread_id() -> None:
    server = gmail(all_mail=[msg(1, 5, "a", INBOX_L, thrid=THREAD, message_id=f"<{MID}>")])
    assert run_locate(server, [MID]).found[MID].thread_id == THREAD
    assert fetch_items(server) == [["FLAGS", "INTERNALDATE", ID_ITEM, "X-GM-LABELS", "X-GM-THRID"]]


@pytest.mark.parametrize("value", [-1, 2**64, "5", True])
def test_invalid_thread_id_is_none(value: object) -> None:
    server = gmail(all_mail=[msg(1, 5, "a", INBOX_L, thrid=value, message_id=f"<{MID}>")])  # type: ignore[arg-type]
    copy = run_locate(server, [MID]).found[MID]
    assert copy.thread_id is None
    assert copy.entry.raw_name == GMAIL_ALL  # the copy itself still counts


# --- A-sec3: a header block cut before its Message-ID ---------------------------------------------------------


@pytest.mark.parametrize(
    ("own", "header"),
    [
        (OWN, f"From: {'x' * 9216} <sender@example.test>\r\nMessage-ID: <{MID}>"),  # From fills the 8 KiB item
        (OWN, f"Message-ID: ({'p' * 9216}) <{MID}>"),  # a comment-padded Message-ID line past 8 KiB
        (frozenset(), f"Message-ID: ({'p' * 3000}) <{MID}>"),  # past 2 KiB without FROM
    ],
    ids=["long_from", "padded_8k", "padded_2k"],
)
def test_cut_header_block_marks_missed_not_gone(own: frozenset[str], header: str) -> None:
    server = icloud(archive=[raw(4, 60, header)])
    result = run_locate(server, [MID], own=own)
    assert (result.found, result.incomplete, result.complete) == ({}, frozenset({MID}), False)  # never gone

    # found elsewhere: the cut block may hide an earlier copy, so the copy found is uncertain
    server = icloud(inbox=[msg(1, 5, "later", message_id=f"<{MID}>")], archive=[raw(4, 60, header)])
    result = run_locate(server, [MID], own=own)
    assert (where(result), result.uncertain, result.incomplete) == (("INBOX", 1), frozenset({MID}), frozenset())

    # a block at its cap with a parsed (other) Message-ID is a plain non-match: the case-insensitive hit is re-checked
    decoy = f"Message-ID: <{MID.upper()}>\r\nFrom: {'x' * 9216} <sender@example.test>"
    server = icloud(archive=[raw(4, 60, decoy)])
    result = run_locate(server, [MID], own=OWN)
    assert (result.found, result.complete) == ({}, True)
    assert [list(argument[0]) for name, argument in server.commands if name == "UID FETCH"] == [[4]]  # type: ignore[index]


def _cut_inside(field_value: str, before: str, cap: int = 8192) -> str:
    """A `From` line padded so that a block of `cap` bytes ends inside `before + field_value`, right after `before`."""
    pad = cap - len(before) - len("From: () <sender@example.test>\r\n")
    return f"From: ({'x' * pad}) <sender@example.test>\r\n{before}{field_value}"


def test_unbracketed_message_id_cut_mid_value_is_missed(monkeypatch: pytest.MonkeyPatch) -> None:
    """T7-1: `Message-ID: abc@example.test` as the last field, cut to `abc@example.te`, still parses as a valid,
    different id; a field not CRLF-terminated before the cut is a failed re-check, never a non-match."""
    wanted = "abc@example.test"
    header = _cut_inside("st", f"Message-ID: {wanted[:-2]}")
    monkeypatch.setattr(FakeImapServer, "client", lambda self: OverMatchingClient(self))
    server = icloud(archive=[raw(4, 60, header)])
    result = run_locate(server, [wanted], own=OWN)
    assert (result.found, result.incomplete, result.complete) == ({}, frozenset({wanted}), False)

    server = icloud(inbox=[msg(1, 5, "later", message_id=wanted)], archive=[raw(4, 60, header)])
    result = run_locate(server, [wanted], own=OWN)
    assert (where(result, wanted), result.uncertain, result.incomplete) == (
        ("INBOX", 1),
        frozenset({wanted}),
        frozenset(),
    )

    # the whole value before the cut, the next field cut instead: the copy counts
    complete = f"Message-ID: <{MID}>\r\n" + _cut_inside("x", "Subject: ", cap=8192 - len(f"Message-ID: <{MID}>\r\n"))
    server = icloud(archive=[raw(4, 60, complete)])
    result = run_locate(server, [MID], own=OWN)
    assert (where(result), result.complete) == (("Archive", 4), True)


def test_cut_from_line_gives_no_from_address() -> None:
    """T7-3: a `From` cut at the cap may be a prefix of the real address (here one of the own addresses)."""
    pad = 8192 - len(f"Message-ID: <{MID}>\r\n") - len("From: () owner@example.org")
    header = f"Message-ID: <{MID}>\r\nFrom: ({'x' * pad}) owner@example.org.evil.example"
    server = icloud(inbox=[raw(1, 60, header)])
    copy = run_locate(server, [MID], own=OWN).found[MID]
    assert (copy.entry.raw_name, copy.from_address) == ("INBOX", None)

    server = icloud(inbox=[raw(1, 60, f"Message-ID: <{MID}>\r\nFrom: Owner <owner@example.org>")])
    assert run_locate(server, [MID], own=OWN).found[MID].from_address == "owner@example.org"


@pytest.mark.parametrize(
    ("block", "complete"),
    [
        (b"From: a@example.test\r\nMessage-ID: <m@x>\r\n\r\n", True),
        (b"Message-ID: <m@x>\r\nFrom: a@exa", True),  # the next field started: Message-ID ended
        (b"From: a@example.test\r\nMessage-ID: m@x.exa", False),  # cut mid-value
        (b"From: a@example.test\r\nMessage-ID: <m@x>\r\n", False),  # a folded line may follow the cut
        (b"Message-ID:\r\n <m@x>\r\n\tcomment", False),  # cut inside a continuation line
        (b"Message-ID:\r\n <m@x>\r\nFrom: a", True),
        (b"message-id : <m@x>\r\nFrom: a", True),  # field names in any case
        (b"From: a@example.test\r\nSubject: s", True),  # no Message-ID field: nothing of it was cut
        (b"Message-ID: <m@x>\r\nX: y\r\nMessage-ID: <n@x", True),  # only the first field counts
    ],
)
def test_field_complete_before_the_cut(block: bytes, complete: bool) -> None:
    assert _field_complete(block, b"message-id") is complete


def test_in_sent_records_any_sent_copy() -> None:
    other = "o@example.test"
    server = icloud(
        inbox=[msg(1, 60, "in", message_id=f"<{MID}>"), msg(2, 60, "only inbox", message_id=f"<{other}>")],
        sent=[
            msg(3, 30, "sent copy", message_id=f"<{MID}>"),
            msg(4, 30, "draft", flags=(b"\\Draft",), message_id=f"<{other}>"),
        ],
    )
    server.honour_not_draft = False  # the draft is dropped on its FLAGS, never counted as a sent copy
    result = run_locate(server, [MID, other])
    assert (where(result), result.found[MID].folder) == (("INBOX", 1), "inbox")  # the earliest copy stays
    assert result.in_sent == frozenset({MID})

    server = gmail(all_mail=[msg(1, 60, "self", INBOX_L, SENT_L, message_id=f"<{MID}>")])
    result = run_locate(server, [MID])
    assert (result.found[MID].folder, result.in_sent) == ("inbox", frozenset({MID}))  # Gmail: the \Sent label


# --- seeds (in-place copies of `m` ids; E-im4, IR3) -----------------------------------------------------------


def test_seed_kept_unless_an_earlier_copy_is_found() -> None:
    server = icloud(inbox=[msg(1, 60, "earlier", message_id=f"<{MID}>")], other=[folder("Projects")])
    result = run_locate(server, [MID], seeds={MID: seed(PROJECTS, 9, 30)})
    assert (where(result), result.complete) == (("INBOX", 1), True)

    planted = seed(inbox_entry("INBOX"), 1, 60)
    server = icloud(archive=[msg(4, 30, "later", message_id=f"<{MID}>")])
    result = run_locate(server, [MID], seeds={MID: planted})
    assert result.found[MID] is planted

    own_copy = seed(inbox_entry("INBOX"), 1, 30, flags=frozenset({b"\\seen"}))
    server = icloud(inbox=[msg(1, 60, "same copy", message_id=f"<{MID}>")])
    result = run_locate(server, [MID], seeds={MID: own_copy})
    assert result.found[MID] is own_copy  # the same folder, UIDVALIDITY and UID: a no-op, whatever was read

    server = icloud()  # the search does not see the fresh copy (index lag): the seed stands
    lagging = seed(inbox_entry("INBOX"), 1, 1)
    result = run_locate(server, [MID], seeds={MID: lagging})
    assert (result.found[MID], result.complete) == (lagging, True)
    assert "Projects" not in examined(server)

    seeded_sent = seed(FolderEntry("Sent Messages", "Sent Messages", frozenset(), "sent"), 3, 60)
    assert run_locate(icloud(), [MID], seeds={MID: seeded_sent}).in_sent == frozenset({MID})


def test_seed_must_name_a_located_id() -> None:
    server = icloud()
    client = GuardedImapClient(server.client(), deadline(server), registry_inbox_raw="INBOX")
    client.login("u", "p")
    session = ImapSession(
        client, account="icloud", registry_inbox="INBOX", folder_maps=FolderMapCache(), own=frozenset()
    )
    with pytest.raises(ValueError, match="seed"):
        session.locate([MID], seeds={"x@example.test": seed(inbox_entry("INBOX"), 1, 60)}, soft_check=lambda: None)


def test_seed_stands_when_location_is_incomplete() -> None:
    planted = seed(inbox_entry("INBOX"), 1, 60)
    server = icloud(archive=[msg(4, 600, "maybe earlier", message_id=f"<{MID}>")])
    fail_search_in(server, {MID: "Archive"})
    result = run_locate(server, [MID], seeds={MID: planted})
    assert result.found[MID] is planted
    assert (result.uncertain, result.incomplete, result.complete) == (frozenset({MID}), frozenset(), False)

    server = icloud(step=1.0)  # a soft stop before the first EXAMINE
    result = run_locate(server, [MID], seeds={MID: planted}, soft=3.5)
    assert result.found[MID] is planted
    assert (result.uncertain, result.incomplete) == (frozenset({MID}), frozenset())


def test_gmail_inbox_seed_yields_all_mail_copy() -> None:
    server = gmail(
        inbox=[msg(5, 60, "inbox view", INBOX_L, message_id=f"<{MID}>")],
        all_mail=[msg(1, 60, "all mail copy", INBOX_L, message_id=f"<{MID}>")],
    )
    result = run_locate(server, [MID], seeds={MID: seed(inbox_entry("INBOX"), 5, 60)})
    assert where(result) == (GMAIL_ALL, 1)  # the same copy as its `ref` reports: INBOX ranks after the core list


# --- search order and the rank tie rule (E-im5) ---------------------------------------------------------------


def test_core_search_order_puts_reply_mailbox_last() -> None:
    server = icloud(other=[folder("Projects")])
    run_locate(server, [MID])
    assert examined(server) == ["INBOX", "Archive", "Deleted Messages", "Sent Messages", "Projects"]

    server = gmail()
    run_locate(server, [MID])
    assert examined(server) == [GMAIL_TRASH, GMAIL_ALL]

    server = gmail(with_all=False)
    run_locate(server, [MID])
    assert examined(server) == ["INBOX", GMAIL_TRASH, GMAIL_SENT]


def test_tie_by_rank_not_search_order() -> None:
    server = icloud(
        archive=[msg(4, 60, "archive", message_id=f"<{MID}>")], sent=[msg(3, 60, "sent", message_id=f"<{MID}>")]
    )
    assert where(run_locate(server, [MID])) == ("Archive", 4)

    server = icloud(
        trash=[msg(2, 60, "trash", message_id=f"<{MID}>")], sent=[msg(3, 60, "sent", message_id=f"<{MID}>")]
    )
    assert where(run_locate(server, [MID])) == ("Sent Messages", 3)  # trash is searched first, sent ranks first

    server = icloud(inbox=[msg(1, 60, "inbox", message_id=f"<{MID}>")], other=[folder("Projects")])
    assert where(run_locate(server, [MID], seeds={MID: seed(PROJECTS, 9, 60)})) == ("INBOX", 1)

    server = gmail(
        all_mail=[msg(1, 60, "all", INBOX_L, message_id=f"<{MID}>")],
        trash=[msg(5, 60, "trash", message_id=f"<{MID}>")],
    )
    assert where(run_locate(server, [MID])) == (GMAIL_ALL, 1)


# --- chunked re-check fetch (A-sec3: rows reduced per chunk, not accumulated) ---------------------------------


def test_locate_fetch_processed_per_chunk(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_accumulating_fetch(*args: object, **kwargs: object) -> None:
        raise AssertionError("locate must not accumulate rows through ImapSession.fetch")

    monkeypatch.setattr(ImapSession, "fetch", no_accumulating_fetch)
    copies = [msg(uid, 60, "copy", message_id=f"<{MID}>") for uid in range(1, FETCH_CHUNK_UIDS + 1)]
    earliest = msg(FETCH_CHUNK_UIDS + 1, 600, "earliest", message_id=f"<{MID}>")
    server = icloud(archive=[*copies, earliest])
    result = run_locate(server, [MID])
    assert (where(result), result.complete) == (("Archive", FETCH_CHUNK_UIDS + 1), True)
    fetched = [list(argument[0]) for name, argument in server.commands if name == "UID FETCH"]  # type: ignore[index]
    assert fetched == [list(range(1, FETCH_CHUNK_UIDS + 1)), [FETCH_CHUNK_UIDS + 1]]

    server = icloud(archive=[*copies, earliest])
    original = server.run
    count = {"fetch": 0}

    def run(command: str, argument: object = None) -> None:
        if command == "UID FETCH":
            count["fetch"] += 1
            if count["fetch"] == 2:
                server.fail["UID FETCH"] = server.no("UID FETCH")
        original(command, argument)

    server.run = run  # type: ignore[method-assign]
    result = run_locate(server, [MID])
    assert (where(result), result.uncertain) == (("Archive", 1), frozenset({MID}))  # the lost chunk may be earlier


def test_soft_check_per_chunk_and_every_rows() -> None:
    """T7-2 (SR2): the soft check runs every SOFT_CHECK_EVERY rows while parsing and before every further chunk (the
    guard checks before every other command); a stop sends no further FETCH and leaves the open refs uncertain or
    incomplete."""
    copies = [msg(uid, 60, "copy", message_id=f"<{MID}>") for uid in range(1, FETCH_CHUNK_UIDS + 2)]
    copies[SOFT_CHECK_EVERY + 49] = msg(SOFT_CHECK_EVERY + 50, 600, "earliest in chunk 1", message_id=f"<{MID}>")
    calls = {"n": 0}

    def counting() -> None:
        calls["n"] += 1

    run_locate(icloud(archive=copies), [MID], soft_check=counting)
    assert calls["n"] == (FETCH_CHUNK_UIDS - 1) // SOFT_CHECK_EVERY + 1  # rows of chunk 1, then before chunk 2

    def stop() -> None:
        raise SoftStop

    server = icloud(archive=copies, other=[folder("Projects")])
    result = run_locate(server, [MID], soft_check=stop)
    assert len([name for name, _ in server.commands if name == "UID FETCH"]) == 1  # no further FETCH
    assert (where(result), result.uncertain) == (("Archive", 1), frozenset({MID}))  # stopped before row 101
    assert examined(server) == ["INBOX", "Archive"]

    server = icloud(inbox=[msg(1, 5, "a", message_id=f"<{MID}>")], step=1.0)
    result = run_locate(server, [MID, "b@example.test"], soft=5.5)  # one row: the guard stops the next EXAMINE
    assert (where(result), result.uncertain, result.incomplete) == (
        ("INBOX", 1),
        frozenset({MID}),
        frozenset({"b@example.test"}),
    )
    assert examined(server) == ["INBOX"]


def test_soft_limit_after_the_last_chunk_keeps_a_finished_search() -> None:
    """PR #51 review CR2: the soft limit passing while the last chunk of the last mailbox is parsed does not discard
    the finished search; a ref found stays certain and an absent ref is not incomplete."""
    copies = [msg(uid, 60, "copy", message_id=f"<{MID}>") for uid in range(1, FETCH_CHUNK_UIDS + 2)]
    server = icloud(sent=copies)  # the reply mailbox is searched last; no `other` mailbox follows

    def stop_after_last_fetch() -> None:
        if len([name for name, _ in server.commands if name == "UID FETCH"]) == 2:
            raise SoftStop

    result = run_locate(server, [MID, "b@example.test"], soft_check=stop_after_last_fetch)
    assert (where(result), result.complete, result.uncertain, result.incomplete) == (
        ("Sent Messages", 1),
        True,
        frozenset(),
        frozenset(),
    )


def test_soft_stop_inside_an_other_mailbox_leaves_its_copy_uncertain() -> None:
    """A ref first found in an `other` mailbox may have an earlier copy in a chunk of that mailbox not yet read."""
    copies = [msg(uid, 60, "copy", message_id=f"<{MID}>") for uid in range(1, FETCH_CHUNK_UIDS + 2)]
    calls = {"n": 0}

    def stop_after_chunk_one() -> None:
        calls["n"] += 1
        if calls["n"] > (FETCH_CHUNK_UIDS - 1) // SOFT_CHECK_EVERY:  # the per-chunk check after chunk 1
            raise SoftStop

    server = icloud(other=[folder("Projects", (), copies)])
    result = run_locate(server, [MID], soft_check=stop_after_chunk_one)
    assert (where(result), result.uncertain) == (("Projects", 1), frozenset({MID}))


def _session(server: FakeImapServer) -> ImapSession:
    client = GuardedImapClient(server.client(), deadline(server), registry_inbox_raw="INBOX")
    client.login("u", "p")
    session = ImapSession(
        client, account="icloud", registry_inbox="INBOX", folder_maps=FolderMapCache(), own=frozenset()
    )
    session.examine(inbox_entry("INBOX"))
    return session


def test_fetch_chunks_yields_per_chunk_and_reports_no() -> None:
    server = icloud(inbox=[msg(uid, 5, "m", message_id=f"<{uid}@example.test>") for uid in range(1, 6)])
    original = server.run
    count = {"fetch": 0}

    def run(command: str, argument: object = None) -> None:
        if command == "UID FETCH":
            count["fetch"] += 1
            if count["fetch"] == 2:
                server.fail["UID FETCH"] = server.no("UID FETCH")
        original(command, argument)

    server.run = run  # type: ignore[method-assign]
    session = _session(server)
    chunks = session.fetch_chunks([5, 3, 1, 2, 4, 3], ["FLAGS"], chunk=2)
    first = next(chunks)
    assert sorted(first) == [1, 2]
    assert count["fetch"] == 1  # lazy: the next chunk is fetched only when asked for
    assert [sorted(rows) for rows in chunks] == [[], [5]]  # the NO chunk yields {} for its UIDs
    fetched = [list(argument[0]) for name, argument in server.commands if name == "UID FETCH"]  # type: ignore[index]
    assert fetched == [[1, 2], [3, 4], [5]]

    server = icloud(inbox=[msg(1, 5, "m")])
    session = _session(server)
    server.fail["UID FETCH"] = server.bad("UID FETCH")
    with pytest.raises(Exception, match="BAD"):
        list(session.fetch_chunks([1], ["FLAGS"], chunk=1))


@pytest.mark.parametrize("chunk", [0, FETCH_CHUNK_UIDS + 1, True])
def test_fetch_chunks_refuses_chunk_sizes(chunk: int) -> None:
    session = _session(icloud())
    with pytest.raises(ValueError, match="chunk"):
        list(session.fetch_chunks([1], ["FLAGS"], chunk=chunk))
