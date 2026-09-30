"""Token verification (spec 080 §4.3 rows 1-8) and the subject allowlist (kill switch, §4.6 L2)."""

import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final

import jwt
from mcp.server.auth.provider import AccessToken

from mcp_hub.jwks import JwksCache
from mcp_hub.logging import log_event

_log = logging.getLogger("mcp_hub.auth")


class SubjectAllowlist:
    """One subject per line, exact case-sensitive match after trimming; missing or empty file = nobody."""

    def __init__(
        self, path: Path, *, reload_interval: float = 60.0, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._path = path
        self._reload_interval = reload_interval
        self._clock = clock
        self._subjects: frozenset[str] = frozenset()
        self._loaded_at: float | None = None

    def contains(self, subject: str) -> bool:
        if self._loaded_at is None or self._clock() - self._loaded_at >= self._reload_interval:
            self._subjects = self._load()
            self._loaded_at = self._clock()
        return bool(subject) and subject in self._subjects

    def _load(self) -> frozenset[str]:
        try:
            text = self._path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError) as exc:  # non-UTF-8 file = nobody, re-read after the interval
            log_event(_log, logging.WARNING, "allowlist_unavailable", exception=type(exc).__name__)
            return frozenset()
        subjects = frozenset(line.strip() for line in text.splitlines() if line.strip())
        if not subjects:
            log_event(_log, logging.WARNING, "allowlist_empty")
        return subjects


ACCEPTED_TYP: Final = frozenset({"at+jwt", "application/at+jwt"})
REQUIRED_CLAIMS: Final = ("iss", "aud", "sub", "exp", "iat", "client_id", "scope")


class TokenRejectedError(Exception):
    def __init__(self, check: str) -> None:
        super().__init__(check)
        self.check = check


class HubTokenVerifier:
    """Offline validation against the auth-service JWKS. Scopes (row 9) are left to the SDK's required_scopes."""

    def __init__(
        self, *, issuer: str, resource: str, client_id: str, leeway: int, jwks: JwksCache, allowlist: SubjectAllowlist
    ) -> None:
        self._issuer = issuer
        self._resource = resource
        self._client_id = client_id
        self._leeway = leeway
        self._jwks = jwks
        self._allowlist = allowlist

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            return await self._verify(token)
        except TokenRejectedError as rejected:
            log_event(_log, logging.INFO, "token_rejected", check=rejected.check)
        except Exception as exc:  # never let a token turn into a 500
            log_event(_log, logging.WARNING, "token_rejected", check="internal", exception=type(exc).__name__)
        return None

    async def _verify(self, token: str) -> AccessToken:
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError:
            raise TokenRejectedError("malformed") from None
        if header.get("alg") != "RS256":
            raise TokenRejectedError("algorithm")
        typ = header.get("typ")
        if not isinstance(typ, str) or typ.lower() not in ACCEPTED_TYP:
            raise TokenRejectedError("type")
        kid = header.get("kid")
        key = await self._jwks.get_key(kid) if isinstance(kid, str) and kid else None
        if key is None:
            raise TokenRejectedError("signature")
        claims = self._decode(token, key)
        if claims.get("client_id") != self._client_id:
            raise TokenRejectedError("client")
        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject:
            raise TokenRejectedError("required_claims")
        scopes = _scopes(claims.get("scope"))
        if not self._allowlist.contains(subject):
            raise TokenRejectedError("subject")
        return AccessToken(
            token=token,
            client_id=self._client_id,
            scopes=scopes,
            # The SDK re-checks expires_at against the wall clock without leeway (spec 080 §4.3 row 6).
            expires_at=int(claims["exp"]) + self._leeway,
            resource=self._resource,
            subject=subject,
            claims={"iss": claims["iss"], "sub": subject, "client_id": self._client_id, "jti": claims.get("jti")},
        )

    def _decode(self, token: str, key: jwt.PyJWK) -> dict[str, Any]:
        try:
            claims: dict[str, Any] = jwt.decode(
                token,
                key.key,
                algorithms=["RS256"],
                issuer=self._issuer,
                audience=self._resource,
                leeway=self._leeway,
                options={"require": list(REQUIRED_CLAIMS)},
            )
        except (jwt.ExpiredSignatureError, jwt.ImmatureSignatureError):
            raise TokenRejectedError("time") from None
        except jwt.InvalidIssuerError:
            raise TokenRejectedError("issuer") from None
        except jwt.InvalidAudienceError:
            raise TokenRejectedError("audience") from None
        except jwt.MissingRequiredClaimError:
            raise TokenRejectedError("required_claims") from None
        except (jwt.InvalidSignatureError, jwt.InvalidAlgorithmError):
            raise TokenRejectedError("signature") from None
        except jwt.PyJWTError:
            raise TokenRejectedError("malformed") from None
        return claims


def _scopes(value: object) -> list[str]:
    if isinstance(value, str):
        return value.split()
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return list(value)
    raise TokenRejectedError("scope_format")
