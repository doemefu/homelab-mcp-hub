"""list_accounts (spec 080 §5.2): registry metadata + in-memory status; no provider call on the request path."""

import time
from datetime import datetime
from typing import Final

from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel

from mcp_hub.errors import ErrorCode
from mcp_hub.health import HealthStatus, capability_status
from mcp_hub.registry import CAPABILITIES, Capability, Protocol, Provider
from mcp_hub.tools import HubContext
from mcp_hub.tools.common import UNTRUSTED_CONTENT_NOTICE, AccountErrorItem, log_tool_call, read_only

DESCRIPTION: Final = (
    "List the configured mail and calendar accounts: id, label, provider and, per capability (mail, calendar), "
    "the protocol and whether it currently works (ok, auth_expired, unreachable, error, unknown, disabled). "
    "Call this first. Read-only; it never contacts a provider. Use the ids as the account argument of other tools."
)


class CapabilityInfo(BaseModel):
    capability: Capability
    protocol: Protocol
    status: HealthStatus
    last_success_at: str | None
    last_error_at: str | None
    last_error_code: ErrorCode | None


class AccountInfo(BaseModel):
    id: str
    label: str
    provider: Provider
    capabilities: list[CapabilityInfo]


class ListAccountsResult(BaseModel):
    untrusted_content_notice: str
    default_timezone: str
    accounts: list[AccountInfo]
    account_errors: list[AccountErrorItem]


def _iso(value: datetime | None) -> str | None:
    return value.isoformat(timespec="seconds") if value else None


def build_list_accounts(ctx: HubContext) -> ListAccountsResult:
    accounts = []
    for account in ctx.registry.accounts:
        capabilities = []
        for capability in CAPABILITIES:
            if not account.has(capability):
                continue
            state = capability_status(account, capability, ctx.settings.secrets_dir, ctx.status)
            capabilities.append(
                CapabilityInfo(
                    capability=capability,
                    protocol=account.protocol(capability),
                    status=state.status,
                    last_success_at=_iso(state.last_success_at),
                    last_error_at=_iso(state.last_error_at),
                    last_error_code=state.last_error_code,
                )
            )
        accounts.append(
            AccountInfo(id=account.id, label=account.label, provider=account.provider, capabilities=capabilities)
        )
    return ListAccountsResult(
        untrusted_content_notice=UNTRUSTED_CONTENT_NOTICE,
        default_timezone=ctx.settings.default_timezone,
        accounts=accounts,
        account_errors=[],
    )


def register(server: MCPServer, ctx: HubContext) -> None:
    @server.tool(name="list_accounts", description=DESCRIPTION, annotations=read_only("List accounts"))
    async def list_accounts() -> ListAccountsResult:
        started = time.perf_counter()
        result = build_list_accounts(ctx)
        log_tool_call(
            "list_accounts",
            started=started,
            outcome="ok",
            accounts=[a.id for a in result.accounts],
            result_count=len(result.accounts),
        )
        return result
