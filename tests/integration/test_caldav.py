"""CalDAV adapter against Radicale (spec 080 §10.2): the unit fixture set again, through a real CalDAV server."""

import json
import logging
import secrets
from collections.abc import Iterator
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from mcp_hub.checker import HealthChecker
from mcp_hub.config import load_settings
from mcp_hub.health import StatusStore
from mcp_hub.logging import configure_logging
from mcp_hub.providers import Adapters
from mcp_hub.providers.base import MAX_HTTP_RESPONSE_BYTES, ProviderError
from mcp_hub.providers.caldav import RawEvent
from mcp_hub.registry import load_registry
from mcp_hub.tools import HubContext
from mcp_hub.tools.accounts import build_list_accounts
from mcp_hub.tools.calendar import run_get_events
from tests.support import greenmail, ics, radicale
from tests.support.logcapture import Capture
from tests.support.logfields import allowed_fields

pytestmark = [pytest.mark.provider, pytest.mark.anyio]  # "integration" means "starts the hub"
ZURICH, UTC_ZONE = ZoneInfo("Europe/Zurich"), ZoneInfo("UTC")
START = datetime(2026, 10, 17, tzinfo=ZURICH)
END = datetime(2026, 11, 2, tzinfo=ZURICH)
FROM, TO = "2026-10-17T00:00:00+02:00", "2026-11-02T00:00:00+01:00"
EXPECTED = [  # (start, recurring, status) in W, Europe/Zurich (plan Task 3 Step 1)
    ("2026-10-18T10:00:00+02:00", True, "confirmed"),
    ("2026-10-19T09:00:00+02:00", True, "confirmed"),
    ("2026-10-21T08:00:00+02:00", False, "confirmed"),
    ("2026-10-21T11:00:00+02:00", True, "confirmed"),
    ("2026-10-23T09:00:00+02:00", True, "confirmed"),
    ("2026-10-24", False, "confirmed"),
    ("2026-10-25T10:00:00+01:00", True, "confirmed"),
    ("2026-10-27T13:00:00+01:00", False, "confirmed"),
    ("2026-10-28T14:00:00+01:00", False, "tentative"),
    ("2026-10-29T15:00:00+01:00", True, "confirmed"),
    ("2026-10-30T17:00:00+01:00", True, "confirmed"),
    ("2026-11-01T10:00:00+01:00", True, "confirmed"),
]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(scope="module", autouse=True)
def services() -> None:
    radicale.require()


@pytest.fixture(scope="module")
def fixture_calendar() -> str:
    name = radicale.calendar_name("fixtures")
    radicale.seed_fixtures(name)
    return name


def shown(event: RawEvent) -> str:
    return event.start.isoformat()


def big_event() -> bytes:
    """One VEVENT whose folded DESCRIPTION makes the REPORT answer larger than 5 MiB."""
    line = "DESCRIPTION:" + "x" * 62
    folded = "\r\n ".join([line] + ["y" * 73] * (MAX_HTTP_RESPONSE_BYTES // 73 + 20_000))
    return (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//mcp-hub tests//EN\r\nBEGIN:VEVENT\r\nUID:big@example.test\r\n"
        "DTSTAMP:20260901T000000Z\r\nDTSTART:20261020T100000Z\r\nDTEND:20261020T110000Z\r\nSUMMARY:Big\r\n"
        f"{folded}\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    ).encode()


def two_accounts(secrets_dir: Path) -> None:
    data = json.loads((secrets_dir / "accounts.json").read_text())
    icloud = next(a for a in data["accounts"] if a["id"] == "icloud")
    data["accounts"].append(icloud | {"id": "icloud2", "label": "iCloud 2"})
    (secrets_dir / "accounts.json").write_text(json.dumps(data))


def test_fixtures_through_radicale_match_the_unit_expectations(fixture_calendar: str) -> None:
    events = sorted(
        radicale.source([fixture_calendar]).events(START, END, ZURICH, ZURICH).events, key=lambda e: e.sort_key
    )
    assert [(shown(e), e.recurring, e.status) for e in events] == EXPECTED
    assert {e.calendar_name for e in events} == {fixture_calendar}
    [allday] = [e for e in events if e.all_day]
    assert (allday.start, allday.end) == (date(2026, 10, 24), date(2026, 10, 26))


def test_dst_in_utc_output(fixture_calendar: str) -> None:
    events = sorted(
        radicale.source([fixture_calendar]).events(START, END, UTC_ZONE, ZURICH).events, key=lambda e: e.sort_key
    )
    weekly = [shown(e) for e in events if e.title == "Weekly DST"]
    assert weekly[:2] == ["2026-10-18T08:00:00+00:00", "2026-10-25T09:00:00+00:00"]


def test_include_calendars_filters_by_display_name() -> None:
    wanted, other = radicale.calendar_name("wanted"), radicale.calendar_name("other")
    radicale.seed_fixtures(wanted, ("allday.ics",))
    radicale.seed_fixtures(other, ("cross-zone.ics",))
    events = radicale.source([wanted]).events(START, END, ZURICH, ZURICH).events
    assert [(e.title, e.calendar_name) for e in events] == [("Weekend away", wanted)]


def test_hostile_rule_is_skipped_next_to_normal_events(caplog: pytest.LogCaptureFixture) -> None:
    hostile = (
        b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//mcp-hub tests//EN\r\n"
        b"BEGIN:VEVENT\r\nUID:hostile-rule@example.test\r\n"
        b"DTSTAMP:20260901T000000Z\r\nDTSTART:20261020T100000Z\r\nDTEND:20261020T100001Z\r\n"
        b"RRULE:FREQ=SECONDLY;COUNT=10\r\nSUMMARY:Every second\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    name = radicale.calendar_name("hostile-rule")
    radicale.seed(name, [hostile, ics.load("dst-weekly.ics"), ics.load("allday.ics")])
    with caplog.at_level(logging.WARNING, logger="mcp_hub"):
        page = radicale.source([name]).events(START, END, ZURICH, ZURICH)
    assert sorted(e.title or "" for e in page.events) == ["Weekend away", "Weekly DST", "Weekly DST", "Weekly DST"]
    assert page.truncated is False
    skipped = [r.fields for r in caplog.records if r.getMessage() == "calendar_object_skipped"]  # type: ignore[attr-defined]
    assert skipped == [{"account": "icloud", "capability": "calendar", "outcome": "rule_refused"}]


def test_wrong_password_is_auth_expired(fixture_calendar: str) -> None:
    wrong = secrets.token_hex(12)  # generated, never a literal (secret scanners)
    source = radicale.source([fixture_calendar], password_override=wrong)
    for call in (source.check, lambda: source.events(START, END, ZURICH, ZURICH)):
        with pytest.raises(ProviderError) as caught:
            call()
        assert caught.value.code == "auth_expired"


async def test_report_above_limit_is_too_large(secrets_dir: Path, fixture_calendar: str) -> None:
    big = radicale.calendar_name("big")
    radicale.seed(big, [big_event()])
    with pytest.raises(ProviderError) as caught:
        radicale.source([big]).events(START, END, ZURICH, ZURICH)
    assert caught.value.code == "too_large"
    two_accounts(secrets_dir)
    adapters = Adapters(
        calendar=lambda account, _dir: radicale.source([big] if account.id == "icloud" else [fixture_calendar])
    )
    ctx = HubContext(
        load_settings({"HUB_SECRETS_DIR": str(secrets_dir)}),
        load_registry(secrets_dir / "accounts.json"),
        StatusStore(),
        adapters,
    )
    result, _, outcome = await run_get_events(ctx, start=FROM, end=TO, account=None, timezone=None, limit=None)
    assert [(e.account, e.code) for e in result.account_errors] == [("icloud", "too_large")]
    assert len(result.items) == 12
    assert {i.account for i in result.items} == {"icloud2"}
    assert outcome == "partial"


async def test_check_is_ok_and_feeds_list_accounts(secrets_dir: Path, fixture_calendar: str) -> None:
    adapters = Adapters(
        calendar=lambda account, _dir: radicale.source([fixture_calendar]),
        mailbox=lambda account, _dir: greenmail.mailbox("hub-get"),
    )
    ctx = HubContext(
        load_settings({"HUB_SECRETS_DIR": str(secrets_dir)}),
        load_registry(secrets_dir / "accounts.json"),
        StatusStore(),
        adapters,
    )
    assert await HealthChecker(ctx, interval=60, first_delay=0).check_once() == (2, 2)
    [icloud] = [a for a in build_list_accounts(ctx).accounts if a.id == "icloud"]
    assert {c.capability: c.status for c in icloud.capabilities} == {"mail": "ok", "calendar": "ok"}


@pytest.fixture
def captured() -> Iterator[Capture]:
    configure_logging("DEBUG")
    handler = Capture()
    logging.getLogger().addHandler(handler)
    yield handler
    logging.getLogger().removeHandler(handler)


async def test_caldav_logs_contain_no_urls_credentials_or_content(
    secrets_dir: Path, fixture_calendar: str, captured: Capture
) -> None:
    settings = load_settings({"HUB_SECRETS_DIR": str(secrets_dir), "LOG_LEVEL": "DEBUG"})
    registry = load_registry(secrets_dir / "accounts.json")
    ctx = HubContext(
        settings, registry, StatusStore(), Adapters(calendar=lambda a, d: radicale.source([fixture_calendar]))
    )
    result, _, _ = await run_get_events(ctx, start=FROM, end=TO, account="icloud", timezone=None, limit=None)
    assert len(result.items) == 12
    wrong = secrets.token_hex(12)
    failing = HubContext(
        settings,
        registry,
        StatusStore(),
        Adapters(
            calendar=lambda a, d: radicale.source([fixture_calendar], password_override=wrong),
            mailbox=lambda a, d: greenmail.mailbox("hub-logs", password=wrong),
        ),
    )
    await HealthChecker(failing, interval=60, first_delay=0).check_once()
    text = "\n".join(captured.lines) + "\n".join(m for _, _, m in captured.raw)
    forbidden = (
        radicale.LOGIN,
        radicale.password(),
        wrong,
        radicale.RUN,
        "5232",
        "127.0.0.1",
        "/hub-cal/",
        "Invoice",
        "evil.example.test",
        "organizer@example.test",
        "@example.test",
        "Weekly DST",
        "standup",
    )
    for value in forbidden:
        assert value not in text, value
    assert not [r for r in captured.raw if r[0].startswith(("httpx2", "httpcore2")) and r[1] < logging.WARNING]
    events = [json.loads(line) for line in captured.lines]
    assert all(set(e) <= allowed_fields(e["event"]) for e in events)
    assert any(
        e["event"] == "status_check_failed" and e["outcome"] == "auth_expired" and e["capability"] == "calendar"
        for e in events
    )
