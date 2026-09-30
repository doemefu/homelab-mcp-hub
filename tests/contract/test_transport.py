from starlette.testclient import TestClient

from tests.support.mcp import modern
from tests.support.tokens import TokenFactory


def test_wrong_host_with_valid_token_gets_421(client: TestClient, tokens: TokenFactory) -> None:
    assert modern(client, tokens.mint(), "tools/list", headers={"Host": "evil.example.org"}).status_code == 421


def test_wrong_host_without_token_gets_401(client: TestClient) -> None:
    assert modern(client, None, "tools/list", headers={"Host": "evil.example.org"}).status_code == 401


def test_disallowed_origin_gets_403_without_challenge(client: TestClient, tokens: TokenFactory) -> None:
    response = modern(client, tokens.mint(), "tools/list", headers={"Origin": "https://evil.example.org"})
    assert response.status_code == 403
    assert "www-authenticate" not in response.headers


def test_claude_origins_are_allowed(client: TestClient, tokens: TokenFactory) -> None:
    for origin in ("https://claude.ai", "https://claude.com"):
        assert modern(client, tokens.mint(), "tools/list", headers={"Origin": origin}).status_code == 200


def test_public_port_serves_no_health_routes(client: TestClient) -> None:
    assert client.get("/healthz").status_code == 404
    assert client.get("/readyz").status_code == 404
