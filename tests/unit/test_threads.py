"""Thread helpers of get_thread: participants, copy merge, member choice, overview and body preparation (spec 080
§5.2 `get_thread`, §5.5; WP13c T5: A-mf3, MF-P3, SG3, Q1, E-td2)."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from mcp_hub.ids import MessageRef
from mcp_hub.providers.base import OMITTED_BODY, Direction, LinkedBy, MailMeta, MailSummary, order_key
from mcp_hub.providers.folders import MailFolder
from mcp_hub.providers.threads import (
    BODY_KEEP_FACTOR,
    FOLDER_RANK,
    MAX_PARTICIPANTS,
    MAX_THREAD_MESSAGES,
    ThreadCopy,
    choose_members,
    merge_copies,
    overview,
    participants_of,
    prepare_body,
    server_body,
)

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
OWN = frozenset({"me@example.org"})
_FOLDERS: dict[MailFolder, str] = {
    "inbox": "INBOX",
    "sent": "Sent Messages",
    "archive": "Archive",
    "trash": "Deleted Messages",
    "other": "Projects",
}


def copy(
    uid: int,
    *,
    at: datetime = T0,
    folder: MailFolder = "inbox",
    message_id: str | None = None,
    direction: Direction = "incoming",
    linked_by: LinkedBy = "headers",
    participants: tuple[str, ...] = (),
    seed: bool = False,
) -> ThreadCopy:
    meta = MailMeta(folder=folder, folder_name=_FOLDERS[folder], direction=direction, message_id=message_id)
    item = MailSummary(MessageRef("icloud", _FOLDERS[folder], 7, uid), at, False, False, None, None, "s", "", meta=meta)
    return ThreadCopy(item, linked_by, participants, folder == "sent", seed=seed)


def test_constants() -> None:
    assert (MAX_THREAD_MESSAGES, MAX_PARTICIPANTS, BODY_KEEP_FACTOR) == (50, 20, 4)
    assert FOLDER_RANK == {"inbox": 0, "archive": 1, "sent": 2, "trash": 3, "other": 4}


def test_participants_of_direction_rules() -> None:
    to = ("Bob@example.org", "me@example.org")
    cc = ("carol@example.org", "BOB@example.org", "Me+List@Example.org", "")
    incoming = participants_of("incoming", "Alice@Example.org", to, cc, OWN)
    assert incoming == ("Alice@Example.org", "carol@example.org", "BOB@example.org")  # From + Cc, header order
    outgoing = participants_of("outgoing", "me@example.org", to, cc, OWN)
    assert outgoing == ("Bob@example.org", "carol@example.org")  # To + Cc; own and +tag removed, first spelling kept
    assert participants_of("incoming", None, to, (), OWN) == ()
    assert participants_of("incoming", "me+news@example.org", (), (), OWN) == ()
    assert participants_of("outgoing", "x@example.org", ("A@Example.org", "a@example.org"), (), frozenset()) == (
        "A@Example.org",
    )


def test_merge_copies_earliest_then_rank_outgoing_if_any_in_sent() -> None:
    # Self-cc: the sent copy and the inbox copy of one Message-ID become one outgoing member.
    sent = copy(
        1, at=T0, folder="sent", message_id="m1@example.org", direction="outgoing", participants=("b@example.test",)
    )
    inbox = copy(2, at=T0 + timedelta(seconds=3), message_id="m1@example.org", participants=("c@example.test",))
    [merged] = merge_copies([inbox, sent], include_trash=True)
    assert merged.summary.ref == sent.summary.ref  # earliest received_at wins (S-e)
    assert (merged.in_sent, merged.summary.meta.direction) == (True, "outgoing")
    # The inbox copy wins when it is earlier, but the member stays outgoing with the sent copy's participants.
    early = replace(inbox, summary=replace(inbox.summary, received_at=T0 - timedelta(seconds=1)))
    [merged] = merge_copies([sent, early], include_trash=True)
    assert merged.summary.ref == early.summary.ref
    assert merged.summary.meta.folder == "inbox"
    assert (merged.in_sent, merged.summary.meta.direction) == (True, "outgoing")
    assert merged.participants == ("b@example.test",)
    # Archive and trash at the same second: the folder rank decides (inbox, archive, sent, trash, other).
    trash = copy(3, folder="trash", message_id="m2@example.org")
    archive = copy(4, folder="archive", message_id="m2@example.org")
    other = copy(5, folder="other", message_id="m2@example.org")
    [merged] = merge_copies([other, trash, archive], include_trash=True)
    assert merged.summary.ref == archive.summary.ref
    assert (merged.in_sent, merged.summary.meta.direction) == (False, "incoming")
    # Without a valid Message-ID copies are never merged.
    loose = [copy(6), copy(7), copy(8, message_id="not valid"), copy(9, message_id="not valid")]
    assert [c.summary.ref for c in merge_copies(loose, include_trash=True)] == [c.summary.ref for c in loose]
    # Distinct Message-IDs stay apart, in first-seen order.
    a, b = copy(10, message_id="a@example.org"), copy(11, message_id="b@example.org")
    later_b = replace(b, summary=replace(b.summary, received_at=T0 + timedelta(1)))
    assert merge_copies([b, a, later_b], include_trash=True) == [b, a]


def test_merge_keeps_seed_flag_when_other_copy_wins() -> None:
    # MF-P3: an `id` seed in archive with an earlier inbox copy found by a member round.
    seed = copy(1, at=T0 + timedelta(seconds=9), folder="archive", message_id="s@example.org", seed=True)
    seed = replace(seed, linked_by="server")
    earlier = copy(2, at=T0, message_id="s@example.org", linked_by="subject")
    for order in ([seed, earlier], [earlier, seed]):
        [merged] = merge_copies(order, include_trash=False)
        assert merged.summary.ref == earlier.summary.ref
        assert (merged.seed, merged.linked_by) == (True, "server")
    # G-R3: a seed copy in trash plus a non-trash copy, without include_trash, is one member: the earliest copy.
    in_trash = copy(3, at=T0, folder="trash", message_id="t@example.org", seed=True)
    later = copy(4, at=T0 + timedelta(seconds=1), message_id="t@example.org")
    counted, kept, capped = choose_members(merge_copies([later, in_trash], include_trash=False), include_trash=False)
    assert [m.summary.ref for m in kept] == [m.summary.ref for m in counted] == [in_trash.summary.ref]
    assert (kept[0].seed, capped) == (True, False)
    in_trash_later = replace(in_trash, summary=replace(in_trash.summary, received_at=T0 + timedelta(seconds=5)))
    _, kept, _ = choose_members(merge_copies([in_trash_later, later], include_trash=False), include_trash=False)
    assert [m.summary.ref for m in kept] == [later.summary.ref]
    assert kept[0].seed


def test_merge_drops_non_seed_trash_copies_first() -> None:
    # F-2: without include_trash, an earlier trash copy of a non-seed message does not hide its inbox copy.
    in_trash = copy(1, at=T0, folder="trash", message_id="n@example.org")
    in_inbox = copy(2, at=T0 + timedelta(seconds=4), message_id="n@example.org")
    counted, kept, _ = choose_members(merge_copies([in_trash, in_inbox], include_trash=False), include_trash=False)
    assert [m.summary.ref for m in counted] == [m.summary.ref for m in kept] == [in_inbox.summary.ref]
    assert kept[0].summary.meta.folder == "inbox"
    counted, _, _ = choose_members(merge_copies([in_trash, in_inbox], include_trash=True), include_trash=True)
    assert [m.summary.ref for m in counted] == [in_trash.summary.ref]  # S-e: the earliest copy
    # A message only in trash, with or without a Message-ID, is gone; a seed group keeps all its copies (G-R3).
    only_trash = [copy(3, folder="trash", message_id="o@example.org"), copy(4, folder="trash")]
    assert merge_copies(only_trash, include_trash=False) == []
    assert len(merge_copies(only_trash, include_trash=True)) == 2
    seed_group = [copy(5, at=T0, folder="trash", message_id="s@example.org"), copy(6, message_id="s@example.org")]
    seed_group[1] = replace(
        seed_group[1], seed=True, summary=replace(seed_group[1].summary, received_at=T0 + timedelta(1))
    )
    [merged] = merge_copies(seed_group, include_trash=False)
    assert (merged.summary.ref, merged.seed) == (seed_group[0].summary.ref, True)
    lone_seed = copy(7, folder="trash", seed=True)
    assert merge_copies([lone_seed], include_trash=False) == [lone_seed]


def test_choose_members_newest_fifty_seed_from_trash() -> None:
    # SG3/Q1: a trash seed is kept without include_trash; other trash members are dropped.
    seed = copy(1, at=T0, folder="trash", seed=True)
    dropped = copy(2, at=T0 + timedelta(seconds=1), folder="trash")
    member = copy(3, at=T0 + timedelta(seconds=2))
    counted, kept, capped = choose_members([member, dropped, seed], include_trash=False)
    assert [m.summary.ref for m in kept] == [m.summary.ref for m in counted] == [seed.summary.ref, member.summary.ref]
    assert not capped
    counted, _, _ = choose_members([member, dropped, seed], include_trash=True)
    assert len(counted) == 3
    # 60 members, pairs within one second: the newest 50 are kept, oldest first by (received_at, k).
    many = [copy(100 + i, at=T0 + timedelta(seconds=i // 2)) for i in range(60)]
    oldest_seed = replace(many[0], seed=True)  # the cap applies to the seed like any member (P6)
    counted, kept, capped = choose_members([oldest_seed, *many[1:]][::-1], include_trash=False)
    ordered = sorted([oldest_seed, *many[1:]], key=lambda c: order_key(c.summary))
    assert [c.summary.ref for c in counted] == [c.summary.ref for c in ordered]
    assert [c.summary.ref for c in kept] == [c.summary.ref for c in ordered[-50:]]
    assert (capped, len(counted)) == (True, 60)
    assert not any(c.seed for c in kept)
    _, kept, capped = choose_members(many[:50], include_trash=False)
    assert (len(kept), capped) == (50, False)


def test_overview_last_direction_and_times_from_all_counted() -> None:
    first_out = copy(1, at=T0, folder="sent", direction="outgoing")
    later_in = copy(2, at=T0 + timedelta(seconds=5))
    last_out = copy(3, at=T0 + timedelta(seconds=9), direction="outgoing")  # own From in the inbox
    view = overview([first_out, later_in, last_out], exact=False)
    assert (view.message_count, view.message_count_exact) == (3, False)
    assert view.last_direction == "outgoing"
    assert (view.last_incoming_at, view.last_outgoing_at) == (later_in.summary.received_at, T0 + timedelta(seconds=9))
    view = overview([later_in], exact=True)
    assert (view.last_direction, view.last_incoming_at, view.last_outgoing_at) == (
        "incoming",
        later_in.summary.received_at,
        None,
    )
    # Members beyond the cap still count: the newest of all counted decides, in (received_at, k) order.
    many = [copy(10 + i, at=T0 + timedelta(seconds=i)) for i in range(60)] + [first_out]
    view = overview(many, exact=True)
    assert view.message_count == 61
    assert (view.last_outgoing_at, view.last_direction) == (T0, "incoming")
    with pytest.raises(ValueError, match="message_count"):
        overview([], exact=True)


def test_overview_participants_newest_first_twenty() -> None:
    members = [
        copy(i, at=T0 + timedelta(seconds=i), participants=(f"p{i}@example.org", "all@example.org")) for i in range(25)
    ]
    view = overview(members, exact=True)
    expected = ["p24@example.org", "all@example.org", *(f"p{i}@example.org" for i in range(23, 5, -1))]
    assert list(view.participants) == expected
    assert len(view.participants) == MAX_PARTICIPANTS
    mixed = [
        copy(1, participants=("ann@example.org",)),
        copy(2, at=T0 + timedelta(seconds=1), participants=("Ann@Example.org",)),
    ]
    assert overview(mixed, exact=True).participants == ("Ann@Example.org",)  # the newest spelling, casefold dedupe


QUOTED = (
    "Thanks, see below.\r\n\r\nOn Tue, 1 Oct 2026 at 10:00, Bob <bob@example.org> wrote:\r\n> earlier\u00a0text\r\n"
)


def test_prepare_body_counts_before_removal_and_keeps_four_times() -> None:
    body = prepare_body(QUOTED, "text/plain", subject="Re: plan", max_chars=200, fetch_cut=False)
    normalised = QUOTED.replace("\r\n", "\n").replace("\u00a0", " ")
    assert body.original_chars == len(normalised)  # counted after CRLF/NBSP normalisation, before quote removal
    assert (body.text, body.quote_removal, body.source) == ("Thanks, see below.", "heuristic", "text/plain")
    assert (body.cut, body.omitted) == (False, False)
    plain = prepare_body("a\u202fb\r\nc", "text/html-converted", subject=None, max_chars=200, fetch_cut=True)
    assert (plain.text, plain.quote_removal, plain.original_chars, plain.cut) == ("a b\nc", "none", 5, True)
    long = "x" * (BODY_KEEP_FACTOR * 200 + 1)
    kept = prepare_body(long, "text/plain", subject=None, max_chars=200, fetch_cut=False)
    assert (len(kept.text), kept.cut, kept.original_chars) == (BODY_KEEP_FACTOR * 200, True, len(long))
    exact = prepare_body(long[:-1], "text/plain", subject=None, max_chars=200, fetch_cut=False)
    assert (len(exact.text), exact.cut) == (800, False)
    empty = prepare_body("", "none", subject=None, max_chars=200, fetch_cut=False)
    assert (empty.text, empty.quote_removal, empty.original_chars, empty.omitted) == ("", "none", 0, False)


def test_server_body_has_no_original_chars() -> None:
    body = server_body("Reply\r\nonly\u00a0text", "text/html-converted", max_chars=200, fetch_cut=False)
    assert (body.text, body.quote_removal, body.original_chars) == ("Reply\nonly text", "server", None)
    assert (body.source, body.cut, body.omitted) == ("text/html-converted", False, False)
    cut = server_body("y" * 900, "text/plain", max_chars=200, fetch_cut=False)
    assert (len(cut.text), cut.cut) == (800, True)
    assert server_body("z", "text/plain", max_chars=200, fetch_cut=True).cut


def test_thread_copy_defaults() -> None:
    item = copy(1)
    assert (item.seed, item.body) == (False, OMITTED_BODY)
