"""IMAP `get_message` by `id` (D75 binding) and by `ref` (the location step) (spec 080 §5.2, §5.5, §6.5; WP13a T14)."""

import json
import logging
import time
from collections.abc import Callable, Iterator, Sequence
from datetime import UTC, datetime, timedelta

import pytest
from imapclient.exceptions import IMAPClientAbortError

import mcp_hub.providers.imap as imap
from mcp_hub.ids import MessageRef, StableRef, ThreadKey
from mcp_hub.providers.base import MailDetail, MailFlags, ProviderError
from mcp_hub.providers.deadline import CallDeadline, SoftStop
from mcp_hub.providers.folders import MAX_LOCATE_FOLDERS, FolderMapCache
from mcp_hub.providers.headers import candidate_header_item
from mcp_hub.providers.imap import ImapMailbox
from mcp_hub.providers.imap_guard import GuardedImapClient
from mcp_hub.providers.imap_session import LOCATE_BATCH_VALUES, FoundCopy, ImapSession, LocateResult
from tests.support.clock import FakeClock
from tests.support.imap_fake import COMMAND_ALLOWLIST, FakeFolder, FakeImapClient, FakeImapServer, FakeMessage
from tests.support.logcapture import Capture

NOW = datetime(2026, 10, 21, 7, 0, tzinfo=UTC)
OWN = frozenset({"owner@example.org"})
MID = "m1@example.test"
THREAD = 1781234567890123456
INBOX_L, SENT_L, DRAFT_L = b"\\Inbox", b"\\Sent", b"\\Draft"
ICLOUD_CORE = ["INBOX", "Archive", "Sent Messages", "Deleted Messages"]
GMAIL_ALL, GMAIL_SENT, GMAIL_TRASH = "[Gmail]/All Mail", "[Gmail]/Sent Mail", "[Gmail]/Trash"


def hdr(
    subject: str,
    *,
    sender: str = "Sender <sender@example.test>",
    message_id: str | None = None,
    in_reply_to: str | None = None,
    references: str | None = None,
) -> bytes:
    lines = [f"From: {sender}", "To: Owner <owner@example.org>", f"Subject: {subject}"]
    for name, value in (("Message-ID", message_id), ("In-Reply-To", in_reply_to), ("References", references)):
        if value is not None:
            lines.append(f"{name}: {value}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode()


def msg(
    uid: int,
    minutes_ago: float | None,
    subject: str = "s",
    *labels: bytes,
    flags: tuple[bytes, ...] = (),
    thrid: int | None = None,
    text: bytes = b"Hello",
    **h: str,
) -> FakeMessage:
    received = None if minutes_ago is None else NOW - timedelta(minutes=minutes_ago)
    return FakeMessage(uid, received, flags=flags, headers=hdr(subject, **h), text=text, labels=labels, thrid=thrid)


def folder(name: str, attributes: tuple[bytes, ...] = (), messages: Sequence[FakeMessage] = ()) -> FakeFolder:
    return FakeFolder(name, (b"\\HasNoChildren", *attributes), messages={m.uid: m for m in messages})


def icloud(
    *,
    inbox: Sequence[FakeMessage] = (),
    archive: Sequence[FakeMessage] = (),
    sent: Sequence[FakeMessage] = (),
    trash: Sequence[FakeMessage] = (),
    junk: Sequence[FakeMessage] = (),
    drafts: Sequence[FakeMessage] = (),
    other: Sequence[FakeFolder] = (),
    step: float = 0.0,
) -> FakeImapServer:
    """iCloud-like LIST: special-use attributes, junk and drafts mailboxes, then the `other` mailboxes."""
    folders = [
        folder("INBOX", (), inbox),
        folder("Sent Messages", (b"\\Sent",), sent),
        folder("Archive", (b"\\Archive",), archive),
        folder("Deleted Messages", (b"\\Trash",), trash),
        folder("Junk", (b"\\Junk",), junk),
        folder("Drafts", (b"\\Drafts",), drafts),
        *other,
    ]
    return FakeImapServer(folders, clock=FakeClock(), step_seconds=step)


def gmail(
    *,
    inbox: Sequence[FakeMessage] = (),
    all_mail: Sequence[FakeMessage] = (),
    sent: Sequence[FakeMessage] = (),
    trash: Sequence[FakeMessage] = (),
    with_all: bool = True,
) -> FakeImapServer:
    """Gmail-like LIST: every label and excluded mailbox holds a planted copy of MID."""
    planted = [msg(90, 1, "planted", message_id=f"<{MID}>")]
    folders = [
        folder("INBOX", (), inbox),
        folder("Receipts", (), planted),
        FakeFolder("[Gmail]", (b"\\HasChildren", b"\\Noselect")),
        *([folder(GMAIL_ALL, (b"\\All",), all_mail)] if with_all else []),
        folder("[Gmail]/Drafts", (b"\\Drafts",), planted),
        folder("[Gmail]/Important", (b"\\Important",), planted),
        folder(GMAIL_SENT, (b"\\Sent",), sent),
        folder("[Gmail]/Spam", (b"\\Junk",), planted),
        folder("[Gmail]/Starred", (b"\\Flagged",), planted),
        folder(GMAIL_TRASH, (b"\\Trash",), trash),
    ]
    return FakeImapServer(folders, capabilities=(b"IMAP4REV1", b"X-GM-EXT-1"), clock=FakeClock())


def clock_of(server: FakeImapServer) -> FakeClock:
    assert server.clock is not None
    return server.clock


def mailbox(
    server: FakeImapServer,
    *,
    account: str = "icloud",
    own: frozenset[str] = frozenset(),
    maps: FolderMapCache | None = None,
) -> ImapMailbox:
    return ImapMailbox(
        account,
        host="imap.example.test",
        port=993,
        folder="INBOX",
        username="u",
        password="p",
        client_factory=lambda host, port, context, timeout: server.client(),
        own_addresses=own,
        folder_maps=maps if maps is not None else FolderMapCache(clock=clock_of(server)),
    )


def deadline(server: FakeImapServer, soft: float = 16.0, hard: float = 20.0) -> CallDeadline:
    clock = clock_of(server)
    return CallDeadline(clock(), soft, hard, clock)


def get(server: FakeImapServer, target: MessageRef | StableRef, *, soft: float = 16.0, **kwargs: object) -> MailDetail:
    account = target.account
    return mailbox(server, account=account, **kwargs).get_message(target, deadline(server, soft))  # type: ignore[arg-type]


def refused(
    server: FakeImapServer, target: MessageRef | StableRef, *, soft: float = 16.0, **kwargs: object
) -> ProviderError:
    with pytest.raises(ProviderError) as caught:
        get(server, target, soft=soft, **kwargs)
    return caught.value


def locate(server: FakeImapServer, ids: Sequence[str], **kwargs: object) -> LocateResult:
    box = mailbox(server, account="gmail" if b"X-GM-EXT-1" in server.capabilities else "icloud")
    return box._run(lambda session: session.locate(ids, **kwargs), deadline(server))  # type: ignore[arg-type]


def prime(box: ImapMailbox, server: FakeImapServer) -> None:
    """Build and cache the folder map, as an earlier tool call would."""
    box._run(lambda session: session.folder_map(), deadline(server))


def names(server: FakeImapServer, start: int = 0) -> list[str]:
    return [name for name, _ in server.commands[start:]]


def examined(server: FakeImapServer, start: int = 0) -> list[object]:
    return [argument for name, argument in server.commands[start:] if name == "EXAMINE"]


def searches(server: FakeImapServer) -> list[object]:
    return [argument for name, argument in server.commands if name == "UID SEARCH"]


def fetch_items(server: FakeImapServer) -> list[list[str]]:
    return [list(argument[1]) for name, argument in server.commands if name == "UID FETCH"]  # type: ignore[index]


def ref(message_id: str = MID, account: str = "icloud") -> StableRef:
    return StableRef(account, message_id)


# --- MessageRef (`id`): D75 binding ----------------------------------------------------------------------------


def test_id_registry_inbox_opens_without_list() -> None:
    server = icloud(inbox=[msg(1, 5, "a")])
    detail = get(server, MessageRef("icloud", "INBOX", 7, 1))
    assert (detail.subject, detail.ref) == ("a", MessageRef("icloud", "INBOX", 7, 1))
    assert "LIST" not in names(server)
    assert examined(server) == ["INBOX"]


@pytest.mark.parametrize(
    ("name", "folder_value"),
    [("Projects", "other"), ("Archive", "archive"), ("Sent Messages", "sent"), ("Deleted Messages", "trash")],
)
def test_id_other_folder_opens_after_list(name: str, folder_value: str) -> None:
    one = [msg(1, 5, "a")]
    server = icloud(archive=one, sent=one, trash=one, other=[folder("Projects", (), one)])
    detail = get(server, MessageRef("icloud", name, 7, 1))
    assert names(server)[:4] == ["LOGIN", "CAPABILITY", "LIST", "EXAMINE"]
    assert examined(server) == [name]
    assert (detail.subject, detail.meta.folder, detail.meta.folder_name) == ("a", folder_value, name)
    assert detail.ref == MessageRef("icloud", name, 7, 1)


@pytest.mark.parametrize("name", ["Junk", "Drafts", "Projects/Spam", "Projects/drafts", "Unlisted"])
def test_id_junk_or_drafts_not_found_without_examine(name: str) -> None:
    planted = [msg(1, 5, "planted")]
    server = icloud(
        junk=planted,
        drafts=planted,
        other=[
            folder("Projects", (b"\\HasChildren",)),
            folder("Projects/Spam", (), planted),
            folder("Projects/drafts", (), planted),
        ],
    )
    error = refused(server, MessageRef("icloud", name, 7, 1))
    assert error.code == "not_found"
    assert "EXAMINE" not in names(server)
    assert names(server).count("LIST") == 1  # a map built in this call is not rebuilt for an unknown name


def test_id_with_cached_refusal_sends_no_list() -> None:
    server = icloud(junk=[msg(1, 5, "junk")])
    box = mailbox(server)
    prime(box, server)
    start = len(server.commands)
    with pytest.raises(ProviderError) as caught:
        box.get_message(MessageRef("icloud", "Junk", 7, 1), deadline(server))
    assert caught.value.code == "not_found"
    assert names(server, start) == ["LOGIN", "CAPABILITY", "LOGOUT"]


def test_unknown_mailbox_rebuilds_map_once() -> None:
    server = icloud()
    box = mailbox(server)
    prime(box, server)
    server.folders.append(folder("Projects 2", (), [msg(1, 5, "in a new mailbox")]))
    start = len(server.commands)
    detail = box.get_message(MessageRef("icloud", "Projects 2", 7, 1), deadline(server))
    assert detail.subject == "in a new mailbox"
    after = names(server, start)
    assert after.count("LIST") == 1
    assert after.index("LIST") < after.index("EXAMINE")


def test_forced_rebuild_limited_to_once_per_60_s() -> None:
    server = icloud()
    box = mailbox(server)
    prime(box, server)

    def unknown(name: str) -> list[str]:
        start = len(server.commands)
        with pytest.raises(ProviderError) as caught:
            box.get_message(MessageRef("icloud", name, 7, 1), deadline(server))
        assert caught.value.code == "not_found"
        assert "EXAMINE" not in names(server, start)
        return names(server, start)

    assert unknown("Gone 1").count("LIST") == 1
    clock_of(server).advance(30)
    assert "LIST" not in unknown("Gone 2")  # Sec16: within 60 s of the last rebuild
    clock_of(server).advance(31)
    assert unknown("Gone 3").count("LIST") == 1


def test_id_examine_no_rebuilds_once_then_not_found_or_upstream_timeout() -> None:
    server = icloud(other=[folder("Projects", (), [msg(1, 5, "a")]), folder("Later", (), [msg(1, 5, "b")])])
    box = mailbox(server)
    prime(box, server)
    server.folders = [f for f in server.folders if f.raw_name != "Projects"]  # deleted since the map was built
    start = len(server.commands)
    with pytest.raises(ProviderError) as gone:
        box.get_message(MessageRef("icloud", "Projects", 7, 1), deadline(server))
    assert gone.value.code == "not_found"
    assert names(server, start).count("LIST") == 1
    clock_of(server).advance(61)
    server.fail["EXAMINE"] = server.no("EXAMINE")  # still listed, but refused now
    with pytest.raises(ProviderError) as refused_now:
        box.get_message(MessageRef("icloud", "Later", 7, 1), deadline(server))
    assert refused_now.value.code == "upstream_timeout"


def test_id_uidvalidity_mismatch_is_not_found() -> None:
    server = icloud(other=[folder("Projects", (), [msg(1, 5, "a")])])
    error = refused(server, MessageRef("icloud", "Projects", 8, 1))
    assert error.code == "not_found"
    assert not [name for name in names(server) if name == "UID FETCH"]


@pytest.mark.parametrize("which", ["summary", "body"])
def test_id_fetch_no_is_upstream_timeout(which: str) -> None:
    server = icloud(inbox=[msg(1, 5, "a")])
    original = server.run

    def run(command: str, argument: object = None) -> None:
        if command == "UID FETCH":
            items = list(argument[1])  # type: ignore[index]
            if (which == "summary") == ("BODYSTRUCTURE" in items):
                server.fail["UID FETCH"] = server.no("UID FETCH")
        original(command, argument)

    server.run = run  # type: ignore[method-assign]
    error = refused(server, MessageRef("icloud", "INBOX", 7, 1))
    assert error.code == "upstream_timeout"


@pytest.mark.parametrize(
    "name",
    [GMAIL_SENT, "Receipts", "[Gmail]/Important", "[Gmail]/Spam", "[Gmail]/Drafts", "[Gmail]/Starred", "[Gmail]"],
)
def test_gmail_id_naming_sent_mailbox_or_label_not_found(name: str) -> None:
    server = gmail(sent=[msg(90, 1, "copy", SENT_L)])
    error = refused(server, MessageRef("gmail", name, 7, 90))
    assert error.code == "not_found"
    assert "EXAMINE" not in names(server)


@pytest.mark.parametrize(
    ("with_all", "name"),
    [(True, "INBOX"), (True, GMAIL_ALL), (True, GMAIL_TRASH), (False, GMAIL_SENT), (False, GMAIL_TRASH)],
)
def test_gmail_id_allowed_mailboxes_open(with_all: bool, name: str) -> None:
    one = [msg(1, 5, "a", INBOX_L)]
    server = gmail(inbox=one, all_mail=one, sent=one, trash=one, with_all=with_all)
    assert get(server, MessageRef("gmail", name, 7, 1)).subject == "a"
    assert examined(server) == [name]


@pytest.mark.parametrize(
    ("kind", "name", "flags", "labels"),
    [
        ("icloud", "INBOX", (b"\\Draft",), ()),
        ("icloud", "Archive", (b"\\draft",), ()),
        ("gmail", GMAIL_ALL, (), (DRAFT_L,)),
        ("gmail", "INBOX", (), (INBOX_L, b"\\DRAFTS")),
    ],
)
def test_draft_by_id_not_found(kind: str, name: str, flags: tuple[bytes, ...], labels: tuple[bytes, ...]) -> None:
    draft = [msg(1, 5, "draft", *labels, flags=flags)]
    server = icloud(inbox=draft, archive=draft) if kind == "icloud" else gmail(inbox=draft, all_mail=draft)
    error = refused(server, MessageRef(kind, name, 7, 1))
    assert error.code == "not_found"
    if kind == "gmail":
        assert all("X-GM-LABELS" in items for items in fetch_items(server))


@pytest.mark.parametrize("soft", [1.5, 2.5, 3.5])
def test_soft_stop_in_get_message_by_id_is_upstream_timeout(soft: float) -> None:
    """One second per command: the stop comes before EXAMINE, the summary fetch or the body fetch (R2b)."""
    server = icloud(inbox=[msg(1, 5, "a")], step=1.0)
    error = refused(server, MessageRef("icloud", "INBOX", 7, 1), soft=soft)
    assert (error.code, error.cause) == ("upstream_timeout", "SoftStop")


# --- StableRef (`ref`): the location step ---------------------------------------------------------------------


def test_ref_found_in_archive_returns_copy() -> None:
    server = icloud(
        inbox=[msg(1, 5, "unrelated", message_id="<x@example.test>")],
        archive=[msg(4, 60, "wanted", message_id=f"<{MID}>")],
    )
    detail = get(server, ref())
    assert detail.ref == MessageRef("icloud", "Archive", 7, 4)
    assert (detail.subject, detail.meta.folder, detail.meta.folder_name, detail.meta.message_id) == (
        "wanted",
        "archive",
        "Archive",
        MID,
    )


@pytest.mark.parametrize(
    ("inbox_minutes", "archive_minutes", "winner"),
    [(5, 60, MessageRef("icloud", "Archive", 7, 4)), (60, 60, MessageRef("icloud", "INBOX", 7, 1))],
)
def test_ref_earliest_copy_wins(inbox_minutes: int, archive_minutes: int, winner: MessageRef) -> None:
    server = icloud(
        inbox=[msg(1, inbox_minutes, "inbox copy", message_id=f"<{MID}>")],
        archive=[msg(4, archive_minutes, "archive copy", message_id=f"<{MID}>")],
    )
    assert get(server, ref()).ref == winner


def test_core_folders_always_searched() -> None:
    server = icloud(inbox=[msg(1, 5, "a", message_id=f"<{MID}>")], other=[folder("Projects")])
    detail = get(server, ref())
    assert detail.ref == MessageRef("icloud", "INBOX", 7, 1)
    assert examined(server) == [*ICLOUD_CORE, "INBOX"]  # every core folder, then the copy is opened again
    assert searches(server) == [f'NOT DRAFT HEADER Message-ID "<{MID}>"'.encode()] * 4


def test_other_folders_only_for_unresolved_refs_and_stop_when_resolved() -> None:
    others = [folder(f"P{i}", (), [msg(7, 30, "x", message_id=f"<{MID}>")] if i in (2, 3) else []) for i in (1, 2, 3)]
    server = icloud(sent=[msg(3, 30, "sent copy", message_id=f"<{MID}>")], other=others)
    assert get(server, ref()).ref == MessageRef("icloud", "Sent Messages", 7, 3)
    assert not {"P1", "P2", "P3"} & set(examined(server))  # resolved in a core folder: no other mailbox

    server = icloud(
        other=[folder(f"P{i}", (), list(f.messages.values())) for i, f in zip((1, 2, 3), others, strict=True)]
    )
    assert get(server, ref()).ref == MessageRef("icloud", "P2", 7, 7)
    assert examined(server) == [*ICLOUD_CORE, "P1", "P2"]  # LIST order; stops after P2; P2 stays open for the read


def test_other_copy_never_beats_core_copy() -> None:
    server = icloud(
        trash=[msg(2, 5, "core copy", message_id=f"<{MID}>")],
        other=[folder("Projects", (), [msg(9, 600, "earlier copy", message_id=f"<{MID}>")])],
    )
    assert get(server, ref()).ref == MessageRef("icloud", "Deleted Messages", 7, 2)
    assert "Projects" not in examined(server)


class OverMatchingClient(FakeImapClient):
    """A server whose UID SEARCH also answers every other message of the folder: false hits for the re-check."""

    def search(self, criteria: bytes) -> list[int]:
        super().search(criteria)
        return sorted(self._selected("UID SEARCH").messages)


def test_ref_substring_hit_rechecked(monkeypatch: pytest.MonkeyPatch) -> None:
    # Every decoy is older than the exact copy, so accepting any of them would make it the earliest copy and win.
    server = icloud(
        inbox=[
            msg(1, 200, "prefix", message_id=f"<x{MID}>"),
            msg(2, 180, "suffix", message_id=f"<{MID}.org>"),
            msg(3, 120, "case", message_id=f"<{MID.upper()}>"),
        ],
        archive=[msg(4, 60, "exact", message_id=f"<{MID}>")],
    )
    detail = get(server, ref())  # the fake's own search: only the case-insensitive hit (3) comes back
    assert (detail.ref, detail.subject) == (MessageRef("icloud", "Archive", 7, 4), "exact")
    assert [3] in [list(argument[0]) for name, argument in server.commands if name == "UID FETCH"]  # type: ignore[index]
    server.commands.clear()
    monkeypatch.setattr(server, "client", lambda: OverMatchingClient(server))
    detail = get(server, ref())  # prefix and suffix variants come back as hits too and are re-checked
    assert (detail.ref, detail.subject) == (MessageRef("icloud", "Archive", 7, 4), "exact")
    assert detail.ref != MessageRef("icloud", "INBOX", 7, 3)
    assert [1, 2, 3] in [list(argument[0]) for name, argument in server.commands if name == "UID FETCH"]  # type: ignore[index]


def test_ref_not_found_after_complete_search() -> None:
    server = icloud(
        inbox=[
            msg(1, 5, "substring", message_id=f"<x{MID}>"),
            msg(2, 5, "draft", flags=(b"\\Draft",), message_id=f"<{MID}>"),
        ],
        junk=[msg(1, 5, "junk", message_id=f"<{MID}>")],
        drafts=[msg(1, 5, "drafts", message_id=f"<{MID}>")],
        other=[folder("Projects"), folder("Projects/Spam", (), [msg(1, 5, "spam", message_id=f"<{MID}>")])],
    )
    server.honour_not_draft = False  # a server that ignores NOT DRAFT: the FLAGS re-check still drops the draft
    error = refused(server, ref())
    assert (error.code, error.cause) == ("not_found", "NotFound")
    assert examined(server) == [*ICLOUD_CORE, "Projects"]


def _soft_stop_before_any_hit() -> tuple[FakeImapServer, float]:
    return icloud(archive=[msg(4, 60, "a", message_id=f"<{MID}>")], step=1.0), 6.5


def _soft_stop_after_a_hit() -> tuple[FakeImapServer, float]:
    return icloud(inbox=[msg(1, 60, "a", message_id=f"<{MID}>")], step=1.0), 6.5


def _folder_cap() -> tuple[FakeImapServer, float]:
    others = [folder(f"P{i:02d}") for i in range(1, 27)]
    others.append(folder("P27", (), [msg(1, 5, "beyond the cap", message_id=f"<{MID}>")]))
    return icloud(other=others), 16.0


def _search_no() -> tuple[FakeImapServer, float]:
    server = icloud(archive=[msg(4, 60, "a", message_id=f"<{MID}.org>")])
    server.fail["UID SEARCH"] = server.no("UID SEARCH")
    return server, 16.0


def _examine_no() -> tuple[FakeImapServer, float]:
    server = icloud()
    server.fail["EXAMINE"] = server.no("EXAMINE")
    return server, 16.0


def _fetch_no() -> tuple[FakeImapServer, float]:
    server = icloud(inbox=[msg(1, 60, "case-insensitive hit", message_id=f"<{MID.upper()}>")])
    server.fail["UID FETCH"] = server.no("UID FETCH")
    return server, 16.0


INCOMPLETE: dict[str, Callable[[], tuple[FakeImapServer, float]]] = {
    "soft_stop_before_any_hit": _soft_stop_before_any_hit,
    "soft_stop_after_a_hit": _soft_stop_after_a_hit,
    "folder_cap": _folder_cap,
    "search_no": _search_no,
    "examine_no": _examine_no,
    "fetch_no": _fetch_no,
}


@pytest.mark.parametrize("case", list(INCOMPLETE))
def test_ref_incomplete_is_upstream_timeout(case: str) -> None:
    server, soft = INCOMPLETE[case]()
    error = refused(server, ref(), soft=soft)
    assert error.code == "upstream_timeout"
    assert error.cause == ("SoftStop" if case.startswith("soft_stop") else "IncompleteSearch")
    if case == "folder_cap":
        assert MAX_LOCATE_FOLDERS == 30
        assert len(examined(server)) == 30
        assert "P27" not in examined(server)


def test_ref_gmail_all_mail_then_trash() -> None:
    server = gmail(
        all_mail=[
            msg(1, 90, "draft copy", DRAFT_L, message_id=f"<{MID}>"),
            msg(2, 5, "other", message_id="<y@example.test>"),
        ],
        trash=[msg(5, 60, "trash copy", SENT_L, message_id=f"<{MID}>")],
    )
    detail = get(server, ref(account="gmail"))
    assert detail.ref == MessageRef("gmail", GMAIL_TRASH, 7, 5)
    assert (detail.meta.folder, detail.meta.direction) == ("trash", "outgoing")
    assert examined(server) == [GMAIL_ALL, GMAIL_TRASH]  # no INBOX, label, Sent, Spam or Drafts mailbox
    assert all("X-GM-LABELS" in items for items in fetch_items(server) if "FLAGS" in items)  # every draft check

    same_time = [msg(1, 60, "all mail copy", INBOX_L, message_id=f"<{MID}>")]
    server = gmail(all_mail=same_time, trash=[msg(5, 60, "trash copy", message_id=f"<{MID}>")])
    detail = get(server, ref(account="gmail"))
    assert detail.ref == MessageRef("gmail", GMAIL_ALL, 7, 1)
    assert detail.meta.folder == "inbox"

    server = gmail(sent=[msg(3, 60, "sent copy", SENT_L, message_id=f"<{MID}>")], with_all=False)
    assert get(server, ref(account="gmail")).ref == MessageRef("gmail", GMAIL_SENT, 7, 3)
    assert examined(server) == ["INBOX", GMAIL_SENT, GMAIL_TRASH, GMAIL_SENT]


@pytest.mark.parametrize(
    ("command", "error", "code"),
    [
        ("UID SEARCH", OSError("reset"), "unreachable"),
        ("UID SEARCH", IMAPClientAbortError("eof"), "upstream_error"),
        ("UID SEARCH", None, "upstream_error"),  # BAD
        ("EXAMINE", OSError("reset"), "unreachable"),
        ("UID FETCH", OSError("reset"), "unreachable"),
    ],
)
def test_ref_connection_loss_is_account_error(command: str, error: Exception | None, code: str) -> None:
    server = icloud(inbox=[msg(1, 5, "a", message_id=f"<{MID}>")])
    server.fail[command] = error if error is not None else server.bad(command)
    assert refused(server, ref()).code == code


# --- the location step itself (reused by WP13b) --------------------------------------------------------------


def test_locate_batches_of_at_most_20_values() -> None:
    ids = [f"r{i:02d}@example.test" for i in range(25)]
    server = icloud(sent=[msg(i + 1, 30, "x", message_id=f"<{value}>") for i, value in enumerate(ids)])
    result = locate(server, ids)
    assert result.complete is True
    assert sorted(result.found) == ids
    assert result.found[ids[0]] == FoundCopy(result.found[ids[0]].entry, 7, 1, NOW - timedelta(minutes=30))
    assert {copy.entry.raw_name for copy in result.found.values()} == {"Sent Messages"}
    per_folder = searches(server)
    assert len(per_folder) == 2 * len(ICLOUD_CORE)  # 20 + 5 values per core folder; no other mailbox
    first, second = per_folder[:2]
    assert isinstance(first, bytes)
    assert isinstance(second, bytes)
    assert first.startswith(b"NOT DRAFT OR ")
    assert first.count(b"HEADER Message-ID") == 20
    assert second.count(b"HEADER Message-ID") == 5
    assert all(f'"<{value}>"'.encode() in first + second for value in ids)


def test_locate_fetches_flags_date_and_message_id_only() -> None:
    server = icloud(inbox=[msg(1, 5, "a", message_id=f"<{MID}>")])
    locate(server, [MID])
    assert fetch_items(server) == [["FLAGS", "INTERNALDATE", candidate_header_item(["MESSAGE-ID"])]]
    server = gmail(all_mail=[msg(1, 5, "a", message_id=f"<{MID}>")])
    locate(server, [MID])
    assert fetch_items(server) == [["FLAGS", "INTERNALDATE", candidate_header_item(["MESSAGE-ID"]), "X-GM-LABELS"]]


def test_locate_other_mailboxes_search_only_unresolved_refs() -> None:
    server = icloud(
        inbox=[msg(1, 5, "a", message_id="<a@example.test>")],
        other=[folder("Projects", (), [msg(2, 5, "b", message_id="<b@example.test>")])],
    )
    result = locate(server, ["a@example.test", "b@example.test", "a@example.test"])
    assert result.complete is True
    assert {value: copy.entry.raw_name for value, copy in result.found.items()} == {
        "a@example.test": "INBOX",
        "b@example.test": "Projects",
    }
    *core, last = searches(server)
    assert all(b'"<a@example.test>"' in criteria and b'"<b@example.test>"' in criteria for criteria in core)  # type: ignore[operator]
    assert last == b'NOT DRAFT HEADER Message-ID "<b@example.test>"'


def test_locate_and_ref_find_messages_on_a_whole_token_server() -> None:
    # O35: iCloud matches HEADER Message-ID against whole bracketed tokens; a bare-id search finds nothing there.
    server = icloud(
        inbox=[msg(1, 5, "wanted", message_id=f"<{MID}>")],
        archive=[msg(4, 60, "other", message_id="<b@example.test>")],
    )
    server.whole_msgid_tokens = True
    result = locate(server, [MID, "b@example.test"])
    assert result.complete is True
    assert {value: copy.entry.raw_name for value, copy in result.found.items()} == {
        MID: "INBOX",
        "b@example.test": "Archive",
    }
    assert (
        searches(server)[0] == f'NOT DRAFT OR HEADER Message-ID "<{MID}>" HEADER Message-ID "<b@example.test>"'.encode()
    )
    detail = get(server, ref())
    assert (detail.ref, detail.subject, detail.meta.message_id) == (MessageRef("icloud", "INBOX", 7, 1), "wanted", MID)


@pytest.mark.parametrize("value", ["X-GM-RAW@example.test", 'a"b@example.test', "a b@example.test", ""])
def test_locate_refuses_values_it_cannot_search(value: str) -> None:
    server = icloud(inbox=[msg(1, 5, "a", message_id=f"<{MID}>")])
    result = locate(server, [value, MID])
    assert list(result.found) == [MID]
    assert result.complete is False  # the refused value was never searched: never "not found"
    assert all(value.encode() not in criteria for criteria in searches(server) if value)  # type: ignore[operator]


def test_locate_without_searchable_values_sends_nothing() -> None:
    server = icloud()
    assert locate(server, []) == LocateResult({}, complete=True, incomplete=frozenset(), uncertain=frozenset())
    refused_value = "X-GM-RAW@example.test"  # pattern-valid but never searchable: stays incomplete (accepted)
    assert locate(server, [refused_value]) == LocateResult(
        {}, complete=False, incomplete=frozenset({refused_value}), uncertain=frozenset()
    )
    assert "LIST" not in names(server)


def test_locate_failed_batch_marks_only_its_refs() -> None:
    ids = [f"r{i:02d}@example.test" for i in range(21)]
    server = icloud(inbox=[msg(1, 5, "x", message_id=f"<{ids[20]}>")])
    original = server.run

    def run(command: str, argument: object = None) -> None:
        if command == "UID SEARCH" and b'"<r20@example.test>"' not in argument:  # type: ignore[operator]
            server.fail["UID SEARCH"] = server.no("UID SEARCH")  # every batch of the first 20 refs fails
        original(command, argument)

    server.run = run  # type: ignore[method-assign]
    result = locate(server, ids)
    assert list(result.found) == [ids[20]]
    assert result.complete is False


def fail_search_in(server: FakeImapServer, plan: dict[str, str]) -> None:
    """UID SEARCH answers NO in mailbox `plan[value]` for every batch that carries `value`."""
    original = server.run
    selected = {"name": ""}

    def run(command: str, argument: object = None) -> None:
        if command == "EXAMINE":
            selected["name"] = str(argument)
        if command == "UID SEARCH":
            for value, mailbox_name in plan.items():
                if selected["name"] == mailbox_name and f'"<{value}>"'.encode() in argument:  # type: ignore[operator]
                    server.fail["UID SEARCH"] = server.no("UID SEARCH")
        original(command, argument)

    server.run = run  # type: ignore[method-assign]


def test_locate_marks_incomplete_and_uncertain_per_ref() -> None:
    """A failed batch marks only its refs: unresolved → `incomplete`, found → `uncertain` (an earlier copy may sit
    in the failed batch, S-e); a ref of another batch that matched nothing is absent and certain (WP13b: `gone`)."""
    a, b, c, d = (f"{x}@example.test" for x in "abcd")
    server = icloud(inbox=[msg(1, 5, "a", message_id=f"<{a}>"), msg(2, 5, "d", message_id=f"<{d}>")])
    fail_search_in(server, {c: "INBOX", d: "Archive"})
    result = locate(server, [a, b, c, d], batch_values=1)
    assert sorted(result.found) == [a, d]
    assert (result.incomplete, result.uncertain, result.complete) == (frozenset({c}), frozenset({d}), False)
    assert b not in result.found


def test_ref_uncertain_copy_is_upstream_timeout() -> None:
    server = icloud(inbox=[msg(1, 5, "inbox copy", message_id=f"<{MID}>")])
    fail_search_in(server, {MID: "Archive"})
    error = refused(server, ref())
    assert (error.code, error.cause) == ("upstream_timeout", "IncompleteSearch")


@pytest.mark.parametrize("path", ["id", "ref"])
def test_no_second_rebuild_after_a_list_in_the_same_call(path: str) -> None:
    """Sec16 window: a map listed in this call is not listed again after an EXAMINE NO."""
    server = icloud(other=[folder("Projects", (), [msg(1, 5, "a")])])
    original = server.run

    def run(command: str, argument: object = None) -> None:
        if command == "EXAMINE" and argument in ("Projects", "Archive"):
            server.fail["EXAMINE"] = server.no("EXAMINE")
        original(command, argument)

    server.run = run  # type: ignore[method-assign]
    target: MessageRef | StableRef = MessageRef("icloud", "Projects", 7, 1) if path == "id" else ref()
    assert refused(server, target).code == "upstream_timeout"
    assert names(server).count("LIST") == 1


class NoHeaderItemClient(FakeImapClient):
    """The server answers the hit's FETCH without the Message-ID header item."""

    def fetch(self, messages: object, items: object) -> dict[int, dict[bytes, object]]:
        rows = super().fetch(messages, items)  # type: ignore[arg-type]
        for row in rows.values():
            for key in [k for k in row if k.startswith(b"BODY[HEADER.FIELDS (MESSAGE-ID)]")]:
                del row[key]
        return rows


def test_hit_without_the_message_id_item_is_incomplete(monkeypatch: pytest.MonkeyPatch) -> None:
    server = icloud(archive=[msg(4, 60, "a", message_id=f"<{MID}>")])
    monkeypatch.setattr(server, "client", lambda: NoHeaderItemClient(server))
    error = refused(server, ref())
    assert (error.code, error.cause) == ("upstream_timeout", "IncompleteSearch")


def test_copy_without_internaldate_is_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    """S-p: an unparsable INTERNALDATE drops that message (one item_degraded), like a non-match."""
    server = icloud(archive=[msg(4, None, "no date", message_id=f"<{MID}>")])
    assert (refused(server, ref()).code, refused(server, ref()).cause) == ("not_found", "NotFound")
    server = icloud(
        archive=[msg(4, None, "no date", message_id=f"<{MID}>")],
        sent=[msg(3, 90, "dated", message_id=f"<{MID}>")],
    )
    assert get(server, ref()).ref == MessageRef("icloud", "Sent Messages", 7, 3)


@pytest.mark.parametrize("field", ["To", "Cc", "From"])
def test_hostile_address_header_parses_in_linear_time(field: str) -> None:
    """Security I1: To, Cc and From of `get_message` go through the linear parser on text cut to 8 KiB, never the
    quadratic policy parser (a 60,000-quote header took seconds there)."""
    lines = {"From": "From: Sender <sender@example.test>", "To": "To: Owner <owner@example.org>", "Cc": None}
    lines[field] = f"{field}: " + '"' * 60_000
    headers = "\r\n".join([line for line in lines.values() if line] + ["Subject: hostile", f"Message-ID: <{MID}>"])
    server = icloud(inbox=[FakeMessage(1, NOW - timedelta(minutes=5), headers=(headers + "\r\n\r\n").encode())])
    start = time.perf_counter()
    detail = get(server, MessageRef("icloud", "INBOX", 7, 1))
    assert time.perf_counter() - start < 0.5
    assert (detail.subject, detail.meta.message_id) == ("hostile", MID)
    if field != "From":
        assert detail.from_address == "sender@example.test"


def test_address_headers_keep_names_and_addresses() -> None:
    headers = (
        "From: =?utf-8?q?J=C3=B6rg?= <joerg@example.test>\r\n"
        'To: "Doe, Jane" <jane@example.test>,\r\n bob@example.test\r\n'
        "Cc: Team: carol@example.test, dave@example.test;\r\n"
        "Subject: s\r\n\r\n"
    )
    server = icloud(inbox=[FakeMessage(1, NOW - timedelta(minutes=5), headers=headers.encode())])
    detail = get(server, MessageRef("icloud", "INBOX", 7, 1))
    assert (detail.from_name, detail.from_address) == ("J\u00f6rg", "joerg@example.test")
    assert detail.to_addresses == ["jane@example.test", "bob@example.test"]
    assert detail.cc_addresses == ["carol@example.test", "dave@example.test"]


def test_history_header_failure_keeps_sender_and_subject(monkeypatch: pytest.MonkeyPatch) -> None:
    handler = Capture()
    logger = logging.getLogger("mcp_hub.providers")
    logger.addHandler(handler)

    def exploding(raw: bytes | None) -> object:
        raise ValueError("SENTINEL")

    monkeypatch.setattr(imap, "parse_summary_headers", exploding)
    try:
        detail = get(icloud(inbox=[msg(1, 5, "kept", message_id=f"<{MID}>")]), MessageRef("icloud", "INBOX", 7, 1))
    finally:
        logger.removeHandler(handler)
    assert (detail.subject, detail.from_address, detail.to_addresses) == (
        "kept",
        "sender@example.test",
        ["owner@example.org"],
    )
    assert (detail.meta.message_id, detail.meta.references, detail.meta.thread) == (None, (), None)
    assert [json.loads(line)["exception"] for line in handler.lines] == ["ValueError"]


@pytest.mark.parametrize("phase", ["core", "other"])
def test_soft_stop_marks_only_the_refs_still_open(phase: str) -> None:
    """One second per command. A stop among the core folders leaves a found ref uncertain (its copies were not all
    compared); a stop among the `other` mailboxes leaves it certain, because they are never searched for it."""
    a, b = "a@example.test", "b@example.test"
    server = icloud(inbox=[msg(1, 5, "a", message_id=f"<{a}>")], other=[folder("P1"), folder("P2")], step=1.0)
    soft = 8.5 if phase == "core" else 13.5  # stops before the Sent EXAMINE, or before the P2 EXAMINE
    box = mailbox(server)
    result = box._run(lambda session: session.locate([a, b]), deadline(server, soft))
    assert list(result.found) == [a]
    assert result.incomplete == frozenset({b})
    assert result.uncertain == (frozenset({a}) if phase == "core" else frozenset())
    assert ("P1" in examined(server)) is (phase == "other")


def test_map_cap_marks_only_unresolved_refs() -> None:
    """More than 500 `other` mailboxes cap the map: an unresolved ref is incomplete, a ref found in a core folder
    stays certain. More than 500 excluded mailboxes leave the search complete: none of them is ever searched."""
    a, b = "a@example.test", "b@example.test"
    boxes = [folder(f"P{i:03d}") for i in range(501)]
    server = icloud(inbox=[msg(1, 5, "a", message_id=f"<{a}>")], other=boxes)
    result = locate(server, [a, b])
    assert (list(result.found), result.incomplete, result.uncertain) == ([a], frozenset({b}), frozenset())
    assert locate(icloud(inbox=[msg(1, 5, "a", message_id=f"<{a}>")], other=boxes), [a]).complete is True
    junk = [folder(f"J{i:03d}", (b"\\Junk",)) for i in range(501)]
    result = locate(icloud(inbox=[msg(1, 5, "a", message_id=f"<{a}>")], other=junk), [a, b])
    assert (list(result.found), result.incomplete, result.complete) == ([a], frozenset(), True)


def test_gmail_with_over_500_labels_answers_not_found(hub_log: Capture) -> None:
    """Gmail labels are excluded mailboxes: more than 500 of them do not cap the map, so a ref missing from All
    Mail and Trash is `not_found` (not `upstream_timeout`) and the map refresh is logged `ok`."""
    server = gmail(all_mail=[msg(1, 5, "other", message_id="<y@example.test>")])
    server.folders.extend(folder(f"Label {i:03d}") for i in range(600))
    error = refused(server, ref(account="gmail"))
    assert (error.code, error.cause) == ("not_found", "NotFound")
    assert examined(server) == [GMAIL_ALL, GMAIL_TRASH]
    refreshed = [json.loads(line) for line in hub_log.lines if '"folder_map_refreshed"' in line]
    assert [(line["account"], line["outcome"]) for line in refreshed] == [("gmail", "ok")]


@pytest.mark.parametrize(
    ("max_folders", "batch_values"),
    [(0, 20), (MAX_LOCATE_FOLDERS + 1, 20), (30, 0), (30, LOCATE_BATCH_VALUES + 1), (True, 20)],
)
def test_locate_parameters_are_clamped(max_folders: int, batch_values: int) -> None:
    server = icloud()
    client = GuardedImapClient(server.client(), deadline(server), registry_inbox_raw="INBOX")
    client.login("u", "p")
    session = ImapSession(
        client, account="icloud", registry_inbox="INBOX", folder_maps=FolderMapCache(), own=frozenset()
    )
    with pytest.raises(ValueError, match="out of range"):
        session.locate([MID], max_folders=max_folders, batch_values=batch_values)
    assert "LIST" not in names(server)


# --- shared get_message steps -----------------------------------------------------------------------------------


def raise_once(original: Callable[..., object]) -> Callable[..., object]:
    calls = {"n": 0}

    def wrapper(*args: object, **kwargs: object) -> object:
        calls["n"] += 1
        if calls["n"] == 1:
            raise SoftStop
        return original(*args, **kwargs)

    return wrapper


@pytest.mark.parametrize("target", [MessageRef("icloud", "INBOX", 7, 1), ref()])
@pytest.mark.parametrize("step", ["choose_text_part", "decode_part", "_sender_and_subject"])
def test_get_message_steps_pass_soft_stop_through(
    step: str, target: MessageRef | StableRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(imap, step, raise_once(getattr(imap, step)))
    error = refused(icloud(inbox=[msg(1, 5, "a", message_id=f"<{MID}>")]), target)
    assert (error.code, error.cause) == ("upstream_timeout", "SoftStop")


def test_get_message_detail_meta() -> None:
    server = icloud(
        inbox=[
            msg(
                1,
                5,
                "Re: plan",
                flags=(b"\\Seen", b"\\Answered", b"\\Flagged"),
                sender="Owner <owner@example.org>",
                message_id=f"<{MID}>",
                in_reply_to="<p2@example.test>",
                references="<root@example.test> <p2@example.test>",
            )
        ],
        sent=[msg(2, 5, "plain", sender="Someone <someone@example.test>")],
    )
    detail = get(server, MessageRef("icloud", "INBOX", 7, 1), own=OWN)
    meta = detail.meta
    assert (meta.folder, meta.folder_name, meta.direction) == ("inbox", "INBOX", "outgoing")
    assert meta.flags == MailFlags(answered=True, flagged=True)
    assert (meta.message_id, meta.in_reply_to, meta.references) == (
        MID,
        "p2@example.test",
        ("root@example.test", "p2@example.test"),
    )
    assert meta.thread == ThreadKey("icloud", "headers", "root@example.test")
    assert detail.unread is False

    sent = get(server, MessageRef("icloud", "Sent Messages", 7, 2)).meta
    assert (sent.folder, sent.direction, sent.message_id) == ("sent", "outgoing", None)
    assert sent.thread is not None
    assert sent.thread.source == "subject"

    server = gmail(all_mail=[msg(1, 5, "self", INBOX_L, SENT_L, thrid=THREAD, message_id=f"<{MID}>")])
    meta = get(server, MessageRef("gmail", GMAIL_ALL, 7, 1)).meta
    assert (meta.folder, meta.folder_name, meta.direction) == ("inbox", GMAIL_ALL, "outgoing")
    assert meta.thread == ThreadKey("gmail", "gmail", THREAD)
    assert fetch_items(server)[0][-2:] == ["X-GM-LABELS", "X-GM-THRID"]


# --- logs and command surface ---------------------------------------------------------------------------------


@pytest.fixture
def hub_log() -> Iterator[Capture]:
    handler = Capture()
    logger = logging.getLogger("mcp_hub")
    logger.addHandler(handler)
    level = logger.level
    logger.setLevel(logging.INFO)  # also the INFO folder_map_refreshed line
    yield handler
    logger.removeHandler(handler)
    logger.setLevel(level)


def test_get_message_logs_carry_no_message_id_or_folder_name(hub_log: Capture, monkeypatch: pytest.MonkeyPatch) -> None:
    """R9: lookups by ref and id, a copy without INTERNALDATE and a broken structure all log; none of it names a
    Message-ID, a mailbox or an address."""
    server = icloud(
        archive=[msg(4, None, "Quarterly numbers", message_id=f"<{MID}>")],
        junk=[msg(1, 5, "junk")],
        other=[
            folder("Projects", (), [msg(7, 60, "Quarterly numbers", message_id=f"<{MID}>", sender="a@example.org")])
        ],
    )
    original = imap.choose_text_part

    def exploding(parts: object) -> object:
        raise ValueError(f"SENTINEL {MID}")

    box = mailbox(server, own=OWN)
    detail = box.get_message(ref(), deadline(server))
    assert detail.ref == MessageRef("icloud", "Projects", 7, 7)
    monkeypatch.setattr(imap, "choose_text_part", exploding)
    assert box.get_message(MessageRef("icloud", "Projects", 7, 7), deadline(server)).body_source == "none"
    monkeypatch.setattr(imap, "choose_text_part", original)
    for target in (MessageRef("icloud", "Junk", 7, 1), MessageRef("icloud", "Gone", 7, 1), ref("none@example.test")):
        with pytest.raises(ProviderError):
            box.get_message(target, deadline(server))
    events = {json.loads(line)["event"] for line in hub_log.lines}
    assert events == {"folder_map_refreshed", "item_degraded"}
    text = "\n".join(hub_log.lines) + "\n" + "\n".join(message for _, _, message in hub_log.raw)
    for secret in (
        MID,
        "none@example.test",
        "Projects",
        "Archive",
        "Junk",
        "Gone",
        "INBOX",
        "@example",
        "Quarterly",
        "SENTINEL",
    ):
        assert secret not in text


def test_get_message_only_uses_allowlisted_commands() -> None:
    server = icloud(inbox=[msg(1, 5, "a", message_id=f"<{MID}>")], other=[folder("Projects")])
    box = mailbox(server)
    box.get_message(ref(), deadline(server))
    box.get_message(MessageRef("icloud", "INBOX", 7, 1), deadline(server))
    assert set(names(server)) <= COMMAND_ALLOWLIST
    assert not {"Junk", "Drafts"} & set(examined(server))
