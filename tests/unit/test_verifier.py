import logging
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from pytest_httpserver import HTTPServer

from mcp_hub.auth import HubTokenVerifier, SubjectAllowlist
from mcp_hub.jwks import JwksCache
from tests.support.clock import FakeClock
from tests.support.keys import TestKey, jwks
from tests.support.tokens import CLIENT_ID, ISSUER, RESOURCE, SUBJECT, TokenFactory

pytestmark = pytest.mark.anyio
KEY = TestKey("k1")
T = TokenFactory(KEY)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def verifier(httpserver: HTTPServer, tmp_path: Path) -> HubTokenVerifier:
    httpserver.expect_request("/jwks.json").respond_with_json(jwks(KEY))
    (tmp_path / "allowed-subjects").write_text(SUBJECT + "\n")
    return HubTokenVerifier(
        issuer=ISSUER,
        resource=RESOURCE,
        client_id=CLIENT_ID,
        leeway=60,
        jwks=JwksCache(httpserver.url_for("/jwks.json"), clock=FakeClock()),
        allowlist=SubjectAllowlist(tmp_path / "allowed-subjects", clock=FakeClock()),
    )


async def test_valid_token_maps_to_access_token(verifier: HubTokenVerifier) -> None:
    token = T.mint()
    access = await verifier.verify_token(token)
    assert access is not None
    assert access.client_id == CLIENT_ID
    assert access.subject == SUBJECT
    assert access.scopes == ["mail:read", "calendar:read"]
    assert access.resource == RESOURCE
    assert access.claims is not None
    assert set(access.claims) == {"iss", "sub", "client_id", "jti"}


async def test_expires_at_includes_leeway(verifier: HubTokenVerifier) -> None:
    exp = int(time.time()) - 30
    access = await verifier.verify_token(T.mint(exp=exp))
    assert access is not None
    assert access.expires_at == exp + 60  # SDK compares expires_at without leeway (K1)


@pytest.mark.parametrize(
    ("token_fn", "check"),
    [
        (lambda: "not-a-jwt", "malformed"),
        (lambda: T.hs256(), "algorithm"),
        (lambda: T.alg_none(), "algorithm"),
        (lambda: T.mint(header={"typ": "JWT"}), "type"),
        (lambda: T.mint(header={"typ": None}), "type"),
        (lambda: T.mint(header={"kid": "other"}), "signature"),
        (lambda: T.mint(header={"kid": None}), "signature"),
        (lambda: TokenFactory(TestKey("k1")).mint(), "signature"),
        (lambda: T.mint(iss="https://evil.example.org"), "issuer"),
        (lambda: T.mint(aud=["claude-mcp-hub"]), "audience"),
        (lambda: T.mint(exp=int(time.time()) - 90), "time"),  # spec §4.3 row 6: 90 s in the past is rejected
        (lambda: T.mint(nbf=int(time.time()) + 120), "time"),
        (lambda: T.mint(drop=("client_id",)), "required_claims"),
        (lambda: T.mint(drop=("scope",)), "required_claims"),
        (lambda: T.mint(drop=("sub",)), "required_claims"),
        (lambda: T.mint(drop=("iat",)), "required_claims"),
        (lambda: T.mint(drop=("exp",)), "required_claims"),
        (lambda: T.mint(drop=("iss",)), "required_claims"),
        (lambda: T.mint(drop=("aud",)), "required_claims"),
        (lambda: T.mint(client_id="furchert-ch"), "client"),
        (lambda: T.mint(scope=7), "scope_format"),
        (lambda: T.mint(scope=["mail:read", 1]), "scope_format"),
        (lambda: T.mint(sub="someone-else"), "subject"),
    ],
)
async def test_rejections_name_the_check(
    verifier: HubTokenVerifier, caplog: pytest.LogCaptureFixture, token_fn: Callable[[], str], check: str
) -> None:
    logging.getLogger("mcp_hub").addHandler(caplog.handler)
    logging.getLogger("mcp_hub").setLevel(logging.INFO)
    token = token_fn()
    assert await verifier.verify_token(token) is None
    rejected = [r for r in caplog.records if r.getMessage() == "token_rejected"]
    assert rejected
    assert rejected[-1].fields == {"check": check}
    assert token not in caplog.text


async def test_space_delimited_scope_string(verifier: HubTokenVerifier) -> None:
    access = await verifier.verify_token(T.mint(scope="mail:read calendar:read"))
    assert access is not None
    assert access.scopes == ["mail:read", "calendar:read"]
