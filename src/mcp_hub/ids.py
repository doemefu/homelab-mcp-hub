"""Opaque ids (spec 080 §5.1): 'v1.' + unpadded base64url of a compact JSON list, ≤ 512 chars.

An EventId carries a 128-bit SHA-256 digest over calendar URL path, UID and recurrence id, not the values
(spec 080 rev. 4.4, D56). An IMAP MessageId (kind "m") carries folder, UIDVALIDITY and UID; its folder is the raw
modified-UTF-7 mailbox name, checked by `mailbox_name_ok` (D73), and the tool layer binds it to the current folder
map or the registry inbox (D75). A stable ref (kind "r") carries account and Message-ID, a thread key (kind "t")
account, source and value; their third-party text is checked against fixed patterns before it is base64url-encoded.
A Graph MessageId (kind "g", rev. 4.6) carries the immutable Graph id only (charset and length checked here, so it can
never change the request path); the Graph adapter binds it by the message's `parentFolderId` (D75).
"""

import base64
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Final, Literal, NoReturn

from mcp_hub.errors import ToolError

PREFIX: Final = "v1."
MAX_ID_LENGTH: Final = 512
_ACCOUNT: Final = re.compile(r"[a-z][a-z0-9-]{1,31}")
_BASE64URL: Final = re.compile(r"[A-Za-z0-9_-]+")
_UINT32_MAX: Final = 2**32 - 1
MESSAGE_ID_PATTERN: Final = re.compile(
    r"[\x21\x23-\x26\x28-\x3B\x3D\x3F-\x5B\x5D-\x7A\x7C\x7E]{1,300}"
)  # fullmatch only
MAX_MAILBOX_NAME_CHARS: Final = 200
_SUBJECT_KEY: Final = re.compile(r"[0-9a-f]{32}")
_UTF7_NAME: Final = re.compile(r"(?:[\x20-\x25\x27-\x7E]|&-|&[A-Za-z0-9+,]+-)*")
_UTF7_RUN: Final = re.compile(r"&(?:-|([A-Za-z0-9+,]+)-)")
GRAPH_ID: Final = re.compile(r"[A-Za-z0-9=_-]{1,300}")  # L5 (spec 080 rev. 4.6 S1)


@dataclass(frozen=True, slots=True)
class MessageRef:
    account: str
    folder: str
    uidvalidity: int
    uid: int


@dataclass(frozen=True, slots=True)
class GraphMessageRef:
    account: str
    graph_id: str


@dataclass(frozen=True, slots=True, repr=False)
class SearchHitRef(GraphMessageRef):
    """The regular (not immutable) id a Graph `$search` returned: an internal order key only (D87).

    Never exposable: `location_id` gives None, so it is no `id` and no log `item`; it is never used for a request."""

    def __repr__(self) -> str:
        return "SearchHitRef(<internal>)"


AnyMessageRef = MessageRef | GraphMessageRef
ThreadSource = Literal["gmail", "graph", "headers", "subject"]
_THREAD_SOURCES: Final = ("gmail", "graph", "headers", "subject")


@dataclass(frozen=True, slots=True)
class StableRef:
    """Spec 080 `MessageRef` (field `ref`, kind "r"): account and Message-ID, stable across moves."""

    account: str
    message_id: str


@dataclass(frozen=True, slots=True)
class ThreadKey:
    """Kind "t"; value is an int for source "gmail", a str otherwise."""

    account: str
    source: ThreadSource
    value: int | str


def _utf7_ok(name: str) -> bool:
    """True when a printable-ASCII name is strict modified UTF-7 (RFC 3501 5.1.3).

    Each shifted run is canonical base64 of UTF-16BE holding no printable-ASCII character (they represent
    themselves), and two shifted runs are never adjacent: every name has exactly one encoding, so a raw name such
    as "&AEoAdQBuAGs-" ("Junk") cannot dodge the name-based exclusion of junk and drafts mailboxes.
    """
    if _UTF7_NAME.fullmatch(name) is None:
        return False
    previous_end = -1
    for run in _UTF7_RUN.finditer(name):
        body = run.group(1)
        if body is None:  # "&-" is a literal ampersand
            continue
        if run.start() == previous_end:
            return False
        previous_end = run.end()
        b64 = body.replace(",", "/")
        try:
            raw = base64.b64decode(b64 + "=" * (-len(b64) % 4), validate=True)
            text = raw.decode("utf-16-be")
        except ValueError:  # binascii.Error and UnicodeDecodeError are ValueErrors
            return False
        if base64.b64encode(raw).decode("ascii").rstrip("=") != b64 or any(0x20 <= ord(c) <= 0x7E for c in text):
            return False
    return True


def mailbox_name_shape_ok(raw_name: str) -> bool:
    """Printable ASCII (0x20-0x7E) that is strict modified UTF-7; no length rule (the folder map keeps longer names)."""
    return all(0x20 <= ord(c) <= 0x7E for c in raw_name) and _utf7_ok(raw_name)


def mailbox_name_ok(raw_name: str) -> bool:
    """1-200 printable ASCII characters (0x20-0x7E) that decode as modified UTF-7 (spec 080 §5.3 rule 9, D73)."""
    return 0 < len(raw_name) <= MAX_MAILBOX_NAME_CHARS and mailbox_name_shape_ok(raw_name)


def _text(parts: list[object]) -> str:
    raw = json.dumps(parts, separators=(",", ":"), ensure_ascii=False).encode()
    return PREFIX + base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _encode(parts: list[object]) -> str:
    value = _text(parts)
    if len(value) > MAX_ID_LENGTH:
        raise ValueError("id longer than 512 characters")
    return value


def _location_parts(ref: AnyMessageRef) -> list[object]:
    if isinstance(ref, GraphMessageRef):
        return ["g", ref.account, ref.graph_id]
    return ["m", ref.account, ref.folder, ref.uidvalidity, ref.uid]


def encode_message_id(ref: AnyMessageRef) -> str:
    if isinstance(ref, SearchHitRef):
        raise ValueError("a search hit reference is never an id")
    return _encode(_location_parts(ref))


def location_id(ref: AnyMessageRef) -> str | None:
    """The MessageId, or None when the mailbox name fails `mailbox_name_ok` or the id would exceed 512 characters."""
    if isinstance(ref, SearchHitRef) or (isinstance(ref, MessageRef) and not mailbox_name_ok(ref.folder)):
        return None
    value = _text(_location_parts(ref))
    return value if len(value) <= MAX_ID_LENGTH else None


def location_k(ref: AnyMessageRef) -> str:
    """Keyset key: first 16 hex chars of SHA-256 over the id text, also for `id: null` (uncapped text)."""
    return hashlib.sha256(_text(_location_parts(ref)).encode()).hexdigest()[:16]


def _invalid() -> NoReturn:
    raise ToolError("invalid_argument", "Malformed message id")


def _uint32(value: object) -> bool:
    return type(value) is int and 0 < value <= _UINT32_MAX


def _decode_list(value: str) -> object:
    if len(value) > MAX_ID_LENGTH or not value.startswith(PREFIX) or not _BASE64URL.fullmatch(value[len(PREFIX) :]):
        return None
    body = value[len(PREFIX) :]
    try:
        return json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except ValueError:
        return None


def decode_message_id(value: str) -> AnyMessageRef:
    data = _decode_list(value)
    if not isinstance(data, list):
        _invalid()
    if isinstance(data, list) and len(data) == 3 and data[0] == "g":
        _, account, graph_id = data
        if not (
            isinstance(account, str)
            and _ACCOUNT.fullmatch(account)
            and isinstance(graph_id, str)
            and GRAPH_ID.fullmatch(graph_id)
        ):
            _invalid()
        return GraphMessageRef(account=account, graph_id=graph_id)
    if not (isinstance(data, list) and len(data) == 5 and data[0] == "m"):
        _invalid()
    _, account, folder, uidvalidity, uid = data
    if not (
        isinstance(account, str)
        and _ACCOUNT.fullmatch(account)
        and isinstance(folder, str)
        and mailbox_name_ok(folder)
        and _uint32(uidvalidity)
        and _uint32(uid)
    ):
        _invalid()
    return MessageRef(account=account, folder=folder, uidvalidity=uidvalidity, uid=uid)


def _bad_ref() -> NoReturn:
    raise ToolError("invalid_argument", "Malformed message reference")


def _bad_thread_key() -> NoReturn:
    raise ToolError("invalid_argument", "Malformed thread key")


def encode_ref(account: str, message_id: str) -> str | None:
    """The stable `ref`, or None when the Message-ID fails the §5.5 pattern or the id would exceed 512 characters."""
    if not (_ACCOUNT.fullmatch(account) and MESSAGE_ID_PATTERN.fullmatch(message_id)):
        return None
    value = _text(["r", account, message_id])
    return value if len(value) <= MAX_ID_LENGTH else None


def decode_ref(value: str) -> StableRef:
    data = _decode_list(value)
    if not (isinstance(data, list) and len(data) == 3 and data[0] == "r"):
        _bad_ref()
    _, account, message_id = data
    if not (
        isinstance(account, str)
        and _ACCOUNT.fullmatch(account)
        and isinstance(message_id, str)
        and MESSAGE_ID_PATTERN.fullmatch(message_id)
    ):
        _bad_ref()
    return StableRef(account=account, message_id=message_id)


def decode_status_entry(value: object) -> StableRef | MessageRef | GraphMessageRef:
    """One `get_message_status` entry: a `ref` (kind "r") or a MessageId (kind "m"/"g"); anything else is refused."""
    if not isinstance(value, str):
        _bad_ref()
    data = _decode_list(value)
    kind = data[0] if isinstance(data, list) and data else None
    try:
        if kind == "r":
            return decode_ref(value)
        if kind in ("m", "g"):
            return decode_message_id(value)
    except ToolError:
        pass  # the per-kind decoders word their own text; the entry error is fixed
    _bad_ref()


def _thread_value_ok(source: object, value: object) -> bool:
    if source == "gmail":
        return type(value) is int and 0 <= value < 2**64
    if not isinstance(value, str):
        return False
    if source == "graph":
        return GRAPH_ID.fullmatch(value) is not None
    if source == "headers":
        return MESSAGE_ID_PATTERN.fullmatch(value) is not None
    if source == "subject":
        return _SUBJECT_KEY.fullmatch(value) is not None
    return False


def encode_thread_key(key: ThreadKey) -> str | None:
    if not (_ACCOUNT.fullmatch(key.account) and _thread_value_ok(key.source, key.value)):
        return None
    value = _text(["t", key.account, key.source, key.value])
    return value if len(value) <= MAX_ID_LENGTH else None


def decode_thread_key(value: str) -> ThreadKey:
    data = _decode_list(value)
    if not (isinstance(data, list) and len(data) == 4 and data[0] == "t"):
        _bad_thread_key()
    _, account, source, thread_value = data
    if not (
        isinstance(account, str)
        and _ACCOUNT.fullmatch(account)
        and source in _THREAD_SOURCES
        and _thread_value_ok(source, thread_value)
    ):
        _bad_thread_key()
    return ThreadKey(account=account, source=source, value=thread_value)


def encode_event_id(account: str, calendar_path: str, uid: str, recurrence_id: str) -> str:
    """calendar_path is the path of the calendar URL only (no scheme or host), so ids survive a host move."""
    digest = hashlib.sha256("\x1f".join((calendar_path, uid, recurrence_id)).encode()).digest()[:16]
    return _encode(["e", account, base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")])
