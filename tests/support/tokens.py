import base64
import json
import time
import uuid
from typing import Any

import jwt

from tests.support.keys import TestKey

ISSUER = "https://auth.furchert.ch"
RESOURCE = "https://mcp.furchert.ch/mcp"
CLIENT_ID = "claude-mcp-hub"
SUBJECT = "owner-test"
SCOPES = ["mail:read", "calendar:read"]


def _b64(data: dict[str, Any]) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()


class TokenFactory:
    def __init__(self, key: TestKey) -> None:
        self.key = key

    def claims(self, drop: tuple[str, ...] = (), **overrides: Any) -> dict[str, Any]:
        now = int(time.time())
        base: dict[str, Any] = {
            "iss": ISSUER,
            "sub": SUBJECT,
            "aud": [RESOURCE],
            "client_id": CLIENT_ID,
            "scope": SCOPES,
            "iat": now,
            "nbf": now,
            "exp": now + 600,
            "jti": uuid.uuid4().hex,
        }
        base.update(overrides)
        return {k: v for k, v in base.items() if k not in drop}

    def mint(self, *, header: dict[str, Any] | None = None, drop: tuple[str, ...] = (), **claims: Any) -> str:
        headers = {"typ": "at+jwt", "kid": self.key.kid} | (header or {})
        headers = {k: v for k, v in headers.items() if v is not None}
        return jwt.encode(self.claims(drop, **claims), self.key.private_key, algorithm="RS256", headers=headers)

    def hs256(self) -> str:
        return jwt.encode(
            self.claims(),
            "shared-secret-for-test-only-000000",
            algorithm="HS256",
            headers={"typ": "at+jwt", "kid": self.key.kid},
        )

    def alg_none(self) -> str:
        return f"{_b64({'alg': 'none', 'typ': 'at+jwt', 'kid': self.key.kid})}.{_b64(self.claims())}."
