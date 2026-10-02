"""Graph access tokens with the rotation contract (spec 080 §6.3, rev. 4.6 S5):
in-process lock → row lock → decrypt → refresh grant (inside one call deadline) → UPDATE + COMMIT → only then use.
A failed commit discards the new tokens; the stored (previous) token normally still works because Microsoft does not
revoke a refresh token when it is used. Blocking; runs in provider worker threads."""

import logging
import threading
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from typing import Final, Protocol

import httpx2

from mcp_hub.logging import log_event
from mcp_hub.providers import msidentity
from mcp_hub.providers.base import PROVIDER_TIMEOUT_SECONDS, ProviderError
from mcp_hub.providers.msidentity import CLIENT_ERRORS, REVOKED_GRANT_ERRORS, GrantError, GraphAccount
from mcp_hub.tokenstore.crypto import KeyUnavailableError, Sealed, TokenCipher, TokenDecryptError
from mcp_hub.tokenstore.store import TokenStoreUnavailableError

REFRESH_MARGIN_SECONDS: Final = 300.0  # L12
# Worst row-lock hold (N3): the call deadline plus one token-endpoint read that started just before it.
MAX_ROW_LOCK_HOLD_SECONDS: Final = PROVIDER_TIMEOUT_SECONDS + msidentity.TOKEN_READ_TIMEOUT_SECONDS
_log = logging.getLogger("mcp_hub.providers.graph_auth")


class LockedRowLike(Protocol):
    sealed: Sealed | None
    invalid_grant: bool

    def rotate(self, sealed: Sealed, granted_scopes: str) -> None: ...
    def mark_invalid_grant(self) -> None: ...
    def replace_login(self, sealed: Sealed, provider: str, granted_scopes: str) -> None: ...


class TokenStoreLike(Protocol):
    def configured(self) -> bool: ...
    def cipher(self) -> TokenCipher: ...
    def locked(self, account_id: str, *, wait_ms: int = ...) -> AbstractContextManager[LockedRowLike]: ...


@dataclass(slots=True)
class _Cached:
    token: str = field(repr=False)
    expires_at: float


class AccessTokenCache:
    """Process memory only (spec 080 §6.3). Keyed by (account id, tenant, client id); bounded by the registry."""

    def __init__(self) -> None:
        self._entries: dict[tuple[str, str, str], _Cached] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def lock_for(self, account_id: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(account_id, threading.Lock())

    def get(self, key: tuple[str, str, str], now: float) -> str | None:
        with self._guard:
            entry = self._entries.get(key)
        return entry.token if entry is not None and entry.expires_at - REFRESH_MARGIN_SECONDS > now else None

    def put(self, key: tuple[str, str, str], token: str, expires_at: float) -> None:
        with self._guard:
            self._entries[key] = _Cached(token, expires_at)

    def drop(self, key: tuple[str, str, str]) -> None:
        with self._guard:
            self._entries.pop(key, None)


ACCESS_TOKENS: Final = AccessTokenCache()


class GraphTokenSource:
    def __init__(
        self,
        account: GraphAccount,
        store: TokenStoreLike,
        *,
        cache: AccessTokenCache = ACCESS_TOKENS,
        client_factory: Callable[[float], httpx2.Client] = msidentity.default_client,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._account, self._store, self._cache = account, store, cache
        self._client_factory, self._clock = client_factory, clock

    @staticmethod
    def lock_wait(deadline: float, now: float) -> float:
        """L19: never longer than the provider timeout (also keeps far deadlines below threading.TIMEOUT_MAX)."""
        return min(max(deadline - now - 1.0, 0.0), PROVIDER_TIMEOUT_SECONDS)

    def invalidate(self) -> None:
        self._cache.drop(self._account.cache_key)

    def access_token(self, deadline: float) -> str:
        key = self._account.cache_key
        cached = self._cache.get(key, self._clock())
        if cached is not None:
            return cached
        lock = self._cache.lock_for(self._account.account_id)
        if not lock.acquire(timeout=self.lock_wait(deadline, self._clock())):
            raise ProviderError("upstream_timeout", "TokenLock")
        try:
            cached = self._cache.get(key, self._clock())
            return cached if cached is not None else self._refresh(deadline)
        finally:
            lock.release()

    def _log(self, level: int, outcome: str, exception: str | None = None, error_code: int | None = None) -> None:
        fields: dict[str, object] = {"account": self._account.account_id, "outcome": outcome}
        if exception is not None:  # absent rather than null, like the other events
            fields["exception"] = exception
        if error_code is not None:
            fields["error_code"] = error_code
        log_event(_log, level, "token_refresh", **fields)

    def _refresh(self, deadline: float) -> str:
        acct = self._account
        if not self._store.configured():
            raise ProviderError("upstream_error", "TokenStoreNotConfigured")
        try:
            cipher = self._store.cipher()
        except KeyUnavailableError as exc:
            self._log(logging.WARNING, "error", str(exc))
            raise ProviderError("upstream_error", "KeyUnavailable") from None
        answer: msidentity.TokenAnswer | None = None
        failure: ProviderError | None = None
        failure_code: int | None = None
        persisting = False
        try:
            with self._store.locked(acct.account_id) as row:
                if row.sealed is None:
                    self._log(logging.WARNING, "no_token")
                    raise ProviderError("auth_expired", "NoToken")
                if row.invalid_grant:
                    self._log(logging.WARNING, "invalid_grant_recorded")
                    raise ProviderError("auth_expired", "InvalidGrantRecorded")
                try:
                    refresh_token = cipher.open(row.sealed, account_id=acct.account_id, provider=acct.provider)
                except TokenDecryptError as exc:
                    self._log(logging.WARNING, "decrypt_failed", str(exc))
                    raise ProviderError("auth_expired", "TokenDecrypt") from None
                remaining = deadline - self._clock()
                if remaining <= 1.0:
                    raise ProviderError("upstream_timeout", "CallDeadline")
                try:
                    with self._client_factory(remaining) as client:
                        answer = msidentity.refresh(client, acct, refresh_token, deadline=deadline, clock=self._clock)
                except GrantError as exc:
                    failure_code = exc.error_code
                    if exc.status == 400 and exc.error in REVOKED_GRANT_ERRORS:  # sticky only for 400 (P6)
                        row.mark_invalid_grant()  # committed when the block ends normally
                        failure = ProviderError("auth_expired", "InvalidGrant")
                    elif exc.error in CLIENT_ERRORS:
                        failure = ProviderError("upstream_error", "ClientRejected")
                    else:
                        failure = ProviderError("upstream_error", "TokenEndpoint")
                else:
                    sealed = cipher.seal(answer.refresh_token, account_id=acct.account_id, provider=acct.provider)
                    persisting = True
                    row.rotate(sealed, answer.scope)  # a row under the previous key is re-encrypted here
            # The transaction is committed here; only now may the new tokens be used.
        except TokenStoreUnavailableError as exc:
            if persisting:
                self._log(logging.ERROR, "persist_failed", str(exc))
                raise ProviderError("upstream_error", "TokenPersist") from None
            self._log(logging.WARNING, "error", str(exc))
            raise ProviderError("upstream_error", "TokenStore") from None
        if failure is not None:
            outcome = "invalid_grant" if failure.code == "auth_expired" else "error"
            self._log(logging.WARNING, outcome, failure.cause, failure_code)
            raise failure
        if answer is None:  # unreachable: every path without an answer sets failure or raises
            raise ProviderError("upstream_error", "TokenEndpoint")
        self._cache.put(acct.cache_key, answer.access_token, self._clock() + answer.expires_in)
        self._log(logging.INFO, "ok")
        return answer.access_token
