import base64
import hashlib
import os
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from mcp_hub.tokenstore import crypto
from mcp_hub.tokenstore.crypto import (
    KEY_FILE,
    PREVIOUS_KEY_FILE,
    KeyUnavailableError,
    Sealed,
    TokenCipher,
    TokenDecryptError,
    key_id_for,
    load_key,
)

SENTINEL = "RT-SENTINEL-7f3a"


def write_key(directory: Path, name: str, raw: bytes | None = None) -> bytes:
    key = raw if raw is not None else os.urandom(32)
    (directory / name).write_text(base64.b64encode(key).decode() + "\n")
    return key


def seal(cipher: TokenCipher, account: str = "outlook", provider: str = "microsoft") -> Sealed:
    return cipher.seal(SENTINEL, account_id=account, provider=provider)


def test_round_trip_fresh_nonce_and_derived_id(tmp_path: Path) -> None:
    key = write_key(tmp_path, KEY_FILE)
    cipher = TokenCipher.from_files(tmp_path)
    a, b = seal(cipher), seal(cipher)
    assert a.nonce != b.nonce
    assert len(a.nonce) == 12
    assert a.key_id == hashlib.sha256(key).hexdigest()[:16] == key_id_for(key) == cipher.current_id
    assert SENTINEL.encode() not in a.ciphertext
    assert cipher.open(a, account_id="outlook", provider="microsoft") == SENTINEL


def test_aad_carries_the_format_label(tmp_path: Path) -> None:
    key = write_key(tmp_path, KEY_FILE)
    sealed = seal(TokenCipher.from_files(tmp_path))
    unlabeled = f"outlook|microsoft|{sealed.key_id}".encode()
    labeled = f"mcp-hub/token/v1|outlook|microsoft|{sealed.key_id}".encode()
    assert AESGCM(key).decrypt(sealed.nonce, sealed.ciphertext, labeled) == SENTINEL.encode()
    with pytest.raises(Exception):  # noqa: B017, PT011 - InvalidTag without the label
        AESGCM(key).decrypt(sealed.nonce, sealed.ciphertext, unlabeled)


def test_ciphertext_moved_to_other_row_fails(tmp_path: Path) -> None:
    write_key(tmp_path, KEY_FILE)
    cipher = TokenCipher.from_files(tmp_path)
    sealed = seal(cipher)
    for account, provider in (("uzh", "microsoft"), ("outlook", "microsoft-org")):
        with pytest.raises(TokenDecryptError):
            cipher.open(sealed, account_id=account, provider=provider)


def test_previous_key_decrypts_and_new_writes_use_current(tmp_path: Path) -> None:
    old = write_key(tmp_path, KEY_FILE)
    sealed = seal(TokenCipher.from_files(tmp_path))
    write_key(tmp_path, PREVIOUS_KEY_FILE, old)
    new = write_key(tmp_path, KEY_FILE)
    rotated = TokenCipher.from_files(tmp_path)
    assert rotated.previous_id == key_id_for(old)
    assert rotated.current_id == key_id_for(new)
    assert rotated.key_state(sealed.key_id) == "previous"
    assert rotated.open(sealed, account_id="outlook", provider="microsoft") == SENTINEL
    resealed = seal(rotated)
    assert resealed.key_id == key_id_for(new)
    assert rotated.key_state(resealed.key_id) == "current"
    assert rotated.key_state("0" * 16) == "other"


def test_same_key_as_current_and_previous_is_refused(tmp_path: Path) -> None:
    key = write_key(tmp_path, KEY_FILE)
    write_key(tmp_path, PREVIOUS_KEY_FILE, key)
    with pytest.raises(KeyUnavailableError):
        TokenCipher.from_files(tmp_path)


def test_both_keys_come_from_one_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Kubelet layout: files are symlinks into ..data, which points to a timestamped directory. The kubelet swaps
    ..data between the two key reads; both keys must still come from the snapshot resolved before the first read
    (review 01 delta N4: without the swap the test could not fail against a naive implementation)."""
    old_dir, new_dir = tmp_path / "..2026_10_02_10_00_00.1", tmp_path / "..2026_10_02_11_00_00.2"
    old_dir.mkdir()
    new_dir.mkdir()
    k_older = write_key(old_dir, PREVIOUS_KEY_FILE)
    k_old = write_key(old_dir, KEY_FILE)
    write_key(new_dir, PREVIOUS_KEY_FILE, k_old)
    k_new = write_key(new_dir, KEY_FILE)
    (tmp_path / "..data").symlink_to(old_dir.name)
    for name in (KEY_FILE, PREVIOUS_KEY_FILE):
        (tmp_path / name).symlink_to(f"..data/{name}")
    original = crypto.load_key
    calls: list[Path] = []

    def load_then_swap(path: Path) -> bytes:
        key = original(path)
        calls.append(path)
        if len(calls) == 1:  # the kubelet re-points ..data right after the first read
            (tmp_path / "..data").unlink()
            (tmp_path / "..data").symlink_to(new_dir.name)
        return key

    monkeypatch.setattr(crypto, "load_key", load_then_swap)
    cipher = TokenCipher.from_files(tmp_path)
    assert len(calls) == 2
    assert (cipher.current_id, cipher.previous_id) == (key_id_for(k_old), key_id_for(k_older))
    assert key_id_for(k_new) not in (cipher.current_id, cipher.previous_id)


def test_unknown_key_id_is_decrypt_error(tmp_path: Path) -> None:
    write_key(tmp_path, KEY_FILE)
    cipher = TokenCipher.from_files(tmp_path)
    sealed = seal(cipher)
    with pytest.raises(TokenDecryptError):
        cipher.open(Sealed("f" * 16, sealed.nonce, sealed.ciphertext), account_id="outlook", provider="microsoft")


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"not base64!!",
        base64.b64encode(os.urandom(16)),
        base64.b64encode(os.urandom(33)),
        base64.urlsafe_b64encode(b"\xfb" * 32),
    ],
)
def test_bad_key_files_are_unavailable(tmp_path: Path, content: bytes) -> None:
    (tmp_path / KEY_FILE).write_bytes(content)
    with pytest.raises(KeyUnavailableError):
        load_key(tmp_path / KEY_FILE)


def test_missing_key_file_is_unavailable(tmp_path: Path) -> None:
    with pytest.raises(KeyUnavailableError):
        TokenCipher.from_files(tmp_path)


def test_no_key_or_token_in_repr_or_errors(tmp_path: Path) -> None:
    key = write_key(tmp_path, KEY_FILE)
    cipher = TokenCipher.from_files(tmp_path)
    sealed = seal(cipher)
    text = repr(cipher) + repr(sealed) + str(cipher)
    assert base64.b64encode(key).decode() not in text
    assert SENTINEL not in text
    with pytest.raises(TokenDecryptError) as info:
        cipher.open(sealed, account_id="other", provider="microsoft")
    assert SENTINEL not in str(info.value)
    assert info.value.__cause__ is None
