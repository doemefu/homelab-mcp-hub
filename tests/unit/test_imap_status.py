"""IMAP `get_message_status` (spec 080 §5.2 "Location, IMAP", §5.4, §6.5; WP13b T8, T9): the in-place check of `m`
ids, the location step, the mapping to `MessageStatus` and the reply and thread lookups on servers other than Gmail
(the Gmail lookups: `test_imap_status_gmail.py`)."""

import contextlib
import json
import logging
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from mcp_hub.ids import GraphMessageRef, MessageRef, StableRef
from mcp_hub.providers import imap_status
from mcp_hub.providers.base import ListQuery, MessageStatus, ProviderError, SearchTerms
from mcp_hub.providers.deadline import CallDeadline, SoftStop
from mcp_hub.providers.folders import MAX_LOCATE_FOLDERS, FolderMapCache, build_imap_folder_map
from mcp_hub.providers.headers import CANDIDATE_HEADER_BYTES, DESCENDANT_HEADER_BYTES, DESCENDANT_HEADER_ITEM
from mcp_hub.providers.imap_session import FETCH_CHUNK_UIDS
from mcp_hub.providers.imap_status import (
    LOOKUP_FETCH_UIDS,
    MAX_DEGRADED_LOGS,
    MAX_LOOKUP_HITS,
    MAX_REPLY_KEYS_PER_REF,
    SOFT_CHECK_ROWS,
)
from tests.support import imap_threads
from tests.support.imap_fake import COMMAND_ALLOWLIST, FakeFolder, FakeImapClient, FakeImapServer, FakeMessage
from tests.support.logcapture import Capture
from tests.support.logfields import allowed_fields
from tests.unit import test_imap_list_messages as list_tests
from tests.unit.test_imap_get_message import (
    GMAIL_ALL,
    ICLOUD_CORE,
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
    assert (status.state, status.reason, status.thread_checked) == ("found", None, True)  # lookups complete, no hit
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


def test_soft_stop_at_the_folder_map_seeds_the_earlier_copy() -> None:
    """PR #51 review CR3: two ids of one Message-ID read in place, then a soft stop at the LIST: both report the
    earlier copy (§5.5 matching rule), not the one named last."""
    copies = [msg(1, 60, "earlier", message_id=f"<{MID}>"), msg(2, 5, "later", message_id=f"<{MID}>")]
    server = server_with(inbox=copies, step=1.0)
    statuses = run(server, [m("INBOX", 1), m("INBOX", 2)], soft=3.5)  # EXAMINE, FETCH; stopped at the LIST
    assert names(server) == ["LOGIN", "CAPABILITY", "EXAMINE", "UID FETCH", "LOGOUT"]
    assert states(statuses) == [FOUND, FOUND]
    assert [located(status) for status in statuses] == [("INBOX", 1), ("INBOX", 1)]


# --- ids: the in-place check ---------------------------------------------------------------------------------


def test_id_in_place_seeds_found_and_earlier_copy_wins() -> None:
    projects = [msg(3, 30, "p", message_id=f"<{MID}>")]
    # the only copy: read in place; Projects is not searched again (a seeded ref is resolved for `other`)
    server = server_with(projects=projects)
    [status] = run(server, [m("Projects", 3)])
    assert (states([status]), located(status), found_folder(status)) == ([FOUND], ("Projects", 3), "other")
    assert status.found is not None
    assert status.found.message_id == MID
    assert examined(server)[:5] == ["Projects", "INBOX", "Archive", "Deleted Messages", "Sent Messages"]

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
    keys = [key for key in searches(server) if b"Message-ID" in key]  # type: ignore[operator]
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
        "BODY.PEEK[HEADER.FIELDS (MESSAGE-ID IN-REPLY-TO REFERENCES FROM)]<0.65536>",  # the lookups' re-check (C9)
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


# --- reply and thread lookups (T9, servers other than Gmail) ----------------------------------------------------

A_MID = "a1@example.test"  # the owner's reply to X
F_MID = "f1@example.test"  # an incoming follow-up
OWNER = "Owner <owner@example.org>"


def ago(minutes: float) -> datetime:
    return NOW - timedelta(minutes=minutes)


def x_inbox(**kwargs: object) -> FakeMessage:
    """X itself, received three hours ago."""
    return msg(1, 180, "x", message_id=f"<{MID}>", **kwargs)  # type: ignore[arg-type]


def follow(
    uid: int,
    minutes: float,
    message_id: str | None = F_MID,
    *,
    to: str = MID,
    sender: str = "Sender <sender@example.test>",
    flags: tuple[bytes, ...] = (),
) -> FakeMessage:
    """A message that answers `to` (`In-Reply-To` and `References`)."""
    fields = {"in_reply_to": f"<{to}>", "references": f"<{to}>", "sender": sender}
    if message_id is not None:
        fields["message_id"] = f"<{message_id}>"
    return msg(uid, minutes, "re", flags=flags, **fields)


def facts(status: MessageStatus) -> tuple[datetime | None, str | None, datetime | None, bool | None]:
    """(reply_at, reply_message_id, newer_incoming_at, thread_checked)."""
    return status.reply_at, status.reply_message_id, status.newer_incoming_at, status.thread_checked


def token(value: str) -> bytes:
    return f'"<{value}>"'.encode()


def lookup_searches(server: FakeImapServer) -> list[bytes]:
    return [key for key in searches(server) if b"In-Reply-To" in key]  # type: ignore[operator]


def lookup_commands(server: FakeImapServer) -> list[tuple[int, str, str, object]]:
    """(index, command, mailbox selected, argument) of every reply/thread SEARCH and re-check FETCH."""
    out: list[tuple[int, str, str, object]] = []
    current = ""
    for index, (name, argument) in enumerate(server.commands):
        if name == "EXAMINE":
            current = str(argument)
        elif (name == "UID SEARCH" and b"In-Reply-To" in argument) or (  # type: ignore[operator]
            name == "UID FETCH" and DESCENDANT_HEADER_ITEM in argument[1]  # type: ignore[index]
        ):
            out.append((index, name, current, argument))
    return out


def lookup_fetches(server: FakeImapServer) -> list[tuple[str, list[int]]]:
    return [(box, list(arg[0])) for _, name, box, arg in lookup_commands(server) if name == "UID FETCH"]  # type: ignore[index]


def lookup_search_index(server: FakeImapServer, mailbox_name: str, value: str) -> int:
    return next(
        index
        for index, name, box, arg in lookup_commands(server)
        if name == "UID SEARCH" and box == mailbox_name and token(value) in arg  # type: ignore[operator]
    )


def test_reply_in_sent_by_in_reply_to_only_and_by_references_only() -> None:
    """MF T2: either field names X; on a substring server and on an iCloud-like whole-token server (O35)."""
    for field in ("in_reply_to", "references"):
        for whole_tokens in (False, True):
            answer = msg(5, 60, "re", sender=OWNER, message_id=f"<{A_MID}>", **{field: f"<{MID}>"})
            server = server_with(inbox=[x_inbox()], sent=[answer])
            server.whole_msgid_tokens = whole_tokens
            assert facts(run(server, [r()])[0]) == (ago(60), A_MID, None, True), (field, whole_tokens)


def test_reply_to_follow_up_counts_for_x() -> None:
    """A-mf3 (a): a reply to the follow-up Y (In-Reply-To Y, References X Y) answers X too."""
    y = "y1@example.test"
    answer = msg(5, 60, "re", sender=OWNER, message_id=f"<{A_MID}>", in_reply_to=f"<{y}>", references=f"<{MID}> <{y}>")
    server = server_with(inbox=[x_inbox(), follow(2, 120, y)], sent=[answer])
    x_status, y_status = run(server, [r(), r(y)])
    assert facts(x_status) == (ago(60), A_MID, ago(120), True)  # Y is an incoming descendant of X
    assert facts(y_status) == (ago(60), A_MID, None, True)


def test_forward_with_references_counts_without_does_not() -> None:
    """A-gr1 residual: a forward that sets `References: <X>` (Gmail web, Thunderbird) counts as a reply; one with
    neither header (Apple Mail style) is not seen."""
    forward = msg(5, 60, "Fwd: x", sender=OWNER, message_id=f"<{A_MID}>", references=f"<{MID}>")
    server = server_with(inbox=[x_inbox()], sent=[forward])
    assert facts(run(server, [r()])[0]) == (ago(60), A_MID, None, True)

    bare = msg(5, 60, "Fwd: x", sender=OWNER, message_id=f"<{A_MID}>")
    server = server_with(inbox=[x_inbox()], sent=[bare])
    assert facts(run(server, [r()])[0]) == (None, None, None, True)


def test_hand_over_both_sides() -> None:
    """The hand-over: X arrives, the owner replies A (Cc a colleague), the colleague answers B with
    `In-Reply-To: <A>` and `References: <X> <A>`. X is replied and continued (B counted once although it names two of
    X's keys); A, as a ref of its own, is outgoing, not replied, and continued through B (the waiting-for side)."""
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
    )
    server = server_with(inbox=[x_inbox(), b], sent=[follow(5, 60, A_MID, sender=OWNER)])
    x_status, a_status = run(server, [r(), r(A_MID)])
    assert facts(x_status) == (ago(60), A_MID, ago(30), True)
    assert facts(a_status) == (None, None, ago(30), True)
    assert (found_folder(a_status), direction(a_status)) == ("sent", "outgoing")


def test_earliest_reply_gives_reply_at_and_message_id() -> None:
    later = msg(5, 30, "re", message_id="<a2@example.test>", in_reply_to=f"<{MID}>")
    earlier = msg(6, 90, "re", message_id=f"<{A_MID}>", references=f"<{MID}>")
    server = server_with(inbox=[x_inbox()], sent=[later, earlier])
    assert facts(run(server, [r()])[0]) == (ago(90), A_MID, None, True)  # by INTERNALDATE, not by UID


def test_reply_without_valid_message_id_keeps_reply_at() -> None:
    later = msg(7, 30, "re", message_id=f"<{A_MID}>", in_reply_to=f"<{MID}>")
    no_id = msg(5, 90, "re", in_reply_to=f"<{MID}>")
    bad_id = msg(6, 80, "re", message_id="<has space@example.test>", in_reply_to=f"<{MID}>")
    for earliest, expected in ((no_id, ago(90)), (bad_id, ago(80))):
        server = server_with(inbox=[x_inbox()], sent=[earliest, later])
        assert facts(run(server, [r()])[0]) == (expected, None, None, True)


def test_substring_hit_is_not_a_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    """MF T3: SEARCH HEADER is a substring match; only whole, case-sensitive tokens count."""
    case = msg(5, 60, "re", message_id=f"<{A_MID}>", in_reply_to=f"<{MID.upper()}>")
    server = server_with(inbox=[x_inbox()], sent=[case])  # the fake matches case-insensitively, as RFC 3501 allows
    assert facts(run(server, [r()])[0]) == (None, None, None, True)
    assert ("Sent Messages", [5]) in lookup_fetches(server)  # the hit came back and was re-checked

    variants = [
        msg(5, 60, "re", message_id=f"<{A_MID}>", in_reply_to=f"<x{MID}>"),
        msg(6, 50, "re", message_id="<a2@example.test>", references=f"<{MID}.org> <{MID}x>"),
        msg(7, 40, "re", message_id="<a3@example.test>", references=f'(<{MID}>) "<{MID}>"'),
    ]
    server = server_with(inbox=[x_inbox()], sent=variants, archive=[follow(4, 30, to=f"{MID}.org")])
    monkeypatch.setattr(server, "client", lambda: OverMatchingClient(server))
    assert facts(run(server, [r()])[0]) == (None, None, None, True)
    assert ("Sent Messages", [5, 6, 7]) in lookup_fetches(server)


def test_copy_of_x_is_never_reply_or_newer() -> None:
    """MF T5, C8, MF3: a self-sent X whose headers name itself, in Sent and in INBOX."""
    self_sent = {"message_id": f"<{MID}>", "in_reply_to": f"<{MID}>", "references": f"<{MID}>"}
    server = server_with(inbox=[msg(1, 60, "x", **self_sent)], sent=[msg(5, 60, "x", **self_sent)])
    [status] = run(server, [r()])
    assert facts(status) == (None, None, None, True)
    assert direction(status) == "outgoing"
    assert ("Sent Messages", [5]) in lookup_fetches(server)
    assert ("INBOX", [1]) in lookup_fetches(server)

    # copies outside Sent only (a self-referencing X in the inbox and the archive): never newer
    server = server_with(inbox=[msg(1, 60, "x", **self_sent)], archive=[msg(4, 30, "x", **self_sent)])
    assert facts(run(server, [r()])[0]) == (None, None, None, True)
    assert ("Archive", [4]) in lookup_fetches(server)


def test_own_reply_delivered_to_inbox_is_not_newer_incoming() -> None:
    """MF T6: the owner's reply from an alias the own-addresses file does not list, delivered to the inbox too (MF4),
    and an own `+tag` address on a message that is not in Sent (MF-R1)."""
    alias = "Alias <alias@example.net>"
    tagged = follow(3, 30, "t1@example.test", sender="Owner <owner+lists@example.org>")
    server = server_with(
        inbox=[x_inbox(), follow(2, 59, A_MID, sender=alias), tagged], sent=[follow(5, 60, A_MID, sender=alias)]
    )
    assert facts(run(server, [r()], own=OWN)[0]) == (ago(60), A_MID, None, True)


def test_outlook_style_follow_up_found_through_own_reply() -> None:
    """E-mf1: B answers the owner's reply A only (In-Reply-To <A>, References <A>), yet continues X's thread."""
    server = server_with(inbox=[x_inbox(), follow(2, 30, F_MID, to=A_MID)], sent=[follow(5, 60, A_MID, sender=OWNER)])
    assert facts(run(server, [r()])[0]) == (ago(60), A_MID, ago(30), True)
    thread = lookup_searches(server)[1:]
    assert len(thread) == 3
    assert all(token(MID) in key and token(A_MID) in key for key in thread)


@pytest.mark.parametrize("count", [MAX_REPLY_KEYS_PER_REF, MAX_REPLY_KEYS_PER_REF + 1])
def test_more_than_five_reply_ids_unchecked(count: int) -> None:
    """E-mf1: at most 5 reply Message-IDs per ref, earliest first, become thread keys; a sixth → unchecked."""
    answers = [follow(10 + i, 100 - i, f"a{i}@example.test", sender=OWNER) for i in range(count)]
    last = f"a{count - 1}@example.test"
    server = server_with(inbox=[x_inbox(), follow(2, 20, to=last)], sent=answers)
    [status] = run(server, [r()])
    thread = lookup_searches(server)[1:]
    assert all(token(f"a{i}@example.test") in key for key in thread for i in range(MAX_REPLY_KEYS_PER_REF))
    if count == MAX_REPLY_KEYS_PER_REF:
        assert facts(status) == (ago(100), "a0@example.test", ago(20), True)
    else:
        assert facts(status) == (ago(100), "a0@example.test", None, False)  # the latest reply is no key
        assert all(token(last) not in key for key in thread)


def test_moved_x_with_later_internaldate_still_newer() -> None:
    """E-mf2: a move resets INTERNALDATE, so X (moved 5 minutes ago) is not compared with its follow-up."""
    server = server_with(archive=[msg(4, 5, "x", message_id=f"<{MID}>")], inbox=[follow(2, 60)])
    [status] = run(server, [r()])
    assert (found_folder(status), facts(status)) == ("archive", (None, None, ago(60), True))


def test_newer_incoming_at_uses_earliest_copy_of_newest_id() -> None:
    """MF T8, MF6: per Message-ID its earliest copy; the newest of those. A hit without one counts on its own date."""
    f2 = "f2@example.test"
    server = server_with(
        inbox=[x_inbox(), follow(2, 120), follow(3, 90, f2), follow(6, 100, None)],
        trash=[follow(7, 30, f2)],  # F2 moved to the trash later: a new INTERNALDATE
    )
    assert facts(run(server, [r()])[0]) == (None, None, ago(90), True)

    server = server_with(inbox=[x_inbox(), follow(2, 120), follow(6, 80, None), follow(7, 110, None)])
    assert run(server, [r()])[0].newer_incoming_at == ago(80)


@pytest.mark.parametrize("where", ["inbox", "archive", "trash"])
def test_newer_incoming_in_inbox_archive_trash(where: str) -> None:
    mail: dict[str, list[FakeMessage]] = {"inbox": [x_inbox()], "projects": [follow(3, 30, "f3@example.test")]}
    mail.setdefault(where, []).append(follow(2, 60))
    server = server_with(**mail)  # type: ignore[arg-type]
    assert facts(run(server, [r()])[0]) == (None, None, ago(60), True)  # `other` mailboxes are not searched (R35)


def test_draft_reply_ignored() -> None:
    """MF T9, S1: a server that ignores NOT DRAFT; the re-check drops drafts in sent and in the thread folders."""
    draft = (b"\\Draft",)
    server = server_with(
        inbox=[x_inbox(), follow(2, 30, flags=draft)], sent=[follow(5, 60, A_MID, sender=OWNER, flags=(b"\\draft",))]
    )
    server.honour_not_draft = False
    assert facts(run(server, [r()])[0]) == (None, None, None, True)
    assert ("Sent Messages", [5]) in lookup_fetches(server)
    assert ("INBOX", [2]) in lookup_fetches(server)
    assert all(key.startswith(b"NOT DRAFT ") for key in lookup_searches(server))


def _block(*lines: str) -> bytes:
    return ("\r\n".join(lines) + "\r\n\r\n").encode()


PADDING = " ".join(f"<p{index}@example.test>" for index in range(5000))  # about 100 KiB of References


def test_cut_references_marks_unchecked(monkeypatch: pytest.MonkeyPatch) -> None:
    """A-sec3, C9, MF-R4, T4-3: a 64 KiB block cut before a wanted id, or with its From after the cut, is never a
    positive and leaves the ref unchecked."""
    late = FakeMessage(5, ago(60), headers=_block(f"Message-ID: <{A_MID}>", f"References: {PADDING} <{MID}>"))
    server = server_with(inbox=[x_inbox()], sent=[late])
    assert facts(run(server, [r()])[0]) == (None, None, None, False)

    sender = "From: Sender <sender@example.test>"
    fields = (f"Message-ID: <{F_MID}>", f"References: <{MID}> {PADDING}")
    server = server_with(inbox=[x_inbox(), FakeMessage(2, ago(30), headers=_block(*fields, sender))])
    assert facts(run(server, [r()])[0]) == (None, None, None, False)  # X shown, From cut off: not counted (MF-R4)
    server = server_with(inbox=[x_inbox(), FakeMessage(2, ago(30), headers=_block(sender, *fields))])
    assert facts(run(server, [r()])[0]) == (None, None, ago(30), True)  # From before the cut: counted

    # a Message-ID after the cut, or cut inside it: the hit may be a copy of the owner's reply (A-sec3)
    mail = FakeMessage(2, ago(30), headers=_block(sender, f"References: <{MID}> {PADDING}", f"Message-ID: <{A_MID}>"))
    server = server_with(inbox=[x_inbox(), mail])
    assert facts(run(server, [r()])[0]) == (None, None, None, False)
    head = f"{sender}\r\nReferences: <{MID}>\r\nMessage-ID: "  # the id itself reaches past the cut
    raw = (head + "z" * (DESCENDANT_HEADER_BYTES - len(head) - 4) + "@example.test\r\n\r\n").encode()
    server = server_with(inbox=[x_inbox(), FakeMessage(2, ago(30), headers=raw)])
    assert facts(run(server, [r()])[0]) == (None, None, None, False)

    # T4-3: an unbracketed References cut inside a longer id that begins with X ("<X>ing")
    head = f"Message-ID: <{A_MID}>\r\nReferences: "
    filler = "q" * (DESCENDANT_HEADER_BYTES - len(head) - 1 - len(MID) - len("@example.test")) + "@example.test"
    raw = (head + filler + " " + MID + "ing\r\nFrom: " + OWNER + "\r\n\r\n").encode()
    assert raw[:DESCENDANT_HEADER_BYTES].endswith(MID.encode())
    server = server_with(inbox=[x_inbox()], sent=[FakeMessage(5, ago(60), headers=raw)])
    monkeypatch.setattr(server, "client", lambda: OverMatchingClient(server))
    assert facts(run(server, [r()])[0]) == (None, None, None, False)


def test_own_reply_in_archive_with_and_without_own_addresses() -> None:
    """MF-R3: an own message outside Sent counts as newer incoming without the own-addresses file, not with it."""
    own_sent = follow(4, 30, "o1@example.test", sender=OWNER)  # sent from a client that did not save it to Sent
    server = server_with(inbox=[x_inbox()], archive=[own_sent])
    assert facts(run(server, [r()])[0]) == (None, None, ago(30), True)
    server = server_with(inbox=[x_inbox()], archive=[own_sent])
    assert facts(run(server, [r()], own=OWN)[0]) == (None, None, None, True)


def test_thread_step_runs_with_x_only_when_in_reply_to_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """P13: without the reply step the thread step still runs, with X as the only key."""
    monkeypatch.setattr(imap_status, "OTHER_REPLY_IN_REPLY_TO", False)
    server = server_with(
        inbox=[x_inbox(), follow(2, 30, F_MID, to=A_MID), follow(3, 50, "f2@example.test")],
        sent=[follow(5, 60, A_MID, sender=OWNER)],
    )
    assert facts(run(server, [r()])[0]) == (None, None, ago(50), False)
    thread = lookup_searches(server)
    assert len(thread) == 3  # inbox, archive, trash
    assert all(token(A_MID) not in key for key in thread)


def test_in_reply_to_off_gives_flag_only_and_unchecked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(imap_status, "OTHER_REPLY_IN_REPLY_TO", False)
    server = server_with(inbox=[x_inbox(flags=(b"\\Answered",))], sent=[follow(5, 60, A_MID, sender=OWNER)])
    [status] = run(server, [r()])
    assert facts(status) == (None, None, None, False)
    assert copy_flags(status)[0] is True  # `replied` comes from `\Answered` alone
    assert all(box != "Sent Messages" for _, _, box, _ in lookup_commands(server))


def test_hit_cap_keeps_lowest_for_replies_highest_for_thread() -> None:
    """E-im2: 501 hits → the 500 lowest UIDs in sent (earliest replies), the 500 highest in the thread folders
    (newest descendants), fetched in chunks of 100; the batch is unchecked."""
    count = MAX_LOOKUP_HITS + 1
    answers = [follow(uid, 600 - uid / 10, None) for uid in range(1, count + 1)]  # no Message-ID: no thread keys
    followers = [follow(uid, 300 - uid / 10, f"f{uid}@example.test") for uid in range(2, count + 2)]
    server = server_with(inbox=[x_inbox(), *followers], sent=answers)
    assert facts(run(server, [r()])[0]) == (ago(600 - 0.1), None, ago(300 - count / 10 - 0.1), False)
    fetched = lookup_fetches(server)
    assert all(len(uids) <= LOOKUP_FETCH_UIDS == 50 for _, uids in fetched)
    by_box = {box: sorted(uid for b, uids in fetched if b == box for uid in uids) for box, _ in fetched}
    assert by_box["Sent Messages"] == list(range(1, MAX_LOOKUP_HITS + 1))
    assert by_box["INBOX"] == list(range(count + 1 - MAX_LOOKUP_HITS + 1, count + 2))


def _lossy_fetch(server: FakeImapServer, mailbox_name: str, uid: int) -> None:
    """Every re-check FETCH in `mailbox_name` loses the row of `uid` (expunged meanwhile)."""

    class Lossy(FakeImapClient):
        def fetch(self, messages: object, items: object) -> dict[int, dict[bytes, object]]:
            rows = super().fetch(messages, items)  # type: ignore[arg-type]
            if self._selected("UID FETCH").raw_name == mailbox_name and DESCENDANT_HEADER_ITEM in items:  # type: ignore[operator]
                rows.pop(uid, None)
            return rows

    server.client = lambda: Lossy(server)  # type: ignore[method-assign]


def _fail_lookup(server: FakeImapServer, command: str, mailbox_name: str, value: str | None = None) -> None:
    """`command` answers NO in a reply/thread lookup in `mailbox_name` (a SEARCH only for the batch with `value`)."""
    original = server.run
    state = {"box": "", "examined": 0}

    def run(name: str, argument: object = None) -> None:
        if name == "EXAMINE":
            state["box"] = str(argument)
            if argument == mailbox_name:
                state["examined"] += 1
                if command == "EXAMINE" and state["examined"] == 2:  # the location opened it first
                    server.fail["EXAMINE"] = server.no("EXAMINE")
        lookup = name == command == "UID SEARCH" and b"In-Reply-To" in argument  # type: ignore[operator]
        if lookup and state["box"] == mailbox_name and value is not None and token(value) in argument:  # type: ignore[operator]
            server.fail["UID SEARCH"] = server.no("UID SEARCH")
        original(name, argument)

    server.run = run  # type: ignore[method-assign]


@pytest.mark.parametrize("case", ["search_no_batch_2_of_3", "fetch_loses_a_row", "examine_no_archive"])
def test_lookup_failures_mark_only_their_batch(case: str) -> None:
    """B7: a failed batch marks only its refs; positives read elsewhere stay; no map rebuild (TS1, §6.5)."""
    xs = [f"x{i}@example.test" for i in range(21)]  # batches x0..x9, x10..x19, x20
    server = server_with(
        inbox=[msg(i + 1, 200, "x", message_id=f"<{x}>") for i, x in enumerate(xs)],
        sent=[follow(1, 60, "a0@example.test", to=xs[0]), follow(2, 60, "a15@example.test", to=xs[15])],
        archive=[follow(1, 30, "f0@example.test", to=xs[0]), follow(2, 30, "f15@example.test", to=xs[15])],
    )
    if case == "search_no_batch_2_of_3":
        _fail_lookup(server, "UID SEARCH", "Sent Messages", xs[12])
    elif case == "fetch_loses_a_row":
        _lossy_fetch(server, "Archive", 2)
    else:
        _fail_lookup(server, "EXAMINE", "Archive")
    statuses = run(server, [r(x) for x in xs])
    assert states(statuses) == [FOUND] * 21
    checked = [s.thread_checked for s in statuses]
    if case == "examine_no_archive":
        assert checked == [False] * 21
        assert (statuses[0].reply_at, statuses[0].newer_incoming_at) == (ago(60), None)  # the reply read stays
        assert names(server).count("LIST") == 1  # only the location's
        return
    assert facts(statuses[0]) == (ago(60), "a0@example.test", ago(30), True)
    if case == "search_no_batch_2_of_3":  # reply batches x0..x9 | x10..x19 | x20
        assert checked == [True] * 10 + [False] * 10 + [True]
        assert facts(statuses[15]) == (None, None, ago(30), False)
    else:  # thread keys x0, a0, x1..x8 | x9..x15, a15, x16, x17 | x18..x20: the reply ids join their ref's batch
        assert checked == [True] * 9 + [False] * 9 + [True] * 3
        assert facts(statuses[15]) == (ago(60), "a15@example.test", None, False)


def test_no_sent_folder_marks_found_refs_unchecked() -> None:
    """C5, E-im3b: replies cannot be searched; the thread step still runs in inbox, archive and trash with X."""
    server = server_with(inbox=[x_inbox(flags=(b"\\Answered",)), follow(2, 30)])
    server.folders = [f for f in server.folders if f.raw_name != "Sent Messages"]
    [status] = run(server, [r()])
    assert facts(status) == (None, None, ago(30), False)
    assert copy_flags(status)[0] is True
    assert [box for _, name, box, _ in lookup_commands(server) if name == "UID SEARCH"] == [
        "INBOX",
        "Archive",
        "Deleted Messages",
    ]


def test_no_message_id_unchecked_and_replied_from_flag() -> None:
    """E-mf3: an id read in place without a valid Message-ID takes no part in the lookups."""
    server = server_with(inbox=[msg(1, 60, "no id", flags=(b"\\Answered",))], sent=[follow(5, 30, A_MID)])
    [status] = run(server, [m("INBOX", 1)])
    assert facts(status) == (None, None, None, False)
    assert copy_flags(status)[0] is True
    assert "UID SEARCH" not in names(server)


def test_refused_lookup_value_marks_only_its_ref() -> None:
    """D80: a pattern-valid Message-ID that search_value_ok refuses is never sent; only its own ref is unchecked."""
    raw = "x-gm-raw@example.test"
    server = server_with(inbox=[msg(2, 60, "a", message_id=f"<{raw}>"), x_inbox()])
    statuses = run(server, [m("INBOX", 2), r()])
    assert states(statuses) == [FOUND, FOUND]  # the id's seed stands although its location was refused
    assert [s.thread_checked for s in statuses] == [False, True]
    assert all(b"X-GM-RAW" not in key.upper() for key in searches(server))  # type: ignore[attr-defined]

    # a refused reply id: the reply counts, but it cannot be a thread key
    server = server_with(inbox=[x_inbox()], sent=[follow(5, 60, "x-gm-raw-reply@example.test", sender=OWNER)])
    assert facts(run(server, [r()])[0]) == (ago(60), "x-gm-raw-reply@example.test", None, False)
    assert all(b"X-GM-RAW" not in key.upper() for key in searches(server))  # type: ignore[attr-defined]


def test_no_searchable_lookup_value_opens_no_lookup_mailbox() -> None:
    """PR #51 review CR5: when no Message-ID can be searched, the reply and thread steps open no mailbox."""
    raw = "x-gm-raw@example.test"
    server = server_with(inbox=[msg(2, 60, "a", message_id=f"<{raw}>")])
    [status] = run(server, [m("INBOX", 2)])
    assert (states([status]), status.thread_checked) == ([FOUND], False)
    assert examined(server) == ["INBOX"]  # the in-place check only
    assert "UID SEARCH" not in names(server)


def test_missing_header_item_marks_unchecked(monkeypatch: pytest.MonkeyPatch) -> None:
    """A hit whose header item does not come back cannot be re-checked: its batch is unchecked, never a match."""
    server = server_with(inbox=[x_inbox()], sent=[FakeMessage(5, ago(60), headers=None)])
    monkeypatch.setattr(server, "client", lambda: LookupHitsClient(server))
    assert facts(run(server, [r()])[0]) == (None, None, None, False)
    assert ("Sent Messages", [5]) in lookup_fetches(server)


def test_gmail_sends_no_header_lookup() -> None:
    """GMAIL_REPLY_HEADER_SEARCH is off (T0 P1): no In-Reply-To/References SEARCH on Gmail (`_gmail_lookups`)."""
    assert imap_status.GMAIL_REPLY_HEADER_SEARCH is False
    server = gmail(all_mail=[msg(1, 60, "a", INBOX_L, message_id=f"<{MID}>")])
    [status] = run(server, [r(account="gmail")])
    assert status.state == "found"
    assert lookup_searches(server) == []


def test_soft_stop_during_lookups_marks_unfinished_refs_unchecked() -> None:
    """IR2: a stop before the trash batch of x10 leaves x0..x9 checked; x10 keeps the reply already read."""
    xs = [f"x{i}@example.test" for i in range(11)]  # keys x0..x9 | x10, a10

    def build() -> FakeImapServer:
        return server_with(
            inbox=[msg(i + 1, 200, "x", message_id=f"<{x}>") for i, x in enumerate(xs)],
            sent=[follow(21, 50, "a10@example.test", to=xs[10], sender=OWNER)],
            trash=[follow(30, 30, "f0@example.test", to=xs[0]), follow(31, 20, "f10@example.test", to=xs[10])],
            step=1.0,
        )

    dry = build()
    run(dry, [r(x) for x in xs], soft=1000.0, hard=2000.0)
    stop = lookup_search_index(dry, "Deleted Messages", xs[10])
    server = build()
    statuses = run(server, [r(x) for x in xs], soft=stop - 0.5, hard=2000.0)
    assert server.commands[:-1] == dry.commands[:stop]  # nothing sent from the stop on
    assert names(server)[-1] == "LOGOUT"
    assert states(statuses) == [FOUND] * 11
    assert [s.thread_checked for s in statuses] == [True] * 10 + [False]
    assert facts(statuses[0]) == (None, None, ago(30), True)
    assert facts(statuses[10]) == (ago(50), "a10@example.test", None, False)

    # a stop in the reply step: nothing of the thread step runs, every ref unchecked, no account error
    server = build()
    statuses = run(
        server, [r(x) for x in xs], soft=lookup_search_index(dry, "Sent Messages", xs[10]) - 0.5, hard=2000.0
    )
    assert [s.thread_checked for s in statuses] == [False] * 11
    assert all(s.newer_incoming_at is None for s in statuses)


def test_reply_step_sends_no_examine_after_location() -> None:
    """E-im5 Design A: the location searched sent last, so the reply SEARCH needs no EXAMINE."""
    server = server_with(inbox=[x_inbox()])
    run(server, [r()])
    assert examined(server) == [*ICLOUD_CORE, "INBOX", "Archive", "Deleted Messages"]
    [(index, _, box, _), *_] = lookup_commands(server)
    assert box == "Sent Messages"
    assert server.commands[index - 1][0] != "EXAMINE"

    # an `other` mailbox searched after sent for an unresolved ref: sent is opened again
    server = server_with(inbox=[x_inbox()])
    run(server, [r(), r(OTHER_MID)])
    assert examined(server) == [*ICLOUD_CORE, "Projects", "Sent Messages", "INBOX", "Archive", "Deleted Messages"]


def test_one_examine_per_thread_folder() -> None:
    xs = [f"x{i}@example.test" for i in range(21)]  # three batches
    server = server_with(inbox=[msg(i + 1, 200, "x", message_id=f"<{x}>") for i, x in enumerate(xs)])
    run(server, [r(x) for x in xs])
    assert examined(server) == [*ICLOUD_CORE, "INBOX", "Archive", "Deleted Messages"]
    boxes = [box for _, name, box, _ in lookup_commands(server) if name == "UID SEARCH"]
    assert boxes == ["Sent Messages"] * 3 + ["INBOX"] * 3 + ["Archive"] * 3 + ["Deleted Messages"] * 3


def test_lookup_searches_send_bracketed_tokens_and_not_draft() -> None:
    server = server_with(inbox=[x_inbox()], sent=[follow(5, 60, A_MID, sender=OWNER)])
    run(server, [r()])
    x, a = token(MID).decode(), token(A_MID).decode()
    reply = f"NOT DRAFT OR HEADER In-Reply-To {x} HEADER References {x}"
    pair = "(OR HEADER In-Reply-To {0} HEADER References {0})"
    thread = f"NOT DRAFT OR {pair.format(x)} {pair.format(a)}"
    assert lookup_searches(server) == [reply.encode()] + [thread.encode()] * 3
    assert {tuple(arg[1]) for _, name, _, arg in lookup_commands(server) if name == "UID FETCH"} == {  # type: ignore[index]
        ("FLAGS", "INTERNALDATE", DESCENDANT_HEADER_ITEM)
    }


@pytest.mark.parametrize("phase", ["location_examine", "location_search", "reply_search", "thread_fetch"])
def test_hard_deadline_in_location_or_lookup_fails_the_account(phase: str) -> None:
    """T8-2, P6: the hard deadline during the location or a lookup fails the account; never incomplete_search or
    thread_checked false."""

    def build() -> FakeImapServer:
        return server_with(inbox=[x_inbox(), follow(2, 30)], sent=[follow(5, 60, A_MID, sender=OWNER)], step=1.0)

    dry = build()
    run(dry, [r()], soft=1000.0, hard=2000.0)
    location = [i for i, (name, _) in enumerate(dry.commands) if name in ("EXAMINE", "UID SEARCH")]
    lookups = lookup_commands(dry)
    at = {
        "location_examine": location[2],  # Archive
        "location_search": location[3],
        "reply_search": lookups[0][0],
        "thread_fetch": next(index for index, name, box, _ in lookups if name == "UID FETCH" and box == "INBOX"),
    }[phase]
    with pytest.raises(ProviderError) as caught:
        run(build(), [r()], soft=1000.0, hard=at - 0.5)
    assert (caught.value.code, caught.value.cause) == ("upstream_timeout", "CallDeadline")


def test_lookups_log_nothing(hub_log: Capture) -> None:
    """§9.7: the lookups add no log line of their own (the location's LIST and the call's one refusal line, #52); no
    Message-ID, thread key or folder name reaches a log."""
    hostile_reply = "leak-reply-0002@example.test"
    server = server_with(
        inbox=[msg(1, 180, "x", message_id=f"<{HOSTILE_MID}>"), follow(2, 30, "leak-f@example.test", to=hostile_reply)],
        sent=[follow(5, 60, hostile_reply, to=HOSTILE_MID), follow(6, 50, None, to=HOSTILE_MID)],
        other=[folder(HOSTILE_FOLDER)],
    )
    _fail_lookup(server, "UID SEARCH", "Archive", HOSTILE_MID)
    [status] = run(server, [r(HOSTILE_MID)], own=OWN)
    assert (status.reply_at, status.newer_incoming_at, status.thread_checked) == (ago(60), ago(30), False)
    assert [json.loads(line)["event"] for line in hub_log.lines] == ["folder_map_refreshed", "provider_call_failed"]
    text = "\n".join(hub_log.lines) + "\n".join(message for _, _, message in hub_log.raw)
    for value in (HOSTILE_MID, hostile_reply, "leak-f", HOSTILE_FOLDER, "Leak", "owner@example.org"):
        assert value not in text


# --- hostile header blocks (SR2) --------------------------------------------------------------------------------

HOSTILE_UNITS = ("<x@y> ", "<x@y>", "(((( ", "<x@y>\r\n ", "a@x.y ", '"<x@y>" ')


def hostile(uid: int, minutes: float, unit: str) -> FakeMessage:
    """A message whose References fill the whole 64 KiB re-check item with hostile tokens."""
    head = f"From: Sender <sender@example.test>\r\nMessage-ID: <h{uid}@example.test>\r\nReferences: "
    return FakeMessage(uid, ago(minutes), headers=(head + unit * (DESCENDANT_HEADER_BYTES // len(unit) + 1)).encode())


class LookupHitsClient(FakeImapClient):
    """Reply and thread searches answer every message of the folder, so every one is re-checked; location searches
    skip the large blocks (they hold no wanted Message-ID), so that the fake does not parse them."""

    def search(self, criteria: bytes) -> list[int]:
        selected = self._selected("UID SEARCH")
        if b"In-Reply-To" in criteria:
            self._server.run("UID SEARCH", criteria)
            return sorted(selected.messages)
        everything = selected.messages
        selected.messages = {u: m for u, m in everything.items() if len(m.headers or b"") < CANDIDATE_HEADER_BYTES}
        try:
            return super().search(criteria)
        finally:
            selected.messages = everything


def test_hostile_references_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """SR2: 50 refs against 50 hostile 64 KiB blocks: a bounded number of commands, a soft check at least every 10
    rows parsed, and a stop at such a check sends nothing more."""
    xs = [f"x{i}@example.test" for i in range(50)]

    def build() -> FakeImapServer:
        server = server_with(
            inbox=[msg(i + 1, 200, "x", message_id=f"<{x}>") for i, x in enumerate(xs)],
            archive=[hostile(100 + i, 30, HOSTILE_UNITS[i % len(HOSTILE_UNITS)]) for i in range(50)],
        )
        monkeypatch.setattr(server, "client", lambda: LookupHitsClient(server))
        return server

    server = build()
    checks: list[str] = []
    statuses = run_direct(server, [r(x) for x in xs], lambda: checks.append("check"))
    assert states(statuses) == [FOUND] * 50
    assert [s.thread_checked for s in statuses] == [False] * 50  # every cut block may hide the ids it does not show
    assert len(lookup_searches(server)) == 5 * 4  # five batches in sent, inbox, archive and trash
    fetched = lookup_fetches(server)
    assert all(len(uids) <= LOOKUP_FETCH_UIDS for _, uids in fetched)
    assert len(checks) >= sum((len(uids) - 1) // SOFT_CHECK_ROWS for _, uids in fetched) > 0

    server = build()

    def stop_in_archive() -> None:
        if lookup_commands(server) and lookup_commands(server)[-1][2] == "Archive":
            raise SoftStop

    statuses = run_direct(server, [r(x) for x in xs], stop_in_archive)
    assert states(statuses) == [FOUND] * 50
    assert [s.thread_checked for s in statuses] == [False] * 50
    assert names(server)[-2:] == ["UID FETCH", "LOGOUT"]  # stopped inside the first archive answer


def test_hostile_recheck_wall_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """SR2: 500 hostile 64 KiB blocks (one thread batch at the hit cap) are re-checked within 3 s of wall time, or the
    soft stop fires within 1 s of its instant. The deadline runs on the real clock here."""
    soft = 1.0
    blocks = [hostile(uid, 30, HOSTILE_UNITS[uid % len(HOSTILE_UNITS)]) for uid in range(2, MAX_LOOKUP_HITS + 2)]
    server = server_with(inbox=[x_inbox(), *blocks])
    monkeypatch.setattr(server, "client", lambda: LookupHitsClient(server))
    box = mailbox(server)
    started = time.perf_counter()
    [status] = box.message_status([r()], CallDeadline(started, soft, 20.0, time.perf_counter))
    elapsed = time.perf_counter() - started
    assert (status.state, status.thread_checked) == ("found", False)
    assert elapsed <= min(3.0, soft + 1.0), elapsed


# --- refused commands: one provider_call_failed per call (#52, spec 080 §9.7) -------------------------------------


def failures(log: Capture) -> list[dict[str, object]]:
    return [record for record in map(json.loads, log.lines) if record["event"] == "provider_call_failed"]


def _refused_in_place(command: str) -> tuple[FakeImapServer, list[StableRef | MessageRef]]:
    server = server_with(projects=[msg(2, 5, "p", message_id=f"<{MID}>")])
    server.fail[command] = server.no(command)
    return server, [m("Projects", 2)]


def _fail_lookup_fetch(server: FakeImapServer, mailbox_name: str) -> None:
    """The lookup re-check FETCH (`fetch_chunks` in `_uid_rows`) in `mailbox_name` answers NO."""
    original = server.run
    state = {"box": ""}

    def run(name: str, argument: object = None) -> None:
        if name == "EXAMINE":
            state["box"] = str(argument)
        lookup = name == "UID FETCH" and DESCENDANT_HEADER_ITEM in argument[1]  # type: ignore[index]
        if lookup and state["box"] == mailbox_name:
            server.fail["UID FETCH"] = server.no("UID FETCH")
        original(name, argument)

    server.run = run  # type: ignore[method-assign]


def _refused_lookup(command: str) -> tuple[FakeImapServer, list[StableRef | MessageRef]]:
    server = server_with(inbox=[msg(1, 200, "x", message_id=f"<{MID}>")], sent=[follow(1, 60, A_MID)])
    if command == "EXAMINE":
        _fail_lookup(server, "EXAMINE", "Archive")
    elif command == "UID FETCH":
        _fail_lookup_fetch(server, "Sent Messages")
    else:
        _fail_lookup(server, "UID SEARCH", "Sent Messages", MID)
    return server, [r()]


REFUSED: dict[str, tuple[Callable[[], tuple[FakeImapServer, list[StableRef | MessageRef]]], str]] = {
    "location_examine_no": (lambda: (_examine_no()[0], [r()]), "ExamineRefused"),
    "location_search_no": (lambda: (_search_no()[0], [r()]), "SearchRefused"),
    "location_fetch_no": (lambda: (_fetch_loses_a_hit()[0], [r()]), "FetchRefused"),
    "in_place_examine_no": (lambda: _refused_in_place("EXAMINE"), "ExamineRefused"),
    "in_place_fetch_no": (lambda: _refused_in_place("UID FETCH"), "FetchRefused"),
    "lookup_examine_no": (lambda: _refused_lookup("EXAMINE"), "ExamineRefused"),
    "lookup_search_no": (lambda: _refused_lookup("UID SEARCH"), "SearchRefused"),
    "lookup_fetch_no": (lambda: _refused_lookup("UID FETCH"), "FetchRefused"),
}


@pytest.mark.parametrize("case", list(REFUSED))
def test_refused_command_logs_one_warning(case: str, hub_log: Capture) -> None:
    factory, cause = REFUSED[case]
    server, targets = factory()
    run(server, targets)
    [line] = failures(hub_log)
    assert {key: line.get(key) for key in ("level", "account", "capability", "outcome", "exception")} == {
        "level": "WARNING",
        "account": ICLOUD,
        "capability": "mail",
        "outcome": "upstream_timeout",
        "exception": cause,
    }
    assert set(line) <= allowed_fields("provider_call_failed")
    text = "\n".join(hub_log.lines)
    for value in (MID, A_MID, "Projects", "Archive", "Sent Messages"):
        assert value not in text


def test_many_refusals_of_one_class_log_one_line(hub_log: Capture) -> None:
    """One line per cause class: every later refusal of a class already logged in the call adds nothing."""
    server = server_with(inbox=[msg(1, 200, "x", message_id=f"<{MID}>")])
    server.fail["EXAMINE"] = server.no("EXAMINE")
    original = server.run

    def refuse_every_examine(command: str, argument: object = None) -> None:
        if command == "EXAMINE":
            server.fail["EXAMINE"] = server.no("EXAMINE")
        original(command, argument)

    server.run = refuse_every_examine  # type: ignore[method-assign]
    statuses = run(server, [r(), r(OTHER_MID)])
    assert states(statuses) == [INCOMPLETE, INCOMPLETE]
    assert len(examined(server)) > 1
    assert [line["exception"] for line in failures(hub_log)] == ["ExamineRefused"]


def test_refusals_of_every_class_log_one_line_each_first_seen_first(hub_log: Capture) -> None:
    """At most three lines per call, one per cause class, in the order the classes were first refused."""
    server = server_with(projects=[msg(2, 5, "p", message_id=f"<{MID}>")])
    server.fail["UID FETCH"] = server.no("UID FETCH")  # the in-place read comes first
    original = server.run

    def refuse(command: str, argument: object = None) -> None:
        if command == "UID SEARCH":  # every location search, from INBOX on
            server.fail["UID SEARCH"] = server.no("UID SEARCH")
        if command == "EXAMINE" and argument == "Deleted Messages":  # the third mailbox of the location
            server.fail["EXAMINE"] = server.no("EXAMINE")
        original(command, argument)

    server.run = refuse  # type: ignore[method-assign]
    statuses = run(server, [m("Projects", 2), r(OTHER_MID)])
    assert states(statuses) == [INCOMPLETE, INCOMPLETE]
    assert len(searches(server)) > 1
    assert [line["exception"] for line in failures(hub_log)] == ["FetchRefused", "SearchRefused", "ExamineRefused"]


def test_deleted_folder_is_stale_without_a_refusal_line(hub_log: Capture) -> None:
    """An id's mailbox deleted since the map was built: EXAMINE answers NO, the one rebuild no longer lists it, so the
    entry is `stale_id` (not a refusal) and no `provider_call_failed` line is written."""
    server = server_with(projects=[msg(2, 5, "p", message_id=f"<{MID}>")])
    maps = FolderMapCache(clock=clock_of(server))
    prime(mailbox(server, maps=maps), server)
    server.folders = [f for f in server.folders if f.raw_name != "Projects"]
    start = len(server.commands)
    assert states(run(server, [m("Projects", 2)], maps=maps)) == [STALE]
    assert names(server, start).count("LIST") == 1  # the one rebuild
    assert failures(hub_log) == []


@pytest.mark.parametrize("case", ["clean", "lost_row_without_no", "folder_cap", "soft_stop"])
def test_no_refusal_logs_no_warning(case: str, hub_log: Capture) -> None:
    server = server_with(
        inbox=[msg(1, 200, "x", message_id=f"<{MID}>")],
        sent=[follow(1, 60, A_MID)],
        archive=[follow(2, 30, to=MID)],
    )
    kwargs: dict[str, object] = {}
    if case == "lost_row_without_no":
        _lossy_fetch(server, "Archive", 2)  # expunged meanwhile: incomplete, but the server refused nothing
    elif case == "folder_cap":
        server, kwargs = _folder_cap()
    elif case == "soft_stop":
        server, kwargs = _soft_stop_in_core()
    run(server, [r()], **kwargs)  # type: ignore[arg-type]
    assert failures(hub_log) == []


@pytest.mark.parametrize("tool", ["list_messages", "search_messages", "get_thread"])
def test_other_tools_write_no_refusal_line(tool: str, hub_log: Capture) -> None:
    """#52 covers `get_message_status` only: the shared session notes a NO in every tool, only the status call logs."""
    if tool == "get_thread":
        server = imap_threads.icloud(inbox=[imap_threads.msg(1, 30, "seed", message_id="<s@example.test>")])
        server.fail["UID SEARCH"] = server.no("UID SEARCH")
        with contextlib.suppress(ProviderError):  # an incomplete seed search answers upstream_timeout
            imap_threads.thread(server, seed=imap_threads.ref("s@example.test", server))
    else:
        server = list_tests.icloud(inbox=[list_tests.msg(1, 5, "Rechnung")])
        words = ("rechnung",) if tool == "search_messages" else ()
        search = SearchTerms(words=words, sender=None, recipient=None, subject=None) if words else None
        query = ListQuery("inbox", list_tests.SINCE, False, None, 20, None, search)
        server.fail["UID SEARCH"] = server.no("UID SEARCH")
        list_tests.run(server, query)
    assert "UID SEARCH" not in server.fail  # the NO was answered
    assert failures(hub_log) == []
