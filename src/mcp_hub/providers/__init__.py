"""Provider adapters (spec 080 §6): factories per capability and the per-account connection limit."""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Final, Protocol

from mcp_hub.ids import MessageRef
from mcp_hub.providers.base import AccountLimiters, MailDetail, UnreadPage, read_credential
from mcp_hub.providers.imap import ImapMailbox
from mcp_hub.registry import Account, Capability, ImapMail
from mcp_hub.registry import Protocol as WireProtocol

# Protocols with an adapter; accounts on other protocols are skipped (spec 080 rev. 4.4 §5.1).
SUPPORTED_PROTOCOLS: Final[dict[Capability, frozenset[WireProtocol]]] = {
    "mail": frozenset({"imap"}),
    "calendar": frozenset(),
}


class Mailbox(Protocol):
    def check(self) -> None: ...
    def list_unread(self, since: datetime, limit: int) -> UnreadPage: ...
    def get_message(self, ref: MessageRef) -> MailDetail: ...


MailboxFactory = Callable[[Account, Path], Mailbox]


def open_mailbox(account: Account, secrets_dir: Path) -> Mailbox:
    """Credentials are read now, i.e. when a new connection opens (spec 080 §7.1)."""
    block = account.mail
    if not isinstance(block, ImapMail):
        raise ValueError("account has no IMAP mail block")  # ready_accounts filters on SUPPORTED_PROTOCOLS first
    return ImapMailbox(
        account.id,
        host=block.host,
        port=block.port,
        folder=block.inbox,
        username=read_credential(secrets_dir, block.username_ref),
        password=read_credential(secrets_dir, block.password_ref),
    )


@dataclass(frozen=True, slots=True)
class Adapters:
    mailbox: MailboxFactory = open_mailbox
    limiters: AccountLimiters = field(default_factory=AccountLimiters)
