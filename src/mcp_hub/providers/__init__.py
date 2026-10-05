"""Provider adapters (spec 080 §6): factories per capability and the per-account connection limit."""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Final, Protocol
from zoneinfo import ZoneInfo

from mcp_hub.ids import AnyMessageRef
from mcp_hub.providers.base import AccountLimiters, MailDetail, UnreadPage, read_credential
from mcp_hub.providers.caldav import CalDavCalendarSource, CalendarPage
from mcp_hub.providers.graph import GraphMailbox
from mcp_hub.providers.graph_auth import GraphTokenSource
from mcp_hub.providers.imap import ImapMailbox
from mcp_hub.providers.msidentity import GraphAccount
from mcp_hub.registry import Account, CalDavCalendar, Capability, GraphBlock, ImapMail
from mcp_hub.registry import Protocol as WireProtocol
from mcp_hub.tokenstore.store import StoreConfig, TokenStore

# Protocols with an adapter; accounts on other protocols are skipped (spec 080 rev. 4.4 §5.1).
SUPPORTED_PROTOCOLS: Final[dict[Capability, frozenset[WireProtocol]]] = {
    "mail": frozenset({"imap", "graph"}),
    "calendar": frozenset({"caldav"}),
}


class Mailbox(Protocol):
    def check(self) -> None: ...
    def list_unread(self, since: datetime, limit: int) -> UnreadPage: ...
    def get_message(self, ref: AnyMessageRef) -> MailDetail: ...


MailboxFactory = Callable[[Account, Path], Mailbox]


class MailboxOpener:
    """Builds the mail adapter for an account. Credentials are read now, i.e. when a new connection opens (§7.1).
    Graph accounts need the token store; without a store configuration the opener refuses them."""

    def __init__(self, store: StoreConfig | None = None) -> None:
        self._store = store

    def __call__(self, account: Account, secrets_dir: Path) -> Mailbox:
        block = account.mail
        if isinstance(block, ImapMail):
            return ImapMailbox(
                account.id,
                host=block.host,
                port=block.port,
                folder=block.inbox,
                username=read_credential(secrets_dir, block.username_ref),
                password=read_credential(secrets_dir, block.password_ref),
            )
        if isinstance(block, GraphBlock) and account.graph is not None and self._store is not None:
            graph = GraphAccount(
                account.id,
                account.provider,
                account.graph.tenant,
                read_credential(secrets_dir, account.graph.client_id_ref),
                tuple(account.graph.scopes),
            )
            return GraphMailbox(account.id, GraphTokenSource(graph, TokenStore(self._store)))
        raise ValueError("account has no supported mail block")  # ready_accounts filters on SUPPORTED_PROTOCOLS first


open_mailbox = MailboxOpener()


class CalendarSource(Protocol):
    def check(self) -> None: ...
    def events(self, start: datetime, end: datetime, zone: ZoneInfo, floating: ZoneInfo) -> CalendarPage: ...


CalendarFactory = Callable[[Account, Path], CalendarSource]


def open_calendar(account: Account, secrets_dir: Path) -> CalendarSource:
    """Credentials are read now, i.e. when a new connection opens (spec 080 §7.1)."""
    block = account.calendar
    if not isinstance(block, CalDavCalendar):
        raise ValueError("account has no CalDAV calendar block")  # ready_accounts filters on SUPPORTED_PROTOCOLS first
    return CalDavCalendarSource(
        account.id,
        url=block.url,
        username=read_credential(secrets_dir, block.username_ref),
        password=read_credential(secrets_dir, block.password_ref),
        include=block.include_calendars,
    )


@dataclass(frozen=True, slots=True)
class Adapters:
    mailbox: MailboxFactory = open_mailbox
    calendar: CalendarFactory = open_calendar
    limiters: AccountLimiters = field(default_factory=AccountLimiters)
