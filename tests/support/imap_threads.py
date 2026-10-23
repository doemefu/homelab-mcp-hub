"""Builders for the IMAP `get_thread` tests (WP13c T7 to T10): iCloud- and Gmail-like fake servers, messages with
thread headers, the call helpers and the command-log readers. Neutral data only (example.org / example.test)."""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from mcp_hub.ids import MessageRef, StableRef, ThreadKey
from mcp_hub.providers.base import ThreadPage, ThreadQuery
from mcp_hub.providers.deadline import CallDeadline
from mcp_hub.providers.folders import FolderMapCache
from mcp_hub.providers.imap import ImapMailbox
from tests.support.clock import FakeClock
from tests.support.imap_fake import PLAIN, FakeFolder, FakeImapServer, FakeMessage

NOW = datetime(2026, 10, 21, 7, 0, tzinfo=UTC)
OWN = frozenset({"owner@example.org"})
THREAD = 1781234567890123456
INBOX_L, SENT_L, DRAFT_L = b"\\Inbox", b"\\Sent", b"\\Draft"
GMAIL_ALL, GMAIL_SENT, GMAIL_TRASH = "[Gmail]/All Mail", "[Gmail]/Sent Mail", "[Gmail]/Trash"
SENDER = "Sender <sender@example.test>"


def structure(
    mime: tuple[bytes, bytes] = (b"text", b"plain"), size: int = 64, encoding: bytes = b"7bit"
) -> tuple[object, ...]:
    """A single-part BODYSTRUCTURE of `mime` with the given size field (0: no size)."""
    return (*mime, (b"charset", b"utf-8"), None, None, encoding, size, 2, None, None, None, None)


def with_attachment(text: tuple[object, ...] = PLAIN) -> tuple[object, ...]:
    """multipart/mixed: the text part (section 1) and a PDF attachment (section 2)."""
    pdf = (b"application", b"pdf", (b"name", b"a.pdf"), None, None, b"base64", 4000, None, (b"attachment", None), None)
    return ([text, pdf], b"mixed", None, None, None, None)


def hdr(
    subject: str,
    *,
    sender: str = SENDER,
    to: str = "Owner <owner@example.org>",
    cc: str | None = None,
    message_id: str | None = None,
    in_reply_to: str | None = None,
    references: str | None = None,
) -> bytes:
    lines = [f"From: {sender}", f"To: {to}", f"Subject: {subject}"]
    for name, value in (
        ("Cc", cc),
        ("Message-ID", message_id),
        ("In-Reply-To", in_reply_to),
        ("References", references),
    ):
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
    bodystructure: object = PLAIN,
    headers: bytes | None = None,
    **h: str,
) -> FakeMessage:
    received = None if minutes_ago is None else NOW - timedelta(minutes=minutes_ago)
    return FakeMessage(
        uid,
        received,
        flags=flags,
        headers=headers if headers is not None else hdr(subject, **h),
        text=text,
        bodystructure=bodystructure,
        labels=labels,
        thrid=thrid,
    )


def folder(
    name: str, attributes: tuple[bytes, ...] = (), messages: Sequence[FakeMessage] = (), *, uidvalidity: int | None = 7
) -> FakeFolder:
    return FakeFolder(
        name, (b"\\HasNoChildren", *attributes), uidvalidity=uidvalidity, messages={m.uid: m for m in messages}
    )


def icloud(
    *,
    inbox: Sequence[FakeMessage] = (),
    archive: Sequence[FakeMessage] = (),
    sent: Sequence[FakeMessage] = (),
    trash: Sequence[FakeMessage] = (),
    other: Sequence[FakeFolder] = (),
    validity: dict[str, int | None] | None = None,
) -> FakeImapServer:
    """iCloud-like LIST: special-use attributes, junk and drafts mailboxes, then the `other` mailboxes."""
    v = validity or {}
    folders = [
        folder("INBOX", (), inbox, uidvalidity=v.get("INBOX", 7)),
        folder("Sent Messages", (b"\\Sent",), sent, uidvalidity=v.get("Sent Messages", 7)),
        folder("Archive", (b"\\Archive",), archive, uidvalidity=v.get("Archive", 7)),
        folder("Deleted Messages", (b"\\Trash",), trash, uidvalidity=v.get("Deleted Messages", 7)),
        folder("Junk", (b"\\Junk",)),
        folder("Drafts", (b"\\Drafts",)),
        *other,
    ]
    return FakeImapServer(folders, clock=FakeClock())


def gmail(
    *,
    inbox: Sequence[FakeMessage] = (),
    all_mail: Sequence[FakeMessage] = (),
    sent: Sequence[FakeMessage] = (),
    trash: Sequence[FakeMessage] = (),
    with_all: bool = True,
    validity: dict[str, int | None] | None = None,
) -> FakeImapServer:
    """Gmail-like LIST: the label and excluded mailboxes each hold a planted message of the same thread."""
    v = validity or {}
    planted = [msg(90, 1, "planted", thrid=THREAD, message_id="<planted@example.test>")]
    folders = [
        folder("INBOX", (), inbox, uidvalidity=v.get("INBOX", 7)),
        folder("Receipts", (), planted),
        FakeFolder("[Gmail]", (b"\\HasChildren", b"\\Noselect")),
        *([folder(GMAIL_ALL, (b"\\All",), all_mail, uidvalidity=v.get(GMAIL_ALL, 11))] if with_all else []),
        folder("[Gmail]/Drafts", (b"\\Drafts",), planted),
        folder("[Gmail]/Important", (b"\\Important",), planted),
        folder(GMAIL_SENT, (b"\\Sent",), sent, uidvalidity=v.get(GMAIL_SENT, 13)),
        folder("[Gmail]/Spam", (b"\\Junk",), planted),
        folder("[Gmail]/Starred", (b"\\Flagged",), planted),
        folder(GMAIL_TRASH, (b"\\Trash",), trash, uidvalidity=v.get(GMAIL_TRASH, 17)),
    ]
    return FakeImapServer(folders, capabilities=(b"IMAP4REV1", b"X-GM-EXT-1"), clock=FakeClock())


def clock_of(server: FakeImapServer) -> FakeClock:
    assert server.clock is not None
    return server.clock


def account_of(server: FakeImapServer) -> str:
    return "gmail" if b"X-GM-EXT-1" in server.capabilities else "icloud"


def mailbox(server: FakeImapServer, *, own: frozenset[str] = OWN) -> ImapMailbox:
    return ImapMailbox(
        account_of(server),
        host="imap.example.test",
        port=993,
        folder="INBOX",
        username="u",
        password="p",
        client_factory=lambda host, port, context, timeout: server.client(),
        own_addresses=own,
        folder_maps=FolderMapCache(clock=clock_of(server)),
    )


def deadline(server: FakeImapServer, *, soft: float = 16.0, hard: float = 20.0, spent: float = 0.0) -> CallDeadline:
    """A deadline on the server's clock; `spent` seconds have already passed when the call starts."""
    clock = clock_of(server)
    return CallDeadline(clock() - spent, soft, hard, clock)


def ref(message_id: str, server: FakeImapServer) -> StableRef:
    return StableRef(account_of(server), message_id)


def mid_ref(server: FakeImapServer, raw_name: str, uid: int, uidvalidity: int) -> MessageRef:
    return MessageRef(account_of(server), raw_name, uidvalidity, uid)


def gmail_key(value: int = THREAD) -> ThreadKey:
    return ThreadKey("gmail", "gmail", value)


def thread(
    server: FakeImapServer,
    *,
    seed: StableRef | MessageRef | None = None,
    key: ThreadKey | None = None,
    include_trash: bool = True,
    max_chars: int = 2000,
    call: CallDeadline | None = None,
    box: ImapMailbox | None = None,
) -> ThreadPage:
    query = ThreadQuery(seed=seed, key=key, include_trash=include_trash, max_chars=max_chars)
    return (box or mailbox(server)).get_thread(query, call or deadline(server))


def stop_after(server: FakeImapServer, count: int, *, seconds: float = 17.0) -> None:
    """The clock jumps by `seconds` once `count` commands have run (by default past the soft limit, not the hard one):
    the next check (a command or a CPU check) stops."""
    original = server.run

    def run(command: str, argument: object = None) -> None:
        original(command, argument)
        if len(server.commands) == count:
            clock_of(server).advance(seconds)

    server.run = run  # type: ignore[method-assign]


def names(server: FakeImapServer) -> list[str]:
    return [name for name, _ in server.commands]


def examined(server: FakeImapServer) -> list[object]:
    return [argument for name, argument in server.commands if name == "EXAMINE"]


def searches(server: FakeImapServer) -> list[bytes]:
    return [argument for name, argument in server.commands if name == "UID SEARCH"]  # type: ignore[misc]


def fetches(server: FakeImapServer) -> list[tuple[list[int], list[str]]]:
    out: list[tuple[list[int], list[str]]] = []
    for name, argument in server.commands:
        if name == "UID FETCH":
            uids, items = argument  # type: ignore[misc]
            out.append((sorted(uids), list(items)))
    return out


def index_of(server: FakeImapServer, name: str, predicate: object = None) -> int:
    """Index in the command log of the first `name` command whose argument satisfies `predicate` (a callable)."""
    for index, (command, argument) in enumerate(server.commands):
        if command == name and (predicate is None or predicate(argument)):  # type: ignore[operator]
            return index
    raise AssertionError(f"no {name} command")
