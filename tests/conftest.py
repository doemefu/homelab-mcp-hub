import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest
from pytest_httpserver import HTTPServer
from starlette.testclient import TestClient

from mcp_hub.app import HubApp, create_app
from mcp_hub.config import Settings, load_settings
from mcp_hub.logging import configure_logging
from mcp_hub.registry import load_registry
from tests.support.clock import FakeClock
from tests.support.keys import TestKey, jwks
from tests.support.tokens import SUBJECT, TokenFactory

FIXTURES = Path(__file__).parent / "fixtures"
JWKS_PATH = "/jwks-7f3a.json"  # sentinel: must never appear in logs


@pytest.fixture(scope="session")
def key() -> TestKey:
    return TestKey("hub-test-1")


@pytest.fixture
def tokens(key: TestKey) -> TokenFactory:
    return TokenFactory(key)


@pytest.fixture
def jwks_server(httpserver: HTTPServer, key: TestKey) -> HTTPServer:
    httpserver.expect_request(JWKS_PATH).respond_with_json(jwks(key))
    return httpserver


@pytest.fixture
def secrets_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "secrets"
    directory.mkdir()
    shutil.copy(FIXTURES / "accounts.example.json", directory / "accounts.json")
    (directory / "allowed-subjects").write_text(SUBJECT + "\n")
    for ref in ("icloud-username", "icloud-app-password"):
        (directory / ref).write_text("placeholder")
    return directory


@pytest.fixture
def settings(jwks_server: HTTPServer, secrets_dir: Path) -> Settings:
    return load_settings(
        {
            "AUTH_JWKS_URL": jwks_server.url_for(JWKS_PATH),
            "HUB_SECRETS_DIR": str(secrets_dir),
            "LOG_LEVEL": "DEBUG",
        }
    )


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def hub(settings: Settings, secrets_dir: Path, clock: FakeClock) -> HubApp:
    configure_logging(settings.log_level)
    return create_app(settings, load_registry(secrets_dir / "accounts.json"), monotonic=clock)


@pytest.fixture
def client(hub: HubApp) -> Iterator[TestClient]:
    with TestClient(hub.public, base_url="https://mcp.furchert.ch") as test_client:
        yield test_client
