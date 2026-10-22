"""IMAP `get_message_status` (spec 080 §5.2 "Location, IMAP", §5.4, §6.5; WP13b T8): the in-place check of `m` ids,
the location step and the mapping to `MessageStatus`. The reply and thread lookups are T9/T10."""

import json
import logging
from collections.abc import Callable, Iterator, Sequence
from dataclasses import replace
from datetime import timedelta

import pytest

from mcp_hub.ids import GraphMessageRef, MessageRef, StableRef
from mcp_hub.providers import imap_status
from mcp_hub.providers.base import MessageStatus, ProviderError
from mcp_hub.providers.deadline import SoftStop
from mcp_hub.providers.folders import MAX_LOCATE_FOLDERS, FolderMapCache, build_imap_folder_map
from mcp_hub.providers.imap_session import FETCH_CHUNK_UIDS
from mcp_hub.providers.imap_status import MAX_DEGRADED_LOGS, SOFT_CHECK_ROWS
from tests.support.imap_fake import COMMAND_ALLOWLIST, FakeFolder, FakeImapClient, FakeImapServer, FakeMessage
from tests.support.logcapture import Capture
from tests.support.logfields import allowed_fields
from tests.unit.test_imap_get_message import (
    GMAIL_ALL,
    INBOX_L,
    MID,
    NOW,
    OWN,
    SENT_L,
    OverMatchingClient,
    clock_of,
    deadline,
    examined,
    fail_search_in,
    folder,
    gmail,
    icloud,
    mailbox,
    msg,
    names,
    prime,
    searches,
)

ICLOUD = "icloud"
OTHER_MID = "m2@example.test"


def server_with(
    *,
    inbox: Sequence[FakeMessage] = (),
    archive: Sequence[FakeMessage] = (),
    sent: Sequence[FakeMessage] = (),
    trash: Sequence[FakeMessage] = (),
    junk: Sequence[FakeMessage] = (),
    drafts: Sequence[FakeMessage] = (),
    projects: Sequence[FakeMessage] = (),
    other: Sequence[FakeFolder] = (),
    step: float = 0.0,
) -> FakeImapServer:
    """iCloud-like: INBOX, Sent Messages (`\\Sent`), Archive, Deleted Messages (`\\Trash`), Junk, Drafts, Projects."""
    return icloud(
        inbox=inbox,
        archive=archive,
        sent=sent,
        trash=trash,
        junk=junk,
        drafts=drafts,
        other=[folder("Projects", (), projects), *other],
        step=step,
    )


def r(message_id: str = MID, account: str = ICLOUD) -> StableRef:
    return StableRef(account, message_id)


def m(name: str, uid: int, validity: int = 7, account: str = ICLOUD) -> MessageRef:
    return MessageRef(account, name, validity, uid)


def run(
    server: FakeImapServer,
    targets: Sequence[StableRef | MessageRef | GraphMessageRef],
    *,
    own: frozenset[str] = frozenset(),
    soft: float = 16.0,
    hard: float = 20.0,
    maps: FolderMapCache | None = None,
) -> list[MessageStatus]:
    account = "gmail" if b"X-GM-EXT-1" in server.capabilities else ICLOUD
    box = mailbox(server, account=account, own=own, maps=maps)
    return box.message_status(targets, deadline(server, soft, hard))


def run_direct(
    server: FakeImapServer, targets: Sequence[StableRef | MessageRef], soft_check: Callable[[], None]
) -> list[MessageStatus]:
    """`imap_status.message_status` inside a session, with a soft check the test controls."""
    box = mailbox(server)
    return box._run(lambda s: imap_status.message_status(s, targets, soft_check=soft_check), deadline(server))


def located(status: MessageStatus) -> tuple[str, int]:
    assert status.found is not None
    location = status.found.location
    assert isinstance(location, MessageRef)
    return location.folder, location.uid


def found_folder(status: MessageStatus) -> str:
    assert status.found is not None
    return status.found.meta.folder


def direction(status: MessageStatus) -> str:
    assert status.found is not None
    return status.found.meta.direction


def copy_flags(status: MessageStatus) -> tuple[bool, bool, bool]:
    """(answered, flagged, unread) of the copy found."""
    assert status.found is not None
    return status.found.meta.flags.answered, status.found.meta.flags.flagged, status.found.unread


def states(statuses: Sequence[MessageStatus]) -> list[tuple[str, str | None]]:
    return [(s.state, s.reason) for s in statuses]


def fetched_uids(server: FakeImapServer) -> list[list[int]]:
    return [list(argument[0]) for name, argument in server.commands if name == "UID FETCH"]  # type: ignore[index]


INCOMPLETE = ("unknown", "incomplete_search")
STALE = ("unknown", "stale_id")
GONE = ("gone", None)
FOUND = ("found", None)


# --- refs: location and mapping ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("where", "raw_name", "expected"),
    [
        ("inbox", "INBOX", "inbox"),
        ("archive", "Archive", "archive"),
        ("sent", "Sent Messages", "sent"),
        ("trash", "Deleted Messages", "trash"),
        ("projects", "Projects", "other"),
    ],
)
def test_ref_found_in_inbox_archive_sent_trash_other(where: str, raw_name: str, expected: str) -> None:
    server = server_with(**{where: [msg(4, 60, "a", message_id=f"<{MID}>")]})  # type: ignore[arg-type]
    [status] = run(server, [r()])
    assert (status.state, status.reason, status.thread_checked) == ("found", None, False)
    assert status.found is not None
    assert status.found.location == MessageRef(ICLOUD, raw_name, 7, 4)
    assert (status.found.message_id, status.found.received_at) == (MID, NOW - timedelta(minutes=60))
    assert found_folder(status) == expected
    assert status.found.meta.folder_name is None  # no third-party text in a status
    assert (status.reply_at, status.reply_message_id, status.newer_incoming_at) == (None, None, None)


def test_earliest_copy_wins_and_ties_by_folder_order() -> None:
    server = server_with(
        inbox=[msg(1, 60, "a", message_id=f"<{MID}>")], archive=[msg(4, 90, "b", message_id=f"<{MID}>")]
    )
    assert located(run(server, [r()])[0]) == ("Archive", 4)

    # ties (same INTERNALDATE) by the rank inbox, archive, sent, trash, other — never by the search order
    cases = [
        ({"archive": 4, "sent": 5}, ("Archive", 4)),
        ({"sent": 5, "trash": 6}, ("Sent Messages", 5)),
        ({"inbox": 1, "projects": 3}, ("INBOX", 1)),
    ]
    for copies, expected in cases:
        server = server_with(**{k: [msg(uid, 60, k, message_id=f"<{MID}>")] for k, uid in copies.items()})  # type: ignore[arg-type]
        [status] = run(server, [r()])
        assert located(status) == expected
        if "sent" in copies:
            assert direction(status) == "outgoing"  # any found copy in sent (MF-R2)


def test_gone_after_a_complete_search() -> None:
    server = server_with(inbox=[msg(1, 5, "a", message_id=f"<{OTHER_MID}>")])
    assert states(run(server, [r()])) == [GONE]
    assert examined(server) == ["INBOX", "Archive", "Deleted Messages", "Sent Messages", "Projects"]


def _soft_stop_in_core() -> tuple[FakeImapServer, dict[str, object]]:
    # LOGIN, CAPABILITY, LIST, EXAMINE INBOX, SEARCH, EXAMINE Archive (elapsed 5 s) → stopped before it
    return server_with(step=1.0), {"soft": 4.5}


def _folder_cap() -> tuple[FakeImapServer, dict[str, object]]:
    others = [folder(f"P{i:02d}") for i in range(1, 27)]  # 4 core + Projects + 26 = 31 mailboxes
    return server_with(other=others), {}


def _map_capped() -> tuple[FakeImapServer, dict[str, object]]:
    server = server_with()
    maps = FolderMapCache(clock=clock_of(server))
    listing = [(f.attributes, f.delimiter, f.raw_name) for f in server.folders]
    built = build_imap_folder_map(ICLOUD, "INBOX", gmail=False, listing=listing)  # type: ignore[arg-type]
    maps.put(replace(built, capped=True))
    return server, {"maps": maps}


def _examine_no() -> tuple[FakeImapServer, dict[str, object]]:
    server = server_with()
    server.fail["EXAMINE"] = server.no("EXAMINE")
    return server, {}


def _search_no() -> tuple[FakeImapServer, dict[str, object]]:
    server = server_with()
    fail_search_in(server, {MID: "Archive"})
    return server, {}


def _fetch_loses_a_hit() -> tuple[FakeImapServer, dict[str, object]]:
    server = server_with(inbox=[msg(1, 60, "case-insensitive hit", message_id=f"<{MID.upper()}>")])
    server.fail["UID FETCH"] = server.no("UID FETCH")
    return server, {}


def _missing_header_item(monkeypatch: pytest.MonkeyPatch) -> tuple[FakeImapServer, dict[str, object]]:
    server = server_with(archive=[FakeMessage(4, NOW - timedelta(minutes=60), headers=None)])
    monkeypatch.setattr(server, "client", lambda: OverMatchingClient(server))
    return server, {}


GONE_BECOMES_INCOMPLETE: dict[str, Callable[..., tuple[FakeImapServer, dict[str, object]]]] = {
    "soft_stop_in_core": _soft_stop_in_core,
    "folder_cap": _folder_cap,
    "folder_map_capped": _map_capped,
    "examine_no": _examine_no,
    "search_no": _search_no,
    "fetch_loses_a_hit": _fetch_loses_a_hit,
    "missing_header_item": _missing_header_item,
}


@pytest.mark.parametrize("case", list(GONE_BECOMES_INCOMPLETE))
def test_gone_only_after_complete_search(case: str, monkeypatch: pytest.MonkeyPatch) -> None:
    factory = GONE_BECOMES_INCOMPLETE[case]
    server, kwargs = factory(monkeypatch) if case == "missing_header_item" else factory()
    assert states(run(server, [r()], **kwargs)) == [INCOMPLETE]  # type: ignore[arg-type]
    if case == "folder_cap":
        assert MAX_LOCATE_FOLDERS == 30
        assert len(examined(server)) == 30
        assert "P26" not in examined(server)


def test_uncertain_ref_reports_found() -> None:
    """C3: found, but a failed batch may hide an earlier copy: still `found`, with the copy read."""
    server = server_with(inbox=[msg(1, 5, "a", message_id=f"<{MID}>")])
    fail_search_in(server, {MID: "Archive"})
    [status] = run(server, [r()])
    assert states([status]) == [FOUND]
    assert located(status) == ("INBOX", 1)


def test_soft_stop_during_location_keeps_found_refs() -> None:
    a, b = MID, OTHER_MID
    server = server_with(inbox=[msg(1, 5, "a", message_id=f"<{a}>")], step=1.0)
    statuses = run(server, [r(a), r(b)], soft=5.5)  # LIST, EXAMINE, SEARCH, FETCH; stopped before the Archive EXAMINE
    assert states(statuses) == [FOUND, INCOMPLETE]
    assert located(statuses[0]) == ("INBOX", 1)
    assert examined(server) == ["INBOX"]


def test_soft_stop_at_login_all_incomplete_not_account_error() -> None:
    server = server_with(inbox=[msg(1, 5, "a", message_id=f"<{MID}>")], step=1.0)
    statuses = run(server, [r(), m("INBOX", 1), m("Projects", 2)], soft=0.5)  # stopped at the CAPABILITY after LOGIN
    assert states(statuses) == [INCOMPLETE] * 3
    assert names(server) == ["LOGIN", "LOGOUT"]
    assert states(imap_status.soft_stopped([r(), m("INBOX", 1)])) == [INCOMPLETE] * 2


# --- ids: the in-place check ---------------------------------------------------------------------------------


def test_id_in_place_seeds_found_and_earlier_copy_wins() -> None:
    projects = [msg(3, 30, "p", message_id=f"<{MID}>")]
    # the only copy: read in place; Projects is not searched again (a seeded ref is resolved for `other`)
    server = server_with(projects=projects)
    [status] = run(server, [m("Projects", 3)])
    assert (states([status]), located(status), found_folder(status)) == ([FOUND], ("Projects", 3), "other")
    assert status.found is not None
    assert status.found.message_id == MID
    assert examined(server) == ["Projects", "INBOX", "Archive", "Deleted Messages", "Sent Messages"]

    server = server_with(projects=projects, archive=[msg(4, 90, "earlier", message_id=f"<{MID}>")])
    [status] = run(server, [m("Projects", 3)])
    assert (located(status), found_folder(status)) == (("Archive", 4), "archive")  # an earlier copy replaces the seed

    server = server_with(projects=projects, inbox=[msg(1, 5, "later", message_id=f"<{MID}>")])
    assert located(run(server, [m("Projects", 3)])[0]) == ("Projects", 3)  # a later copy does not


class BlindSearchClient(FakeImapClient):
    """A server whose search index lags: UID SEARCH finds nothing."""

    def search(self, criteria: bytes) -> list[int]:
        super().search(criteria)
        return []


def test_id_seed_stands_when_index_lags(monkeypatch: pytest.MonkeyPatch) -> None:
    server = server_with(inbox=[msg(1, 1, "fresh", message_id=f"<{MID}>")])
    monkeypatch.setattr(server, "client", lambda: BlindSearchClient(server))
    statuses = run(server, [m("INBOX", 1), r()])
    assert states(statuses) == [FOUND, FOUND]  # the ref of the same message finds the seed too
    assert located(statuses[0]) == located(statuses[1]) == ("INBOX", 1)

    # two ids of one Message-ID: the earlier copy seeds, whatever the input order (§5.5 matching rule)
    server = server_with(
        inbox=[msg(1, 30, "later", message_id=f"<{MID}>")], projects=[msg(3, 90, "earlier", message_id=f"<{MID}>")]
    )
    monkeypatch.setattr(server, "client", lambda: BlindSearchClient(server))
    statuses = run(server, [m("INBOX", 1), m("Projects", 3)])
    assert located(statuses[0]) == located(statuses[1]) == ("Projects", 3)


def test_id_without_message_id_found_with_null_message_id() -> None:
    server = server_with(inbox=[msg(1, 5, "no id", flags=(b"\\Seen",))])
    [status] = run(server, [m("INBOX", 1)])
    assert (states([status]), located(status), status.thread_checked) == ([FOUND], ("INBOX", 1), False)
    assert status.found is not None
    assert (status.found.message_id, status.found.unread) == (None, False)
    assert "UID SEARCH" not in names(server)  # a copy without a valid Message-ID is final (A-td4)
    assert "LIST" not in names(server)  # the registry inbox needs no LIST


def _validity_changed() -> tuple[FakeImapServer, MessageRef]:
    return server_with(inbox=[msg(1, 5, "a", message_id=f"<{MID}>")]), m("INBOX", 1, validity=8)


def _uid_gone() -> tuple[FakeImapServer, MessageRef]:
    return server_with(inbox=[msg(1, 5, "a", message_id=f"<{MID}>")]), m("INBOX", 9)


def _draft() -> tuple[FakeImapServer, MessageRef]:
    return server_with(inbox=[msg(1, 5, "a", flags=(b"\\Draft",), message_id=f"<{MID}>")]), m("INBOX", 1)


def _refused_junk() -> tuple[FakeImapServer, MessageRef]:
    return server_with(junk=[msg(1, 5, "j", message_id=f"<{MID}>")]), m("Junk", 1)


def _unknown_after_rebuild() -> tuple[FakeImapServer, MessageRef]:
    return server_with(), m("Gone Folder", 1)


def _gmail_label() -> tuple[FakeImapServer, MessageRef]:
    return gmail(), m("Receipts", 90, account="gmail")


def _no_internaldate() -> tuple[FakeImapServer, MessageRef]:
    return server_with(inbox=[msg(1, None, "a", message_id=f"<{MID}>")]), m("INBOX", 1)


STALE_CASES: dict[str, Callable[[], tuple[FakeImapServer, MessageRef]]] = {
    "uidvalidity_changed": _validity_changed,
    "uid_gone": _uid_gone,
    "draft": _draft,
    "refused_junk": _refused_junk,
    "unknown_after_rebuild": _unknown_after_rebuild,
    "gmail_label": _gmail_label,
    "no_internaldate": _no_internaldate,
}


@pytest.mark.parametrize("case", list(STALE_CASES))
def test_id_stale_cases(case: str) -> None:
    server, target = STALE_CASES[case]()
    maps = FolderMapCache(clock=clock_of(server))
    box = mailbox(server, account=target.account, maps=maps)
    prime(box, server)  # a cached map: an unknown name is listed once more before the refusal
    start = len(server.commands)
    assert states(box.message_status([target], deadline(server))) == [STALE]
    assert target.folder not in examined(server, start) or target.folder == "INBOX"  # never opened
    if case == "uidvalidity_changed":
        assert "UID FETCH" not in names(server, start)
    if case == "unknown_after_rebuild":
        assert names(server, start).count("LIST") == 1
    assert "UID SEARCH" not in names(server, start)  # nothing to locate


@pytest.mark.parametrize("case", ["examine_no", "fetch_no", "no_uidvalidity", "missing_header", "cut_block"])
def test_id_examine_or_fetch_no_is_incomplete_search(case: str) -> None:
    message = msg(2, 5, "p", message_id=f"<{MID}>")
    if case == "missing_header":
        message = FakeMessage(2, NOW - timedelta(minutes=5), headers=None)
    if case == "cut_block":  # a 2 KiB block cut inside a padded Message-ID line (A-sec3)
        message = FakeMessage(2, NOW, headers=f"Message-ID: ({'p' * 3000}) <{MID}>\r\n\r\n".encode())
    server = server_with(projects=[message])
    if case == "examine_no":
        server.fail["EXAMINE"] = server.no("EXAMINE")
    if case == "fetch_no":
        server.fail["UID FETCH"] = server.no("UID FETCH")
    if case == "no_uidvalidity":
        next(f for f in server.folders if f.raw_name == "Projects").uidvalidity = None
    assert states(run(server, [m("Projects", 2)])) == [INCOMPLETE]
    assert "UID SEARCH" not in names(server)


def test_missing_uidvalidity_on_the_registry_inbox_fails_the_account() -> None:
    server = server_with(inbox=[msg(1, 5, "a", message_id=f"<{MID}>")])
    server.folders[0].uidvalidity = None
    with pytest.raises(ProviderError) as caught:
        run(server, [m("INBOX", 1)])
    assert (caught.value.code, caught.value.cause) == ("upstream_error", "MissingUidValidity")


def test_id_with_one_of_two_validities_fetches_only_the_current() -> None:
    server = server_with(inbox=[msg(1, 5, "a", message_id=f"<{MID}>"), msg(2, 5, "b", message_id=f"<{OTHER_MID}>")])
    statuses = run(server, [m("INBOX", 1, validity=6), m("INBOX", 2)])
    assert states(statuses) == [STALE, FOUND]
    assert fetched_uids(server)[0] == [2]

    # a FETCH NO for the current ids leaves the stale one stale
    server = server_with(inbox=[msg(1, 5, "a", message_id=f"<{MID}>"), msg(2, 5, "b", message_id=f"<{OTHER_MID}>")])
    server.fail["UID FETCH"] = server.no("UID FETCH")
    assert states(run(server, [m("INBOX", 1, validity=6), m("INBOX", 2)])) == [STALE, INCOMPLETE]


def test_gmail_inbox_id_reports_the_all_mail_copy() -> None:
    """IR3: the INBOX copy read in place ties with its All Mail copy, which ranks first; the folder stays inbox."""
    same = {"message_id": f"<{MID}>"}
    server = gmail(inbox=[msg(1, 60, "a", **same)], all_mail=[msg(7, 60, "a", INBOX_L, **same)])
    [status] = run(server, [m("INBOX", 1, account="gmail")])
    assert (located(status), found_folder(status)) == ((GMAIL_ALL, 7), "inbox")


def test_graph_id_is_id_mismatch() -> None:
    server = server_with()
    assert states(run(server, [GraphMessageRef(ICLOUD, "AAMk")])) == [("unknown", "id_mismatch")]
    assert "EXAMINE" not in names(server)


def test_soft_stop_during_in_place_check_keeps_checked_ids() -> None:
    """IR2, P7: ids read in place are found (thread unchecked); unchecked ids and every ref are incomplete_search."""
    server = server_with(
        inbox=[msg(1, 5, "a", message_id=f"<{MID}>")],
        projects=[msg(2, 5, "b", message_id=f"<{OTHER_MID}>")],
        step=1.0,
    )
    # LOGIN, CAPABILITY, EXAMINE INBOX, FETCH (elapsed 3), LIST (elapsed 4) → stopped before the LIST
    statuses = run(server, [m("INBOX", 1), m("Projects", 2), r(OTHER_MID)], soft=3.5)
    assert states(statuses) == [FOUND, INCOMPLETE, INCOMPLETE]
    assert (located(statuses[0]), statuses[0].thread_checked) == (("INBOX", 1), False)
    assert "UID SEARCH" not in names(server)  # location skipped

    # a stop between rows of one FETCH answer: the rows parsed before it are kept
    count = SOFT_CHECK_ROWS + 1
    server = server_with(inbox=[msg(uid, 5, "x", message_id=f"<u{uid}@example.test>") for uid in range(1, count + 1)])
    calls: list[int] = []

    def stop_at_first_check() -> None:
        calls.append(1)
        raise SoftStop

    statuses = run_direct(server, [m("INBOX", uid) for uid in range(1, count + 1)], stop_at_first_check)
    assert states(statuses) == [FOUND] * SOFT_CHECK_ROWS + [INCOMPLETE]
    assert calls == [1]


def test_soft_stop_at_the_location_list_keeps_seeds() -> None:
    """Registry-inbox ids need no LIST in place; a stop at the location's LIST leaves the seed found (C3)."""
    server = server_with(inbox=[msg(1, 5, "a", message_id=f"<{MID}>")], step=1.0)
    # LOGIN, CAPABILITY, EXAMINE INBOX, FETCH (elapsed 3), LIST (elapsed 4) → stopped before the LIST
    statuses = run(server, [m("INBOX", 1), r(OTHER_MID)], soft=3.5)
    assert states(statuses) == [FOUND, INCOMPLETE]
    assert located(statuses[0]) == ("INBOX", 1)
    assert "LIST" not in names(server)


def test_in_place_mailboxes_count_against_folder_cap() -> None:
    """SR5: the distinct mailboxes opened in place count against the 30 folders, location included."""
    boxes = [folder(f"P{i:02d}", (), [msg(1, 5, "x")]) for i in range(1, MAX_LOCATE_FOLDERS + 2)]
    server = server_with(other=boxes)
    statuses = run(server, [*(m(f"P{i:02d}", 1) for i in range(1, MAX_LOCATE_FOLDERS + 2)), r()])
    assert states(statuses) == [FOUND] * MAX_LOCATE_FOLDERS + [INCOMPLETE, INCOMPLETE]  # no folder left for the ref
    assert len(examined(server)) == MAX_LOCATE_FOLDERS
    assert f"P{MAX_LOCATE_FOLDERS + 1:02d}" not in examined(server)

    # 27 `other` mailboxes in place leave 3 for the location: Sent is beyond the cap, so the ref is not gone
    boxes = [folder(f"P{i:02d}", (), [msg(1, 5, "x")]) for i in range(1, 28)]
    server = server_with(other=boxes)
    statuses = run(server, [*(m(f"P{i:02d}", 1) for i in range(1, 28)), r()])
    assert states(statuses) == [FOUND] * 27 + [INCOMPLETE]
    assert len(set(examined(server))) == MAX_LOCATE_FOLDERS
    assert "Sent Messages" not in examined(server)

    # an in-place core mailbox is opened by the location anyway: it does not use up a folder
    server = server_with(other=[folder(f"P{i:02d}") for i in range(1, 26)])
    statuses = run(server, [m("Archive", 9), r()])
    assert states(statuses) == [STALE, GONE]
    assert len(set(examined(server))) == MAX_LOCATE_FOLDERS


def test_one_folder_map_rebuild_per_call() -> None:
    """TS1: the in-place check and the location share one rebuild of a cached map."""
    server = server_with(inbox=[msg(1, 5, "a", message_id=f"<{MID}>")])
    box = mailbox(server)
    prime(box, server)
    server.folders.append(folder("Fresh", (), [msg(3, 5, "f", message_id=f"<{OTHER_MID}>")]))  # added after the map
    server.fail["EXAMINE"] = server.no("EXAMINE")  # INBOX (in place) answers NO: no rebuild for the inbox
    start = len(server.commands)
    statuses = box.message_status([m("INBOX", 1), m("Fresh", 3), m("Unknown", 4), r(MID)], deadline(server))
    assert states(statuses) == [INCOMPLETE, FOUND, STALE, FOUND]
    assert names(server, start).count("LIST") == 1
    assert located(statuses[1]) == ("Fresh", 3)


def test_duplicate_targets_searched_once() -> None:
    server = server_with(inbox=[msg(1, 5, "a", message_id=f"<{MID}>")])
    statuses = run(server, [r(), m("INBOX", 1)])
    assert states(statuses) == [FOUND, FOUND]
    assert located(statuses[0]) == located(statuses[1]) == ("INBOX", 1)
    keys = searches(server)
    assert len(keys) == 4  # one SEARCH per core folder; the seeded ref is resolved for `other`
    assert all(key.count(f'"<{MID}>"'.encode()) == 1 for key in keys)  # type: ignore[attr-defined]


# --- direction, flags, unread -----------------------------------------------------------------------------------


def test_self_sent_x_is_outgoing_with_earliest_copy_folder() -> None:
    server = server_with(inbox=[msg(1, 90, "x", message_id=f"<{MID}>")], sent=[msg(5, 60, "x", message_id=f"<{MID}>")])
    [status] = run(server, [r()])
    assert (located(status), found_folder(status), direction(status)) == (("INBOX", 1), "inbox", "outgoing")

    # Gmail: the All Mail copy with `\Inbox` and `\Sent`
    server = gmail(all_mail=[msg(1, 60, "x", INBOX_L, SENT_L, message_id=f"<{MID}>")])
    [status] = run(server, [r(account="gmail")])
    assert (found_folder(status), direction(status)) == ("inbox", "outgoing")

    # an id read in place, its sent copy found by the location
    server = server_with(inbox=[msg(1, 90, "x", message_id=f"<{MID}>")], sent=[msg(5, 60, "x", message_id=f"<{MID}>")])
    [status] = run(server, [m("INBOX", 1)])
    assert (located(status), direction(status)) == (("INBOX", 1), "outgoing")


def test_flags_direction_and_unread_of_the_copy() -> None:
    server = server_with(inbox=[msg(1, 5, "a", flags=(b"\\Answered", b"\\Flagged"), message_id=f"<{MID}>")])
    [status] = run(server, [r()])
    assert (copy_flags(status), direction(status)) == ((True, True, True), "incoming")

    server = server_with(inbox=[msg(1, 5, "a", flags=(b"\\Seen",), message_id=f"<{MID}>")])
    assert copy_flags(run(server, [r()])[0]) == (False, False, False)

    server = server_with(sent=[msg(5, 5, "a", message_id=f"<{MID}>")])
    assert direction(run(server, [r()])[0]) == "outgoing"

    own_from = [msg(1, 5, "a", sender="Owner <owner@example.org>", message_id=f"<{MID}>")]
    assert direction(run(server_with(inbox=own_from), [r()], own=OWN)[0]) == "outgoing"
    assert direction(run(server_with(inbox=own_from), [m("INBOX", 1)], own=OWN)[0]) == "outgoing"
    assert direction(run(server_with(inbox=own_from), [r()])[0]) == "incoming"  # no own addresses: folder rule only

    # a final copy (no Message-ID) read in place in Sent
    [status] = run(server_with(sent=[msg(5, 5, "no id")]), [m("Sent Messages", 5)])
    assert (found_folder(status), direction(status)) == ("sent", "outgoing")


# --- failures ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("command", "failure", "code"),
    [
        ("UID SEARCH", "bad", "upstream_error"),
        ("UID SEARCH", "abort", "upstream_error"),
        ("UID FETCH", "bad", "upstream_error"),
        ("EXAMINE", "bad", "upstream_error"),
        ("LIST", "abort", "upstream_error"),
    ],
)
def test_bad_or_abort_fails_the_account(command: str, failure: str, code: str) -> None:
    server = server_with(inbox=[msg(1, 5, "a", message_id=f"<{MID}>")])
    server.fail[command] = server.abort() if failure == "abort" else server.bad(command)
    targets = [r()] if command == "UID SEARCH" else [m("Projects", 1), r()]
    with pytest.raises(ProviderError) as caught:
        run(server, targets)
    assert caught.value.code == code


@pytest.mark.parametrize("hard", [1.5, 2.5, 3.5])  # at the in-place LIST, EXAMINE or FETCH
def test_hard_deadline_in_place_is_an_account_error(hard: float) -> None:
    """P6: the hard deadline is `upstream_timeout` like EXAMINE NO, but fails the account, never incomplete_search.
    No Message-ID and no ref: no command follows the in-place check that could fail the account instead."""
    server = server_with(projects=[msg(1, 5, "a")], step=1.0)
    with pytest.raises(ProviderError) as caught:
        run(server, [m("Projects", 1)], soft=100.0, hard=hard)
    assert (caught.value.code, caught.value.cause) == ("upstream_timeout", "CallDeadline")


def test_only_allowlisted_commands_and_items() -> None:
    server = server_with(
        inbox=[msg(1, 5, "a", message_id=f"<{MID}>")],
        projects=[msg(2, 5, "b", message_id=f"<{OTHER_MID}>")],
        sent=[msg(3, 5, "c", message_id=f"<{MID}>")],
    )
    run(server, [r(), m("Projects", 2), m("INBOX", 1)], own=OWN)
    gm = gmail(all_mail=[msg(1, 5, "a", INBOX_L, message_id=f"<{MID}>")])
    run(gm, [r(account="gmail"), m("INBOX", 1, account="gmail")])
    allowed = {
        "FLAGS",
        "INTERNALDATE",
        "X-GM-LABELS",
        "X-GM-THRID",
        "BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)]<0.2048>",
        "BODY.PEEK[HEADER.FIELDS (MESSAGE-ID FROM)]<0.8192>",
    }
    for each in (server, gm):
        assert {name for name, _ in each.commands} <= COMMAND_ALLOWLIST
        items = {item for name, argument in each.commands if name == "UID FETCH" for item in argument[1]}  # type: ignore[index]
        assert items <= allowed
        assert all(b"NOT DRAFT" in key for key in searches(each))  # type: ignore[operator]


def test_in_place_fetch_stays_one_folder_group() -> None:
    """T7-4: the accumulating in-place FETCH covers one mailbox's ids at a time."""
    server = server_with(inbox=[msg(uid, 5, "i") for uid in (1, 2)], projects=[msg(uid, 5, "p") for uid in (3, 4)])
    run(server, [m("INBOX", 1), m("Projects", 3), m("INBOX", 2), m("Projects", 4)])
    assert fetched_uids(server) == [[1, 2], [3, 4]]
    assert FETCH_CHUNK_UIDS == 500


# --- logging ----------------------------------------------------------------------------------------------------


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


HOSTILE_FOLDER = "Leak-Folder-Name"
HOSTILE_MID = "leak-canary-0001@example.test"


def test_status_logs_carry_no_message_id_or_folder_name(hub_log: Capture, monkeypatch: pytest.MonkeyPatch) -> None:
    hostile = f"<{HOSTILE_MID}>"
    secret = [msg(1, None, "no date", message_id=hostile), msg(2, 5, "dated", message_id=hostile)]
    cases: list[tuple[FakeImapServer, list[StableRef | MessageRef]]] = []
    server = server_with(other=[folder(HOSTILE_FOLDER, (), secret)], archive=[msg(4, None, "a", message_id=hostile)])
    cases.append((server, [r(HOSTILE_MID), m(HOSTILE_FOLDER, 1), m(HOSTILE_FOLDER, 2), m(HOSTILE_FOLDER, 2, 8)]))
    server = server_with(other=[folder(HOSTILE_FOLDER, (), secret)])
    server.fail["UID FETCH"] = server.no("UID FETCH")
    cases.append((server, [m(HOSTILE_FOLDER, 2), r(HOSTILE_MID)]))
    server = server_with(other=[folder(HOSTILE_FOLDER, (), secret)])
    fail_search_in(server, {HOSTILE_MID: "Archive"})
    cases.append((server, [r(HOSTILE_MID), m(HOSTILE_FOLDER, 2)]))
    for each, targets in cases:
        run(each, targets, own=OWN)
    server = server_with(other=[folder(HOSTILE_FOLDER, (), secret)], step=1.0)
    run(server, [m(HOSTILE_FOLDER, 2), r(HOSTILE_MID)], soft=5.5)
    server = server_with(other=[folder(HOSTILE_FOLDER, (), secret)])
    server.fail["UID SEARCH"] = server.bad("UID SEARCH")
    with pytest.raises(ProviderError):
        run(server, [r(HOSTILE_MID)])

    assert any(json.loads(line)["event"] == "item_degraded" for line in hub_log.lines)
    for line in hub_log.lines:
        record = json.loads(line)
        assert set(record) <= allowed_fields(record["event"])
    text = "\n".join(hub_log.lines) + "\n".join(message for _, _, message in hub_log.raw)
    for value in (HOSTILE_MID, HOSTILE_FOLDER, "owner@example.org", "sender@example.test", "Leak"):
        assert value not in text


def test_item_degraded_capped_per_call(hub_log: Capture) -> None:
    """SR6: at most 20 item_degraded lines per provider call, the in-place check and the location together."""
    undated = [msg(uid, None, "x", message_id=f"<u{uid}@example.test>") for uid in range(1, 16)]
    copies = [msg(uid, None, "y", message_id=f"<{MID}>") for uid in range(1, 11)]
    server = server_with(inbox=undated, archive=copies)
    statuses = run(server, [*(m("INBOX", uid) for uid in range(1, 16)), r()])
    assert states(statuses) == [STALE] * 15 + [GONE]
    degraded = [line for line in hub_log.lines if json.loads(line)["event"] == "item_degraded"]
    assert len(degraded) == MAX_DEGRADED_LOGS == 20
