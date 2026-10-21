"""The owner's own addresses of a mail account (spec 080 §8.2, D70): a credential file named by
`mail.own_addresses_ref`. The addresses are never logged and never returned; errors carry fixed texts only."""

import logging
import re
import threading
from pathlib import Path
from typing import Final

from mcp_hub.logging import log_event

MAX_OWN_ADDRESSES: Final = 20
MAX_ADDRESS_CHARS: Final = 254
MAX_FILE_BYTES: Final = 8192  # read cap (Sec14)
# local part: 1-64 printable ASCII characters other than space and "(),:;<>@[\]; domain: two or more LDH labels
ADDRESS: Final = re.compile(
    r"[\x21\x23-\x27\x2A\x2B\x2D-\x39\x3D\x3F\x41-\x5A\x5E-\x7E]{1,64}@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+"
)

_log = logging.getLogger("mcp_hub.own_addresses")
_MISSING: Final = "missing"
_INVALID: Final = "invalid"
_OK: Final = "ok"


def parse_own_addresses(text: str) -> frozenset[str]:
    """Lowercased addresses of the file; ValueError with a fixed text (never a value) when it is not valid."""
    if len(text.encode("utf-8")) > MAX_FILE_BYTES:
        raise ValueError("the file is larger than 8 KiB")
    entries = [line.strip() for line in text.split("\n")]
    entries = [e for e in entries if e]
    if not entries:
        raise ValueError("the file holds no address")
    if len(entries) > MAX_OWN_ADDRESSES:
        raise ValueError("the file holds more than 20 addresses")
    for entry in entries:
        if len(entry) > MAX_ADDRESS_CHARS:
            raise ValueError("an entry is longer than 254 characters")
        if ADDRESS.fullmatch(entry) is None:
            raise ValueError("an entry is not a plain address")
    lowered = [e.lower() for e in entries]
    if len(set(lowered)) != len(lowered):
        raise ValueError("the file holds a duplicate")
    return frozenset(lowered)


def _load(path: Path) -> tuple[str, frozenset[str]]:
    """(state, addresses); the state is `ok`, `missing` (absent or unreadable) or `invalid`."""
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_FILE_BYTES + 1)
    except OSError:
        return _MISSING, frozenset()
    try:
        return _OK, parse_own_addresses(raw.decode("utf-8"))
    except ValueError:  # includes UnicodeDecodeError; the text is not used
        return _INVALID, frozenset()


class OwnAddressReader:
    """Reads the file on every call (a changed list needs no restart) and warns once per change of its state."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state: dict[str, str] = {}

    def read(self, account_id: str, secrets_dir: Path, ref: str | None) -> frozenset[str]:
        if ref is None:
            return frozenset()
        state, addresses = _load(secrets_dir / ref)
        with self._lock:
            changed = self._state.get(account_id) != state
            self._state[account_id] = state
            if changed and state != _OK:
                log_event(_log, logging.WARNING, "registry_value_invalid", account=account_id, key=ref)
        return addresses

    def reset(self) -> None:
        with self._lock:
            self._state.clear()


OWN_ADDRESSES: Final = OwnAddressReader()


def own_address_count(secrets_dir: Path, ref: str) -> int | None:
    """Number of addresses for `check-registry`; None when the file is missing or invalid. Never logs."""
    state, addresses = _load(secrets_dir / ref)
    return len(addresses) if state == _OK else None
