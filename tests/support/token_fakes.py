"""In-memory token store for unit tests and the memory probe (no PostgreSQL)."""

import os
from collections.abc import Iterator
from contextlib import contextmanager

from mcp_hub.tokenstore.crypto import Sealed, TokenCipher
from mcp_hub.tokenstore.store import TokenStoreUnavailableError

KEY = os.urandom(32)


class FakeRow:
    def __init__(self, store: "FakeStore") -> None:
        self.store, self.sealed, self.invalid_grant = store, store.sealed, store.invalid_grant
        self.pending: list[tuple[str, Sealed | None]] = []

    def rotate(self, sealed: Sealed, granted_scopes: str) -> None:
        self.store.events.append("rotate")
        self.pending.append(("sealed", sealed))

    def mark_invalid_grant(self) -> None:
        self.pending.append(("invalid", None))

    def replace_login(self, sealed: Sealed, provider: str, granted_scopes: str) -> None:
        self.pending.append(("login", sealed))


class FakeStore:
    def __init__(
        self,
        token: str | None = "RT-SENTINEL-0",  # noqa: S107 - a test sentinel, not a password
        *,
        fail_commit: bool = False,
        configured: bool = True,
    ) -> None:
        self.cipher_ = TokenCipher(KEY)
        self.sealed: Sealed | None = (
            None if token is None else self.cipher_.seal(token, account_id="outlook", provider="microsoft")
        )
        self.invalid_grant, self.fail_commit, self._configured = False, fail_commit, configured
        self.events: list[str] = []

    def configured(self) -> bool:
        return self._configured

    def cipher(self) -> TokenCipher:
        return self.cipher_

    @contextmanager
    def locked(self, account_id: str, *, wait_ms: int = 5000) -> Iterator[FakeRow]:
        row = FakeRow(self)
        yield row
        if row.pending and self.fail_commit:
            raise TokenStoreUnavailableError("SerializationFailure")
        for kind, value in row.pending:
            if kind in ("sealed", "login"):
                self.sealed = value
                self.invalid_grant = self.invalid_grant and kind != "login"
            else:
                self.invalid_grant = True
        self.events.append("commit")

    def current(self) -> str:
        assert self.sealed is not None
        return self.cipher_.open(self.sealed, account_id="outlook", provider="microsoft")
