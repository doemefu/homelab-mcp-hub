"""IMAP `list_messages` on Gmail (`X-GM-EXT-1`): All Mail, labels, thread ids, no `\\All` (spec 080 §5.5, §6.2, §6.5;
WP13a T13)."""

import json
import logging
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta

import pytest

from mcp_hub.cursors import Position
from mcp_hub.ids import MessageRef, ThreadKey
from mcp_hub.providers.base import UNDECODABLE_NOTE, FolderPage, ListQuery, ProviderError
from mcp_hub.providers.deadline import CallDeadline
from mcp_hub.providers.folders import FolderMapCache, FolderPlan
from mcp_hub.providers.headers import SUMMARY_HEADER_ITEM, candidate_header_item
from mcp_hub.providers.imap import ImapMailbox
from tests.support.clock import FakeClock
from tests.support.imap_fake import COMMAND_ALLOWLIST, FakeFolder, FakeImapClient, FakeImapServer, FakeMessage
from tests.support.logcapture import Capture

NOW = datetime(2026, 10, 21, 7, 0, tzinfo=UTC)
SINCE = NOW - timedelta(hours=24)  # SEARCH SINCE 19-Oct-2026 (window start date - 1)
OWN = frozenset({"owner@example.org"})
CANDIDATE = ["INTERNALDATE", "FLAGS", "X-GM-LABELS"]
INBOX_L, SENT_L, IMPORTANT_L = b"\\Inbox", b"\\Sent", b"\\Important"
THREAD = 1781234567890123456
SENT_KEYS = b'NOT DRAFT SINCE 19-Oct-2026 X-GM-LABELS "\\\\Sent" NOT X-GM-LABELS "\\\\Inbox"'
ARCHIVE_KEYS = b'NOT DRAFT SINCE 19-Oct-2026 NOT X-GM-LABELS "\\\\Inbox" NOT X-GM-LABELS "\\\\Sent"'
FOLDER_VALUES = ("inbox", "sent", "archive", "trash", "all")

NAMES = {
    "en": {"all": "[Gmail]/All Mail", "sent": "[Gmail]/Sent Mail", "trash": "[Gmail]/Trash", "label": "Receipts"},
    "de": {
        "all": "[Gmail]/Alle Nachrichten",
        "sent": "[Gmail]/Gesendet",
        "trash": "[Gmail]/Papierkorb",
        "label": "Belege",
    },
}


def hdr(subject: str, *, sender: str = "Sender <sender@example.test>", message_id: str | None = None) -> bytes:
    lines = [f"From: {sender}", "To: Owner <owner@example.org>", f"Subject: {subject}"]
    if message_id is not None:
        lines.append(f"Message-ID: {message_id}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode()


def gm(
    uid: int,
    minutes_ago: float,
    subject: str,
    *labels: bytes,
    flags: tuple[bytes, ...] = (),
    thrid: int | None = THREAD,
    sender: str = "Sender <sender@example.test>",
    message_id: str | None = None,
) -> FakeMessage:
    return FakeMessage(
        uid,
        NOW - timedelta(minutes=minutes_ago),
        flags=flags,
        headers=hdr(subject, sender=sender, message_id=message_id),
        labels=labels,
        thrid=thrid,
    )


def folder(name: str, attributes: tuple[bytes, ...], messages: Sequence[FakeMessage] = ()) -> FakeFolder:
    return FakeFolder(name, (b"\\HasNoChildren", *attributes), messages={m.uid: m for m in messages})


def gmail(
    *,
    inbox: Sequence[FakeMessage] = (),
    all_mail: Sequence[FakeMessage] = (),
    sent: Sequence[FakeMessage] = (),
    trash: Sequence[FakeMessage] = (),
    with_all: bool = True,
    names: str = "en",
) -> FakeImapServer:
    """Gmail-like LIST (English or German names): every label mailbox and the excluded mailboxes hold a message."""
    n = NAMES[names]
    planted = [gm(90, 1, "planted")]
    folders = [
        folder("INBOX", (), inbox),
        folder(n["label"], (), planted),
        FakeFolder("[Gmail]", (b"\\HasChildren", b"\\Noselect")),
        *([folder(n["all"], (b"\\All",), all_mail)] if with_all else []),
        folder("[Gmail]/Drafts", (b"\\Drafts",), planted),
        folder("[Gmail]/Important", (b"\\Important",), planted),
        folder(n["sent"], (b"\\Sent",), sent),
        folder("[Gmail]/Spam", (b"\\Junk",), planted),
        folder("[Gmail]/Starred", (b"\\Flagged",), planted),
        folder(n["trash"], (b"\\Trash",), trash),
    ]
    return FakeImapServer(folders, capabilities=(b"IMAP4REV1", b"X-GM-EXT-1"), clock=FakeClock())


def clock_of(server: FakeImapServer) -> FakeClock:
    assert server.clock is not None
    return server.clock


def box(server: FakeImapServer, *, own: frozenset[str] = frozenset()) -> ImapMailbox:
    return ImapMailbox(
        "gmail",
        host="imap.example.test",
        port=993,
        folder="INBOX",
        username="u",
        password="p",
        client_factory=lambda host, port, context, timeout: server.client(),
        own_addresses=own,
        folder_maps=FolderMapCache(clock=clock_of(server)),
    )


def query(
    folder_value: str = "inbox",
    *,
    direction: str | None = None,
    limit: int = 20,
    position: Position | None = None,
    unread_only: bool = False,
) -> ListQuery:
    return ListQuery(folder_value, SINCE, unread_only, direction, limit, position)  # type: ignore[arg-type]


def deadline(server: FakeImapServer) -> CallDeadline:
    clock = clock_of(server)
    return CallDeadline(clock(), 16.0, 20.0, clock)


def run(server: FakeImapServer, q: ListQuery, *, own: frozenset[str] = frozenset()) -> FolderPage:
    return box(server, own=own).list_messages(q, deadline(server))


def names(server: FakeImapServer) -> list[str]:
    return [name for name, _ in server.commands]


def examined(server: FakeImapServer) -> list[object]:
    return [argument for name, argument in server.commands if name == "EXAMINE"]


def searches(server: FakeImapServer) -> list[object]:
    return [argument for name, argument in server.commands if name == "UID SEARCH"]


def fetch_items(server: FakeImapServer) -> list[list[str]]:
    return [list(a[1]) for n, a in server.commands if n == "UID FETCH"]  # type: ignore[index]


def view(page: FolderPage) -> list[tuple[str | None, str, str]]:
    return [(item.subject, item.meta.folder, item.meta.direction) for item in page.items]


# --- inbox ------------------------------------------------------------------------------------------------------


def test_inbox_keeps_examine_inbox() -> None:
    """F3: `inbox` and `list_unread` read the registry inbox, never All Mail, and need no LIST."""
    server = gmail(inbox=[gm(1, 5, "a", INBOX_L), gm(2, 6, "b", INBOX_L, flags=(b"\\Seen",))])
    page = run(server, query())
    assert server.commands[:4] == [
        ("LOGIN", None),
        ("CAPABILITY", None),
        ("EXAMINE", "INBOX"),
        ("UID SEARCH", b"NOT DRAFT SINCE 19-Oct-2026"),
    ]
    assert fetch_items(server)[0] == CANDIDATE
    assert "LIST" not in names(server)
    assert view(page) == [("a", "inbox", "incoming"), ("b", "inbox", "incoming")]
    assert [item.ref for item in page.items] == [MessageRef("gmail", "INBOX", 7, uid) for uid in (1, 2)]
    unread = gmail(inbox=[gm(1, 5, "a", INBOX_L), gm(2, 6, "b", INBOX_L, flags=(b"\\Seen",))])
    listed = box(unread).list_unread(SINCE, 20, deadline(unread))
    assert [item.subject for item in listed.items] == ["a"]
    assert examined(unread) == ["INBOX"]
    assert searches(unread) == [b"NOT DRAFT UNSEEN SINCE 19-Oct-2026"]


def test_inbox_message_with_inbox_and_sent_labels_is_outgoing() -> None:
    """R11: a message the owner sent to themselves is `inbox` with `direction: outgoing`, also as a filter."""

    def server() -> FakeImapServer:
        return gmail(inbox=[gm(1, 5, "to myself", INBOX_L, SENT_L), gm(2, 6, "from them", INBOX_L)])

    assert view(run(server(), query())) == [("to myself", "inbox", "outgoing"), ("from them", "inbox", "incoming")]
    outgoing = run(server(), query(direction="outgoing", limit=1))  # sent_label filters before `limit` (C4)
    assert (view(outgoing), outgoing.more) == ([("to myself", "inbox", "outgoing")], False)
    incoming = run(server(), query(direction="incoming", limit=1))
    assert (view(incoming), incoming.more) == ([("from them", "inbox", "incoming")], False)


# --- All Mail ---------------------------------------------------------------------------------------------------


def all_mail_messages() -> list[FakeMessage]:
    return [
        gm(1, 5, "in", INBOX_L, IMPORTANT_L),
        gm(2, 10, "sent", SENT_L, thrid=THREAD + 1),
        gm(3, 15, "self", INBOX_L, SENT_L),
        gm(4, 20, "labelled", b"Receipts"),
        gm(5, 25, "archived"),
        gm(6, 30, "draft", b"\\Draft"),
        gm(7, 35, "lower draft", b"\\drafts", INBOX_L),
        gm(8, 40, "no thread", INBOX_L, thrid=None, message_id="<n@example.test>"),
    ]


def test_sent_uses_all_mail_with_label_keys() -> None:
    server = gmail(all_mail=all_mail_messages())
    page = run(server, query("sent"))
    assert server.commands[:5] == [
        ("LOGIN", None),
        ("CAPABILITY", None),
        ("LIST", ("", "*")),
        ("EXAMINE", "[Gmail]/All Mail"),
        ("UID SEARCH", SENT_KEYS),
    ]
    assert view(page) == [("sent", "sent", "outgoing")]
    [item] = page.items
    assert (item.ref, item.meta.folder_name) == (MessageRef("gmail", "[Gmail]/All Mail", 7, 2), "[Gmail]/All Mail")
    assert item.meta.thread == ThreadKey("gmail", "gmail", THREAD + 1)
    assert examined(server) == ["[Gmail]/All Mail"]


def test_archive_label_keys() -> None:
    server = gmail(all_mail=all_mail_messages())
    page = run(server, query("archive"))
    assert searches(server) == [ARCHIVE_KEYS]
    assert examined(server) == ["[Gmail]/All Mail"]
    assert view(page) == [("labelled", "archive", "incoming"), ("archived", "archive", "incoming")]


def test_all_reads_all_mail_alone_with_label_precedence() -> None:
    server = gmail(all_mail=all_mail_messages(), inbox=[gm(1, 5, "in", INBOX_L)], sent=[gm(2, 10, "sent", SENT_L)])
    page = run(server, query("all"))
    assert examined(server) == ["[Gmail]/All Mail"]  # All Mail alone: no INBOX, no \Sent mailbox
    assert searches(server) == [b"NOT DRAFT SINCE 19-Oct-2026"]  # no label key
    assert view(page) == [
        ("in", "inbox", "incoming"),
        ("sent", "sent", "outgoing"),
        ("self", "inbox", "outgoing"),
        ("labelled", "archive", "incoming"),
        ("archived", "archive", "incoming"),
        ("no thread", "inbox", "incoming"),
    ]
    assert all(item.ref.folder == "[Gmail]/All Mail" for item in page.items)
    threads = [item.meta.thread for item in page.items]
    assert threads[:5] == [
        ThreadKey("gmail", "gmail", THREAD),
        ThreadKey("gmail", "gmail", THREAD + 1),
        *[ThreadKey("gmail", "gmail", THREAD)] * 3,
    ]
    assert threads[5] is None  # no X-GM-THRID: no key, never a header-derived one on Gmail
    items = fetch_items(server)
    assert [listed for listed in items if listed[:2] == ["INTERNALDATE", "FLAGS"]] == [CANDIDATE]  # I13: no dedup
    assert [*("FLAGS", "INTERNALDATE", "BODYSTRUCTURE", SUMMARY_HEADER_ITEM), "X-GM-LABELS", "X-GM-THRID"] in items


def test_all_later_page_keeps_before_on_gmail() -> None:
    """Gmail `all` reads one mailbox, so later pages keep the BEFORE bound (S-a covers non-Gmail `all` only)."""
    position = Position(int(datetime(2026, 10, 20, 10, 0, tzinfo=UTC).timestamp()), "f" * 16)
    server = gmail(all_mail=all_mail_messages())
    run(server, query("all", position=position))
    assert searches(server) == [b"NOT DRAFT SINCE 19-Oct-2026 BEFORE 22-Oct-2026"]


def test_self_sent_message_not_in_sent() -> None:
    def server() -> FakeImapServer:
        return gmail(inbox=[gm(31, 15, "self", INBOX_L, SENT_L)], all_mail=[gm(3, 15, "self", INBOX_L, SENT_L)])

    assert run(server(), query("sent")).items == []
    assert view(run(server(), query("inbox"))) == [("self", "inbox", "outgoing")]
    assert view(run(server(), query("all"))) == [("self", "inbox", "outgoing")]
    assert run(server(), query("archive")).items == []


def test_draft_label_dropped_per_message() -> None:
    """`\\Draft`/`\\Drafts` labels in any case drop a message in pass 1 and, when set meanwhile, in pass 2."""
    server = gmail(
        inbox=[gm(1, 5, "draft", b"\\Draft"), gm(2, 6, "drafts", b"\\DRAFTS"), gm(3, 7, "mail", INBOX_L)],
        all_mail=all_mail_messages(),
    )
    assert [item.subject for item in run(server, query()).items] == ["mail"]
    assert "draft" not in [item.subject for item in run(server, query("all")).items]
    assert "lower draft" not in [item.subject for item in run(server, query("all")).items]

    later = gmail(inbox=[gm(1, 5, "becomes a draft", INBOX_L), gm(2, 6, "mail", INBOX_L)])
    original = later.run

    def run_command(command: str, argument: object = None) -> None:
        if command == "UID FETCH" and SUMMARY_HEADER_ITEM in argument[1]:  # type: ignore[index]
            later.folders[0].messages[1].labels = (INBOX_L, b"\\drafts")
        original(command, argument)

    later.run = run_command  # type: ignore[method-assign]
    page = run(later, query())
    assert [item.subject for item in page.items] == ["mail"]
    assert page.complete is True


class DeepStructureClient(FakeImapClient):
    """UID 1's BODYSTRUCTURE cannot be parsed, so its pass-2 row comes from the FLAGS X-GM-LABELS fallback fetch."""

    def fetch(self, messages: object, items: object) -> dict[int, dict[bytes, object]]:
        if "BODYSTRUCTURE" in items and 1 in messages:  # type: ignore[operator]
            raise RecursionError("maximum recursion depth exceeded")
        return super().fetch(messages, items)  # type: ignore[arg-type]


@pytest.mark.parametrize("unparsable", [False, True])
def test_sent_label_added_between_passes_drops_an_incoming_item(
    unparsable: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under `direction: incoming`, a `\\Sent` label that appears after pass 1 drops the item, so `folder: sent`
    never pairs with `direction: incoming` (T13 review); without the filter it is listed as sent and outgoing."""
    later = gmail(all_mail=[gm(1, 5, "becomes sent"), gm(2, 6, "stays incoming")])
    all_mail = next(f for f in later.folders if f.raw_name == "[Gmail]/All Mail")
    original = later.run
    fetched = {"n": 0}

    def run_command(command: str, argument: object = None) -> None:
        if command == "UID FETCH":
            fetched["n"] += 1
            if fetched["n"] == 2:  # the first pass-2 fetch: the summary batch, or the FLAGS X-GM-LABELS fallback
                all_mail.messages[1].labels = (SENT_L,)
        original(command, argument)

    later.run = run_command  # type: ignore[method-assign]
    if unparsable:
        monkeypatch.setattr(later, "client", lambda: DeepStructureClient(later))
    page = run(later, query("all", direction="incoming"))
    assert [item.subject for item in page.items] == ["stays incoming"]
    assert page.complete is True
    listed = run(later, query("all"))
    assert [(item.meta.folder, item.meta.direction) for item in listed.items] == [
        ("sent", "outgoing"),
        ("archive", "incoming"),
    ]


def test_per_message_folder_fails_closed_off_gmail(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only Gmail All Mail may leave the folder to the labels: a plan without a fixed folder on any other server is
    `upstream_error`, never a guessed folder (T13 review)."""
    server = FakeImapServer([folder("INBOX", (), [gm(1, 5, "a", INBOX_L)])], clock=FakeClock())
    monkeypatch.setattr(
        ImapMailbox, "_plans", staticmethod(lambda session, value: [FolderPlan(session.inbox, None, None)])
    )
    with pytest.raises(ProviderError) as caught:
        run(server, query())
    assert (caught.value.code, caught.value.cause) == ("upstream_error", "UnmappedFolder")
    assert "BODYSTRUCTURE" not in [item for items in fetch_items(server) for item in items]  # before pass 2 reads


def test_trash_reads_trash_mailbox() -> None:
    server = gmail(trash=[gm(1, 5, "deleted", SENT_L)], all_mail=all_mail_messages())
    page = run(server, query("trash"))
    assert examined(server) == ["[Gmail]/Trash"]
    assert searches(server) == [b"NOT DRAFT SINCE 19-Oct-2026"]
    assert view(page) == [("deleted", "trash", "outgoing")]  # Gmail: the \Sent label makes it outgoing
    assert page.items[0].ref == MessageRef("gmail", "[Gmail]/Trash", 7, 1)


@pytest.mark.parametrize("folder_value", FOLDER_VALUES)
@pytest.mark.parametrize("with_all", [True, False])
def test_german_mailbox_names_same_result(folder_value: str, with_all: bool) -> None:
    def server(names: str) -> FakeImapServer:
        return gmail(
            inbox=[gm(31, 5, "in", INBOX_L, IMPORTANT_L)],
            all_mail=all_mail_messages(),
            sent=[gm(41, 10, "sent", SENT_L)],
            trash=[gm(51, 3, "deleted")],
            with_all=with_all,
            names=names,
        )

    english, german = server("en"), server("de")
    en_page, de_page = run(english, query(folder_value)), run(german, query(folder_value))
    assert view(en_page) == view(de_page)
    assert (en_page.more, en_page.truncated, en_page.complete) == (de_page.more, de_page.truncated, de_page.complete)
    translate = {NAMES["en"][role]: NAMES["de"][role] for role in ("all", "sent", "trash")}
    assert [translate.get(str(name), name) for name in examined(english)] == examined(german)


def test_without_all_mail() -> None:
    """All Mail hidden from IMAP: inbox, `\\Sent` and trash only; `archive` empty; `all` = inbox + sent, merged."""

    def server() -> FakeImapServer:
        return gmail(
            inbox=[
                gm(1, 5, "self", INBOX_L, SENT_L, sender="owner@example.org", message_id="<s@example.test>"),
                gm(2, 6, "in", INBOX_L, message_id="<i@example.test>"),
            ],
            sent=[
                gm(11, 5, "self", INBOX_L, SENT_L, sender="owner@example.org", message_id="<s@example.test>"),
                gm(12, 8, "out", SENT_L, sender="owner@example.org", message_id="<o@example.test>"),
            ],
            with_all=False,
        )

    sent = server()
    page = run(sent, query("sent"))
    assert examined(sent) == ["[Gmail]/Sent Mail"]
    assert searches(sent) == [b"NOT DRAFT SINCE 19-Oct-2026"]  # a real mailbox: no label key
    assert view(page) == [("self", "sent", "outgoing"), ("out", "sent", "outgoing")]

    archive = server()
    page = run(archive, query("archive"))
    assert (page.items, page.more, page.truncated, page.complete) == ([], False, False, True)
    assert "EXAMINE" not in names(archive)

    every = server()
    page = run(every, query("all"))
    assert examined(every)[:2] == ["INBOX", "[Gmail]/Sent Mail"]  # pass 1; pass 2 opens them again
    assert set(examined(every)) == {"INBOX", "[Gmail]/Sent Mail"}
    assert view(page) == [("self", "inbox", "outgoing"), ("in", "inbox", "incoming"), ("out", "sent", "outgoing")]
    assert fetch_items(every)[0] == [*CANDIDATE, candidate_header_item(["MESSAGE-ID", "FROM"])]  # the dedup key


@pytest.mark.parametrize("with_all", [True, False])
def test_label_mailboxes_never_examined(with_all: bool) -> None:
    server = gmail(
        inbox=[gm(31, 5, "in", INBOX_L)],
        all_mail=all_mail_messages(),
        sent=[gm(41, 10, "sent", SENT_L)],
        trash=[gm(51, 3, "deleted")],
        with_all=with_all,
    )
    mailbox = box(server, own=OWN)
    listed: list[str | None] = []
    for folder_value in FOLDER_VALUES:
        for direction in (None, "outgoing"):
            page = mailbox.list_messages(query(folder_value, direction=direction), deadline(server))
            listed.extend(item.subject for item in page.items)
    allowed = {"INBOX", "[Gmail]/Trash", "[Gmail]/All Mail" if with_all else "[Gmail]/Sent Mail"}
    assert set(examined(server)) == allowed
    assert listed
    assert "planted" not in listed


@pytest.mark.parametrize("folder_value", ["inbox", "all", "sent"])
@pytest.mark.parametrize(
    ("direction", "own", "with_from"),
    [(None, OWN, False), ("incoming", frozenset(), False), ("outgoing", frozenset(), False), ("incoming", OWN, True)],
)
def test_from_fetched_only_with_direction_and_own_addresses(
    folder_value: str, direction: str | None, own: frozenset[str], with_from: bool
) -> None:
    """I13: FROM only with `direction` and own addresses; Gmail `all` never fetches a Message-ID."""
    server = gmail(inbox=[gm(1, 5, "in", INBOX_L)], all_mail=all_mail_messages())
    run(server, query(folder_value, direction=direction), own=own)
    expected = [*CANDIDATE, candidate_header_item(["FROM"])] if with_from else CANDIDATE
    assert fetch_items(server)[0] == expected


def test_no_x_gm_raw_in_any_command() -> None:
    """D80: X-GM-RAW is never sent, on any path."""
    for with_all in (True, False):
        server = gmail(
            inbox=[gm(1, 5, "in", INBOX_L)],
            all_mail=all_mail_messages(),
            sent=[gm(41, 10, "sent", SENT_L)],
            trash=[gm(51, 3, "deleted")],
            with_all=with_all,
        )
        mailbox = box(server, own=OWN)
        for folder_value in FOLDER_VALUES:
            for unread_only in (False, True):
                q = query(folder_value, direction="outgoing", unread_only=unread_only)
                mailbox.list_messages(q, deadline(server))
        mailbox.list_unread(SINCE, 20, deadline(server))
        mailbox.check(deadline(server))
        assert set(names(server)) <= COMMAND_ALLOWLIST
        assert "X-GM-RAW" not in repr(server.commands).upper()


# --- logs and degraded messages ---------------------------------------------------------------------------------


class GmailDegradingClient(FakeImapClient):
    """Drops INTERNALDATE of UID 5 (item_degraded in pass 1) and fails the BODYSTRUCTURE fetch of UID 1 (placeholder
    in pass 2), so every Gmail path writes log lines."""

    def fetch(self, messages: object, items: object) -> dict[int, dict[bytes, object]]:
        if "BODYSTRUCTURE" in items and 1 in messages:  # type: ignore[operator]
            raise RecursionError("maximum recursion depth exceeded")
        rows = super().fetch(messages, items)  # type: ignore[arg-type]
        if 5 in rows and b"INTERNALDATE" in rows[5]:
            rows[5][b"INTERNALDATE"] = None
        return rows


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


def test_unparsable_gmail_message_keeps_its_label_folder(monkeypatch: pytest.MonkeyPatch) -> None:
    server = gmail(all_mail=[gm(1, 5, "deep", SENT_L, flags=(b"\\Seen",)), gm(2, 6, "fine", INBOX_L)])
    monkeypatch.setattr(server, "client", lambda: GmailDegradingClient(server))
    page = run(server, query("all"))
    deep, fine = page.items
    assert (deep.snippet_text, deep.unread, deep.meta.folder, deep.meta.direction) == (
        UNDECODABLE_NOTE,
        False,
        "sent",
        "outgoing",
    )
    assert (fine.subject, fine.meta.folder) == ("fine", "inbox")
    assert ["FLAGS", "X-GM-LABELS"] in fetch_items(server)  # the labels of the unparsable UID, fetched alone


@pytest.mark.parametrize("with_all", [True, False])
def test_gmail_logs_carry_no_labels_or_names(with_all: bool, hub_log: Capture, monkeypatch: pytest.MonkeyPatch) -> None:
    """R9: during every folder value, no mailbox name, label, thread id, Message-ID, address or subject is logged."""
    subject = "Quarterly numbers"

    def labelled(uid: int, minutes: float, *labels: bytes) -> FakeMessage:
        return gm(
            uid, minutes, subject, *labels, b"Receipts", sender="owner@example.org", message_id="<q@example.test>"
        )

    server = gmail(
        inbox=[labelled(1, 5, INBOX_L), labelled(5, 6, INBOX_L)],
        all_mail=[labelled(1, 5, INBOX_L), labelled(5, 6, SENT_L), labelled(2, 7)],
        sent=[labelled(1, 5, SENT_L), labelled(5, 6, SENT_L)],
        trash=[labelled(1, 5, SENT_L), labelled(5, 6)],
        with_all=with_all,
    )
    monkeypatch.setattr(server, "client", lambda: GmailDegradingClient(server))
    mailbox = box(server, own=OWN)
    for folder_value in FOLDER_VALUES:
        for direction in (None, "outgoing"):
            mailbox.list_messages(query(folder_value, direction=direction), deadline(server))
    events = {json.loads(line)["event"] for line in hub_log.lines}
    assert events == {"folder_map_refreshed", "item_degraded"}  # the degraded paths did run
    text = "\n".join(hub_log.lines) + "\n" + "\n".join(message for _, _, message in hub_log.raw)
    for secret in (
        "[Gmail]",
        "Gmail]",
        "All Mail",
        "Sent Mail",
        "Trash",
        "INBOX",
        "Inbox",
        "Sent",
        "Receipts",
        str(THREAD),
        "q@example.test",
        "@example",
        "Quarterly",
    ):
        assert secret not in text
