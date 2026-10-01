from datetime import UTC, datetime
from pathlib import Path

import anyio
import pytest

from mcp_hub.config import load_settings
from mcp_hub.errors import ToolError
from mcp_hub.health import StatusStore
from mcp_hub.providers.base import ProviderError
from mcp_hub.registry import Account, load_registry
from mcp_hub.tools import HubContext
from mcp_hub.tools.common import gather_accounts, int_argument, parse_timestamp, ready_accounts

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def ctx(secrets_dir: Path) -> HubContext:
    settings = load_settings({"HUB_SECRETS_DIR": str(secrets_dir)})
    return HubContext(settings, load_registry(secrets_dir / "accounts.json"), StatusStore())


def test_ready_accounts_skips_missing_credentials_and_unsupported_protocols(secrets_dir: Path) -> None:
    # conftest writes icloud credentials only: gmail (IMAP, no files) and outlook (Graph, no adapter) are skipped.
    assert [a.id for a in ready_accounts(ctx(secrets_dir), "mail", None)] == ["icloud"]


@pytest.mark.parametrize(
    ("account", "code"),
    [
        ("gmail", "capability_unavailable"),
        ("outlook", "capability_unavailable"),
        ("nobody", "unknown_account"),
        ("uzh", "capability_unavailable"),
    ],
)
def test_named_accounts_that_cannot_serve_are_tool_errors(secrets_dir: Path, account: str, code: str) -> None:
    with pytest.raises(ToolError) as caught:
        ready_accounts(ctx(secrets_dir), "mail", account)
    assert caught.value.code == code


def test_ready_accounts_for_calendar_skips_graph_accounts(secrets_dir: Path) -> None:
    # icloud has CalDAV credentials; uzh is disabled; gmail and outlook lack the calendar capability (no error).
    assert [a.id for a in ready_accounts(ctx(secrets_dir), "calendar", None)] == ["icloud"]
    with pytest.raises(ToolError) as caught:
        ready_accounts(ctx(secrets_dir), "calendar", "gmail")
    assert caught.value.code == "capability_unavailable"


async def test_gather_records_status_and_reports_failures(secrets_dir: Path) -> None:
    for ref in ("gmail-username", "gmail-app-password"):
        (secrets_dir / ref).write_text("placeholder")
    context = ctx(secrets_dir)
    accounts = ready_accounts(context, "mail", None)

    async def call(account: Account) -> list[str]:
        if account.id == "gmail":
            raise ProviderError("auth_expired", "LoginError")
        return ["item"]

    gathered = await gather_accounts(accounts, "mail", call, context.status)
    assert [(a.id, r) for a, r in gathered.results] == [("icloud", ["item"])]
    assert [(e.account, e.code, e.message) for e in gathered.errors] == [
        ("gmail", "auth_expired", "Credential rejected by provider; re-login required")
    ]
    assert context.status.get("icloud", "mail").status == "ok"
    assert context.status.get("gmail", "mail").status == "auth_expired"


async def test_gather_deadline_turns_slow_accounts_into_upstream_timeout(secrets_dir: Path) -> None:
    context = ctx(secrets_dir)

    async def slow(account: Account) -> list[str]:
        await anyio.sleep(5)
        return []

    gathered = await gather_accounts(ready_accounts(context, "mail", None), "mail", slow, context.status, deadline=0.1)
    assert [(e.account, e.code) for e in gathered.errors] == [("icloud", "upstream_timeout")]
    assert context.status.get("icloud", "mail").status == "unreachable"


async def test_unexpected_exception_is_upstream_error_without_text(secrets_dir: Path) -> None:
    context = ctx(secrets_dir)

    async def boom(account: Account) -> list[str]:
        raise RuntimeError("provider said something secret")

    gathered = await gather_accounts(ready_accounts(context, "mail", None), "mail", boom, context.status)
    assert [(e.code, e.message) for e in gathered.errors] == [("upstream_error", "Provider returned an error")]


@pytest.mark.parametrize(
    "value",
    [
        "2026-09-29T07:00:00",
        "yesterday",
        "2026-13-01T00:00:00Z",
        "",
        "2026-09-29 07:00:00Z",
        "20260929T070000Z",
        "2026-W40-1T07:00:00Z",
    ],
)
def test_timestamps_without_offset_or_malformed_are_invalid(value: str) -> None:
    with pytest.raises(ToolError) as caught:
        parse_timestamp(value, "since")
    assert caught.value.code == "invalid_argument"


def test_timestamp_with_offset_or_z_is_accepted() -> None:
    assert parse_timestamp("2026-09-29T07:00:00Z", "since") == datetime(2026, 9, 29, 7, tzinfo=UTC)
    assert parse_timestamp("2026-09-29T09:00:00+02:00", "since") == datetime(2026, 9, 29, 7, tzinfo=UTC)


@pytest.mark.parametrize("value", [0, 51, True, -1])
def test_int_argument_bounds(value: int) -> None:
    with pytest.raises(ToolError):
        int_argument(value, "limit", default=20, low=1, high=50)
    assert int_argument(None, "limit", default=20, low=1, high=50) == 20
