"""get_message_status (spec 080 §5.2, §5.4 A-td6): entry validation, per-position mapping, pre-checks without a
provider call, the null rules (E-td1), the tool's own output budget and the wrapper (WP13b T14), and the real Graph
adapter behind the tool (M3)."""

import base64
import json
import logging
import re
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx2
import pytest
from pydantic import ValidationError
from starlette.testclient import TestClient

from mcp_hub.app import create_app
from mcp_hub.config import Settings, load_settings
from mcp_hub.errors import ToolError
from mcp_hub.health import StatusStore
from mcp_hub.ids import (
    AnyMessageRef,
    GraphMessageRef,
    MessageRef,
    StableRef,
    ThreadKey,
    encode_event_id,
    encode_message_id,
    encode_ref,
    encode_thread_key,
)
from mcp_hub.logging import JsonFormatter, configure_logging
from mcp_hub.providers import Adapters
from mcp_hub.providers.base import (
    AccountLimiters,
    AccountSlot,
    FolderPage,
    FoundMessage,
    ListQuery,
    MailDetail,
    MailFlags,
    MailMeta,
    MessageStatus,
    ProviderError,
    StatusReason,
    StatusTarget,
    status_unknown,
)
from mcp_hub.providers.deadline import CallDeadline
from mcp_hub.providers.folders import FOLDER_MAPS, FolderMapCache, MailFolder, build_imap_folder_map
from mcp_hub.providers.graph import Backoff, GraphMailbox
from mcp_hub.registry import Account, load_registry
from mcp_hub.tools import HubContext, common, mail_status
from mcp_hub.tools.common import UNTRUSTED_CONTENT_NOTICE, structured_result
from mcp_hub.tools.mail import Flags
from mcp_hub.tools.mail_status import (
    GET_MESSAGE_STATUS_DESCRIPTION,
    MAX_STATUS_ENTRIES,
    STATUS_BUDGET_FLOOR,
    MessageStatusResult,
    StatusItem,
    found_item,
    gone_item,
    run_get_message_status,
    unknown_item,
)
from tests.support.graph_fixtures import FOLDER_IDS, FakeTokens, message
from tests.support.logfields import allowed_fields
from tests.support.mcp import body, modern
from tests.support.tokens import TokenFactory

pytestmark = pytest.mark.anyio
NOW = datetime(2026, 10, 20, 5, 12, 3, tzinfo=UTC)
MID = "m1@example.test"
REPLY_MID = "reply1@example.test"
ZONE_OFFSET = "+02:00"  # Europe/Zurich in October (summer time)
REFS_MESSAGE = "refs must be a list of 1 to 50 strings"
WORST_ENTRY_BYTES = 381  # JSON bytes whose base64url text, after "v1.", is the longest id that fits 512 characters
GRAPH_LIKE = "AAMkAGI2" + "x" * 262  # an immutable Graph id of about 270 characters
GMAIL_LIKE = "CAH" + "q" * 52 + "@mail.example.com"  # a Gmail-length Message-ID of about 70 characters
ICLOUD_LIST = [
    ((b"\\HasNoChildren",), b"/", b"INBOX"),
    ((b"\\HasNoChildren", b"\\Sent"), b"/", b"Sent Messages"),
    ((b"\\HasNoChildren", b"\\Archive"), b"/", b"Archive"),
    ((b"\\HasNoChildren", b"\\Trash"), b"/", b"Deleted Messages"),
    ((b"\\HasNoChildren", b"\\Junk"), b"/", b"Junk"),
    ((b"\\HasNoChildren",), b"/", b"Projects"),
]
EXPECTED_DESCRIPTION = (
    "Re-check up to 50 mails seen earlier (for example the mail a to-do came from): still there, in which folder, "
    "answered, conversation continued. `refs`: stored `ref` values (preferred: they follow moves) or `id` values (an "
    "IMAP id cannot follow a move). items[i] answers refs[i]. `state`: found = `folder` says where it is now; gone = "
    "every readable folder was searched and it is not there (deleted, junk or draft); unknown = could not tell, never "
    "treat as gone; `reason`: account_error (retry later), incomplete_search (re-ask in a smaller call), "
    "account_unavailable, id_mismatch, stale_id (the id is outdated: pass the mail's stored `ref` instead). "
    "`replied` = the owner answered (`replied_at` null when only the answered flag shows it). "
    "`newer_incoming_in_thread` = someone else wrote later in the conversation (a follow-up or hand-over): compare "
    "`newer_incoming_at` with `replied_at` before treating it as done. `thread_checked: false` = `replied` and "
    "`newer_incoming_in_thread` may be false negatives: re-ask only those refs in a smaller call. `truncated: true` = "
    "only the first len(items) refs are answered: call again with the rest. Read-only."
)
NULL_FIELDS = {
    "id": None,
    "folder": None,
    "received_at": None,
    "direction": None,
    "flags": None,
    "replied": False,
    "replied_at": None,
    "reply_ref": None,
    "newer_incoming_in_thread": False,
    "newer_incoming_at": None,
    "thread_checked": None,
}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


GONE = MessageStatus("gone")


@dataclass
class Fake:
    """Adapter stand-in: one answer per target, in order, like the real adapters (T8/T12)."""

    answers: dict[StatusTarget, MessageStatus] = field(default_factory=dict)
    errors: dict[str, Exception] = field(default_factory=dict)
    default: MessageStatus = GONE
    opened: list[str] = field(default_factory=list)
    calls: list[tuple[str, list[StatusTarget], CallDeadline]] = field(default_factory=list)

    def factory(self, account: Account, _secrets_dir: Path) -> "FakeMailbox":
        self.opened.append(account.id)
        return FakeMailbox(self, account.id)


class FakeMailbox:
    def __init__(self, fake: Fake, account: str) -> None:
        self.fake, self.account = fake, account

    def check(self, deadline: CallDeadline) -> None: ...

    def get_message(self, target: object, deadline: CallDeadline) -> MailDetail:
        raise AssertionError("not used")

    def list_messages(self, query: ListQuery, deadline: CallDeadline) -> FolderPage:
        raise AssertionError("not used")

    def message_status(self, targets: Sequence[StatusTarget], deadline: CallDeadline) -> list[MessageStatus]:
        self.fake.calls.append((self.account, list(targets), deadline))
        if self.account in self.fake.errors:
            raise self.fake.errors[self.account]
        return [self.fake.answers.get(target, self.fake.default) for target in targets]


class RecordingLimiters(AccountLimiters):
    def __init__(self) -> None:
        super().__init__()
        self.asked: list[str] = []

    def get(self, account_id: str) -> AccountSlot:
        self.asked.append(account_id)
        return super().get(account_id)


def context(
    secrets_dir: Path, fake: Fake, env: dict[str, str] | None = None, limiters: AccountLimiters | None = None
) -> HubContext:
    """icloud and gmail (IMAP) and outlook (Graph) are ready; uzh is disabled."""
    for name in ("gmail-username", "gmail-app-password", "outlook-ms-client-id"):
        (secrets_dir / name).write_text("placeholder")
    for name in ("db-username", "db-password", "token-encryption-key"):
        (secrets_dir / name).write_text("placeholder")
    settings = load_settings({"HUB_SECRETS_DIR": str(secrets_dir)} | (env or {}))
    adapters = Adapters(mailbox=fake.factory, limiters=limiters or AccountLimiters())
    return HubContext(settings, load_registry(secrets_dir / "accounts.json"), StatusStore(), adapters)


def ref(account: str, message_id: str = MID) -> str:
    value = encode_ref(account, message_id)
    assert value is not None
    return value


def found(
    location: AnyMessageRef,
    message_id: str | None = MID,
    *,
    folder: MailFolder = "inbox",
    unread: bool = False,
    answered: bool = False,
    flagged: bool = False,
    outgoing: bool = False,
    reply_at: datetime | None = None,
    reply_message_id: str | None = None,
    newer_incoming_at: datetime | None = None,
    thread_checked: bool = True,
) -> MessageStatus:
    meta = MailMeta(
        folder=folder,
        flags=MailFlags(answered=answered, flagged=flagged),
        direction="outgoing" if outgoing else "incoming",
    )
    return MessageStatus(
        "found",
        found=FoundMessage(location=location, message_id=message_id, received_at=NOW, unread=unread, meta=meta),
        reply_at=reply_at,
        reply_message_id=reply_message_id,
        newer_incoming_at=newer_incoming_at,
        thread_checked=thread_checked,
    )


def non_canonical(account: str, message_id: str, spaces: int = 1) -> str:
    """A valid `r` entry whose JSON differs from the hub's compact form (spaces after the bracket): it decodes, but
    its canonical `ref` is another string."""
    raw = ("[" + " " * spaces + json.dumps(["r", account, message_id], separators=(",", ":"))[1:]).encode()
    return "v1." + base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


async def run(ctx: HubContext, refs: object) -> tuple[MessageStatusResult, list[str], str]:
    return await run_get_message_status(ctx, refs=refs)


async def refused(ctx: HubContext, refs: object) -> ToolError:
    with pytest.raises(ToolError) as caught:
        await run(ctx, refs)
    return caught.value


def payload(item: StatusItem) -> dict[str, Any]:
    return json.loads(item.model_dump_json())  # type: ignore[no-any-return]  # JSON object of one item


# --- input --------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "refs",
    [None, [], [ref("icloud")] * 51, ref("icloud"), {"refs": [ref("icloud")]}, 7, (ref("icloud"),)],
    ids=["none", "empty", "fifty-one", "bare-string", "object", "number", "tuple"],
)
async def test_refs_count_and_shape(secrets_dir: Path, refs: object) -> None:
    fake = Fake()
    error = await refused(context(secrets_dir, fake), refs)
    assert (error.code, error.message) == ("invalid_argument", REFS_MESSAGE)
    assert fake.opened == []


@pytest.mark.parametrize(
    "bad",
    [
        5,
        None,
        ["nested"],
        "v1.garbage!",
        "not-an-id",
        encode_thread_key(ThreadKey("icloud", "headers", MID)),
        encode_event_id("icloud", "/calendars/home/", "uid-1", ""),
        "v1." + base64.urlsafe_b64encode(b'["r","icloud","has space@example.test"]').rstrip(b"=").decode(),
    ],
    ids=["int", "null", "list", "garbage", "plain", "thread-key", "event-id", "bad-message-id"],
)
async def test_refs_entry_shape_names_the_index_never_the_value(secrets_dir: Path, bad: object) -> None:
    fake = Fake()
    error = await refused(context(secrets_dir, fake), [ref("icloud"), ref("gmail"), bad])
    assert (error.code, error.message) == ("invalid_argument", "refs[2] is not a valid ref or message id")
    assert fake.opened == []  # one bad entry fails the whole call before any provider call
    if isinstance(bad, str):
        assert bad not in error.message


async def test_fifty_entries_are_accepted(secrets_dir: Path) -> None:
    fake = Fake()
    result, _, _ = await run(context(secrets_dir, fake), [ref("icloud")] * MAX_STATUS_ENTRIES)
    assert len(result.items) == MAX_STATUS_ENTRIES
    assert MAX_STATUS_ENTRIES == 50


# --- mapping ------------------------------------------------------------------------------------------------------


async def test_items_in_input_order_with_duplicates_each_answered(secrets_dir: Path) -> None:
    a, b, c = (
        StableRef("icloud", "a@example.test"),
        StableRef("gmail", "b@example.test"),
        MessageRef("icloud", "INBOX", 7, 3),
    )
    fake = Fake(answers={a: found(MessageRef("icloud", "INBOX", 7, 1), a.message_id), c: status_unknown("stale_id")})
    entries = [
        ref("icloud", "a@example.test"),
        ref("gmail", "b@example.test"),
        ref("icloud", "a@example.test"),
        encode_message_id(c),
        non_canonical("icloud", "a@example.test"),  # the same target as entry 0, written differently
        ref("icloud", "a@example.test"),
    ]
    result, accounts, outcome = await run(context(secrets_dir, fake), entries)
    assert [(i.account, i.state, i.reason) for i in result.items] == [
        ("icloud", "found", None),
        ("gmail", "gone", None),
        ("icloud", "found", None),
        ("icloud", "unknown", "stale_id"),
        ("icloud", "found", None),
        ("icloud", "found", None),
    ]
    assert [i.ref for i in result.items] == [
        entries[0], entries[1], entries[0], None, entries[0], entries[0],
    ]  # fmt: skip
    assert result.items[0] == result.items[2] == result.items[5]
    assert result.items[4].input == entries[4]
    assert sorted((account, targets) for account, targets, _ in fake.calls) == [
        ("gmail", [b]),
        ("icloud", [a, c]),  # distinct targets, first-seen order
    ]
    assert (accounts, outcome) == (["icloud", "gmail"], "ok")


def _add_mailless_account(secrets_dir: Path) -> None:
    path = secrets_dir / "accounts.json"
    registry = json.loads(path.read_text())
    calendar = next(a for a in registry["accounts"] if a["id"] == "icloud")["calendar"]
    registry["accounts"].append(
        {
            "id": "calonly",
            "label": "Calendar only",
            "provider": "icloud",
            "enabled": True,
            "capabilities": {"mail": False, "calendar": True},
            "calendar": calendar,
        }
    )
    path.write_text(json.dumps(registry))


@pytest.mark.parametrize("cause", ["unknown", "disabled", "mail-less", "adapter-less", "no-credentials"])
async def test_account_unavailable_without_provider_call(
    secrets_dir: Path, monkeypatch: pytest.MonkeyPatch, cause: str
) -> None:
    account = {
        "unknown": "nobody",
        "disabled": "uzh",
        "mail-less": "calonly",
        "adapter-less": "outlook",
        "no-credentials": "gmail",
    }[cause]
    if cause == "mail-less":
        _add_mailless_account(secrets_dir)
    fake = Fake(answers={StableRef("icloud", MID): found(MessageRef("icloud", "INBOX", 7, 1))})
    limiters = RecordingLimiters()
    ctx = context(secrets_dir, fake, limiters=limiters)
    if cause == "adapter-less":
        monkeypatch.setattr(common, "SUPPORTED_PROTOCOLS", {"mail": frozenset({"imap"}), "calendar": frozenset()})
    if cause == "no-credentials":
        (secrets_dir / "gmail-app-password").unlink()
    entries = [ref(account), ref("icloud"), encode_message_id(GraphMessageRef(account, "AAMkMSG0001="))]
    result, accounts, outcome = await run(ctx, entries)
    first, second, third = result.items
    assert payload(first) == {"ref": entries[0], "account": account, "state": "unknown",
                              "reason": "account_unavailable"} | NULL_FIELDS  # fmt: skip
    assert payload(third) == {"input": entries[2], "ref": None, "account": account, "state": "unknown",
                              "reason": "account_unavailable"} | NULL_FIELDS  # fmt: skip
    assert second.state == "found"
    assert fake.opened == ["icloud"]
    assert limiters.asked == ["icloud"]
    assert ctx.status.get(account, "mail").status == "unknown"  # no status record
    assert ctx.status.get(account, "mail").last_error_at is None
    assert result.account_errors == []
    assert (accounts, outcome) == (["icloud"], "ok")


async def test_id_mismatch_without_provider_call(secrets_dir: Path) -> None:
    fake = Fake()
    entries = [
        encode_message_id(MessageRef("outlook", "INBOX", 1, 1)),
        encode_message_id(GraphMessageRef("icloud", "AAMkMSG0001=")),
        encode_message_id(GraphMessageRef("gmail", "AAMkMSG0002=")),
    ]
    result, accounts, outcome = await run(context(secrets_dir, fake), entries)
    assert [(i.account, i.state, i.reason, i.ref, i.input) for i in result.items] == [
        ("outlook", "unknown", "id_mismatch", None, entries[0]),
        ("icloud", "unknown", "id_mismatch", None, entries[1]),
        ("gmail", "unknown", "id_mismatch", None, entries[2]),
    ]
    assert (fake.opened, fake.calls) == ([], [])
    assert (accounts, outcome, result.account_errors) == ([], "ok", [])


async def test_cached_refused_binding_is_stale_id_without_provider_call(secrets_dir: Path) -> None:
    FOLDER_MAPS.put(build_imap_folder_map("icloud", "INBOX", gmail=False, listing=ICLOUD_LIST))
    fake = Fake()
    junk = MessageRef("icloud", "Junk", 7, 3)
    result, accounts, _ = await run(context(secrets_dir, fake), [encode_message_id(junk)])
    [item] = result.items
    assert (item.state, item.reason, item.ref) == ("unknown", "stale_id", None)
    assert (fake.opened, accounts) == ([], [])


@pytest.mark.parametrize("cached", [False, True], ids=["no-map", "map-cached"])
async def test_registry_inbox_id_reaches_adapter(secrets_dir: Path, cached: bool) -> None:
    if cached:
        FOLDER_MAPS.put(build_imap_folder_map("icloud", "INBOX", gmail=False, listing=ICLOUD_LIST))
    inbox, projects, unlisted = (
        MessageRef("icloud", "INBOX", 7, 3),
        MessageRef("icloud", "Projects", 7, 4),
        MessageRef("icloud", "Unlisted", 7, 5),  # unknown to the map: the adapter decides after LIST
    )
    fake = Fake()
    await run(context(secrets_dir, fake), [encode_message_id(m) for m in (inbox, projects, unlisted)])
    assert [(account, targets) for account, targets, _ in fake.calls] == [("icloud", [inbox, projects, unlisted])]


async def test_one_provider_call_per_account_with_distinct_targets(secrets_dir: Path) -> None:
    fake = Fake()
    graph_id = GraphMessageRef("outlook", "AAMkMSG0001=")
    entries = [
        ref("gmail", "d@example.test"),
        ref("icloud", "a@example.test"),
        ref("icloud", "b@example.test"),
        encode_message_id(graph_id),
        ref("icloud", "a@example.test"),
        ref("outlook", "f@example.test"),
        ref("gmail", "d@example.test"),
    ]
    _, accounts, _ = await run(context(secrets_dir, fake), entries)
    assert sorted((account, targets) for account, targets, _ in fake.calls) == [
        ("gmail", [StableRef("gmail", "d@example.test")]),
        ("icloud", [StableRef("icloud", "a@example.test"), StableRef("icloud", "b@example.test")]),
        ("outlook", [graph_id, StableRef("outlook", "f@example.test")]),
    ]
    assert sorted(fake.opened) == ["gmail", "icloud", "outlook"]
    assert accounts == ["gmail", "icloud", "outlook"]  # first-seen order


async def test_deadline_started_per_account_before_run_blocking(
    secrets_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[tuple[str, object]] = []
    real_run_blocking = mail_status.run_blocking

    class Deadlines:
        @staticmethod
        def start() -> CallDeadline:
            deadline = CallDeadline.start()
            events.append(("start", deadline))
            return deadline

    async def recording_run_blocking(func: Callable[[], Any], *, slot: AccountSlot) -> Any:
        events.append(("run_blocking", slot))
        return await real_run_blocking(func, slot=slot)

    monkeypatch.setattr(mail_status, "CallDeadline", Deadlines)
    monkeypatch.setattr(mail_status, "run_blocking", recording_run_blocking)
    fake = Fake()
    await run(context(secrets_dir, fake), [ref("icloud"), ref("gmail"), ref("icloud", "x@example.test")])
    assert [name for name, _ in events] == ["start", "run_blocking", "start", "run_blocking"]
    started = [value for name, value in events if name == "start"]
    assert sorted(id(d) for _, _, d in fake.calls) == sorted(id(d) for d in started)  # each call got its own


async def test_account_failure_marks_every_ref_account_error(secrets_dir: Path) -> None:
    fake = Fake(
        answers={StableRef("gmail", MID): found(MessageRef("gmail", "INBOX", 9, 1))},
        errors={"icloud": ProviderError("unreachable", "TimeoutError")},
    )
    mismatch = encode_message_id(GraphMessageRef("icloud", "AAMkMSG0001="))
    entries = [ref("icloud"), ref("gmail"), ref("icloud", "b@example.test"), ref("icloud"), mismatch]
    ctx = context(secrets_dir, fake)
    result, accounts, outcome = await run(ctx, entries)
    assert ctx.status.get("icloud", "mail").status == "unreachable"
    assert [(i.account, i.state, i.reason) for i in result.items] == [
        ("icloud", "unknown", "account_error"),
        ("gmail", "found", None),
        ("icloud", "unknown", "account_error"),
        ("icloud", "unknown", "account_error"),
        ("icloud", "unknown", "id_mismatch"),  # decided without the provider: not part of the failed call
    ]
    assert [i.ref for i in result.items[:4]] == [entries[0], entries[1], entries[2], entries[0]]
    assert [(e.account, e.capability, e.code, e.message) for e in result.account_errors] == [
        ("icloud", "mail", "unreachable", "Provider not reachable")
    ]
    assert (accounts, outcome) == (["icloud", "gmail"], "partial")


async def test_status_count_mismatch_fails_the_account(secrets_dir: Path) -> None:
    class Short(FakeMailbox):
        def message_status(self, targets: Sequence[StatusTarget], deadline: CallDeadline) -> list[MessageStatus]:
            return [GONE]

    fake = Fake()
    ctx = context(secrets_dir, fake)
    ctx = HubContext(ctx.settings, ctx.registry, ctx.status, Adapters(mailbox=lambda a, d: Short(fake, a.id)))
    result, _, outcome = await run(ctx, [ref("icloud"), ref("icloud", "b@example.test")])
    assert [(i.state, i.reason) for i in result.items] == [("unknown", "account_error")] * 2
    assert [(e.account, e.code) for e in result.account_errors] == [("icloud", "upstream_error")]
    assert outcome == "error"


async def test_all_incomplete_answer_is_no_account_error(secrets_dir: Path) -> None:
    """C12: a soft stop before any folder (e.g. at LOGIN) answers every entry incomplete_search; the account is
    recorded ok and is not listed in account_errors."""
    fake = Fake(default=status_unknown("incomplete_search"))
    ctx = context(secrets_dir, fake)
    result, accounts, outcome = await run(ctx, [ref("icloud"), ref("icloud", "b@example.test")])
    assert [(i.state, i.reason) for i in result.items] == [("unknown", "incomplete_search")] * 2
    assert (result.account_errors, accounts, outcome) == ([], ["icloud"], "ok")
    assert ctx.status.get("icloud", "mail").status == "ok"


# --- item fields --------------------------------------------------------------------------------------------------


async def test_found_item_fields(secrets_dir: Path) -> None:
    location = MessageRef("icloud", "Archive", 7, 9)
    status = found(
        location,
        folder="archive",
        unread=True,
        flagged=True,
        reply_at=NOW + timedelta(hours=2),
        reply_message_id=REPLY_MID,
        newer_incoming_at=NOW + timedelta(hours=3),
        thread_checked=False,
    )
    fake = Fake(answers={StableRef("icloud", MID): status})
    result, _, _ = await run(context(secrets_dir, fake), [ref("icloud")])
    [item] = result.items
    assert list(payload(item)) == [
        "ref", "account", "state", "reason", "id", "folder", "received_at", "direction", "flags", "replied",
        "replied_at", "reply_ref", "newer_incoming_in_thread", "newer_incoming_at", "thread_checked",
    ]  # fmt: skip
    assert payload(item) == {
        "ref": ref("icloud"),
        "account": "icloud",
        "state": "found",
        "reason": None,
        "id": encode_message_id(location),
        "folder": "archive",
        "received_at": "2026-10-20T07:12:03" + ZONE_OFFSET,
        "direction": "incoming",
        "flags": {"seen": False, "answered": False, "flagged": True},
        "replied": True,
        "replied_at": "2026-10-20T09:12:03" + ZONE_OFFSET,
        "reply_ref": ref("icloud", REPLY_MID),
        "newer_incoming_in_thread": True,
        "newer_incoming_at": "2026-10-20T10:12:03" + ZONE_OFFSET,
        "thread_checked": False,
    }
    assert (result.untrusted_content_notice, result.next_cursor, result.truncated) == (
        UNTRUSTED_CONTENT_NOTICE,
        None,
        False,
    )


async def test_found_outgoing_graph_copy(secrets_dir: Path) -> None:
    location = GraphMessageRef("outlook", "AAMkMSG0009=")
    fake = Fake(answers={StableRef("outlook", MID): found(location, folder="sent", outgoing=True)})
    result, _, _ = await run(context(secrets_dir, fake), [ref("outlook")])
    [item] = result.items
    assert (item.id, item.folder, item.direction) == (encode_message_id(location), "sent", "outgoing")
    assert item.flags == Flags(seen=True, answered=False, flagged=False)
    assert (item.replied, item.newer_incoming_in_thread, item.thread_checked) == (False, False, True)


async def test_replied_from_flag_only_has_null_reply_fields(secrets_dir: Path) -> None:
    fake = Fake(answers={StableRef("icloud", MID): found(MessageRef("icloud", "INBOX", 7, 1), answered=True)})
    result, _, _ = await run(context(secrets_dir, fake), [ref("icloud")])
    [item] = result.items
    assert (item.replied, item.replied_at, item.reply_ref) == (True, None, None)
    assert item.flags is not None
    assert item.flags.answered is True


async def test_reply_found_sets_replied_at_and_reply_ref(secrets_dir: Path) -> None:
    status = found(MessageRef("icloud", "INBOX", 7, 1), reply_at=NOW + timedelta(minutes=5), reply_message_id=REPLY_MID)
    fake = Fake(answers={StableRef("icloud", MID): status})
    result, _, _ = await run(context(secrets_dir, fake), [ref("icloud")])
    [item] = result.items
    assert (item.replied, item.replied_at, item.reply_ref) == (
        True,
        "2026-10-20T07:17:03" + ZONE_OFFSET,
        ref("icloud", REPLY_MID),
    )
    assert item.flags is not None
    assert item.flags.answered is False  # the reply alone counts


@pytest.mark.parametrize("reply_message_id", [None, "has space@example.test"], ids=["none", "fails-pattern"])
async def test_reply_without_message_id_has_null_reply_ref(secrets_dir: Path, reply_message_id: str | None) -> None:
    status = found(
        MessageRef("icloud", "INBOX", 7, 1), reply_at=NOW + timedelta(minutes=5), reply_message_id=reply_message_id
    )
    fake = Fake(answers={StableRef("icloud", MID): status})
    result, _, _ = await run(context(secrets_dir, fake), [ref("icloud")])
    [item] = result.items
    assert (item.replied, item.replied_at, item.reply_ref) == (True, "2026-10-20T07:17:03" + ZONE_OFFSET, None)


async def test_not_found_items_follow_the_null_rules(secrets_dir: Path) -> None:
    FOLDER_MAPS.put(build_imap_folder_map("icloud", "INBOX", gmail=False, listing=ICLOUD_LIST))
    fake = Fake(
        answers={
            StableRef("icloud", "gone@example.test"): GONE,
            StableRef("icloud", "cut@example.test"): status_unknown("incomplete_search"),
            MessageRef("icloud", "INBOX", 7, 3): status_unknown("stale_id"),
        },
        errors={"gmail": ProviderError("auth_expired", "LoginError")},
    )
    entries = [
        ref("icloud", "gone@example.test"),
        ref("icloud", "cut@example.test"),
        encode_message_id(MessageRef("icloud", "INBOX", 7, 3)),
        encode_message_id(MessageRef("icloud", "Junk", 7, 3)),  # stale_id from the cached map
        ref("gmail"),  # account_error
        ref("uzh"),  # account_unavailable
        encode_message_id(MessageRef("outlook", "INBOX", 1, 1)),  # id_mismatch
    ]
    result, _, _ = await run(context(secrets_dir, fake), entries)
    reasons: list[StatusReason | None] = [i.reason for i in result.items]
    assert reasons == [None, "incomplete_search", "stale_id", "stale_id", "account_error", "account_unavailable",
                       "id_mismatch"]  # fmt: skip
    assert [i.state for i in result.items] == ["gone"] + ["unknown"] * 6
    for item in result.items:
        assert {key: payload(item)[key] for key in NULL_FIELDS} == NULL_FIELDS
        assert item.thread_checked is None


def test_builders_enforce_invariants() -> None:
    zone_status = found(MessageRef("icloud", "INBOX", 7, 1))
    with pytest.raises(ValueError, match="found status"):
        found_item("e", None, "icloud", GONE, ZoneInfo("Europe/Zurich"))
    assert found_item("e", "e", "icloud", zone_status, ZoneInfo("UTC")).input is None
    assert gone_item("e", None, "icloud").input == "e"
    assert unknown_item("e", "e", "icloud", "stale_id").model_dump(exclude={"input"}) == {
        "ref": "e", "account": "icloud", "state": "unknown", "reason": "stale_id",
    } | NULL_FIELDS  # fmt: skip
    valid = payload(found_item("e", "e", "icloud", zone_status, ZoneInfo("UTC")))
    broken: list[dict[str, Any]] = [
        payload(gone_item("e", "e", "icloud")) | {"reason": "stale_id"},  # gone with a reason
        payload(unknown_item("e", "e", "icloud", "stale_id")) | {"reason": None},  # unknown without one
        payload(gone_item("e", "e", "icloud")) | {"thread_checked": False},  # E-td1
        payload(gone_item("e", "e", "icloud")) | {"folder": "inbox"},
        payload(gone_item("e", "e", "icloud")) | {"id": "v1.x"},
        payload(unknown_item("e", "e", "icloud", "account_error")) | {"replied": True},
        valid | {"thread_checked": None},  # found without thread_checked
        valid | {"reason": "stale_id"},
        valid | {"reply_ref": "v1.x"},  # reply_ref without replied_at
        valid | {"replied_at": "2026-10-20T07:12:03+02:00"},  # replied_at without replied
        valid | {"newer_incoming_in_thread": True},  # flag without the time
        valid | {"newer_incoming_at": "2026-10-20T07:12:03+02:00"},  # time without the flag
    ]
    for case in broken:
        with pytest.raises(ValidationError):
            StatusItem.model_validate(case)


async def test_input_echo_only_when_differs_from_ref(secrets_dir: Path) -> None:
    inbox, graph = MessageRef("icloud", "INBOX", 7, 3), GraphMessageRef("outlook", "AAMkMSG0001=")
    no_mid = MessageRef("icloud", "INBOX", 7, 4)
    fake = Fake(
        answers={
            StableRef("icloud", MID): found(MessageRef("icloud", "INBOX", 7, 1)),
            inbox: found(inbox),
            graph: found(graph),
            no_mid: found(no_mid, None),
        }
    )
    entries = [
        ref("icloud"),
        non_canonical("icloud", MID),
        encode_message_id(inbox),
        encode_message_id(graph),
        encode_message_id(no_mid),
    ]
    result, _, _ = await run(context(secrets_dir, fake), entries)
    wire = structured_result(result)
    assert wire.structured_content is not None
    text_items = json.loads(wire.content[0].text)["items"]  # type: ignore[union-attr]  # TextContent
    for items in (text_items, wire.structured_content["items"]):
        assert "input" not in items[0]  # canonical ref: the key is absent, not null
        assert [item.get("input") for item in items[1:]] == entries[1:]
        assert [item["ref"] for item in items] == [ref("icloud"), ref("icloud"), ref("icloud"), ref("outlook"), None]


def _string_fields(schema: dict[str, Any]) -> dict[str, dict[str, Any]]:
    def types(spec: dict[str, Any]) -> set[str]:
        options = spec.get("anyOf", [spec])
        return {option.get("type", "ref") for option in options}

    return {name: spec for name, spec in schema["properties"].items() if "string" in types(spec)}


def test_status_item_schema_has_no_free_text() -> None:
    schema = StatusItem.model_json_schema()
    assert "untrusted" not in json.dumps(schema)
    strings = _string_fields(schema)
    opaque, enums, timestamps = {"input", "ref", "id", "reply_ref"}, {"state", "reason", "folder", "direction"}, {
        "received_at", "replied_at", "newer_incoming_at",
    }  # fmt: skip
    assert set(strings) == opaque | enums | timestamps | {"account"}
    for name in enums:
        assert any("enum" in option for option in strings[name].get("anyOf", [strings[name]])), name
    assert set(schema["$defs"]) == {"Flags"}
    assert {spec["type"] for spec in schema["$defs"]["Flags"]["properties"].values()} == {"boolean"}


async def test_status_item_values_are_opaque_ids_enums_or_timestamps(secrets_dir: Path) -> None:
    status = found(MessageRef("icloud", "INBOX", 7, 1), reply_at=NOW, reply_message_id=REPLY_MID, newer_incoming_at=NOW)
    fake = Fake(answers={StableRef("icloud", MID): status})
    result, _, _ = await run(context(secrets_dir, fake), [non_canonical("icloud", MID)])
    item = payload(result.items[0])
    rfc3339 = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}")
    for name in ("input", "ref", "id", "reply_ref"):
        assert item[name].startswith("v1."), name
    for name in ("received_at", "replied_at", "newer_incoming_at"):
        assert rfc3339.fullmatch(item[name]), name


# --- budget -------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("configured", "expected"), [(10_000, 50_000), (30_000, 50_000), (50_000, 50_000),
                                                      (60_000, 60_000), (70_000, 70_000)])  # fmt: skip
async def test_budget_is_floor_50000_clamped(
    secrets_dir: Path, monkeypatch: pytest.MonkeyPatch, configured: int, expected: int
) -> None:
    seen: list[int] = []
    real_fit_items = mail_status.fit_items

    def recording(items: list[StatusItem], build: Any, budget: int, *, shrink: Any) -> Any:
        seen.append(budget)
        return real_fit_items(items, build, budget, shrink=shrink)

    monkeypatch.setattr(mail_status, "fit_items", recording)
    ctx = context(secrets_dir, Fake(), env={"HUB_RESPONSE_BUDGET_CHARS": str(configured)})
    await run(ctx, [ref("icloud")])
    assert seen == [expected]
    assert expected <= 70_000
    assert STATUS_BUDGET_FLOOR == 50_000


def _graph_id(n: int) -> str:
    return f"{GRAPH_LIKE[:-4]}{n:04d}"


def _gmail_mid(n: int, suffix: str = "") -> str:
    return f"CAH{n:04d}{suffix}" + "q" * (52 - 4 - len(suffix)) + "@mail.example.com"


async def test_fifty_realistic_outlook_refs_fit_one_result(secrets_dir: Path) -> None:
    """TD-R7: stored refs with Gmail-length Message-IDs, all found as Graph copies with immutable ids of about 270
    characters; every second one answered by a reply with a Message-ID, every third one with newer incoming mail:
    one result at the default setting. (With a `reply_ref` on all 50 items the result is about 50,900 characters
    and the last item moves to a second call.)"""
    assert len(GMAIL_LIKE) == len(_gmail_mid(1)) == 72
    answers: dict[StatusTarget, MessageStatus] = {
        StableRef("outlook", _gmail_mid(n)): found(
            GraphMessageRef("outlook", _graph_id(n)),
            _gmail_mid(n),
            folder="archive",
            reply_at=NOW + timedelta(hours=1) if n % 2 == 0 else None,
            reply_message_id=_gmail_mid(n, "r") if n % 2 == 0 else None,
            newer_incoming_at=NOW + timedelta(hours=2) if n % 3 == 0 else None,
        )
        for n in range(50)
    }
    fake = Fake(answers=answers)
    ctx = context(secrets_dir, fake)
    assert ctx.settings.response_budget_chars == 30_000  # the default setting
    result, _, _ = await run(ctx, [ref("outlook", _gmail_mid(n)) for n in range(50)])
    assert len(_graph_id(1)) == 270
    assert (len(result.items), result.truncated) == (50, False)
    assert all(i.state == "found" and i.id is not None for i in result.items)
    assert sum(i.reply_ref is not None for i in result.items) == 25
    assert len(result.model_dump_json()) <= STATUS_BUDGET_FLOOR


async def test_fifty_realistic_imap_ids_fit_one_result(secrets_dir: Path) -> None:
    """The IMAP sizes: `id` entries (the input is echoed) on an `other` mailbox with a long name."""
    folder = "Projects/" + "P" * 40
    locations = [MessageRef("gmail", folder, 4_000_000_000, 4_000_000_000 - n) for n in range(50)]
    answers: dict[StatusTarget, MessageStatus] = {
        location: found(
            location,
            _gmail_mid(n),
            folder="other",
            reply_at=NOW + timedelta(hours=1),
            reply_message_id=_gmail_mid(n, "r"),
            newer_incoming_at=NOW + timedelta(hours=2),
        )
        for n, location in enumerate(locations)
    }
    fake = Fake(answers=answers)
    result, _, _ = await run(context(secrets_dir, fake), [encode_message_id(location) for location in locations])
    assert (len(result.items), result.truncated) == (50, False)
    assert all(i.input is not None for i in result.items)


def _worst_case_entries() -> tuple[list[str], dict[StatusTarget, MessageStatus]]:
    """Every opaque field at the largest size it can take: the entry at 511 characters (a padded `r` encoding; an
    unpadded base64url text is never 1 more than a multiple of 4 long, so 512 is not reachable),
    300-character Message-IDs in `ref` and `reply_ref`, a 300-character Graph id in `id`."""
    entries: list[str] = []
    answers: dict[StatusTarget, MessageStatus] = {}
    for n in range(50):
        message_id = f"{n:03d}" + "m" * 285 + "@example.org"
        compact = len(json.dumps(["r", "outlook", message_id], separators=(",", ":")))
        entries.append(non_canonical("outlook", message_id, WORST_ENTRY_BYTES - compact))
        answers[StableRef("outlook", message_id)] = found(
            GraphMessageRef("outlook", f"{n:03d}" + "G" * 297),
            message_id,
            reply_at=NOW + timedelta(hours=1),
            reply_message_id=f"{n:03d}" + "r" * 285 + "@example.org",
            newer_incoming_at=NOW + timedelta(hours=2),
        )
    return entries, answers


async def test_fifty_worst_case_items_truncate_and_rest_answers_tail(secrets_dir: Path) -> None:
    entries, answers = _worst_case_entries()
    assert {len(e) for e in entries} == {511}
    fake = Fake(answers=answers)
    ctx = context(secrets_dir, fake)
    answered: list[StatusItem] = []
    rest = entries
    while rest:
        result, _, _ = await run(ctx, rest)
        assert len(result.model_dump_json()) <= STATUS_BUDGET_FLOOR
        assert result.next_cursor is None
        assert result.items
        assert result.truncated is (len(result.items) < len(rest))  # a prefix, and the flag says so
        answered += result.items
        rest = rest[len(result.items) :]
    assert len(fake.calls) > 1
    assert [i.input for i in answered] == entries  # every entry answered once, in order
    assert all(i.state == "found" for i in answered)


async def test_minimum_budget_holds_one_maximal_item(secrets_dir: Path) -> None:
    entries, answers = _worst_case_entries()
    fake = Fake(answers=answers, errors={"icloud": ProviderError("unreachable", "TimeoutError")})
    ctx = context(secrets_dir, fake, env={"HUB_RESPONSE_BUDGET_CHARS": "10000"})
    result, _, _ = await run(ctx, [*entries[:1], ref("icloud")])
    assert (len(result.items), result.truncated) == (2, False)  # the floor, not the 10,000 setting
    one = result.model_copy(update={"items": result.items[:1]})
    assert len(one.model_dump_json()) < STATUS_BUDGET_FLOOR // 10
    result, _, _ = await run(ctx, entries)
    assert len(result.items) >= 20


# --- wire, description and logs ------------------------------------------------------------------------------------


def test_description_verbatim_and_read_only() -> None:
    assert GET_MESSAGE_STATUS_DESCRIPTION == EXPECTED_DESCRIPTION
    assert GET_MESSAGE_STATUS_DESCRIPTION.endswith("Read-only.")


@pytest.fixture
def wired(settings: Settings, secrets_dir: Path) -> Iterator[tuple[TestClient, Fake]]:
    configure_logging(settings.log_level)
    fake = Fake(answers={StableRef("icloud", MID): found(MessageRef("icloud", "Private Folder", 7, 1))})
    hub = create_app(settings, load_registry(secrets_dir / "accounts.json"), adapters=Adapters(mailbox=fake.factory))
    with TestClient(hub.public, base_url="https://mcp.furchert.ch") as client:
        yield client, fake


def call(client: TestClient, tokens: TokenFactory, arguments: dict[str, Any]) -> dict[str, Any]:
    params = {"name": "get_message_status", "arguments": arguments}
    return body(modern(client, tokens.mint(), "tools/call", params, name="get_message_status"))["result"]  # type: ignore[no-any-return]  # JSON-RPC result


def test_wire_missing_refs_is_error_not_pinned(wired: tuple[TestClient, Fake], tokens: TokenFactory) -> None:
    client, fake = wired
    result = call(client, tokens, {})
    assert result["isError"] is True  # the SDK's validation text; not pinned (TD-R3)
    assert fake.opened == []


def test_wire_json_string_list_is_accepted_and_bare_string_refused(
    wired: tuple[TestClient, Fake], tokens: TokenFactory
) -> None:
    client, _ = wired
    listed = call(client, tokens, {"refs": json.dumps([ref("icloud")])})
    assert listed["isError"] is False
    assert json.loads(listed["content"][0]["text"]) == listed["structuredContent"]
    assert [i["state"] for i in listed["structuredContent"]["items"]] == ["found"]
    for refs in (ref("icloud"), json.dumps(ref("icloud")), [ref("icloud")] * 51):
        refused = call(client, tokens, {"refs": refs})
        assert refused["isError"] is True
        assert json.loads(refused["content"][0]["text"]) == {"code": "invalid_argument", "message": REFS_MESSAGE}


class Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.setFormatter(JsonFormatter())
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self.format(record))


@pytest.fixture
def captured() -> Iterator[Capture]:
    handler = Capture()
    logging.getLogger().addHandler(handler)
    try:
        yield handler
    finally:
        logging.getLogger().removeHandler(handler)


@pytest.mark.parametrize("where", ["adapter", "tool"])
def test_unexpected_exception_answers_fixed_upstream_error(
    wired: tuple[TestClient, Fake],
    tokens: TokenFactory,
    captured: Capture,
    monkeypatch: pytest.MonkeyPatch,
    where: str,
) -> None:
    client, fake = wired
    if where == "adapter":
        fake.errors["icloud"] = RuntimeError("<x@example.org>")
    else:

        def broken(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("<x@example.org>")

        monkeypatch.setattr(mail_status, "fit_items", broken)
    result = call(client, tokens, {"refs": [ref("icloud")]})
    text = json.dumps(result) + "\n".join(captured.lines)
    assert "example.org" not in text
    events = [json.loads(line) for line in captured.lines]
    [tool_call] = [e for e in events if e["event"] == "tool_call"]
    if where == "adapter":  # one failing account never fails the call (§5.1)
        assert result["isError"] is False
        [item] = result["structuredContent"]["items"]
        assert (item["state"], item["reason"]) == ("unknown", "account_error")
        assert result["structuredContent"]["account_errors"] == [
            {
                "account": "icloud",
                "capability": "mail",
                "code": "upstream_error",
                "message": "Provider returned an error",
            }
        ]
        assert (tool_call["outcome"], tool_call["result_count"]) == ("error", 1)
    else:  # SR3: a hub bug answers with the fixed §5.1 body
        assert result["isError"] is True
        assert json.loads(result["content"][0]["text"]) == {
            "code": "upstream_error",
            "message": "Provider returned an error",
        }
        assert (tool_call["outcome"], tool_call["accounts"], tool_call["result_count"]) == ("upstream_error", [], 0)


def test_tool_call_log_line(wired: tuple[TestClient, Fake], tokens: TokenFactory, captured: Capture) -> None:
    client, _ = wired
    entries = [ref("icloud"), encode_message_id(MessageRef("icloud", "INBOX", 7, 3)), ref("uzh")]
    ok = call(client, tokens, {"refs": entries})
    error = call(client, tokens, {"refs": []})
    assert ok["isError"] is False
    assert error["isError"] is True
    events = [json.loads(line) for line in captured.lines]
    calls = [e for e in events if e["event"] == "tool_call"]
    assert [(e["tool"], e["outcome"], e["accounts"], e["result_count"]) for e in calls] == [
        ("get_message_status", "ok", ["icloud"], 3),
        ("get_message_status", "invalid_argument", [], 0),
    ]
    assert all(set(e) <= allowed_fields(e["event"]) for e in events)
    text = "\n".join(captured.lines)
    item_ids = [i["id"] for i in ok["structuredContent"]["items"] if i["id"]]
    for forbidden in [*entries, *item_ids, MID, "Private Folder", "INBOX"]:
        assert forbidden not in text, forbidden


# --- the real Graph adapter behind the tool (M3) ------------------------------------------------------------------

GONE_MID = "gone@example.test"


def graph_copy(n: int, message_id: str) -> dict[str, object]:
    """Graph item `n` in the inbox, a copy of `message_id` received at NOW; no extended properties (not answered)."""
    return message(n, internetMessageId=f"<{message_id}>", receivedDateTime=NOW.strftime("%Y-%m-%dT%H:%M:%SZ"))


def graph_json(body: object, status: int = 200) -> httpx2.Response:
    return httpx2.Response(status, content=json.dumps(body).encode(), headers={"Content-Type": "application/json"})


@dataclass
class GraphStub:
    """Graph v1.0 by path: the well-known folder GETs, the location and conversation lookups over `items`, and the
    `g` reads, which answer 500 for item 3 and 404 otherwise. `location` is the status of every location lookup."""

    items: list[dict[str, object]]
    location: int = 200

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        path = request.url.path
        if path.startswith("/v1.0/me/mailFolders/"):
            return graph_json({"id": FOLDER_IDS[path.rsplit("/", 1)[1]]})
        if path != "/v1.0/me/messages":
            return graph_json({"error": {"code": "x"}}, 500 if path.endswith("MSG0003=") else 404)
        text = request.url.params["$filter"]
        key = "conversationId" if text.startswith("conversationId eq ") else "internetMessageId"
        if key == "internetMessageId" and self.location != 200:
            return graph_json({"error": {"code": "x"}}, self.location)
        return graph_json({"value": [i for i in self.items if f"{key} eq '{i[key]}'" in text]})


def test_graph_adapter_behind_the_tool(
    settings: Settings, secrets_dir: Path, tokens: TokenFactory, captured: Capture
) -> None:
    """run_get_message_status over the wire with the real GraphMailbox on a MockTransport: found, gone (location and
    404 read), a failed `g` read, an id mismatch, then an account failure; items in input order with the null rules,
    provider_call_failed next to tool_call, and no value in any log line."""
    for name in ("outlook-ms-client-id", "db-username", "db-password", "token-encryption-key"):
        (secrets_dir / name).write_text("placeholder")
    stub = GraphStub([graph_copy(1, MID)])
    maps = FolderMapCache()

    def factory(account: Account, _secrets_dir: Path) -> GraphMailbox:
        assert account.id == "outlook"
        return GraphMailbox(
            "outlook",
            FakeTokens(),
            client_factory=lambda timeout: httpx2.Client(transport=httpx2.MockTransport(stub.handle), trust_env=False),
            backoff=Backoff(),
            folder_maps=maps,
        )

    found_id = encode_message_id(GraphMessageRef("outlook", "AAMkMSG0001="))
    failed_id = encode_message_id(GraphMessageRef("outlook", "AAMkMSG0003="))
    gone_id = encode_message_id(GraphMessageRef("outlook", "AAMkMSG0004="))
    mismatch = encode_message_id(MessageRef("outlook", "INBOX", 7, 1))
    entries = [ref("outlook"), ref("outlook", GONE_MID), failed_id, gone_id, mismatch]
    configure_logging(settings.log_level)
    logging.getLogger().addHandler(captured)  # configure_logging replaced the root handlers
    hub = create_app(settings, load_registry(secrets_dir / "accounts.json"), adapters=Adapters(mailbox=factory))
    with TestClient(hub.public, base_url="https://mcp.furchert.ch") as client:
        answered = call(client, tokens, {"refs": entries})
        stub.location = 403  # Forbidden fails the account (GR1)
        failing = call(client, tokens, {"refs": entries[:2]})

    assert answered["isError"] is False
    content = answered["structuredContent"]
    assert content["items"] == [
        {
            "ref": ref("outlook"),
            "account": "outlook",
            "state": "found",
            "reason": None,
            "id": found_id,
            "folder": "inbox",
            "received_at": "2026-10-20T07:12:03" + ZONE_OFFSET,
            "direction": "incoming",
            "flags": {"seen": False, "answered": False, "flagged": False},
            "replied": False,
            "replied_at": None,
            "reply_ref": None,
            "newer_incoming_in_thread": False,
            "newer_incoming_at": None,
            "thread_checked": True,
        },
        {"ref": ref("outlook", GONE_MID), "account": "outlook", "state": "gone", "reason": None} | NULL_FIELDS,
        {"input": failed_id, "ref": None, "account": "outlook", "state": "unknown", "reason": "incomplete_search"}
        | NULL_FIELDS,
        {"input": gone_id, "ref": None, "account": "outlook", "state": "gone", "reason": None} | NULL_FIELDS,
        {"input": mismatch, "ref": None, "account": "outlook", "state": "unknown", "reason": "id_mismatch"}
        | NULL_FIELDS,
    ]
    assert (content["account_errors"], content["truncated"], content["next_cursor"]) == ([], False, None)

    assert failing["isError"] is False
    assert [(i["state"], i["reason"]) for i in failing["structuredContent"]["items"]] == [
        ("unknown", "account_error")
    ] * 2
    assert failing["structuredContent"]["account_errors"] == [
        {"account": "outlook", "capability": "mail", "code": "upstream_error", "message": "Provider returned an error"}
    ]

    events = [json.loads(line) for line in captured.lines]
    logged = [e for e in events if e["event"] in {"provider_call_failed", "tool_call"}]
    assert [(e["event"], e.get("outcome"), e.get("exception")) for e in logged] == [
        ("provider_call_failed", "upstream_error", "UnexpectedStatus"),  # the failed `g` read, while the call goes on
        ("tool_call", "ok", None),
        ("provider_call_failed", "upstream_error", "Forbidden"),  # the failed account
        ("tool_call", "error", None),
    ]
    assert [(e["accounts"], e["result_count"]) for e in logged if e["event"] == "tool_call"] == [
        (["outlook"], 5),
        (["outlook"], 2),
    ]
    assert all(set(e) <= allowed_fields(e["event"]) for e in events)
    text = "\n".join(captured.lines)
    for forbidden in [
        *entries,
        found_id,
        MID,
        GONE_MID,
        "example.test",
        "AAMkMSG",
        "AAQkCONV",
        *FOLDER_IDS.values(),
        "AT-SENTINEL",
        "graph.microsoft.com",
    ]:
        assert forbidden not in text, forbidden
