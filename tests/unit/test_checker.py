import json
import logging
import threading
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import anyio
import pytest

from mcp_hub.checker import HealthChecker
from mcp_hub.config import load_settings
from mcp_hub.health import StatusStore
from mcp_hub.providers import Adapters
from mcp_hub.providers.base import ProviderError
from mcp_hub.providers.caldav import CalendarPage
from mcp_hub.registry import Account, load_registry
from mcp_hub.tools import HubContext
from tests.support.logfields import allowed_fields

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class Probe:
    """Fake mailbox/calendar: check() records the call and raises `error` if set."""

    def __init__(
        self, calls: list[tuple[str, str]], account: Account, capability: str, error: Exception | None
    ) -> None:
        self.calls, self.account, self.capability, self.error = calls, account, capability, error

    def check(self) -> None:
        self.calls.append((self.account.id, self.capability))
        if self.error is not None:
            raise self.error

    def events(self, start: datetime, end: datetime, zone: ZoneInfo, floating: ZoneInfo) -> CalendarPage:
        return CalendarPage(events=[], truncated=False)


def context(secrets_dir: Path, calls: list[tuple[str, str]], errors: dict[str, Exception] | None = None) -> HubContext:
    failing = errors or {}
    adapters = Adapters(
        mailbox=lambda account, _dir: Probe(calls, account, "mail", failing.get("mail")),  # type: ignore[arg-type,return-value]
        calendar=lambda account, _dir: Probe(calls, account, "calendar", failing.get("calendar")),
    )
    settings = load_settings({"HUB_SECRETS_DIR": str(secrets_dir)})
    return HubContext(settings, load_registry(secrets_dir / "accounts.json"), StatusStore(), adapters)


async def test_checks_enabled_capabilities_with_credentials_and_adapters(secrets_dir: Path) -> None:
    calls: list[tuple[str, str]] = []
    ctx = context(secrets_dir, calls)
    assert await HealthChecker(ctx, interval=60).check_once() == (2, 2)
    # gmail has no credential files, outlook has no adapter (Graph), uzh is disabled.
    assert sorted(calls) == [("icloud", "calendar"), ("icloud", "mail")]
    assert ctx.status.get("icloud", "mail").status == "ok"
    assert ctx.status.get("icloud", "calendar").status == "ok"
    assert ctx.status.get("gmail", "mail").status == "unknown"


async def test_failures_are_recorded_and_logged_without_detail(
    secrets_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    calls: list[tuple[str, str]] = []
    errors = {"mail": ProviderError("auth_expired", "LoginError"), "calendar": RuntimeError("secret detail")}
    ctx = context(secrets_dir, calls, errors)
    with caplog.at_level(logging.DEBUG, logger="mcp_hub"):
        assert await HealthChecker(ctx, interval=60).check_once() == (2, 0)
    assert ctx.status.get("icloud", "mail").status == "auth_expired"
    assert ctx.status.get("icloud", "calendar").status == "error"
    failed = [r for r in caplog.records if r.getMessage() == "status_check_failed"]
    assert len(failed) == 2
    by_capability = {r.fields["capability"]: r.fields for r in failed}  # type: ignore[attr-defined]
    assert by_capability["mail"] == {
        "account": "icloud",
        "capability": "mail",
        "outcome": "auth_expired",
        "exception": "LoginError",
    }
    assert by_capability["calendar"]["outcome"] == "upstream_error"
    assert by_capability["calendar"]["exception"] == "RuntimeError"
    for fields in by_capability.values():
        assert set(fields) <= allowed_fields("status_check_failed")
    assert "secret detail" not in caplog.text


async def test_run_waits_first_delay_then_repeats(secrets_dir: Path, caplog: pytest.LogCaptureFixture) -> None:
    calls: list[tuple[str, str]] = []
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 3:
            raise anyio.get_cancelled_exc_class()

    checker = HealthChecker(context(secrets_dir, calls), interval=1800, sleep=sleep)
    with caplog.at_level(logging.INFO, logger="mcp_hub"), pytest.raises(anyio.get_cancelled_exc_class()):
        await checker.run()
    assert sleeps == [30.0, 1800.0, 1800.0]
    assert len(calls) == 4  # two rounds of icloud mail + calendar
    cycles = [r.fields for r in caplog.records if r.getMessage() == "status_check_cycle"]  # type: ignore[attr-defined]
    assert cycles == [{"result_count": 2, "outcome": "ok"}, {"result_count": 2, "outcome": "ok"}]


async def test_run_survives_an_exception_in_a_cycle(
    secrets_dir: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    sleeps: list[float] = []
    rounds: list[int] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 3:
            raise anyio.get_cancelled_exc_class()

    checker = HealthChecker(context(secrets_dir, []), interval=60, first_delay=0, sleep=sleep)
    original = checker.check_once

    async def flaky() -> tuple[int, int]:
        rounds.append(1)
        if len(rounds) == 1:
            raise KeyError("registry state")
        return await original()

    monkeypatch.setattr(checker, "check_once", flaky)
    with caplog.at_level(logging.INFO, logger="mcp_hub"), pytest.raises(anyio.get_cancelled_exc_class()):
        await checker.run()
    cycles = [(r.levelno, r.fields) for r in caplog.records if r.getMessage() == "status_check_cycle"]  # type: ignore[attr-defined]
    assert cycles == [
        (logging.WARNING, {"outcome": "error", "exception": "KeyError"}),
        (logging.INFO, {"result_count": 2, "outcome": "ok"}),
    ]


async def test_slow_check_times_out(secrets_dir: Path) -> None:
    release = threading.Event()

    class Slow(Probe):
        def check(self) -> None:
            release.wait(5)

    adapters = Adapters(
        mailbox=lambda account, _dir: Slow([], account, "mail", None),  # type: ignore[arg-type,return-value]
        calendar=lambda account, _dir: Slow([], account, "calendar", None),
    )
    settings = load_settings({"HUB_SECRETS_DIR": str(secrets_dir)})
    ctx = HubContext(settings, load_registry(secrets_dir / "accounts.json"), StatusStore(), adapters)
    try:
        assert await HealthChecker(ctx, interval=60, timeout=0.2).check_once() == (2, 0)
    finally:
        release.set()
    assert ctx.status.get("icloud", "mail").status == "unreachable"
    assert ctx.status.get("icloud", "calendar").status == "unreachable"
    assert ctx.status.get("icloud", "calendar").last_error_code == "upstream_timeout"


async def test_list_accounts_reflects_background_results(secrets_dir: Path) -> None:
    from mcp_hub.tools.accounts import build_list_accounts

    ctx = context(secrets_dir, [])
    await HealthChecker(ctx, interval=60).check_once()
    [icloud] = [a for a in build_list_accounts(ctx).accounts if a.id == "icloud"]
    assert {c.capability: c.status for c in icloud.capabilities} == {"mail": "ok", "calendar": "ok"}


async def test_a_cycle_without_targets_is_not_ok(secrets_dir: Path, caplog: pytest.LogCaptureFixture) -> None:
    # Review 19 F10: stage a (every account disabled) checks nothing, so the cycle must not report "ok".
    data = json.loads((secrets_dir / "accounts.json").read_text())
    for entry in data["accounts"]:
        entry["enabled"] = False
    (secrets_dir / "accounts.json").write_text(json.dumps(data))
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 2:
            raise anyio.get_cancelled_exc_class()

    checker = HealthChecker(context(secrets_dir, []), interval=60, first_delay=0, sleep=sleep)
    with caplog.at_level(logging.INFO, logger="mcp_hub"), pytest.raises(anyio.get_cancelled_exc_class()):
        await checker.run()
    cycles = [r.fields for r in caplog.records if r.getMessage() == "status_check_cycle"]  # type: ignore[attr-defined]
    assert cycles == [{"result_count": 0, "outcome": "skipped"}]
