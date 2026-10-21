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

from imapclient.exceptions import InvalidCriteriaError, LoginError

from mcp_hub.ids import mailbox_name_ok
from mcp_hub.providers.base import ProviderError
from mcp_hub.providers.deadline import CallDeadline, SoftStop

SECTION: Final = re.compile(r"[0-9.]+|TEXT|HEADER\.FIELDS \([A-Z -]+\)")
PARTIAL: Final = re.compile(r"<0\.([0-9]{1,6})>")
MAX_PARTIAL_BYTES: Final = 262_144
PLAIN_ITEMS: Final = frozenset({"FLAGS", "INTERNALDATE", "BODYSTRUCTURE", "X-GM-LABELS", "X-GM-THRID"})
MAX_FETCH_UIDS: Final = 500  # bounds the UID FETCH line (at most 500 UIDs of 11 bytes each); the guard owns it
MAX_UID: Final = 2**32 - 1  # nz-number (RFC 3501)
MIN_SOCKET_TIMEOUT: Final = 0.5

_PEEK: Final = re.compile(r"BODY\.PEEK\[([^\]]*)\](<[^>]*>)")
_PRINTABLE: Final = re.compile(rb"[\x20-\x7e]+")
_LITERAL_MARKER: Final = re.compile(rb".*\{[0-9]+\+?\}")  # a trailing {N} or {N+} would announce a literal
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
    never stored, logged or returned (C2).

    A LoginError is always "other": for a NO its text is the server's own, with no library-built prefix. A NOOP BAD
    is classified "no", because IMAPClient reports NOOP failures of both kinds as "NOOP failed: …"."""
    if isinstance(exc, InvalidCriteriaError):
        return "bad"
    if isinstance(exc, (imaplib.IMAP4.abort, LoginError)) or not isinstance(exc, imaplib.IMAP4.error):
        return "other"
    text = str(exc)
    status = _STATUS_ERROR.fullmatch(text)
    if status is not None:
        return "no" if status.group(1) == "NO" else "bad"
    if _FAILED.fullmatch(text) is not None:
        return "no"
    return "other"


def search_criteria_ok(data: bytes) -> bool:
    """The one rule for UID SEARCH criteria (the guard and `imap_search.criteria` share it): `bytes` of printable
    ASCII (no CR, LF, NUL or 8-bit byte), no `X-GM-RAW` in any case (D80), and no trailing `{N}`/`{N+}` literal
    marker."""
    return (
        type(data) is bytes
        and _PRINTABLE.fullmatch(data) is not None
        and b"X-GM-RAW" not in data.upper()
        and _LITERAL_MARKER.fullmatch(data) is None
    )


def _uid_snapshot(uids: Sequence[int]) -> list[int]:
    """One pass over `uids`; the checked copy is what gets sent."""
    if isinstance(uids, (str, bytes, bytearray)):
        raise ImapCommandRefused("uid set refused")
    snapshot = list(uids)
    if not 0 < len(snapshot) <= MAX_FETCH_UIDS:
        raise ImapCommandRefused("uid set refused")
    for uid in snapshot:
        if type(uid) is not int or not 0 < uid <= MAX_UID:
            raise ImapCommandRefused("uid refused")
    return sorted(set(snapshot))


def _item_snapshot(items: Sequence[str]) -> list[str]:
    """One pass over `items`; the checked copy is what gets sent."""
    if isinstance(items, (str, bytes, bytearray)):
        raise ImapCommandRefused("fetch item refused")
    snapshot = list(items)
    if not snapshot or not all(_fetch_item_ok(item) for item in snapshot):
        raise ImapCommandRefused("fetch item refused")
    return snapshot


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
        # UID SEARCH/UID FETCH only (plain SEARCH/FETCH are outside the allowlist), and raw names sent as given:
        # IMAPClient's default folder_encode=True would re-encode a raw modified-UTF-7 name and open another folder.
        client.use_uid = True
        client.folder_encode = False
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
        if not search_criteria_ok(criteria):
            raise ImapCommandRefused("search criteria refused")
        self._before_command()
        return self._client.search(criteria)

    def fetch(self, uids: Sequence[int], items: Sequence[str]) -> dict[int, dict[bytes, object]]:
        uid_list = _uid_snapshot(uids)
        item_list = _item_snapshot(items)
        self._before_command()
        return self._client.fetch(uid_list, item_list)

    def noop(self) -> None:
        self._before_command()
        self._client.noop()

    def logout(self) -> None:
        """No deadline check: the session is always closed, with a short socket timeout."""
        self._client.socket().settimeout(MIN_SOCKET_TIMEOUT)
        self._client.logout()
