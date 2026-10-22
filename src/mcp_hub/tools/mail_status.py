"""get_message_status (spec 080 §5.2, §5.4): re-check stored refs and ids. A status item carries no third-party text
(only opaque ids, the account id, enums, booleans and timestamps), so it has no `untrusted` object (A-td6)."""

import time
from collections.abc import Callable
from datetime import datetime
from functools import partial
from typing import Annotated, Final, Self
from zoneinfo import ZoneInfo

from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult
from pydantic import BaseModel, Field, model_validator

from mcp_hub.budget import HARD_MAX_CHARS, fit_items
from mcp_hub.errors import ToolError
from mcp_hub.ids import GraphMessageRef, MessageRef, StableRef, decode_status_entry, encode_ref, location_id
from mcp_hub.providers.base import (
    ACCOUNT_ERROR_MESSAGES,
    Direction,
    MessageStatus,
    ProviderError,
    StatusReason,
    StatusState,
    StatusTarget,
    run_blocking,
    status_unknown,
)
from mcp_hub.providers.deadline import CallDeadline
from mcp_hub.providers.folders import FolderMap, MailFolder
from mcp_hub.registry import Account, GraphBlock, ImapMail
from mcp_hub.tools import HubContext
from mcp_hub.tools.common import (
    UNTRUSTED_CONTENT_NOTICE,
    AccountErrorItem,
    gather_accounts,
    log_tool_call,
    read_only,
    ready_accounts,
    structured_result,
    tool_outcome,
)
from mcp_hub.tools.mail import Flags

MAX_STATUS_ENTRIES: Final = 50
STATUS_BUDGET_FLOOR: Final = 50_000  # A-td6: budget = min(max(settings.response_budget_chars, 50_000), HARD_MAX_CHARS)
_REFS_MESSAGE: Final = f"refs must be a list of 1 to {MAX_STATUS_ENTRIES} strings"
# Advertised as a required array of strings, but typed permissively so that the SDK passes any value through and the
# hub answers every malformed one with the §5.1 body (C11, TD-R3). The SDK still decodes a JSON-encoded list string.
RefsArgument = Annotated[
    object,
    Field(
        json_schema_extra={
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
            "maxItems": MAX_STATUS_ENTRIES,
        }
    ),
]
GET_MESSAGE_STATUS_DESCRIPTION: Final = (
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


class StatusItem(BaseModel):
    """Field order follows the spec bullets; `input` is left out of the output unless it differs from `ref`."""

    input: str | None = Field(default=None, exclude_if=lambda value: value is None)
    ref: str | None
    account: str
    state: StatusState
    reason: StatusReason | None
    id: str | None
    folder: MailFolder | None
    received_at: str | None
    direction: Direction | None
    flags: Flags | None
    replied: bool
    replied_at: str | None
    reply_ref: str | None
    newer_incoming_in_thread: bool
    newer_incoming_at: str | None
    thread_checked: bool | None

    @model_validator(mode="after")
    def _null_rules(self) -> Self:
        """§5.2: a reason exactly for unknown; the copy, reply and thread fields only when found (E-td1)."""
        if (self.reason is None) != (self.state != "unknown"):
            raise ValueError("reason is set exactly when state is unknown")
        found = self.state == "found"
        copy = (self.folder, self.received_at, self.direction, self.flags, self.thread_checked)
        if found and any(value is None for value in copy):
            raise ValueError("a found item needs folder, received_at, direction, flags and thread_checked")
        if not found and (
            self.id is not None
            or any(value is not None for value in copy)
            or self.replied
            or self.newer_incoming_in_thread
            or self.replied_at is not None
            or self.reply_ref is not None
            or self.newer_incoming_at is not None
        ):
            raise ValueError("only a found item has copy, reply and thread fields")
        if self.reply_ref is not None and self.replied_at is None:
            raise ValueError("reply_ref needs replied_at")
        if self.replied_at is not None and not self.replied:
            raise ValueError("replied_at needs replied")
        if self.newer_incoming_in_thread != (self.newer_incoming_at is not None):
            raise ValueError("newer_incoming_in_thread is set exactly with newer_incoming_at")
        return self


class MessageStatusResult(BaseModel):
    untrusted_content_notice: str
    items: list[StatusItem]
    next_cursor: None
    truncated: bool
    account_errors: list[AccountErrorItem]


def _local(value: datetime, zone: ZoneInfo) -> str:
    return value.astimezone(zone).isoformat(timespec="seconds")


def _echo(entry: str, ref: str | None) -> str | None:
    return None if entry == ref else entry


def found_item(entry: str, ref: str | None, account: str, status: MessageStatus, zone: ZoneInfo) -> StatusItem:
    found = status.found
    if status.state != "found" or found is None or status.thread_checked is None:
        raise ValueError("found_item needs a found status")
    meta = found.meta
    reply_ref = None
    if status.reply_at is not None and status.reply_message_id is not None:
        reply_ref = encode_ref(account, status.reply_message_id)
    return StatusItem(
        input=_echo(entry, ref),
        ref=ref,
        account=account,
        state="found",
        reason=None,
        id=location_id(found.location),
        folder=meta.folder,
        received_at=_local(found.received_at, zone),
        direction=meta.direction,
        flags=Flags(seen=not found.unread, answered=meta.flags.answered, flagged=meta.flags.flagged),
        replied=meta.flags.answered or status.reply_at is not None,
        replied_at=None if status.reply_at is None else _local(status.reply_at, zone),
        reply_ref=reply_ref,
        newer_incoming_in_thread=status.newer_incoming_at is not None,
        newer_incoming_at=None if status.newer_incoming_at is None else _local(status.newer_incoming_at, zone),
        thread_checked=status.thread_checked,
    )


def _not_found(
    entry: str, ref: str | None, account: str, state: StatusState, reason: StatusReason | None
) -> StatusItem:
    return StatusItem(
        input=_echo(entry, ref),
        ref=ref,
        account=account,
        state=state,
        reason=reason,
        id=None,
        folder=None,
        received_at=None,
        direction=None,
        flags=None,
        replied=False,
        replied_at=None,
        reply_ref=None,
        newer_incoming_in_thread=False,
        newer_incoming_at=None,
        thread_checked=None,
    )


def gone_item(entry: str, ref: str | None, account: str) -> StatusItem:
    return _not_found(entry, ref, account, "gone", None)


def unknown_item(entry: str, ref: str | None, account: str, reason: StatusReason) -> StatusItem:
    return _not_found(entry, ref, account, "unknown", reason)


def _entries(refs: object) -> list[tuple[str, StatusTarget]]:
    """Every entry is decoded before any provider call; one bad entry fails the whole call (§5.2). The error names
    the position, never the value."""
    if not isinstance(refs, list) or not 1 <= len(refs) <= MAX_STATUS_ENTRIES:
        raise ToolError("invalid_argument", _REFS_MESSAGE)
    entries: list[tuple[str, StatusTarget]] = []
    for index, value in enumerate(refs):
        message = f"refs[{index}] is not a valid ref or message id"
        if not isinstance(value, str):
            raise ToolError("invalid_argument", message)
        try:
            entries.append((value, decode_status_entry(value)))
        except ToolError:
            raise ToolError("invalid_argument", message) from None
    return entries


def _precheck(ctx: HubContext, account: Account, target: StatusTarget) -> MessageStatus | None:
    """What the tool can decide without a provider call (§5.2, D75, C4): an id of the other protocol is id_mismatch;
    an IMAP id naming a mailbox that the cached folder map refuses is stale_id (never opened). The registry inbox, an
    unknown name or no cached map is left to the adapter."""
    mail = account.mail
    if isinstance(target, MessageRef):
        if not isinstance(mail, ImapMail):
            return status_unknown("id_mismatch")
        if target.folder == mail.inbox:
            return None
        cached = ctx.adapters.folder_maps.get(account.id)
        if isinstance(cached, FolderMap) and cached.binding(target.folder) == "refused":
            return status_unknown("stale_id")
    elif isinstance(target, GraphMessageRef) and not isinstance(mail, GraphBlock):
        return status_unknown("id_mismatch")
    return None


def _item(entry: str, target: StatusTarget, status: MessageStatus, zone: ZoneInfo) -> StatusItem:
    if isinstance(target, StableRef):
        ref = encode_ref(target.account, target.message_id)  # the key given, in its canonical form
    elif status.found is not None and status.found.message_id is not None:
        ref = encode_ref(target.account, status.found.message_id)  # the key of the copy found
    else:
        ref = None
    if status.state == "found":
        return found_item(entry, ref, target.account, status, zone)
    if status.state == "gone":
        return gone_item(entry, ref, target.account)
    if status.reason is None:
        raise ValueError("an unknown status needs a reason")
    return unknown_item(entry, ref, target.account, status.reason)


def _never_shrink(_candidate: StatusItem, _fits: Callable[[StatusItem], bool]) -> StatusItem | None:
    return None  # no untrusted text to shorten: a status item is returned whole or not at all


async def run_get_message_status(ctx: HubContext, *, refs: object) -> tuple[MessageStatusResult, list[str], str]:
    """Returns the result, the queried account ids and the outcome for the tool_call log line.

    Every entry is validated and pre-checked before any provider call. Per account one provider call over its
    distinct targets in first-seen order; duplicates are answered at each of their positions, in input order."""
    entries = _entries(refs)
    ready = {account.id: account for account in ready_accounts(ctx, "mail", None)}
    decided: list[MessageStatus | None] = []
    targets: dict[str, list[StatusTarget]] = {}
    for _, target in entries:
        account = ready.get(target.account)
        # Unknown, disabled, mail-less, adapter-less or without credentials: no provider call, no limiter slot, no
        # status record, no account_errors entry (security S4).
        status = status_unknown("account_unavailable") if account is None else _precheck(ctx, account, target)
        decided.append(status)
        if status is None and target not in targets.setdefault(target.account, []):
            targets[target.account].append(target)
    queried = [ready[account_id] for account_id in targets]

    async def call(chosen: Account) -> list[MessageStatus]:
        wanted = targets[chosen.id]
        opened = partial(ctx.adapters.mailbox, chosen, ctx.settings.secrets_dir)
        deadline = CallDeadline.start()  # R2a: started here, so a slot wait counts against the limits
        statuses = await run_blocking(
            lambda: opened().message_status(wanted, deadline), slot=ctx.adapters.limiters.get(chosen.id)
        )
        if len(statuses) != len(wanted):  # one answer per target, in order; anything else fails the account
            raise ProviderError("upstream_error", "StatusCountMismatch")
        return statuses

    gathered = await gather_accounts(queried, "mail", call, ctx.status)
    answers: dict[StatusTarget, MessageStatus] = {}
    for account, statuses in gathered.results:
        answers.update(zip(targets[account.id], statuses, strict=True))
    failed = status_unknown("account_error")  # the account is listed in account_errors
    zone = ZoneInfo(ctx.settings.default_timezone)
    items = [
        _item(entry, target, status if status is not None else answers.get(target, failed), zone)
        for (entry, target), status in zip(entries, decided, strict=True)
    ]

    def build(chosen: list[StatusItem], cut: bool) -> MessageStatusResult:
        return MessageStatusResult(
            untrusted_content_notice=UNTRUSTED_CONTENT_NOTICE,
            items=chosen,
            next_cursor=None,
            truncated=cut,  # the budget cut only; soft stops and caps are reported per item
            account_errors=gathered.errors,
        )

    budget = min(max(ctx.settings.response_budget_chars, STATUS_BUDGET_FLOOR), HARD_MAX_CHARS)
    result = fit_items(items, build, budget, shrink=_never_shrink)
    return result, [account.id for account in queried], tool_outcome(gathered)


def register(server: MCPServer, ctx: HubContext) -> None:
    @server.tool(
        name="get_message_status",
        description=GET_MESSAGE_STATUS_DESCRIPTION,
        annotations=read_only("Re-check messages"),
    )
    async def get_message_status(refs: RefsArgument) -> Annotated[CallToolResult, MessageStatusResult]:
        started = time.perf_counter()
        try:
            result, accounts, outcome = await run_get_message_status(ctx, refs=refs)
        except ToolError as exc:
            log_tool_call("get_message_status", started=started, outcome=exc.code, accounts=[], result_count=0)
            return exc.to_result()
        except Exception:  # a hub bug still answers with the §5.1 body, never the SDK's generic text (SR3)
            log_tool_call("get_message_status", started=started, outcome="upstream_error", accounts=[], result_count=0)
            return ToolError("upstream_error", ACCOUNT_ERROR_MESSAGES["upstream_error"]).to_result()
        log_tool_call(
            "get_message_status", started=started, outcome=outcome, accounts=accounts, result_count=len(result.items)
        )
        return structured_result(result)
