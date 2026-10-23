"""IMAP `get_thread` on Gmail: the seed, the X-GM-THRID members in All Mail and Trash, the account without `\\All`
(spec 080 §5.2 `get_thread`, §6.5; WP13c T7, Option 4a, G3, Q-b, SG3, SG4, P1, P9)."""

import pytest

from mcp_hub.ids import MessageRef
from mcp_hub.providers.base import ProviderError, ThreadPage
from mcp_hub.providers.headers import SUMMARY_HEADER_ITEM
from tests.support.imap_fake import FakeImapServer
from tests.support.imap_threads import (
    DRAFT_L,
    GMAIL_ALL,
    GMAIL_SENT,
    GMAIL_TRASH,
    INBOX_L,
    SENT_L,
    THREAD,
    deadline,
    examined,
    fetches,
    gmail,
    gmail_key,
    mailbox,
    msg,
    names,
    ref,
    searches,
    thread,
)

THRID_SEARCH = f"NOT DRAFT X-GM-THRID {THREAD}".encode()
OWNER = "Owner <owner@example.org>"


def members(page: ThreadPage) -> list[tuple[str | None, str, int]]:
    """(subject, raw folder name, uid) of each member, oldest first."""
    out: list[tuple[str | None, str, int]] = []
    for member in page.members:
        assert isinstance(member.summary.ref, MessageRef)
        out.append((member.summary.subject, member.summary.ref.folder, member.summary.ref.uid))
    return out


def thread_searches(server: FakeImapServer) -> list[tuple[object, bytes]]:
    """(selected mailbox, criteria) of every X-GM-THRID search, by replaying the EXAMINEs of the command log."""
    selected: object = None
    out: list[tuple[object, bytes]] = []
    for name, argument in server.commands:
        if name == "EXAMINE":
            selected = argument
        elif name == "UID SEARCH" and b"X-GM-THRID" in argument:  # type: ignore[operator]
            out.append((selected, argument))  # type: ignore[arg-type]
    return out


def a_thread() -> dict[str, list]:  # type: ignore[type-arg]
    return {
        "all_mail": [
            msg(1, 60, "request", INBOX_L, thrid=THREAD, message_id="<a@example.test>"),
            msg(
                2,
                30,
                "Re: request",
                SENT_L,
                thrid=THREAD,
                sender=OWNER,
                to="Sender <sender@example.test>",
                message_id="<b@example.test>",
                in_reply_to="<a@example.test>",
                references="<a@example.test>",
            ),
            msg(3, 10, "Re: request", INBOX_L, thrid=THREAD, message_id="<c@example.test>"),
            msg(4, 5, "unrelated", INBOX_L, thrid=THREAD + 1, message_id="<x@example.test>"),
        ],
    }


def test_ref_seed_located_then_members_oldest_first() -> None:
    server = gmail(**a_thread())
    page = thread(server, seed=ref("b@example.test", server))
    assert page.outcome == "ok"
    assert page.thread == gmail_key()
    assert members(page) == [("request", GMAIL_ALL, 1), ("Re: request", GMAIL_ALL, 2), ("Re: request", GMAIL_ALL, 3)]
    assert [m.linked_by for m in page.members] == ["server", "server", "server"]  # the seed too (MF-P1)
    assert [m.summary.meta.direction for m in page.members] == ["incoming", "outgoing", "incoming"]
    assert [m.summary.meta.folder for m in page.members] == ["inbox", "sent", "inbox"]
    assert [m.body.text for m in page.members] == ["Hello", "Hello", "Hello"]
    assert [m.summary.snippet_text for m in page.members] == ["", "", ""]
    assert page.overview is not None
    assert (page.overview.message_count, page.overview.message_count_exact) == (3, True)
    assert page.overview.last_direction == "incoming"
    assert page.truncated is False
    # the location (Trash, then All Mail), then the thread search in the still selected All Mail, then Trash
    assert examined(server) == [GMAIL_TRASH, GMAIL_ALL, GMAIL_TRASH, GMAIL_ALL]
    assert thread_searches(server) == [(GMAIL_ALL, THRID_SEARCH), (GMAIL_TRASH, THRID_SEARCH)]


def test_gmail_seed_row_from_thread_fetch() -> None:
    """Q-b: the X-GM-THRID fetch carries the seed's row; only a seed outside the searched mailboxes is read alone."""
    server = gmail(**a_thread())
    thread(server, seed=ref("b@example.test", server))
    summary_fetches = [(uids, items) for uids, items in fetches(server) if SUMMARY_HEADER_ITEM in items]
    assert summary_fetches == [([1, 2, 3], ["FLAGS", "INTERNALDATE", "X-GM-LABELS", "X-GM-THRID", SUMMARY_HEADER_ITEM])]

    in_trash = gmail(
        all_mail=[msg(1, 60, "request", INBOX_L, thrid=THREAD, message_id="<a@example.test>")],
        trash=[
            msg(5, 30, "deleted reply", thrid=THREAD, message_id="<t@example.test>"),
            msg(6, 20, "other deleted", thrid=THREAD, message_id="<u@example.test>"),
        ],
    )
    page = thread(in_trash, seed=ref("t@example.test", in_trash), include_trash=False)
    assert members(page) == [("request", GMAIL_ALL, 1), ("deleted reply", GMAIL_TRASH, 5)]  # SG3: the seed is kept
    assert page.members[1].summary.meta.folder == "trash"
    assert thread_searches(in_trash) == [(GMAIL_ALL, THRID_SEARCH)]  # Trash is not searched for members
    seed_reads = [(uids, items) for uids, items in fetches(in_trash) if SUMMARY_HEADER_ITEM in items]
    assert seed_reads == [
        ([1], ["FLAGS", "INTERNALDATE", "X-GM-LABELS", "X-GM-THRID", SUMMARY_HEADER_ITEM]),  # still selected All Mail
        ([5], ["FLAGS", "INTERNALDATE", "X-GM-LABELS", "X-GM-THRID", SUMMARY_HEADER_ITEM]),  # the seed, read alone
    ]
    assert examined(in_trash)[:3] == [GMAIL_TRASH, GMAIL_ALL, GMAIL_TRASH]
    assert page.overview is not None
    assert page.overview.message_count_exact is True


def test_gmail_trash_search_not_started_after_reserve() -> None:
    """Q-b: after 12 s the Trash search is not started; the members so far, truncated, not exact (P11)."""
    data = a_thread()
    data["trash"] = [msg(5, 1, "deleted", thrid=THREAD, message_id="<t@example.test>")]
    late = gmail(**data)
    page = thread(late, key=gmail_key(), call=deadline(late, spent=12.5))
    assert [subject for subject, _, _ in members(page)] == ["request", "Re: request", "Re: request"]
    assert GMAIL_TRASH not in examined(late)
    assert page.truncated is True
    assert page.overview is not None
    assert page.overview.message_count_exact is False

    early = gmail(**data)
    page = thread(early, key=gmail_key(), call=deadline(early, spent=11.5))
    assert [subject for subject, _, _ in members(page)] == ["request", "Re: request", "Re: request", "deleted"]
    assert page.truncated is False
    assert page.overview is not None
    assert page.overview.message_count_exact is True


def test_member_ids_carry_folder_uidvalidity() -> None:
    """P1: every member id carries the UIDVALIDITY of the mailbox it was read in, also when the seed lies in another
    one; a mailbox without UIDVALIDITY is skipped and the list is not exact."""
    server = gmail(
        all_mail=[msg(1, 60, "request", INBOX_L, thrid=THREAD, message_id="<a@example.test>")],
        trash=[msg(5, 30, "deleted reply", thrid=THREAD, message_id="<t@example.test>")],
        validity={GMAIL_ALL: 11, GMAIL_TRASH: 17},
    )
    page = thread(server, seed=ref("t@example.test", server))
    refs = [m.summary.ref for m in page.members]
    assert refs == [MessageRef("gmail", GMAIL_ALL, 11, 1), MessageRef("gmail", GMAIL_TRASH, 17, 5)]
    assert examined(server)[:2] == [GMAIL_TRASH, GMAIL_ALL]  # the seed's mailbox is not the selected one

    no_validity = gmail(
        all_mail=[msg(1, 60, "request", INBOX_L, thrid=THREAD, message_id="<a@example.test>")],
        trash=[msg(5, 30, "deleted reply", thrid=THREAD, message_id="<t@example.test>")],
        validity={GMAIL_TRASH: None},
    )
    page = thread(no_validity, key=gmail_key())
    assert members(page) == [("request", GMAIL_ALL, 1)]
    assert page.overview is not None
    assert page.overview.message_count_exact is False
    assert page.truncated is True
    assert [criteria for box, criteria in thread_searches(no_validity) if box == GMAIL_TRASH] == []


def test_reserve_and_soft_stop_follow_the_call_deadline() -> None:
    """P9: the reserve and the soft limit read the deadline of each call, not one stored by an earlier call."""
    data = a_thread()
    data["trash"] = [msg(5, 1, "deleted", thrid=THREAD, message_id="<t@example.test>")]
    server = gmail(**data)
    box = mailbox(server)
    late = thread(server, key=gmail_key(), call=deadline(server, spent=13.0), box=box)
    assert len(late.members) == 3
    assert late.truncated is True
    fresh = thread(server, key=gmail_key(), call=deadline(server), box=box)
    assert len(fresh.members) == 4
    assert fresh.truncated is False
    with pytest.raises(ProviderError) as caught:
        thread(server, key=gmail_key(), call=deadline(server, spent=16.5), box=box)
    assert (caught.value.code, caught.value.cause) == ("upstream_timeout", "SoftStop")
    again = thread(server, key=gmail_key(), call=deadline(server), box=box)
    assert len(again.members) == 4
    assert again.truncated is False


def test_gmail_key_members_from_all_mail_and_trash() -> None:
    server = gmail(
        all_mail=[
            msg(1, 60, "request", INBOX_L, thrid=THREAD, message_id="<a@example.test>"),
            msg(2, 50, "draft reply", DRAFT_L, thrid=THREAD, message_id="<d@example.test>"),  # label draft (S1)
            msg(3, 40, "flag draft", flags=(b"\\Draft",), thrid=THREAD, message_id="<e@example.test>"),
            msg(4, 30, "reply", thrid=THREAD, message_id="<b@example.test>"),
        ],
        trash=[
            msg(5, 20, "deleted reply", thrid=THREAD, message_id="<t@example.test>"),
            msg(6, 10, "other thread", thrid=THREAD + 1, message_id="<o@example.test>"),
        ],
    )
    page = thread(server, key=gmail_key())
    assert members(page) == [("request", GMAIL_ALL, 1), ("reply", GMAIL_ALL, 4), ("deleted reply", GMAIL_TRASH, 5)]
    assert [m.summary.meta.folder for m in page.members] == ["inbox", "archive", "trash"]
    assert [m.linked_by for m in page.members] == ["server"] * 3
    assert [m.summary.meta.thread for m in page.members] == [gmail_key()] * 3
    assert page.thread == gmail_key()
    assert page.overview is not None
    assert page.overview.message_count == 3
    assert "LIST" in names(server)


def test_gmail_without_all_mail_merges_inbox_sent_trash() -> None:
    """G3: without `\\All` the thread is read in INBOX, `\\Sent` and `\\Trash`, merged by Message-ID; a self-sent
    copy in INBOX and `\\Sent` is one outgoing member."""
    server = gmail(
        with_all=False,
        inbox=[
            msg(1, 60, "request", thrid=THREAD, message_id="<a@example.test>"),
            msg(2, 20, "note to self", thrid=THREAD, sender=OWNER, message_id="<s@example.test>"),
        ],
        sent=[
            msg(3, 40, "reply", thrid=THREAD, sender=OWNER, message_id="<b@example.test>"),
            msg(4, 20, "note to self", thrid=THREAD, sender=OWNER, message_id="<s@example.test>"),
        ],
        trash=[msg(5, 10, "deleted", thrid=THREAD, message_id="<t@example.test>")],
    )
    page = thread(server, key=gmail_key())
    assert members(page) == [
        ("request", "INBOX", 1),
        ("reply", GMAIL_SENT, 3),
        ("note to self", "INBOX", 2),
        ("deleted", GMAIL_TRASH, 5),
    ]
    assert [m.summary.meta.direction for m in page.members] == ["incoming", "outgoing", "outgoing", "incoming"]
    assert page.overview is not None
    assert (page.overview.message_count, page.overview.message_count_exact) == (4, True)
    assert [box for box, _ in thread_searches(server)] == ["INBOX", GMAIL_SENT, GMAIL_TRASH]


def test_gmail_seed_without_thrid_alone_not_exact() -> None:
    """SG4: a seed without a valid X-GM-THRID is returned alone, `thread_key` null, not exact; no thread search."""
    server = gmail(
        all_mail=[
            msg(1, 60, "lonely", INBOX_L, thrid=None, message_id="<a@example.test>"),
            msg(2, 30, "other", INBOX_L, thrid=THREAD, message_id="<b@example.test>"),
        ]
    )
    page = thread(server, seed=ref("a@example.test", server))
    assert members(page) == [("lonely", GMAIL_ALL, 1)]
    assert page.thread is None
    assert page.members[0].linked_by == "server"
    assert page.members[0].summary.meta.thread is None
    assert page.overview is not None
    assert (page.overview.message_count, page.overview.message_count_exact) == (1, False)
    assert page.truncated is False
    assert thread_searches(server) == []


def test_gmail_trash_only_with_include_trash_seed_kept() -> None:
    """SG3: without `include_trash` Trash is not searched and trash members are not returned, but a seed in Trash
    is."""
    data = a_thread()
    data["trash"] = [
        msg(5, 25, "deleted one", thrid=THREAD, message_id="<t@example.test>"),
        msg(6, 15, "deleted two", thrid=THREAD, message_id="<u@example.test>"),
    ]
    server = gmail(**data)
    page = thread(server, key=gmail_key(), include_trash=False)
    assert all(folder != GMAIL_TRASH for _, folder, _ in members(page))
    assert GMAIL_TRASH not in examined(server)
    assert page.overview is not None
    assert page.overview.message_count_exact is True

    seeded = gmail(**data)
    page = thread(seeded, seed=ref("u@example.test", seeded), include_trash=False)
    assert [(subject, folder) for subject, folder, _ in members(page)] == [
        ("request", GMAIL_ALL),
        ("Re: request", GMAIL_ALL),
        ("deleted two", GMAIL_TRASH),
        ("Re: request", GMAIL_ALL),
    ]


def test_gmail_key_without_member_not_found() -> None:
    server = gmail(**a_thread())
    with pytest.raises(ProviderError) as caught:
        thread(server, key=gmail_key(THREAD + 7))
    assert (caught.value.code, caught.value.cause) == ("not_found", "NotFound")

    incomplete = gmail(**a_thread())
    original = incomplete.run

    def refuse_search(command: str, argument: object = None) -> None:
        if command == "UID SEARCH" and b"X-GM-THRID" in argument:  # type: ignore[operator]
            original(command, argument)
            raise incomplete.no("UID SEARCH")
        original(command, argument)

    incomplete.run = refuse_search  # type: ignore[method-assign]
    with pytest.raises(ProviderError) as caught:
        thread(incomplete, key=gmail_key(THREAD + 7))
    assert (caught.value.code, caught.value.cause) == ("upstream_timeout", "IncompleteSearch")


def test_gmail_cut_block_member_without_message_id_not_exact() -> None:
    """A 64 KiB summary block whose Message-ID did not end before the cut keeps the member (the server linked it)
    without a Message-ID, so it cannot merge, and the count is not exact (P11: not truncated)."""
    long_refs = " ".join(f"<r{i}@example.test>" for i in range(4000))
    block = f"From: {OWNER}\r\nSubject: long\r\nReferences: {long_refs}\r\nMessage-ID: <cut@example.test>\r\n\r\n"
    server = gmail(
        all_mail=[
            msg(1, 60, "request", INBOX_L, thrid=THREAD, message_id="<a@example.test>"),
            msg(2, 30, "long", INBOX_L, thrid=THREAD, headers=block.encode()),
        ]
    )
    page = thread(server, key=gmail_key())
    assert [m.summary.meta.message_id for m in page.members] == ["a@example.test", None]
    assert page.overview is not None
    assert page.overview.message_count_exact is False
    assert page.truncated is False
    assert searches(server)[-1] == THRID_SEARCH
