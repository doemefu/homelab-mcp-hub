"""search_messages (spec 080 §5.2, §5.5, §5.7 rev 4.14): validation before any provider call, cursor binding,
`incomplete_accounts`, Graph hits without an `id`, and the cursor rules."""

import base64
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from mcp_hub.config import load_settings
from mcp_hub.cursors import CursorCodec, Position
from mcp_hub.errors import ToolError
from mcp_hub.health import StatusStore
from mcp_hub.ids import GraphMessageRef, MessageRef, StableRef, decode_message_id, encode_ref, location_id
from mcp_hub.providers import Adapters
from mcp_hub.providers.base import (
    FolderPage,
    ListQuery,
    MailDetail,
    MailMeta,
    MailSummary,
    ProviderError,
    SearchTerms,
    order_key,
)
from mcp_hub.providers.deadline import CallDeadline
from mcp_hub.registry import TOKEN_STORE_REFS, Account, load_registry
from mcp_hub.search import EMPTY_GRAPH_QUERY, GRAPH_QUERY_TOO_LONG, VALUE_TOO_LONG
from mcp_hub.tools import HubContext
from mcp_hub.tools.common import UNTRUSTED_CONTENT_NOTICE
from mcp_hub.tools.mail import run_get_message, run_list_messages
from mcp_hub.tools.mail_search import SearchMessagesResult, run_search_messages

pytestmark = pytest.mark.anyio
NOW = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
KEY = b"t" * 32
SEARCH_ID = "AAMkSearchResultRegularId"  # a Graph `$search` hit's returned (regular, non-exposable) id
IMMUTABLE_ID = "AAMkImmutableIdOfTheSameMessage"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class Clock:
    def __init__(self) -> None:
        self.now = NOW.timestamp()

    def __call__(self) -> float:
        return self.now


def summary(account: str, uid: int, minutes_ago: float, *, graph: bool = False, **meta: object) -> MailSummary:
    ref: MessageRef | GraphMessageRef = (
        GraphMessageRef(account, f"{SEARCH_ID}{uid}") if graph else MessageRef(account, "INBOX", 7, uid)
    )
    return MailSummary(
        ref=ref,
        received_at=NOW - timedelta(minutes=minutes_ago),
        unread=True,
        has_attachments=False,
        from_address="sender@example.test",
        from_name="Sender",
        subject="Hi",
        snippet_text="Hello",
        meta=replace(MailMeta(), **meta),  # type: ignore[arg-type]  # MailMeta fields chosen per test
    )


@dataclass
class Fake:
    """Adapter stand-in applying the keyset rules of §5.5; `flags` overrides page fields per account."""

    items: dict[str, list[MailSummary]] = field(default_factory=dict)
    errors: dict[str, Exception] = field(default_factory=dict)
    flags: dict[str, dict[str, Any]] = field(default_factory=dict)
    queries: list[tuple[str, ListQuery]] = field(default_factory=list)
    details: dict[str, MailDetail] = field(default_factory=dict)  # by Message-ID, for get_message(ref)

    def factory(self, account: Account, _secrets_dir: Path) -> "FakeMailbox":
        return FakeMailbox(self, account.id)


class FakeMailbox:
    def __init__(self, fake: Fake, account: str) -> None:
        self.fake, self.account = fake, account

    def check(self, deadline: CallDeadline) -> None: ...

    def get_message(self, ref: object, deadline: CallDeadline) -> MailDetail:
        if isinstance(ref, StableRef) and ref.message_id in self.fake.details:
            return self.fake.details[ref.message_id]
        raise ProviderError("not_found")

    def list_messages(self, query: ListQuery, deadline: CallDeadline) -> FolderPage:
        self.fake.queries.append((self.account, query))
        if self.account in self.fake.errors:
            raise self.fake.errors[self.account]
        matching = sorted(
            (
                m
                for m in self.fake.items.get(self.account, [])
                if m.received_at >= query.since and (query.position is None or query.position.precedes(*order_key(m)))
            ),
            key=order_key,
            reverse=True,
        )
        page = FolderPage(matching[: query.limit], len(matching) > query.limit, truncated=False, complete=True)
        return replace(page, **self.fake.flags.get(self.account, {}))


REFUSED: dict[str, Any] = {
    "truncated": True,
    "complete": False,
    "incomplete": frozenset({"search_refused"}),
    "refused_only": True,
}


def context(secrets_dir: Path, fake: Fake, env: dict[str, str] | None = None) -> HubContext:
    for ref in ("gmail-username", "gmail-app-password", "outlook-ms-client-id", *TOKEN_STORE_REFS):
        (secrets_dir / ref).write_text("placeholder")
    settings = load_settings({"HUB_SECRETS_DIR": str(secrets_dir)} | (env or {}))
    registry = load_registry(secrets_dir / "accounts.json")
    return HubContext(settings, registry, StatusStore(), Adapters(mailbox=fake.factory), CursorCodec(KEY, Clock()))


ARGUMENTS: dict[str, object] = {
    "query": None,
    "sender": None,
    "recipient": None,
    "subject": None,
    "account": None,
    "folder": None,
    "since": None,
    "limit": None,
    "cursor": None,
}


async def run(ctx: HubContext, **kwargs: object) -> tuple[SearchMessagesResult, list[str], str]:
    return await run_search_messages(ctx, now=NOW, **(ARGUMENTS | kwargs))  # type: ignore[arg-type]  # per test


def ids(result: SearchMessagesResult) -> list[tuple[str, int]]:
    pairs = []
    for item in result.items:
        assert item.id is not None
        ref = decode_message_id(item.id)
        assert isinstance(ref, MessageRef)
        pairs.append((item.account, ref.uid))
    return pairs


def incomplete(result: SearchMessagesResult) -> list[tuple[str, str]]:
    return [(i.account, i.reason) for i in result.incomplete_accounts]


async def test_defaults_and_terms_reach_the_adapter(secrets_dir: Path) -> None:
    fake = Fake(items={"icloud": [summary("icloud", 1, 10)]})
    result, accounts, outcome = await run(
        context(secrets_dir, fake), query="Invoice  Müller", sender="Jo", recipient="Al", subject="Q4"
    )
    assert ids(result) == [("icloud", 1)]
    assert result.untrusted_content_notice == UNTRUSTED_CONTENT_NOTICE
    assert (result.next_cursor, result.truncated, result.incomplete_accounts, result.account_errors) == (
        None,
        False,
        [],
        [],
    )
    assert (accounts, outcome) == (["icloud", "gmail", "outlook"], "ok")
    assert {account for account, _ in fake.queries} == {"icloud", "gmail", "outlook"}
    expected = ListQuery(
        folder="all",
        since=NOW - timedelta(days=30),
        unread_only=False,
        direction=None,
        limit=20,
        search=SearchTerms(words=("Invoice", "Müller"), sender="Jo", recipient="Al", subject="Q4"),
    )
    for _, query in fake.queries:
        assert query == expected


async def test_filter_only_search_and_explicit_arguments(secrets_dir: Path) -> None:
    fake = Fake()
    since = (NOW - timedelta(days=89)).isoformat()
    await run(context(secrets_dir, fake), sender="jo", folder="trash", since=since, limit=5, account="gmail")
    [(account, query)] = fake.queries
    assert account == "gmail"
    assert query.folder == "trash"
    assert query.limit == 5
    assert query.since == NOW - timedelta(days=89)
    assert query.search == SearchTerms(words=(), sender="jo", recipient=None, subject=None)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({}, "Pass query or at least one of sender, recipient, subject"),
        ({"query": "x" * 201}, "1 to 200 characters"),
        ({"sender": "a" * 201}, "1 to 200 characters"),
        ({"subject": "a\nb"}, "control characters"),  # Cc category
        ({"query": ""}, "1 to 200 characters"),
        ({"query": "X-GM-RAW foo"}, "must not contain X-GM-RAW"),
        ({"query": "x", "sender": "a X-GM-RAW b"}, "must not contain X-GM-RAW"),
        ({"query": "foo OR bar"}, "plain words"),
        ({"query": "from:alice"}, "plain words"),
        ({"query": "-spam"}, "plain words"),
        ({"query": "a b c d e f g h i"}, "at most 8 words"),
        ({"query": "report", "since": "2026-07-28T11:59:00Z"}, "since must lie within the last 90 days"),  # -91 d
        ({"query": "report", "since": "2026-10-27T12:00:00"}, "since"),
        ({"query": "report", "folder": "junk"}, "folder"),
        ({"query": "report", "folder": "INBOX"}, "folder"),
        ({"query": "report", "limit": 0}, "limit"),
        ({"query": "report", "limit": 51}, "limit"),
        ({"query": "report", "limit": True}, "limit"),
        ({"query": "report", "cursor": "garbage"}, "Cursor"),
    ],
)
async def test_validation_matrix_refuses_before_any_provider_call(
    secrets_dir: Path, kwargs: dict[str, Any], message: str
) -> None:
    fake = Fake()
    with pytest.raises(ToolError) as caught:
        await run(context(secrets_dir, fake), **kwargs)
    assert caught.value.code in ("invalid_argument", "invalid_cursor")
    assert message.lower() in caught.value.message.lower()
    assert fake.queries == []
    for value in (kwargs.get("query"), kwargs.get("sender"), kwargs.get("subject")):
        if isinstance(value, str) and len(value) > 4:
            assert value not in caught.value.message  # a refused value is never echoed


@pytest.mark.parametrize("text", ["*", "--", "@"])
async def test_empty_graph_query_is_refused_when_outlook_is_ready(secrets_dir: Path, text: str) -> None:
    fake = Fake()
    with pytest.raises(ToolError) as caught:
        await run(context(secrets_dir, fake), query=text)
    assert (caught.value.code, caught.value.message) == ("invalid_argument", EMPTY_GRAPH_QUERY)
    assert fake.queries == []


async def test_empty_graph_query_does_not_apply_without_a_graph_account(secrets_dir: Path) -> None:
    fake = Fake()
    await run(context(secrets_dir, fake), query="12:30", account="icloud")  # IMAP: searched (TDd-9)
    await run(context(secrets_dir, fake), query="*", account="gmail")
    assert [a for a, _ in fake.queries] == ["icloud", "gmail"]
    with pytest.raises(ToolError, match="Outlook"):  # `account: outlook` names the Graph account
        await run(context(secrets_dir, fake), query="*", account="outlook")
    with pytest.raises(ToolError) as unknown:
        await run(context(secrets_dir, fake), query="a", account="nobody")
    assert unknown.value.code == "unknown_account"


async def test_graph_query_too_long_is_refused_at_the_tool_layer(secrets_dir: Path) -> None:
    """G2: 35 distinct tokens exceed Graph's 32; the tool refuses before any adapter call, so the call is not a partial
    result with an `outlook` `upstream_error`."""
    fake = Fake()
    with pytest.raises(ToolError) as caught:
        await run(context(secrets_dir, fake), query=" ".join(f"w{n}/b/c/d/e" for n in range(7)))
    assert (caught.value.code, caught.value.message) == ("invalid_argument", GRAPH_QUERY_TOO_LONG)
    assert fake.queries == []


async def test_value_over_the_guard_bytes_after_nfc_is_refused_before_any_provider_call(secrets_dir: Path) -> None:
    """The guard would refuse the 1,600 bytes mid-call and the account would show as unhealthy."""
    fake = Fake()
    with pytest.raises(ToolError) as caught:
        await run(context(secrets_dir, fake), subject="\U0001d15e" * 200)
    assert (caught.value.code, caught.value.message) == ("invalid_argument", VALUE_TOO_LONG)
    assert fake.queries == []


async def test_empty_graph_check_only_applies_to_words(secrets_dir: Path) -> None:
    fake = Fake()
    await run(context(secrets_dir, fake), sender="@")  # a filter value is not sanitised for Graph
    assert {a for a, _ in fake.queries} == {"icloud", "gmail", "outlook"}


async def test_cursor_bound_to_search_arguments(secrets_dir: Path) -> None:
    fake = Fake(items={"icloud": [summary("icloud", uid, uid) for uid in range(1, 6)]})
    ctx = context(secrets_dir, fake)
    base: dict[str, object] = {"query": "Müller", "sender": "jo", "subject": "plan", "account": "icloud", "limit": 2}
    first, _, _ = await run(ctx, **base)
    cursor = first.next_cursor
    assert cursor is not None
    body = base64.urlsafe_b64decode(cursor.split(".")[1] + "==")
    assert b"M\xc3\xbcller" not in body
    for needle in (b"Muller", b"plan", b'jo"'):
        assert needle not in body
    again, _, _ = await run(ctx, **base, cursor=cursor)
    assert ids(again) == [("icloud", 3), ("icloud", 4)]
    other_limit, _, _ = await run(ctx, **(base | {"limit": 1}), cursor=cursor)
    assert ids(other_limit) == [("icloud", 3)]
    for change in (
        {"query": "other"},
        {"sender": "al"},
        {"subject": "other"},
        {"recipient": "al"},
        {"folder": "inbox"},
        {"since": (NOW - timedelta(days=30)).isoformat()},
        {"query": None},  # omitted vs given
        {"query": "Müller"},  # NFD: same normalised form as NFC, different raw form (TDr-8)
        {"account": None},
    ):
        with pytest.raises(ToolError) as caught:
            await run(ctx, **(base | change), cursor=cursor)
        assert caught.value.code == "invalid_cursor", change


async def test_cursor_of_another_tool_is_invalid(secrets_dir: Path) -> None:
    fake = Fake(items={"icloud": [summary("icloud", uid, uid) for uid in (1, 2, 3)]})
    ctx = context(secrets_dir, fake)
    foreign = ctx.cursors.issue(
        tool="list_messages", args_digest="0" * 64, window_start=1, position=Position(1, "0" * 16)
    )
    with pytest.raises(ToolError) as caught:
        await run(ctx, query="a", account="icloud", cursor=foreign)
    assert caught.value.code == "invalid_cursor"


async def test_imap_paging_with_graph_capped(secrets_dir: Path) -> None:
    """GD7: every page names the capped Graph account, the IMAP cursor continues, no duplicates, the sequence ends
    with next_cursor null and truncated true."""
    fake = Fake(
        items={
            "icloud": [summary("icloud", uid, uid * 10) for uid in range(1, 6)],
            "outlook": [summary("outlook", 9, 5, graph=True)],
        },
        flags={"outlook": {"truncated": True, "incomplete": frozenset({"result_cap"})}},
    )
    ctx = context(secrets_dir, fake)
    seen: list[str | None] = []
    cursor = None
    pages = 0
    while True:
        result, _, _ = await run(ctx, query="a", account=None, limit=2, cursor=cursor, folder="all")
        assert ("outlook", "result_cap") in incomplete(result)
        seen += [i.ref or f"{i.account}:{i.id}" for i in result.items]
        pages += 1
        cursor = result.next_cursor
        if cursor is None:
            break
        assert result.truncated is True
        assert pages < 10
    assert result.truncated is True  # the sequence ends truncated: the cap is not pageable
    assert len(seen) == len(set(seen)) == 6


async def test_graph_hits_have_no_id_and_round_trip_through_ref(secrets_dir: Path) -> None:
    """Owner decision A: a `$search` hit has `id: null` and a `ref`; get_message(ref) works and returns the immutable
    id; the search id is in no output."""
    hit = summary("outlook", 1, 5, graph=True, message_id="m1@example.test")
    fake = Fake(items={"outlook": [hit, replace(summary("outlook", 2, 6, graph=True), meta=MailMeta())]})
    ctx = context(secrets_dir, fake)
    first, _, _ = await run(ctx, query="invoice", account="outlook", limit=1)
    second, _, _ = await run(ctx, query="invoice", account="outlook", limit=1, cursor=first.next_cursor)
    [shown] = first.items
    assert shown.id is None
    assert shown.ref == encode_ref("outlook", "m1@example.test")
    [none_ref] = second.items
    assert (none_ref.id, none_ref.ref) == (None, None)  # no valid Message-ID: nothing to name it by
    dumped = first.model_dump_json() + second.model_dump_json() + str(first.next_cursor)
    assert SEARCH_ID not in dumped
    assert base64.urlsafe_b64encode(SEARCH_ID.encode()).decode().rstrip("=") not in dumped
    detail = MailDetail(
        ref=GraphMessageRef("outlook", IMMUTABLE_ID),
        received_at=NOW,
        unread=True,
        attachments=[],
        from_address="sender@example.test",
        from_name="Sender",
        to_addresses=[],
        cc_addresses=[],
        subject="Hi",
        body_text="Body",
        body_source="text/plain",
        body_cut=False,
        meta=MailMeta(message_id="m1@example.test"),
    )
    fake.details["m1@example.test"] = detail
    opened, _ = await run_get_message(ctx, ref=shown.ref, max_chars=None)
    assert opened.id == location_id(detail.ref)
    assert opened.id is not None
    # `k` is stable across pages: the same hit sorts to the same place with a different limit
    again, _, _ = await run(ctx, query="invoice", account="outlook", limit=2)
    assert next(i.ref for i in again.items) == shown.ref


async def test_filter_only_graph_hits_keep_their_id(secrets_dir: Path) -> None:
    fake = Fake(items={"outlook": [summary("outlook", 1, 5, graph=True, message_id="m1@example.test")]})
    result, _, _ = await run(context(secrets_dir, fake), sender="jo", account="outlook")
    [item] = result.items
    assert item.id is not None  # the list request of the filter-only path returns immutable ids


async def test_b8_matrix(secrets_dir: Path) -> None:
    ctx = context(secrets_dir, Fake())
    none, _, _ = await run(ctx, query="a")
    assert (none.items, none.truncated, none.next_cursor, none.incomplete_accounts) == ([], False, None, [])

    many = Fake(items={"icloud": [replace(summary("icloud", uid, uid), subject="S" * 300) for uid in range(1, 51)]})
    cut, _, _ = await run(context(secrets_dir, many, {"HUB_RESPONSE_BUDGET_CHARS": "10000"}), query="a", limit=50)
    assert cut.truncated
    assert cut.next_cursor is not None
    assert cut.incomplete_accounts == []

    cases: dict[str, tuple[dict[str, Any], str]] = {
        "badcharset": (REFUSED, "search_refused"),
        "soft_stop": ({"truncated": True, "complete": False, "incomplete": frozenset({"time_limit"})}, "time_limit"),
        "examine_no": (REFUSED, "search_refused"),
        "pass2_loss": (
            {"truncated": True, "complete": False, "incomplete": frozenset({"changed_during_call"})},
            "changed_during_call",
        ),
    }
    for name, (flags, reason) in cases.items():
        fake = Fake(flags={"icloud": flags})
        result, _, outcome = await run(context(secrets_dir, fake), query="a", account="icloud")
        assert incomplete(result) == [("icloud", reason)], name
        assert result.account_errors == [], name
        assert result.truncated is True, name
        assert result.next_cursor is None, name
        assert outcome == "ok", name

    failing = Fake(errors={"icloud": ProviderError("upstream_error")})
    result, _, _ = await run(context(secrets_dir, failing), query="a", account=None)
    assert [e.account for e in result.account_errors] == ["icloud"]
    assert "icloud" not in {a for a, _ in incomplete(result)}


async def test_incomplete_accounts_order_and_dedup(secrets_dir: Path) -> None:
    fake = Fake(
        flags={
            "outlook": {"complete": False, "truncated": True, "incomplete": frozenset({"time_limit", "result_cap"})},
            "gmail": {
                "complete": False,
                "truncated": True,
                "incomplete": frozenset({"changed_during_call", "search_refused", "result_cap"}),
            },
        }
    )
    result, _, _ = await run(context(secrets_dir, fake), query="a")
    assert incomplete(result) == [
        ("gmail", "result_cap"),
        ("gmail", "search_refused"),
        ("gmail", "changed_during_call"),
        ("outlook", "result_cap"),
        ("outlook", "time_limit"),
    ]


async def test_truncated_without_cursor_always_explained(secrets_dir: Path) -> None:
    """TDr-4: `truncated` with `next_cursor` null implies an `incomplete_accounts` or `account_errors` entry."""
    scenarios: list[Fake] = [
        Fake(),
        Fake(flags={"icloud": REFUSED}),
        Fake(flags={"icloud": {"truncated": True, "complete": False, "incomplete": frozenset({"time_limit"})}}),
        Fake(
            items={"outlook": [summary("outlook", 1, 5, graph=True)]},
            flags={"outlook": {"truncated": True, "complete": False, "incomplete": frozenset({"result_cap"})}},
        ),
        Fake(errors={"gmail": ProviderError("upstream_timeout")}),
        Fake(
            flags={"icloud": {"truncated": True, "complete": False, "incomplete": frozenset({"changed_during_call"})}}
        ),
    ]
    for fake in scenarios:
        result, _, _ = await run(context(secrets_dir, fake), query="a")
        if result.truncated and result.next_cursor is None:
            assert result.incomplete_accounts or result.account_errors, fake


async def test_refused_folder_does_not_block_the_cursor(secrets_dir: Path) -> None:
    """TDr-1: a refused folder is searched as far as it can be; other accounts keep paging."""
    fake = Fake(
        items={"gmail": [summary("gmail", uid, uid * 10) for uid in range(1, 7)]},
        flags={"icloud": REFUSED},
    )
    ctx = context(secrets_dir, fake)
    cursor = None
    pages = 0
    seen = 0
    while True:
        result, _, _ = await run(ctx, query="a", account=None, limit=2, cursor=cursor)
        assert ("icloud", "search_refused") in incomplete(result)
        seen += len(result.items)
        pages += 1
        cursor = result.next_cursor
        if cursor is None:
            break
        assert pages < 10
    assert (seen, pages) == (6, 3)
    assert result.truncated is True  # the refusal stays visible at the end


async def test_refused_folder_still_blocks_list_messages_cursor(secrets_dir: Path) -> None:
    fake = Fake(
        items={"gmail": [summary("gmail", uid, uid) for uid in (1, 2, 3)]},
        flags={"icloud": REFUSED},
    )
    result, _, _ = await run_list_messages(
        context(secrets_dir, fake),
        tool="list_messages",
        account=None,
        folder=None,
        since=None,
        unread_only=None,
        direction=None,
        limit=2,
        cursor=None,
        now=NOW,
    )
    assert result.next_cursor is None
    assert result.truncated is True
