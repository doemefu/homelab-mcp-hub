from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from mcp_hub.config import load_settings
from mcp_hub.errors import ToolError
from mcp_hub.health import StatusStore
from mcp_hub.ids import AnyMessageRef, GraphMessageRef, MessageRef, encode_message_id
from mcp_hub.providers import SUPPORTED_PROTOCOLS, Adapters
from mcp_hub.providers.base import MailDetail, MailSummary, ProviderError, UnreadPage
from mcp_hub.registry import Account, load_registry
from mcp_hub.tools import HubContext
from mcp_hub.tools.mail import run_get_message, run_list_unread

pytestmark = pytest.mark.anyio
NOW = datetime(2026, 9, 29, 7, 0, tzinfo=UTC)
SURROGATE = "x\ud800y"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def ready(secrets_dir: Path) -> None:
    for ref in ("outlook-ms-client-id", "db-username", "db-password", "token-encryption-key"):
        (secrets_dir / ref).write_text("placeholder")


def summary(account: str, n: int, *, graph: bool, text: str = "Hi") -> MailSummary:
    ref: AnyMessageRef = GraphMessageRef(account, f"AAMkMSG{n:04d}=") if graph else MessageRef(account, "INBOX", 7, n)
    return MailSummary(
        ref=ref,
        received_at=NOW - timedelta(minutes=n),
        unread=True,
        has_attachments=False,
        from_address=f"{text}@example.test",
        from_name=text,
        subject=text * 300,
        snippet_text=text * 300,
    )


class Box:
    def __init__(self, account: Account, pages: dict[str, UnreadPage | Exception], detail: MailDetail | None) -> None:
        self.account, self.pages, self.detail = account, pages, detail
        self.asked: list[AnyMessageRef] = []

    def check(self) -> None: ...

    def list_unread(self, since: datetime, limit: int) -> UnreadPage:
        page = self.pages[self.account.id]
        if isinstance(page, Exception):
            raise page
        return page

    def get_message(self, ref: AnyMessageRef) -> MailDetail:
        self.asked.append(ref)
        assert self.detail is not None
        return self.detail


def ctx(
    secrets_dir: Path,
    pages: dict[str, UnreadPage | Exception],
    detail: MailDetail | None = None,
    budget: str = "10000",
    opened: list[str] | None = None,
) -> HubContext:
    ready(secrets_dir)
    settings = load_settings({"HUB_SECRETS_DIR": str(secrets_dir), "HUB_RESPONSE_BUDGET_CHARS": budget})

    def mailbox(account: Account, _dir: Path) -> Box:
        if opened is not None:
            opened.append(account.id)
        return Box(account, pages, detail)

    return HubContext(settings, load_registry(secrets_dir / "accounts.json"), StatusStore(), Adapters(mailbox=mailbox))


def test_graph_is_a_supported_mail_protocol() -> None:
    assert SUPPORTED_PROTOCOLS["mail"] == frozenset({"imap", "graph"})


async def test_two_accounts_budget_and_isolation(secrets_dir: Path) -> None:
    pages: dict[str, UnreadPage | Exception] = {
        "icloud": UnreadPage([summary("icloud", n, graph=False) for n in range(1, 51)], False),
        "outlook": UnreadPage([summary("outlook", n, graph=True, text=SURROGATE) for n in range(1, 51)], False),
    }
    result, accounts, outcome = await run_list_unread(
        ctx(secrets_dir, pages), account=None, since=None, limit=50, now=NOW
    )
    text = result.model_dump_json()
    assert len(text) <= 10_000
    assert result.truncated
    assert "\ud800" not in text
    assert "\\ud800" not in text
    assert set(accounts) == {"icloud", "outlook"}
    assert outcome == "ok"
    assert {i.account for i in result.items} == {"icloud", "outlook"}
    assert all(i.folder == "inbox" for i in result.items if i.account == "outlook")


async def test_one_account_failing_keeps_the_other(secrets_dir: Path) -> None:
    pages: dict[str, UnreadPage | Exception] = {
        "icloud": UnreadPage([summary("icloud", 1, graph=False)], False),
        "outlook": ProviderError("auth_expired", "InvalidGrant"),
    }
    result, _, outcome = await run_list_unread(ctx(secrets_dir, pages), account=None, since=None, limit=20, now=NOW)
    assert outcome == "partial"
    assert [e.account for e in result.account_errors] == ["outlook"]
    assert result.account_errors[0].code == "auth_expired"
    assert len(result.items) == 1


@pytest.mark.parametrize(
    "message_id",
    [
        encode_message_id(MessageRef("outlook", "INBOX", 1, 1)),
        encode_message_id(MessageRef("outlook", "inbox", 1, 1)),
        encode_message_id(GraphMessageRef("icloud", "AAMkMSG0001=")),
    ],
)
async def test_cross_kind_ids_are_not_found_without_provider_call(secrets_dir: Path, message_id: str) -> None:
    opened: list[str] = []
    with pytest.raises(ToolError) as info:
        await run_get_message(ctx(secrets_dir, {}, opened=opened), message_id=message_id, max_chars=None)
    assert info.value.code == "not_found"
    assert opened == []


async def test_get_message_graph_result_folder_inbox(secrets_dir: Path) -> None:
    ref = GraphMessageRef("outlook", "AAMkMSG0001=")
    detail = MailDetail(
        ref=ref,
        received_at=NOW,
        unread=True,
        attachments=[],
        from_address="a@example.test",
        from_name=SURROGATE,
        to_addresses=[],
        cc_addresses=[],
        subject="S",
        body_text="B" + SURROGATE,
        body_source="text/plain",
        body_cut=False,
    )
    opened: list[str] = []
    result, _ = await run_get_message(
        ctx(secrets_dir, {}, detail, opened=opened), message_id=encode_message_id(ref), max_chars=None
    )
    assert result.folder == "inbox"
    assert result.account == "outlook"
    assert opened == ["outlook"]
    dumped = result.model_dump_json()
    assert "\ud800" not in dumped
    assert "\\ud800" not in dumped
