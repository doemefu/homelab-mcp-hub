from starlette.testclient import TestClient

from mcp_hub.app import HubApp


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
