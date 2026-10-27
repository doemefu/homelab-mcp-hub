"""The hub's read-only IMAP boundary (spec 080 §5.1, S5): every command and argument passes this guard.

Commands: CAPABILITY, LOGIN, LIST, EXAMINE, UID SEARCH, UID FETCH, NOOP, LOGOUT. UID SEARCH carries a literal only
through `search_values` (one synchronising literal per non-ASCII search value; `search` never does). Inputs are
checked before anything is sent, and the call deadline and socket timeout are applied before every command except
LOGOUT. The raw client stays in a private slot; no write method exists here.
"""

import imaplib
import re
import socket
from collections.abc import Sequence
from typing import Final, Literal, Protocol

from imapclient import response_parser
from imapclient.exceptions import InvalidCriteriaError, LoginError

from mcp_hub.ids import mailbox_name_shape_ok
from mcp_hub.providers.base import ProviderError, SearchKey
from mcp_hub.providers.deadline import CallDeadline, SoftStop

SECTION: Final = re.compile(r"[0-9.]+|TEXT|HEADER\.FIELDS \([A-Z -]+\)")
PARTIAL: Final = re.compile(r"<0\.([0-9]{1,6})>")
MAX_PARTIAL_BYTES: Final = 262_144
PLAIN_ITEMS: Final = frozenset({"FLAGS", "INTERNALDATE", "BODYSTRUCTURE", "X-GM-LABELS", "X-GM-THRID"})
MAX_FETCH_UIDS: Final = 500  # bounds the UID FETCH line (at most 500 UIDs of 11 bytes each); the guard owns it
MAX_UID: Final = 2**32 - 1  # nz-number (RFC 3501)
MIN_SOCKET_TIMEOUT: Final = 0.5
# Mailboxes over 200 characters are listed with `id: null` (§5.3 rule 9), so EXAMINE takes longer names; this cap only
# bounds the command line.
MAX_EXAMINE_NAME_CHARS: Final = 1000

_PEEK: Final = re.compile(r"BODY\.PEEK\[([^\]]*)\](<[^>]*>)")
_PRINTABLE: Final = re.compile(rb"[\x20-\x7e]+")
_LITERAL_MARKER: Final = re.compile(rb".*\{[0-9]+\+?\}")  # a trailing {N} or {N+} would announce a literal
_PRINTABLE_TEXT: Final = re.compile(r"[\x20-\x7e]+")
# The library builds the leading words of these texts from the command name; only that prefix and the status token
# are read. imaplib reports a tagged BAD as "<CMD> command error: BAD …"; IMAPClient 4.1.0 reports a tagged NO as
# "<cmd> failed: …" (`_check_resp`). The test fakes use "<CMD> command error: NO|BAD …".
_STATUS_ERROR: Final = re.compile(r"[A-Za-z]+(?: [A-Za-z]+)? command error: (NO|BAD)\b.*", re.DOTALL)
_FAILED: Final = re.compile(r"[A-Za-z]+(?: [A-Za-z]+)? failed: .*", re.DOTALL)

# search_values: typed value tokens (D82, SEC-d1). Literal bytes are checked on the code points, the part re-check on
# the wire form.
_SEARCH_KEYS: Final = frozenset({"TEXT", "FROM", "TO", "SUBJECT"})
MAX_TEXT_VALUES: Final = 8  # at most 8 TEXT keys + one each of FROM, TO, SUBJECT
MAX_SEARCH_VALUES: Final = 11
MAX_LITERAL_BYTES: Final = 800
MAX_VALUE_BYTES: Final = 4096  # all values of one command together
_QUOTED_PART: Final = re.compile(rb'"[\x20\x21\x23-\x5b\x5d-\x7e]+"')
_UNSAFE_CODE_POINT: Final = re.compile("[\x00-\x1f\x7f-\x9f]")


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
    # IMAPClient's private method: the only way to send several literals; used by `search_values` alone (T3).
    def _raw_command_untagged(
        self, command: bytes, args: list[bytes], *, uid: bool = True
    ) -> list[bytes | tuple[bytes, bytes]]: ...
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
    marker. It checks the ASCII segments only: a value with an 8-bit byte never passes here and goes through
    `GuardedImapClient.search_values` instead, which sends it as one synchronising literal."""
    return (
        type(data) is bytes
        and _PRINTABLE.fullmatch(data) is not None
        and b"X-GM-RAW" not in data.upper()
        and _LITERAL_MARKER.fullmatch(data) is None
    )


def examine_name_ok(raw_name: object) -> bool:
    """A name EXAMINE may send (other than the registry inbox): 1-1,000 characters of printable ASCII in strict
    modified UTF-7 (`ids.mailbox_name_shape_ok`)."""
    return type(raw_name) is str and 0 < len(raw_name) <= MAX_EXAMINE_NAME_CHARS and mailbox_name_shape_ok(raw_name)


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
        if type(raw_name) is not str or (raw_name != self._inbox_raw and not examine_name_ok(raw_name)):
            raise ImapCommandRefused("mailbox name refused")
        self._before_command()
        return self._client.select_folder(raw_name, readonly=True)

    def search(self, criteria: bytes) -> list[int]:
        if not search_criteria_ok(criteria):
            raise ImapCommandRefused("search criteria refused")
        self._before_command()
        return self._client.search(criteria)

    def search_values(self, prefix: bytes, values: Sequence[tuple[SearchKey, str]]) -> list[int]:
        """UID SEARCH with `prefix` (the `NOT DRAFT` window and label keys) followed by `key value` pairs. An ASCII
        value goes out as a hub-quoted string through `search`. A non-ASCII value is one synchronising literal
        with `CHARSET UTF-8` (never `{N+}`: the client class answers False for LITERAL+). Everything is checked
        before the first byte is sent; after an abort the session only attempts LOGOUT."""
        from mcp_hub.providers import imap_search  # imap_search imports this module's criteria rule

        if not search_criteria_ok(prefix) or not (prefix == b"NOT DRAFT" or prefix.startswith(b"NOT DRAFT ")):
            raise ImapCommandRefused("search criteria refused")
        pairs = list(values)  # one snapshot: the checked copy is what gets sent
        if not 0 < len(pairs) <= MAX_SEARCH_VALUES:
            raise ImapCommandRefused("search values refused")
        keys = [key for key, _ in pairs]
        if not all(type(key) is str and key in _SEARCH_KEYS for key in keys):
            raise ImapCommandRefused("search key refused")
        if keys.count("TEXT") > MAX_TEXT_VALUES or any(keys.count(key) > 1 for key in ("FROM", "TO", "SUBJECT")):
            raise ImapCommandRefused("search key refused")
        total = 0
        parts: list[bytes] = []
        literal = False
        for _, value in pairs:
            if type(value) is not str or not value or "X-GM-RAW" in value.upper():
                raise ImapCommandRefused("search value refused")
            if value.isascii():
                if not imap_search.search_value_ok(value):
                    raise ImapCommandRefused("search value refused")
                total += len(value)
            else:
                try:
                    encoded = value.encode("utf-8")
                except UnicodeEncodeError:
                    raise ImapCommandRefused("search value refused") from None
                if len(encoded) > MAX_LITERAL_BYTES or _UNSAFE_CODE_POINT.search(value) is not None:
                    raise ImapCommandRefused("search value refused")
                total += len(encoded)
                literal = True
            parts.append(imap_search.value_part(value))
        if total > MAX_VALUE_BYTES:
            raise ImapCommandRefused("search values refused")
        if not literal:
            return self.search(
                prefix
                + b" "
                + b" ".join(key.encode("ascii") + b" " + part for key, part in zip(keys, parts, strict=True))
            )
        # Right before the send: IMAPClient sends a part without an 8-bit byte as an atom, so every such part must be
        # a well-formed quoted string, whatever built it.
        for part in parts:
            if type(part) is not bytes or (
                part.isascii() and (_QUOTED_PART.fullmatch(part) is None or b"X-GM-RAW" in part.upper())
            ):
                raise ImapCommandRefused("search value refused")
        args = [b"CHARSET", b"UTF-8", prefix]
        for key, part in zip(keys, parts, strict=True):
            args += [key.encode("ascii"), part]
        self._before_command()
        data = self._client._raw_command_untagged(b"SEARCH", args, uid=True)
        return list(response_parser.parse_message_list(data))

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
