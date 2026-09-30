"""auth-service JWKS cache: offline key lookup with refetch throttling (spec 080 §4.3 row 1)."""

import logging
import time
from collections.abc import Callable
from typing import Any, Final

import anyio
import httpx2
import jwt

from mcp_hub.logging import log_event

MAX_JWKS_BYTES: Final = 65536
_log = logging.getLogger("mcp_hub.jwks")


class JwksCache:
    def __init__(
        self,
        url: str,
        *,
        min_refetch_interval: float = 60.0,
        max_age: float = 3600.0,
        clock: Callable[[], float] = time.monotonic,
        timeout: float = 5.0,
    ) -> None:
        self._url = url
        self._min_refetch_interval = min_refetch_interval
        self._max_age = max_age
        self._clock = clock
        self._timeout = timeout
        self._keys: dict[str, jwt.PyJWK] = {}
        self._fetched_at: float | None = None
        self._attempted_at: float | None = None
        self._lock = anyio.Lock()

    async def get_key(self, kid: str) -> jwt.PyJWK | None:
        key = self._keys.get(kid)
        if key is not None and not self._stale():
            return key
        async with self._lock:
            key = self._keys.get(kid)
            if key is not None and not self._stale():
                return key
            if self._may_fetch():
                await self._refresh()
            return self._keys.get(kid)

    def _stale(self) -> bool:
        return self._fetched_at is None or self._clock() - self._fetched_at >= self._max_age

    def _may_fetch(self) -> bool:
        return self._attempted_at is None or self._clock() - self._attempted_at >= self._min_refetch_interval

    async def _refresh(self) -> None:
        self._attempted_at = self._clock()
        try:
            async with httpx2.AsyncClient(timeout=self._timeout, trust_env=False, follow_redirects=False) as client:
                response = await client.get(self._url)
            response.raise_for_status()
            if len(response.content) > MAX_JWKS_BYTES:
                raise ValueError("JWKS document too large")
            keys = _parse(response.json())
        except Exception as exc:  # keep the previous keys; a token that needs a new key gets 401
            log_event(_log, logging.WARNING, "jwks_fetch_failed", exception=type(exc).__name__)
            return
        self._keys = keys
        self._fetched_at = self._clock()
        log_event(_log, logging.INFO, "jwks_refreshed", key_count=len(keys))


def _parse(document: Any) -> dict[str, jwt.PyJWK]:
    if not isinstance(document, dict) or not isinstance(document.get("keys"), list):
        raise ValueError("not a JWK set")
    keys: dict[str, jwt.PyJWK] = {}
    for entry in document["keys"]:
        if not isinstance(entry, dict) or entry.get("kty") != "RSA" or entry.get("use", "sig") != "sig":
            continue
        kid = entry.get("kid")
        if not isinstance(kid, str) or not kid:
            continue
        try:
            keys[kid] = jwt.PyJWK(entry, algorithm="RS256")
        except jwt.PyJWTError:
            continue
    return keys
