from pathlib import Path

import pytest

from mcp_hub.checker import HealthChecker
from mcp_hub.config import load_settings
from mcp_hub.health import StatusStore
from mcp_hub.providers import Adapters
from mcp_hub.providers.base import ProviderError
from mcp_hub.registry import Account, load_registry
from mcp_hub.tools import HubContext

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def test_graph_account_is_checked_once_token_store_files_exist(secrets_dir: Path) -> None:
    checked: list[str] = []

    class Box:
        def __init__(self, account: Account) -> None:
            self.account = account

        def check(self) -> None:
            checked.append(self.account.id)
            if self.account.id == "outlook":
                raise ProviderError("auth_expired", "NoToken")

    settings = load_settings({"HUB_SECRETS_DIR": str(secrets_dir)})
    status = StatusStore()
    adapters = Adapters(mailbox=lambda a, d: Box(a), calendar=lambda a, d: Box(a))  # type: ignore[arg-type,return-value]
    hub = HubContext(settings, load_registry(secrets_dir / "accounts.json"), status, adapters)
    checker = HealthChecker(hub, interval=60, first_delay=0)
    await checker.check_once()
    assert "outlook" not in checked  # token-store files missing -> disabled, not probed
    (secrets_dir / "outlook-ms-client-id").write_text("placeholder")
    await checker.check_once()
    assert "outlook" not in checked  # client id alone is not enough: the token-store files are credentials too
    for ref in ("db-username", "db-password", "token-encryption-key"):
        (secrets_dir / ref).write_text("placeholder")
    await checker.check_once()
    assert "outlook" in checked
    assert status.get("outlook", "mail").status == "auth_expired"
