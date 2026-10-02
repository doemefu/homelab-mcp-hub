"""AES-256-GCM for refresh tokens at rest (spec 080 §7.2, rev. 4.6 S17). Key ids are derived from the key bytes; both
key files are read from one snapshot of the Secret volume. Key material is never logged, printed or put into an
exception message; errors carry a fixed cause only."""

import base64
import binascii
import hashlib
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Literal

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

KEY_FILE: Final = "token-encryption-key"
PREVIOUS_KEY_FILE: Final = "token-encryption-key-previous"
AAD_LABEL: Final = "mcp-hub/token/v1"
KEY_BYTES: Final = 32
NONCE_BYTES: Final = 12
MAX_PLAINTEXT_BYTES: Final = 16_384  # L14


class KeyUnavailableError(Exception):
    """Key file missing, unreadable, not exactly 32 bytes of strict base64, or current == previous."""


class TokenDecryptError(Exception):
    """Wrong key, wrong associated data (row moved), tampered data or unknown key id."""


@dataclass(frozen=True, slots=True)
class Sealed:
    key_id: str
    nonce: bytes = field(repr=False)
    ciphertext: bytes = field(repr=False)


def key_id_for(key: bytes) -> str:
    """First 16 hex characters of SHA-256 over the raw key (decision D64): a key-check value, not a secret."""
    return hashlib.sha256(key).hexdigest()[:16]


def associated_data(account_id: str, provider: str, key_id: str) -> bytes:
    return f"{AAD_LABEL}|{account_id}|{provider}|{key_id}".encode()


def snapshot_dir(secrets_dir: Path) -> Path:
    """The kubelet swaps the Secret volume atomically by re-pointing `..data`; reading every key file below the
    directory it points to *now* means two reads can never straddle a swap. Plain directory (tests) as fallback."""
    data = secrets_dir / "..data"
    return Path(os.path.realpath(data)) if data.exists() else secrets_dir


def load_key(path: Path) -> bytes:
    try:
        key = base64.b64decode(path.read_text(encoding="ascii").strip(), validate=True)
    except (OSError, UnicodeDecodeError, binascii.Error, ValueError):
        raise KeyUnavailableError("KeyUnreadable") from None
    if len(key) != KEY_BYTES:
        raise KeyUnavailableError("KeyLength")
    return key


class TokenCipher:
    def __init__(
        self,
        current_key: bytes,
        previous_key: bytes | None = None,
        *,
        nonce: Callable[[int], bytes] = os.urandom,
    ) -> None:
        self._current = key_id_for(current_key)
        self._keys: dict[str, bytes] = {self._current: current_key}
        self._previous: str | None = None
        if previous_key is not None:
            previous_id = key_id_for(previous_key)
            if previous_id == self._current:
                raise KeyUnavailableError("SameKey")
            self._keys[previous_id] = previous_key
            self._previous = previous_id
        self._nonce = nonce

    @classmethod
    def from_files(cls, secrets_dir: Path) -> "TokenCipher":
        base = snapshot_dir(secrets_dir)
        previous_path = base / PREVIOUS_KEY_FILE
        previous = load_key(previous_path) if previous_path.exists() else None
        return cls(load_key(base / KEY_FILE), previous)

    @property
    def current_id(self) -> str:
        return self._current

    @property
    def previous_id(self) -> str | None:
        return self._previous

    def key_state(self, key_id: str) -> Literal["current", "previous", "other"]:
        if key_id == self._current:
            return "current"
        return "previous" if key_id == self._previous else "other"

    def seal(self, plaintext: str, *, account_id: str, provider: str) -> Sealed:
        data = plaintext.encode()
        if not data or len(data) > MAX_PLAINTEXT_BYTES:
            raise ValueError("token size outside 1..16384 bytes")
        nonce = self._nonce(NONCE_BYTES)
        aad = associated_data(account_id, provider, self._current)
        return Sealed(self._current, nonce, AESGCM(self._keys[self._current]).encrypt(nonce, data, aad))

    def open(self, sealed: Sealed, *, account_id: str, provider: str) -> str:
        key = self._keys.get(sealed.key_id)
        if key is None:
            raise TokenDecryptError("UnknownKeyId")
        try:
            aad = associated_data(account_id, provider, sealed.key_id)
            return AESGCM(key).decrypt(sealed.nonce, sealed.ciphertext, aad).decode()
        except (InvalidTag, UnicodeDecodeError, ValueError):
            raise TokenDecryptError("InvalidTag") from None

    def __repr__(self) -> str:
        return f"TokenCipher(current={self._current!r}, previous={self._previous!r})"
