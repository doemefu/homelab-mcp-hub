"""HMAC-signed keyset cursors for the mail list tools (spec 080 §5.5 "Cursors", D78)."""

import base64
import hashlib
import hmac
import json
import re
import secrets
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Final

from mcp_hub.errors import ToolError

CURSOR_PREFIX: Final = "c1."
MAX_CURSOR_CHARS: Final = 2048
CURSOR_TTL_SECONDS: Final = 3600
CURSOR_FUTURE_SKEW_SECONDS: Final = 60
CURSOR_TOOLS: Final = frozenset({"list_messages", "list_unread"})  # WP13d adds search_messages
_B64URL: Final = re.compile(r"[A-Za-z0-9_-]+")
_K: Final = re.compile(r"[0-9a-f]{16}")
_DIGEST: Final = re.compile(r"[0-9a-f]{64}")
_KEYS: Final = frozenset({"a", "i", "k", "r", "t", "w"})
_EXCLUDED_ARGUMENTS: Final = frozenset({"cursor", "limit"})


@dataclass(frozen=True, slots=True)
class Position:
    """Position of the last returned item in the total order (received_at desc, k desc)."""

    received_at: int
    k: str

    def precedes(self, received_at: int, k: str) -> bool:
        """True when the item (received_at, k) comes strictly after this position in list order."""
        return received_at < self.received_at or (received_at == self.received_at and k < self.k)


@dataclass(frozen=True, slots=True)
class CursorState:
    tool: str
    args_digest: str
    window_start: int
    position: Position
    issued_at: int


def argument_digest(arguments: Mapping[str, object]) -> str:
    """SHA-256 over the canonical JSON of every argument except `cursor` and `limit`.

    Callers pass omitted arguments as None so that an omitted value differs from an explicit one.
    """
    relevant = {name: value for name, value in arguments.items() if name not in _EXCLUDED_ARGUMENTS}
    canonical = json.dumps(relevant, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _invalid() -> ToolError:
    return ToolError("invalid_cursor", "Cursor is invalid or expired")


class CursorCodec:
    def __init__(self, key: bytes | None = None, clock: Callable[[], float] = time.time) -> None:
        self._key = key if key is not None else secrets.token_bytes(32)
        self._clock = clock

    def _mac(self, signed: str) -> str:
        return _b64encode(hmac.new(self._key, signed.encode("ascii"), hashlib.sha256).digest())

    def issue(self, *, tool: str, args_digest: str, window_start: int, position: Position) -> str:
        if tool not in CURSOR_TOOLS:
            raise ValueError("unsupported cursor tool")
        if not (_DIGEST.fullmatch(args_digest) and _K.fullmatch(position.k)):
            raise ValueError("malformed cursor digest or key")
        if not (type(window_start) is int and type(position.received_at) is int):
            raise ValueError("cursor positions must be integers")
        payload = {
            "a": args_digest,
            "i": int(self._clock()),
            "k": position.k,
            "r": position.received_at,
            "t": tool,
            "w": window_start,
        }
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        signed = CURSOR_PREFIX + _b64encode(body)
        return f"{signed}.{self._mac(signed)}"

    def open(self, value: str, *, tool: str, args_digest: str) -> CursorState:
        if len(value) > MAX_CURSOR_CHARS or not value.startswith(CURSOR_PREFIX):
            raise _invalid()
        parts = value[len(CURSOR_PREFIX) :].split(".")
        if len(parts) != 2 or not all(_B64URL.fullmatch(part) for part in parts):
            raise _invalid()
        payload_b64, signature = parts
        # The MAC is verified before any base64 or JSON decoding of attacker-controlled data.
        expected = self._mac(CURSOR_PREFIX + payload_b64)
        if not hmac.compare_digest(signature.encode("ascii"), expected.encode("ascii")):
            raise _invalid()
        state = self._decode(payload_b64)
        if state.tool != tool or not hmac.compare_digest(
            state.args_digest.encode("ascii"), args_digest.encode("utf-8")
        ):
            raise _invalid()
        age = int(self._clock()) - state.issued_at
        if age > CURSOR_TTL_SECONDS or -age > CURSOR_FUTURE_SKEW_SECONDS:
            raise _invalid()
        return state

    @staticmethod
    def _decode(payload_b64: str) -> CursorState:
        try:
            data = json.loads(_b64decode(payload_b64).decode("utf-8"))
        except ValueError:  # binascii.Error and UnicodeDecodeError are ValueErrors
            raise _invalid() from None
        if not isinstance(data, dict) or set(data) != _KEYS:
            raise _invalid()
        issued, received, window = data["i"], data["r"], data["w"]
        k, digest, tool = data["k"], data["a"], data["t"]
        if not (type(issued) is int and type(received) is int and type(window) is int):
            raise _invalid()
        if not (isinstance(k, str) and _K.fullmatch(k) and isinstance(digest, str) and _DIGEST.fullmatch(digest)):
            raise _invalid()
        if not isinstance(tool, str) or tool not in CURSOR_TOOLS:
            raise _invalid()
        return CursorState(
            tool=tool,
            args_digest=digest,
            window_start=window,
            position=Position(received_at=received, k=k),
            issued_at=issued,
        )
