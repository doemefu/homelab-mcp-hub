"""Mail history (WP13a) against GreenMail (spec 080 §10.2): folders, `folder: all` dedup, keyset paging, a `ref`
after moves, the read-only flag snapshot and log capture. The test seeds the folders through its own IMAP client
(APPEND, CREATE, COPY, MOVE, EXPUNGE); the hub only reads. Every subject and Message-ID carries the run token."""

import json
import logging
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path

import pytest

from mcp_hub.config import load_settings
from mcp_hub.errors import ToolError
from mcp_hub.health import StatusStore
from mcp_hub.ids import encode_ref
from mcp_hub.logging import configure_logging
from mcp_hub.providers import Adapters
from mcp_hub.providers.folders import NAME_FALLBACK
from mcp_hub.registry import load_registry
from mcp_hub.tools import HubContext
from mcp_hub.tools.mail import ListMessagesResult, MessageSummary, run_get_message, run_list_messages, run_list_unread
from tests.support import greenmail
from tests.support.logcapture import Capture
from tests.support.logfields import allowed_fields

pytestmark = [pytest.mark.provider, pytest.mark.anyio]
S = greenmail.subject
RUN = greenmail.RUN
# GreenMail lists no special-use attributes (O38, test_greenmail_capabilities_recorded): the roles come from names.
SENT, ARCHIVE, TRASH = NAME_FALLBACK["sent"], NAME_FALLBACK["archive"], NAME_FALLBACK["trash"]
PROJECTS = f"Projects-{RUN}"  # an `other` folder: listed by no folder value, still found by a `ref`
FOLDERS = ("INBOX", SENT, ARCHIVE, TRASH, PROJECTS)
LIST_FOLDERS = ("inbox", "sent", "archive", "trash", "all")
OWNER = f"owner@{greenmail.DOMAIN}"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(scope="module", autouse=True)
def services() -> None:
    greenmail.require()


@pytest.fixture(autouse=True)
def greenmail_literals(monkeypatch: pytest.MonkeyPatch) -> None:
    greenmail.tolerate_greenmail_partial_literals(monkeypatch)


def message_id(tag: str) -> str:
    """The Message-ID as the hub reports it (without angle brackets)."""
    return f"{RUN}-{tag}@{greenmail.DOMAIN}"


def mail(tag: str, *, outgoing: bool = False, references: tuple[str, ...] = ()) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = S(tag)
    msg["Message-ID"] = f"<{message_id(tag)}>"
    if outgoing:
        msg["From"] = f"Owner <{OWNER}>"
        msg["To"] = f"Peer <peer@{greenmail.DOMAIN}>"
    if references:
        msg["In-Reply-To"] = f"<{references[-1]}>"
        msg["References"] = " ".join(f"<{r}>" for r in references)
    msg.set_content(f"body of {tag}")
    return msg


def seed(
    login: str,
    folder: str,
    tag: str,
    *,
    flags: tuple[bytes, ...] = (),
    outgoing: bool = False,
    references: tuple[str, ...] = (),
) -> str:
    when = datetime.now(UTC) - timedelta(minutes=5)
    greenmail.append(login, mail(tag, outgoing=outgoing, references=references), when, folder=folder, flags=flags)
    return message_id(tag)


def hub(secrets_dir: Path, login: str, password: str | None = None) -> HubContext:
    return HubContext(
        load_settings({"HUB_SECRETS_DIR": str(secrets_dir), "LOG_LEVEL": "DEBUG"}),
        load_registry(secrets_dir / "accounts.json"),
        StatusStore(),
        Adapters(mailbox=lambda a, d: greenmail.mailbox(login, password)),
    )


def last_hour() -> str:
    return (datetime.now(UTC) - timedelta(hours=1)).isoformat()


async def listing(
    ctx: HubContext,
    folder: str,
    *,
    since: str | None = None,
    unread_only: bool | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> ListMessagesResult:
    result, _, _ = await run_list_messages(
        ctx,
        tool="list_messages",
        account="icloud",
        folder=folder,
        since=since or last_hour(),
        unread_only=unread_only,
        direction=None,
        limit=limit,
        cursor=cursor,
    )
    return result


def tagged(result: ListMessagesResult, prefix: str) -> dict[str, MessageSummary]:
    """Items of this run whose subject starts with `prefix`, by subject; fails on a subject listed twice."""
    out: dict[str, MessageSummary] = {}
    for item in result.items:
        if item.untrusted.subject.startswith(S(prefix)):
            assert item.untrusted.subject not in out, "listed twice"
            out[item.untrusted.subject] = item
    return out


def test_greenmail_capabilities_recorded() -> None:
    """O38 as observed on GreenMail 2.1.14: `MOVE` is announced and works, `SPECIAL-USE` is not, and `LIST` returns
    no special-use attributes, so the folder map takes its roles from the name fallback (`NAME_FALLBACK`) and the
    tests move with `MOVE`. `HEADER References` searches work. An image change that alters any of this fails here."""
    login = "hub-get"
    greenmail.ensure_folders(login, SENT, ARCHIVE, TRASH, PROJECTS)
    parent = message_id("cap-parent")
    seed(login, "INBOX", "cap-child", references=(parent,))
    with greenmail.connected(login) as client:
        capabilities = set(client.capabilities())
        listed = {str(name): set(attributes) for attributes, _, name in client.list_folders()}
        client.select_folder("INBOX", readonly=True)
        by_references = client.search(f'HEADER References "<{parent}>"'.encode())
    assert b"MOVE" in capabilities
    assert b"SPECIAL-USE" not in capabilities
    assert {name: listed[name] for name in FOLDERS} == {name: set() for name in FOLDERS}
    assert len(by_references) == 1
    assert greenmail.move(login, "INBOX", message_id("cap-child"), ARCHIVE) == "MOVE"


async def test_list_messages_each_folder(secrets_dir: Path) -> None:
    login = "hub-get"
    greenmail.ensure_folders(login, SENT, ARCHIVE, TRASH, PROJECTS)
    seeded = {
        "inbox": seed(login, "INBOX", "each-inbox"),
        "sent": seed(login, SENT, "each-sent", flags=(b"\\Seen",), outgoing=True),
        "archive": seed(login, ARCHIVE, "each-archive", flags=(b"\\Seen", b"\\Answered")),
        "trash": seed(login, TRASH, "each-trash", flags=(b"\\Seen",)),
    }
    seed(login, PROJECTS, "each-other")
    ctx = hub(secrets_dir, login)
    found = {folder: tagged(await listing(ctx, folder), "each-") for folder in LIST_FOLDERS}
    assert {folder: sorted(items) for folder, items in found.items()} == {
        "inbox": [S("each-inbox")],
        "sent": [S("each-sent")],
        "archive": [S("each-archive")],
        "trash": [S("each-trash")],
        "all": sorted([S("each-inbox"), S("each-sent"), S("each-archive")]),  # never trash, never `other`
    }
    for folder in ("inbox", "sent", "archive", "trash"):
        [item] = found[folder].values()
        assert item.folder == folder
        assert item.untrusted.message_id == seeded[folder]
        assert item.ref is not None
        assert item.id is not None
        assert item.direction == ("outgoing" if folder == "sent" else "incoming")
        if folder != "trash":
            assert found["all"][item.untrusted.subject] == item  # the same summary in `all`
    assert found["inbox"][S("each-inbox")].unread is True
    assert found["archive"][S("each-archive")].flags.model_dump() == {"seen": True, "answered": True, "flagged": False}


async def test_all_has_no_duplicate_for_inbox_and_archive_copy(secrets_dir: Path) -> None:
    login = "hub-get"
    greenmail.ensure_folders(login, SENT, ARCHIVE, TRASH, PROJECTS)
    seeded = seed(login, "INBOX", "dup-copied")
    greenmail.copy(login, "INBOX", seeded, ARCHIVE)
    ctx = hub(secrets_dir, login)
    in_archive = tagged(await listing(ctx, "archive"), "dup-")
    assert [i.folder for i in in_archive.values()] == ["archive"]  # the copy is there
    for page_size in (50, 1):  # one page, then one item per page: the dedup holds across pages too
        items: list[MessageSummary] = []
        cursor: str | None = None
        since = last_hour()
        for _ in range(500):
            page = await listing(ctx, "all", since=since, limit=page_size, cursor=cursor)
            items.extend(i for i in page.items if i.untrusted.subject.startswith(S("dup-")))
            cursor = page.next_cursor
            if cursor is None:
                break
        assert cursor is None, "paging does not end"
        assert not page.truncated
        assert [(i.untrusted.subject, i.folder) for i in items] == [(S("dup-copied"), "inbox")]  # the inbox copy
        assert items[0].ref == next(iter(in_archive.values())).ref  # one ref for both copies


async def test_read_only_snapshot_unchanged(secrets_dir: Path) -> None:
    """Spec §10.2 read-only: FLAGS and folder of every message are identical before and after each tool path of
    WP13a (list_messages for every folder value, list_unread, get_message by `id` and by `ref`)."""
    login = "hub-get"
    greenmail.ensure_folders(login, SENT, ARCHIVE, TRASH, PROJECTS)
    combos: tuple[tuple[bytes, ...], ...] = (
        (),
        (b"\\Seen",),
        (b"\\Answered",),
        (b"\\Flagged",),
        (b"\\Seen", b"\\Flagged"),
    )
    for index, folder in enumerate(FOLDERS):
        for n, flags in enumerate(combos):
            seed(login, folder, f"ro-{index}-{n}", flags=flags, outgoing=folder == SENT)
    before = greenmail.snapshot(login, FOLDERS)
    for folder in FOLDERS:  # the snapshot is meaningful: unread mail exists in every folder
        assert any(key[0] == folder and b"\\Seen" not in flags for key, (_, flags) in before.items())
    ctx = hub(secrets_dir, login)
    listed: dict[str, MessageSummary] = {}  # by id: each message is opened once by `id` and once by `ref`
    for folder in LIST_FOLDERS:
        for unread_only in (None, True):
            page = await listing(ctx, folder, unread_only=unread_only)
            listed.update((item.id or "", item) for item in tagged(page, "ro-").values())
    opened: set[str] = set()
    for item in listed.values():
        assert item.id is not None
        assert item.ref is not None
        by_id, _ = await run_get_message(ctx, message_id=item.id, max_chars=None)
        by_ref, _ = await run_get_message(ctx, ref=item.ref, max_chars=None)
        assert by_id.untrusted.body.strip() == by_ref.untrusted.body.strip() != ""
        opened.add(by_ref.untrusted.subject)
    unread, _, _ = await run_list_unread(ctx, account="icloud", since=last_hour(), limit=50, cursor=None)
    assert tagged(unread, "ro-")
    assert opened == {S(f"ro-{index}-{n}") for index in range(len(FOLDERS) - 1) for n in range(len(combos))}
    assert greenmail.snapshot(login, FOLDERS) == before


async def test_paging_120_messages_same_internaldate_each_once(secrets_dir: Path) -> None:
    """Spec §10.2: paging over 120 messages that share one INTERNALDATE second returns each exactly once."""
    login = "hub-list"
    when = (datetime.now(UTC) - timedelta(hours=2)).replace(microsecond=0)
    messages = [mail(f"page-{n:03d}") for n in range(120)]
    greenmail.append_many(login, messages, when, flags=(b"\\Seen",))  # read: the unread tests stay unaffected
    ctx = hub(secrets_dir, login)
    since = (when - timedelta(minutes=1)).isoformat()
    seen: list[str] = []
    cursor: str | None = None
    pages = 0
    while True:
        page = await listing(ctx, "inbox", since=since, limit=20, cursor=cursor)
        pages += 1
        seen.extend(i.untrusted.subject for i in page.items if i.untrusted.subject.startswith(S("page-")))
        cursor = page.next_cursor
        if cursor is None:
            break
        assert pages < 20, "paging does not end"
    assert not page.truncated
    assert pages >= 6
    assert sorted(seen) == sorted(S(f"page-{n:03d}") for n in range(120))  # each exactly once


async def test_get_message_ref_follows_moves(secrets_dir: Path) -> None:
    """Scenario "archived" (spec §10.2), as far as WP13a goes: a mail listed by list_unread is moved by the test; its
    `ref` still opens it in the new folder, and list_messages lists it there with the same `ref`. The status tool
    (`get_message_status`, `state: gone`) is WP13b; here an expunged mail is `not_found`."""
    login = "hub-get"
    greenmail.ensure_folders(login, SENT, ARCHIVE, TRASH, PROJECTS)
    seeded = seed(login, "INBOX", "moved")
    ctx = hub(secrets_dir, login)
    unread, _, _ = await run_list_unread(ctx, account="icloud", since=last_hour(), limit=50, cursor=None)
    [item] = tagged(unread, "moved").values()
    ref = item.ref
    assert ref is not None
    first, _ = await run_get_message(ctx, ref=ref, max_chars=None)
    assert (first.folder, first.id) == ("inbox", item.id)

    source = "INBOX"
    for target, folder in ((ARCHIVE, "archive"), (PROJECTS, "other"), (TRASH, "trash")):
        greenmail.move(login, source, seeded, target)
        source = target
        detail, _ = await run_get_message(ctx, ref=ref, max_chars=None)
        assert (detail.folder, detail.ref, detail.untrusted.message_id) == (folder, ref, seeded)
        assert detail.id is not None
        assert detail.id != item.id
        assert detail.unread is True
        if folder != "other":  # `other` is listed by no folder value
            listed = tagged(await listing(ctx, folder), "moved")
            assert [(i.ref, i.id, i.folder) for i in listed.values()] == [(ref, detail.id, folder)]
        assert not tagged(await listing(ctx, "inbox"), "moved")

    greenmail.expunge(login, TRASH, seeded)
    with pytest.raises(ToolError) as caught:
        await run_get_message(ctx, ref=ref, max_chars=None)
    assert caught.value.code == "not_found"


@pytest.fixture
def captured() -> Iterator[Capture]:
    configure_logging("DEBUG")
    handler = Capture()
    logging.getLogger().addHandler(handler)
    yield handler
    logging.getLogger().removeHandler(handler)


async def test_logs_contain_no_history_values(secrets_dir: Path, captured: Capture) -> None:
    """Spec §9.7: no Message-ID, References value, ref, id, thread key, cursor, folder name, subject or address in a
    log line, on the success paths and on the failing ones."""
    login = "hub-logs"
    greenmail.ensure_folders(login, SENT, ARCHIVE, TRASH, PROJECTS)
    parent = message_id("Hist5k-Parent")
    seeded = [
        seed(login, "INBOX", "Hist5k-Inbox", references=(parent,)),
        seed(login, SENT, "Hist5k-Sent", outgoing=True, references=(parent,)),
        seed(login, ARCHIVE, "Hist5k-Archive", flags=(b"\\Seen",)),
        seed(login, TRASH, "Hist5k-Trash"),
        seed(login, PROJECTS, "Hist5k-Other"),
    ]
    ctx = hub(secrets_dir, login)
    results: list[ListMessagesResult] = []
    for folder in LIST_FOLDERS:
        results.append(await listing(ctx, folder))
    since = last_hour()  # a cursor is bound to the arguments as given
    first = await listing(ctx, "all", since=since, limit=1)
    assert first.next_cursor is not None
    results += [first, await listing(ctx, "all", since=since, limit=1, cursor=first.next_cursor)]
    unread, _, _ = await run_list_unread(ctx, account="icloud", since=last_hour(), limit=50, cursor=None)
    results.append(unread)
    items = [i for result in results for i in tagged(result, "Hist5k-").values()]
    assert len({i.untrusted.subject for i in items}) == 4
    opened = []
    for item in items:
        assert item.id is not None
        assert item.ref is not None
        opened.append((await run_get_message(ctx, message_id=item.id, max_chars=None))[0])
        opened.append((await run_get_message(ctx, ref=item.ref, max_chars=None))[0])
    absent = encode_ref("icloud", message_id("Hist5k-Absent"))  # a complete search over every folder
    assert absent is not None
    with pytest.raises(ToolError) as caught:
        await run_get_message(ctx, ref=absent, max_chars=None)
    assert caught.value.code == "not_found"
    failing = hub(secrets_dir, login, password="not-the-password")
    await listing(failing, "all")
    with pytest.raises(ToolError):
        await run_get_message(failing, ref=items[0].ref, max_chars=None)

    text = "\n".join(captured.lines) + "\n".join(m for _, _, m in captured.raw)
    forbidden = {
        login,
        greenmail.passwords()[login],
        "not-the-password",
        absent,
        RUN,
        "Hist5k",
        parent,
        *seeded,
        *FOLDERS,
        "Sent",
        "Archive",
        "Deleted",
        "Projects",
        "@",
        "3993",
        "127.0.0.1",
        OWNER,
    }
    for result in results:
        if result.next_cursor:
            forbidden.add(result.next_cursor)
    for item in items:
        forbidden.update(v for v in (item.id, item.ref, item.thread_key) if v)
    for detail in opened:
        forbidden.update(v for v in (detail.id, detail.ref, detail.thread_key) if v)
    for value in sorted(forbidden):
        assert value not in text, value
    assert not [r for r in captured.raw if r[0].startswith("imapclient") and r[1] < logging.WARNING]
    events = [json.loads(line) for line in captured.lines]
    assert all(set(e) <= allowed_fields(e["event"]) for e in events)
    assert any(e["event"] == "provider_call_failed" and e["outcome"] == "auth_expired" for e in events)
