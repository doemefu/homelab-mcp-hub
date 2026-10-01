"""Opaque MessageId / EventId (spec 080 §5.1): 'v1.' + unpadded base64url of a compact JSON list, ≤ 512 chars.

An EventId carries a 128-bit SHA-256 digest over calendar URL path, UID and recurrence id, not the values
(spec 080 rev. 4.4, D56); a MessageId's folder is checked against the registry inbox by the tool layer.
"""

import base64
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Final, NoReturn

from mcp_hub.errors import ToolError

PREFIX: Final = "v1."
MAX_ID_LENGTH: Final = 512
_ACCOUNT: Final = re.compile(r"^[a-z][a-z0-9-]{1,31}$")
_BASE64URL: Final = re.compile(r"^[A-Za-z0-9_-]+$")
_UINT32_MAX: Final = 2**32 - 1


@dataclass(frozen=True, slots=True)
class MessageRef:
    account: str
    folder: str
    uidvalidity: int
    uid: int


def _encode(parts: list[object]) -> str:
    raw = json.dumps(parts, separators=(",", ":"), ensure_ascii=False).encode()
    value = PREFIX + base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
    if len(value) > MAX_ID_LENGTH:
        raise ValueError("id longer than 512 characters")
    return value


def encode_message_id(ref: MessageRef) -> str:
    return _encode(["m", ref.account, ref.folder, ref.uidvalidity, ref.uid])


def _invalid() -> NoReturn:
    raise ToolError("invalid_argument", "Malformed message id")


def _uint32(value: object) -> bool:
    return type(value) is int and 0 < value <= _UINT32_MAX


def decode_message_id(value: str) -> MessageRef:
    if len(value) > MAX_ID_LENGTH or not value.startswith(PREFIX) or not _BASE64URL.fullmatch(value[len(PREFIX) :]):
        _invalid()
    body = value[len(PREFIX) :]
    try:
        data = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except ValueError:  # binascii.Error, UnicodeDecodeError and JSONDecodeError are ValueErrors
        _invalid()
    if not (isinstance(data, list) and len(data) == 5 and data[0] == "m"):
        _invalid()
    _, account, folder, uidvalidity, uid = data
    if not (
        isinstance(account, str)
        and _ACCOUNT.fullmatch(account)
        and isinstance(folder, str)
        and 0 < len(folder) <= 100
        and _uint32(uidvalidity)
        and _uint32(uid)
    ):
        _invalid()
    return MessageRef(account=account, folder=folder, uidvalidity=uidvalidity, uid=uid)


def encode_event_id(account: str, calendar_path: str, uid: str, recurrence_id: str) -> str:
    """calendar_path is the path of the calendar URL only (no scheme or host), so ids survive a host move."""
    digest = hashlib.sha256("\x1f".join((calendar_path, uid, recurrence_id)).encode()).digest()[:16]
    return _encode(["e", account, base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")])
