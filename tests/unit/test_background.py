import threading
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from starlette.testclient import TestClient

from mcp_hub import checker
from mcp_hub.app import create_app
from mcp_hub.config import Settings, load_settings
from mcp_hub.providers import Adapters
from mcp_hub.providers.caldav import RawEvent
from mcp_hub.registry import Account, load_registry


class Recorder:
    def __init__(self) -> None:
        self.checks = threading.Semaphore(0)
        self.count = 0

    def factory(self, account: Account, secrets_dir: Path) -> "Recorder":
        return self

    def check(self) -> None:
        self.count += 1
        self.checks.release()

    def events(self, start: datetime, end: datetime, zone: ZoneInfo, floating: ZoneInfo) -> list[RawEvent]:
        return []


def settings_with(settings: Settings, enabled: str) -> Settings:
    return load_settings(
        {
            "AUTH_JWKS_URL": settings.auth_jwks_url,
            "HUB_SECRETS_DIR": str(settings.secrets_dir),
            "HUB_HEALTH_CHECK_INTERVAL_SECONDS": "60",
            "HUB_STATUS_CHECK_ENABLED": enabled,
        }
    )


def test_background_check_starts_and_stops_with_lifespan(
    settings: Settings, secrets_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(checker, "FIRST_CHECK_DELAY_SECONDS", 0.0)
    stopped = threading.Event()
    original_run = checker.HealthChecker.run

    async def run(self: checker.HealthChecker) -> None:
        try:
            await original_run(self)
        finally:
            stopped.set()

    monkeypatch.setattr(checker.HealthChecker, "run", run)
    recorder = Recorder()
    adapters = Adapters(mailbox=recorder.factory, calendar=recorder.factory)  # type: ignore[arg-type]
    hub = create_app(
        settings_with(settings, "true"),
        load_registry(secrets_dir / "accounts.json"),
        adapters=adapters,
        background_checks=True,
    )
    with TestClient(hub.asgi, base_url="http://127.0.0.1:8084"):
        assert recorder.checks.acquire(timeout=5)  # icloud mail or calendar checked inside the lifespan
        assert not stopped.is_set()
    assert stopped.wait(5)  # the task was cancelled when the lifespan ended


@pytest.mark.parametrize(("background", "enabled"), [(False, "true"), (True, "false")], ids=["default", "env-off"])
def test_no_background_task_unless_requested_and_enabled(
    settings: Settings, secrets_dir: Path, monkeypatch: pytest.MonkeyPatch, background: bool, enabled: str
) -> None:
    monkeypatch.setattr(checker, "FIRST_CHECK_DELAY_SECONDS", 0.0)
    recorder = Recorder()
    adapters = Adapters(mailbox=recorder.factory, calendar=recorder.factory)  # type: ignore[arg-type]
    hub = create_app(
        settings_with(settings, enabled),
        load_registry(secrets_dir / "accounts.json"),
        adapters=adapters,
        background_checks=background,
    )
    with TestClient(hub.asgi, base_url="http://127.0.0.1:8084") as internal:
        assert internal.get("/readyz").status_code == 200
        assert not recorder.checks.acquire(timeout=0.5)
    assert recorder.count == 0
