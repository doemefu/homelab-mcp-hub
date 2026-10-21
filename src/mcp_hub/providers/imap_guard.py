"""The hub's read-only IMAP boundary (spec 080 §5.1, S5): every command and argument passes this guard.

Commands: CAPABILITY, LOGIN, LIST, EXAMINE, UID SEARCH, UID FETCH, NOOP, LOGOUT. Inputs are checked before anything is
sent, and the call deadline and socket timeout are applied before every command except LOGOUT. The raw client stays in
a private slot; no write method exists here.
"""

import imaplib
import re
import socket
from collections.abc import Sequence
from typing import Final, Literal, Protocol

from imapclient.exceptions import InvalidCriteriaError

from mcp_hub.ids import mailbox_name_ok
from mcp_hub.providers.base import ProviderError
from mcp_hub.providers.deadline import CallDeadline, SoftStop

SECTION: Final = re.compile(r"[0-9.]+|TEXT|HEADER\.FIELDS \([A-Z -]+\)")
PARTIAL: Final = re.compile(r"<0\.([0-9]{1,6})>")
MAX_PARTIAL_BYTES: Final = 262_144
PLAIN_ITEMS: Final = frozenset({"FLAGS", "INTERNALDATE", "BODYSTRUCTURE", "X-GM-LABELS", "X-GM-THRID"})
MAX_UID: Final = 2**32 - 1  # nz-number (RFC 3501)
MIN_SOCKET_TIMEOUT: Final = 0.5

_PEEK: Final = re.compile(r"BODY\.PEEK\[([^\]]*)\](<[^>]*>)")
_PRINTABLE: Final = re.compile(rb"[\x20-\x7e]+")
_PRINTABLE_TEXT: Final = re.compile(r"[\x20-\x7e]+")
# The library builds the leading words of these texts from the command name; only that prefix and the status token
# are read. imaplib reports a tagged BAD as "<CMD> command error: BAD …"; IMAPClient 4.1.0 reports a tagged NO as
# "<cmd> failed: …" (`_check_resp`). The test fakes use "<CMD> command error: NO|BAD …".
_STATUS_ERROR: Final = re.compile(r"[A-Za-z]+(?: [A-Za-z]+)? command error: (NO|BAD)\b.*", re.DOTALL)
_FAILED: Final = re.compile(r"[A-Za-z]+(?: [A-Za-z]+)? failed: .*", re.DOTALL)


class ImapClientLike(Protocol):
    normalise_times: bool
    folder_encode: bool
    use_uid: bool

    def socket(self) -> socket.socket: ...
    def capabilities(self) -> tuple[bytes, ...]: ...
    def login(self, username: str, password: str) -> object: ...
    def list_folders(
        self, directory: str = "", pattern: str = "*"
    ) -> list[tuple[tuple[bytes, ...], bytes | None, bytes | str | int]]: ...
    def select_folder(self, folder: str, readonly: bool = False) -> dict[bytes, object]: ...
    def search(self, criteria: bytes) -> list[int]: ...
    def fetch(self, messages: Sequence[int], data: Sequence[str]) -> dict[int, dict[bytes, object]]: ...
    def noop(self) -> object: ...
    def logout(self) -> object: ...


class ImapCommandRefused(Exception):  # noqa: N818
    """The guard refused a command or argument. The message is a fixed text; it never carries the refused value."""


# Re-raised first by every broad handler in the IMAP adapter (Sec3): a stop, a refusal, a mapped error or a lost
# connection must never be swallowed as a degraded item.
PASS_THROUGH: Final = (SoftStop, ImapCommandRefused, ProviderError, imaplib.IMAP4.abort, OSError)


def imap_error_kind(exc: Exception) -> Literal["no", "bad", "other"]:
    """Tagged NO, tagged BAD or anything else. The exception text is matched against fixed patterns only and is
    never stored, logged or returned (C2)."""
    if isinstance(exc, InvalidCriteriaError):
        return "bad"
    if isinstance(exc, imaplib.IMAP4.abort) or not isinstance(exc, imaplib.IMAP4.error):
        return "other"
    text = str(exc)
    status = _STATUS_ERROR.fullmatch(text)
    if status is not None:
        return "no" if status.group(1) == "NO" else "bad"
    if _FAILED.fullmatch(text) is not None:
        return "no"
    return "other"


def _checked_uids(uids: Sequence[int]) -> list[int]:
    if isinstance(uids, (str, bytes, bytearray)) or not uids:
        raise ImapCommandRefused("uid set refused")
    for uid in uids:
        if type(uid) is not int or not 0 < uid <= MAX_UID:
            raise ImapCommandRefused("uid refused")
    return sorted(set(uids))


def uid_set(uids: Sequence[int]) -> str:
    """Sorted UIDs as an IMAP sequence set with ranges: [1, 2, 3, 5, 7, 8] → "1:3,5,7:8" (I10).

    Not passed to IMAPClient 4.1.0's fetch(), which filters its reply with int(messages) and fails on a range."""
    ordered = _checked_uids(uids)
    runs: list[str] = []
    start = prev = ordered[0]
    for uid in ordered[1:]:
        if uid != prev + 1:
            runs.append(f"{start}:{prev}" if prev > start else str(start))
            start = uid
        prev = uid
    runs.append(f"{start}:{prev}" if prev > start else str(start))
    return ",".join(runs)


def _fetch_item_ok(item: object) -> bool:
    if type(item) is not str:
        return False
    if item in PLAIN_ITEMS:
        return True
    peek = _PEEK.fullmatch(item)
    if peek is None or SECTION.fullmatch(peek.group(1)) is None:
        return False
    partial = PARTIAL.fullmatch(peek.group(2))
    return partial is not None and int(partial.group(1)) <= MAX_PARTIAL_BYTES


class GuardedImapClient:
    __slots__ = ("_caps", "_client", "_deadline", "_inbox_raw")

    def __init__(self, client: ImapClientLike, deadline: CallDeadline, *, registry_inbox_raw: str) -> None:
        if type(registry_inbox_raw) is not str or _PRINTABLE_TEXT.fullmatch(registry_inbox_raw) is None:
            raise ImapCommandRefused("registry inbox refused")
        self._client = client
        self._deadline = deadline
        self._inbox_raw = registry_inbox_raw
        self._caps: frozenset[bytes] | None = None

    def _before_command(self) -> None:
        self._deadline.before_command()
        self._client.socket().settimeout(max(MIN_SOCKET_TIMEOUT, self._deadline.remaining()))

    def login(self, username: str, password: str) -> None:
        """LOGIN, then CAPABILITY at once (I1). Python's imaplib may already re-read CAPABILITY inside LOGIN, but it
        consumes that reply, so IMAPClient's cache stays empty and its search() would send another CAPABILITY before
        UID SEARCH; this explicit call fills that cache."""
        self._before_command()
        self._client.login(username, password)
        self._before_command()
        self._caps = frozenset(self._client.capabilities())

    def capabilities(self) -> frozenset[bytes]:
        if self._caps is None:
            raise ImapCommandRefused("capabilities before login")
        return self._caps

    def list_folders(self) -> list[tuple[tuple[bytes, ...], bytes | None, bytes | str | int]]:
        self._before_command()
        return self._client.list_folders()

    def examine(self, raw_name: str) -> dict[bytes, object]:
        if type(raw_name) is not str or (raw_name != self._inbox_raw and not mailbox_name_ok(raw_name)):
            raise ImapCommandRefused("mailbox name refused")
        self._before_command()
        return self._client.select_folder(raw_name, readonly=True)

    def search(self, criteria: bytes) -> list[int]:
        if type(criteria) is not bytes or _PRINTABLE.fullmatch(criteria) is None or b"X-GM-RAW" in criteria.upper():
            raise ImapCommandRefused("search criteria refused")
        self._before_command()
        return self._client.search(criteria)

    def fetch(self, uids: Sequence[int], items: Sequence[str]) -> dict[int, dict[bytes, object]]:
        ordered = _checked_uids(uids)
        if isinstance(items, (str, bytes, bytearray)) or not items or not all(_fetch_item_ok(i) for i in items):
            raise ImapCommandRefused("fetch item refused")
        self._before_command()
        return self._client.fetch(ordered, list(items))

    def noop(self) -> None:
        self._before_command()
        self._client.noop()

    def logout(self) -> None:
        """No deadline check: the session is always closed, with a short socket timeout."""
        self._client.socket().settimeout(MIN_SOCKET_TIMEOUT)
        self._client.logout()
