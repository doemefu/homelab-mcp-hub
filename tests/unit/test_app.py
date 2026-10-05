import pytest
from starlette.testclient import TestClient

import mcp_hub.app as app_module
from mcp_hub.app import HubApp
from mcp_hub.config import Settings
from mcp_hub.registry import load_registry
from mcp_hub.tokenstore.store import StoreConfig


def test_internal_port_serves_health_only(hub: HubApp) -> None:
    with TestClient(hub.asgi, base_url="http://127.0.0.1:8084") as internal:
        assert internal.get("/healthz").json() == {"status": "ok"}
        assert internal.get("/readyz").status_code == 200
        assert internal.post("/mcp", json={}).status_code == 404


def test_readyz_is_503_before_startup(hub: HubApp) -> None:
    internal = TestClient(hub.asgi, base_url="http://127.0.0.1:8084")  # no context manager: no lifespan
    assert internal.get("/readyz").status_code == 503


def test_public_port_via_dispatcher_reaches_mcp(hub: HubApp) -> None:
    with TestClient(hub.asgi, base_url="https://mcp.furchert.ch") as public:
        assert public.get("/.well-known/oauth-protected-resource/mcp").status_code == 200


def test_production_adapters_get_the_token_store_configuration(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    stores: list[StoreConfig | None] = []
    real = app_module.MailboxOpener

    def recording(store: StoreConfig | None = None) -> object:
        stores.append(store)
        return real(store)

    monkeypatch.setattr(app_module, "MailboxOpener", recording)
    app_module.create_app(settings, load_registry(settings.secrets_dir / "accounts.json"))
    assert stores == [StoreConfig.from_settings(settings)]
