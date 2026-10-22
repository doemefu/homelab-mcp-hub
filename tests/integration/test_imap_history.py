"""Mail history (WP13a, WP13b) against GreenMail (spec 080 §10.2): folders, `folder: all` dedup, keyset paging, a
`ref` after moves, the `get_message_status` scenarios "hand-over" and "archived", the read-only flag snapshot and log
capture. The test seeds the folders through its own IMAP client (APPEND, CREATE, COPY, MOVE, EXPUNGE); the hub only
reads. Every subject and Message-ID carries the run token."""

import itertools
import json
import logging
from collections.abc import Callable, Iterator
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
from mcp_hub.providers.deadline import CallDeadline
from mcp_hub.providers.folders import NAME_FALLBACK
from mcp_hub.registry import load_registry
from mcp_hub.tools import HubContext, mail_status
from mcp_hub.tools.mail import ListMessagesResult, MessageSummary, run_get_message, run_list_messages, run_list_unread
from mcp_hub.tools.mail_status import MessageStatusResult, StatusItem, run_get_message_status
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
THIRD = f"Third <third@{greenmail.DOMAIN}>"


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


def mail(
    tag: str,
    *,
    outgoing: bool = False,
    references: tuple[str, ...] = (),
    in_reply_to: str | None = None,
    cc: tuple[str, ...] = (),
) -> EmailMessage:
    """`In-Reply-To` defaults to the last of `references`."""
    msg = EmailMessage()
    msg["Subject"] = S(tag)
    msg["Message-ID"] = f"<{message_id(tag)}>"
    if outgoing:
        msg["From"] = f"Owner <{OWNER}>"
        msg["To"] = f"Peer <peer@{greenmail.DOMAIN}>"
    if cc:
        msg["Cc"] = ", ".join(cc)
    parent = in_reply_to or (references[-1] if references else None)
    if parent:
        msg["In-Reply-To"] = f"<{parent}>"
    if references:
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
    in_reply_to: str | None = None,
    cc: tuple[str, ...] = (),
    when: datetime | None = None,
) -> str:
    """APPENDs the message with INTERNALDATE `when` (default: five minutes ago) and returns its Message-ID."""
    message = mail(tag, outgoing=outgoing, references=references, in_reply_to=in_reply_to, cc=cc)
    greenmail.append(login, message, when or datetime.now(UTC) - timedelta(minutes=5), folder=folder, flags=flags)
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


async def statuses(ctx: HubContext, *entries: str) -> list[StatusItem]:
    """get_message_status for an account that answers: every entry answered, nothing cut, no account error."""
    result, _, _ = await run_get_message_status(ctx, refs=list(entries))
    assert (result.truncated, result.next_cursor, result.account_errors) == (False, None, [])
    assert len(result.items) == len(entries)
    return result.items


def ref_of(tag: str) -> str:
    ref = encode_ref("icloud", message_id(tag))
    assert ref is not None
    return ref


def instant(value: str | None) -> datetime | None:
    """A reported local timestamp as an aware instant (the hub reports HUB_DEFAULT_TIMEZONE offsets)."""
    return None if value is None else datetime.fromisoformat(value)


def stepping_deadline(monkeypatch: pytest.MonkeyPatch, soft_ticks: int) -> None:
    """Every read of the call clock advances one tick, so the soft stop lands after a fixed number of deadline checks
    (`soft_ticks`), at a different step of the call for each value; the hard deadline stays out of reach."""
    ticks = itertools.count()

    def clock() -> float:
        return float(next(ticks))

    def start() -> CallDeadline:
        return CallDeadline(clock(), float(soft_ticks), 1e9, clock)

    monkeypatch.setattr(mail_status, "CallDeadline", type("SteppingDeadline", (), {"start": staticmethod(start)}))


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


async def test_read_only_snapshot_unchanged(secrets_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Spec §10.2 read-only: FLAGS and folder of every message are identical before and after each tool path of
    WP13a and WP13b (list_messages for every folder value, list_unread, get_message by `id` and by `ref`,
    get_message_status over every `ref` and `id`, also when the call runs into the soft stop at any step)."""
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
    entries = [value for item in listed.values() for value in (item.ref or "", item.id or "")]
    checked = await statuses(ctx, *entries)
    assert [(item.state, item.thread_checked) for item in checked] == [("found", True)] * len(entries)
    assert [item.folder for item in checked] == [item.folder for item in listed.values() for _ in range(2)]
    partial = []
    for soft_ticks in (2, 8, 20, 40, 80, 160, 320):  # the soft stop at login, in place, during location and lookups
        stepping_deadline(monkeypatch, soft_ticks)
        result, _, _ = await run_get_message_status(ctx, refs=entries)
        assert (result.truncated, result.account_errors) == (False, [])
        states = [(item.state, item.reason, item.thread_checked) for item in result.items]
        assert all(
            state in {("found", None, True), ("found", None, False), ("unknown", "incomplete_search", None)}
            for state in states
        )
        partial.append(states)
    assert any(all(state[0] == "unknown" for state in states) for states in partial)  # stopped before any read
    assert any(
        any(state[0] == "found" for state in states) and any(state != ("found", None, True) for state in states)
        for states in partial
    )  # stopped in the middle: what was read is kept
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


def seed_hand_over(
    login: str, prefix: str, follow_up_references: Callable[[str, str], tuple[str, ...]]
) -> tuple[datetime, tuple[str, str, str]]:
    """The request R from the sender at t0, the owner's reply A in sent at t0 + 1 min (without `\\Answered` on R),
    and the sender's hand-over B at t0 + 2 min, copying a third person; B's `References` from
    `follow_up_references(R, A)`, its `In-Reply-To` naming A. Returns t0 and the refs of R, A and B."""
    t0 = (datetime.now(UTC) - timedelta(minutes=30)).replace(microsecond=0)
    request = seed(login, "INBOX", f"{prefix}-request", when=t0)
    reply = seed(
        login,
        SENT,
        f"{prefix}-reply",
        flags=(b"\\Seen",),
        outgoing=True,
        references=(request,),
        when=t0 + timedelta(minutes=1),
    )
    seed(
        login,
        "INBOX",
        f"{prefix}-handover",
        references=follow_up_references(request, reply),
        in_reply_to=reply,
        cc=(THIRD,),
        when=t0 + timedelta(minutes=2),
    )
    return t0, (ref_of(f"{prefix}-request"), ref_of(f"{prefix}-reply"), ref_of(f"{prefix}-handover"))


def assert_hand_over(items: list[StatusItem], t0: datetime, refs: tuple[str, str, str]) -> None:
    """R: answered by A (found in sent, the flag unset) and continued by B. A (the waiting-for side, MF-R5): an
    outgoing ref, not replied, continued by B. B: neither answered nor continued."""
    request, reply, follow_up = items
    assert [item.ref for item in items] == list(refs)
    assert all((item.state, item.reason, item.thread_checked) == ("found", None, True) for item in items)
    assert (request.folder, request.direction, instant(request.received_at)) == ("inbox", "incoming", t0)
    assert request.flags is not None
    assert request.flags.answered is False
    assert (request.replied, instant(request.replied_at), request.reply_ref) == (
        True,
        t0 + timedelta(minutes=1),
        refs[1],
    )
    assert (request.newer_incoming_in_thread, instant(request.newer_incoming_at)) == (True, t0 + timedelta(minutes=2))
    assert (reply.folder, reply.direction, instant(reply.received_at)) == (
        "sent",
        "outgoing",
        t0 + timedelta(minutes=1),
    )
    assert (reply.replied, reply.replied_at, reply.reply_ref) == (False, None, None)
    assert (reply.newer_incoming_in_thread, instant(reply.newer_incoming_at)) == (True, t0 + timedelta(minutes=2))
    assert (follow_up.folder, follow_up.direction) == ("inbox", "incoming")
    assert (follow_up.replied, follow_up.newer_incoming_in_thread, follow_up.newer_incoming_at) == (False, False, None)


async def test_status_hand_over(secrets_dir: Path) -> None:
    """Scenario "hand-over" (spec 080 §10.2): the request is answered (`replied_at`, `reply_ref` = the owner's reply)
    and continued by the hand-over; B names R and A in `References`."""
    login = "hub-get"
    greenmail.ensure_folders(login, SENT, ARCHIVE, TRASH, PROJECTS)
    t0, refs = seed_hand_over(login, "ho", lambda request, reply: (request, reply))
    assert_hand_over(await statuses(hub(secrets_dir, login), *refs), t0, refs)


async def test_status_hand_over_outlook_style(secrets_dir: Path) -> None:
    """E-mf1: an Outlook-style hand-over names only the owner's reply A in `References`, never the request R; R is
    still continued, found through A as an extra thread key, and A reports the same values (MF-R5)."""
    login = "hub-get"
    greenmail.ensure_folders(login, SENT, ARCHIVE, TRASH, PROJECTS)
    t0, refs = seed_hand_over(login, "hoo", lambda _request, reply: (reply,))
    assert_hand_over(await statuses(hub(secrets_dir, login), *refs), t0, refs)


async def test_status_archived(secrets_dir: Path) -> None:
    """Scenario "archived" (spec 080 §10.2): a mail listed by list_unread is moved by the test; its `ref` reports
    the new folder each time with the same `ref`; its old `id` is `stale_id` (an IMAP id cannot follow a move); once
    expunged it is `gone`, like a ref that was never seen."""
    login = "hub-get"
    greenmail.ensure_folders(login, SENT, ARCHIVE, TRASH, PROJECTS)
    seeded = seed(login, "INBOX", "st-archived")
    ctx = hub(secrets_dir, login)
    unread, _, _ = await run_list_unread(ctx, account="icloud", since=last_hour(), limit=50, cursor=None)
    [listed] = tagged(unread, "st-archived").values()
    assert listed.ref is not None
    assert listed.id is not None
    ref, old_id = listed.ref, listed.id
    by_ref, by_id = await statuses(ctx, ref, old_id)
    assert (by_ref.state, by_ref.folder, by_ref.ref, by_ref.id, by_ref.input) == ("found", "inbox", ref, old_id, None)
    assert (by_id.state, by_id.folder, by_id.ref, by_id.id, by_id.input) == ("found", "inbox", ref, old_id, old_id)
    assert by_ref.flags is not None
    assert by_ref.flags.seen is False  # list_unread and the status calls did not read it

    source = "INBOX"
    for target, folder in ((ARCHIVE, "archive"), (TRASH, "trash")):
        greenmail.move(login, source, seeded, target)
        source = target
        moved, stale = await statuses(ctx, ref, old_id)
        assert (moved.state, moved.reason, moved.folder, moved.ref, moved.input) == ("found", None, folder, ref, None)
        assert moved.id is not None
        assert moved.id != old_id
        assert (moved.direction, moved.thread_checked) == ("incoming", True)
        assert (stale.state, stale.reason, stale.ref, stale.input, stale.folder) == (
            "unknown",
            "stale_id",
            None,
            old_id,
            None,
        )

    greenmail.expunge(login, TRASH, seeded)
    expunged, never_seen = await statuses(ctx, ref, ref_of("st-never-seen"))
    for item in (expunged, never_seen):
        assert (item.state, item.reason, item.id, item.folder, item.thread_checked) == ("gone", None, None, None, None)
        assert (item.replied, item.newer_incoming_in_thread) == (False, False)
    assert (expunged.ref, never_seen.ref) == (ref, ref_of("st-never-seen"))


async def test_status_other_folder(secrets_dir: Path) -> None:
    """A mail in a folder that no folder value lists (`Projects-<RUN>`) is found by its `ref` as `other`."""
    login = "hub-get"
    greenmail.ensure_folders(login, SENT, ARCHIVE, TRASH, PROJECTS)
    seed(login, PROJECTS, "st-other")
    [item] = await statuses(hub(secrets_dir, login), ref_of("st-other"))
    assert (item.state, item.folder, item.direction, item.thread_checked) == ("found", "other", "incoming", True)
    assert item.id is not None


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
    inbox = seed(login, "INBOX", "Hist5k-Inbox", references=(parent,))
    seeded = [
        inbox,
        seed(login, SENT, "Hist5k-Sent", outgoing=True, references=(parent, inbox)),  # a reply: reply_ref, newer
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
    entries = sorted({value for item in items for value in (item.ref, item.id) if value})
    checked = await statuses(ctx, *entries, absent)
    assert {item.state for item in checked[:-1]} == {"found"}
    assert checked[-1].state == "gone"
    assert any(item.reply_ref for item in checked)
    failing = hub(secrets_dir, login, password="not-the-password")
    await listing(failing, "all")
    with pytest.raises(ToolError):
        await run_get_message(failing, ref=items[0].ref, max_chars=None)
    failed: MessageStatusResult = (await run_get_message_status(failing, refs=entries))[0]
    assert {(item.state, item.reason) for item in failed.items} == {("unknown", "account_error")}
    assert [error.account for error in failed.account_errors] == ["icloud"]

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
    for status in [*checked, *failed.items]:
        forbidden.update(v for v in (status.input, status.ref, status.id, status.reply_ref) if v)
    for value in sorted(forbidden):
        assert value not in text, value
    assert not [r for r in captured.raw if r[0].startswith("imapclient") and r[1] < logging.WARNING]
    events = [json.loads(line) for line in captured.lines]
    assert all(set(e) <= allowed_fields(e["event"]) for e in events)
    assert any(e["event"] == "provider_call_failed" and e["outcome"] == "auth_expired" for e in events)
    assert not [
        e for e in events if e["event"] not in {"provider_call_failed", "item_degraded", "folder_map_refreshed"}
    ]
