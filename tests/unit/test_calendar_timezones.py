"""Time-zone definitions are isolated per calendar object (spec 080 rev. 4.5 D62): icalendar caches every unknown
VTIMEZONE process-wide by TZID, first writer wins, so one object could shift another object's events."""

import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from icalendar import Calendar
from icalendar.timezone import tzp

from mcp_hub.config import load_settings
from mcp_hub.health import StatusStore
from mcp_hub.providers import Adapters, caldav
from mcp_hub.providers.caldav import CalendarPage, ObjectSkippedError, expand
from mcp_hub.registry import Account, load_registry
from mcp_hub.tools import HubContext
from mcp_hub.tools.calendar import run_get_events

pytestmark = pytest.mark.anyio
ZURICH = ZoneInfo("Europe/Zurich")
START = datetime(2026, 10, 17, tzinfo=ZURICH)
END = datetime(2026, 11, 2, tzinfo=ZURICH)
CUSTOM = "Hub Probe Zone"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def vtimezone(tzid: str, offset: str) -> str:
    return (
        f"BEGIN:VTIMEZONE\r\nTZID:{tzid}\r\nBEGIN:STANDARD\r\nDTSTART:19700101T000000\r\nTZOFFSETFROM:{offset}\r\n"
        f"TZOFFSETTO:{offset}\r\nEND:STANDARD\r\nEND:VTIMEZONE\r\n"
    )


def obj(offset: str, uid: str, tzid: str = CUSTOM, rule: str = "", zones: str | None = None) -> bytes:
    """One event at 10:00-11:00 on 2026-10-20 in `tzid`, whose own VTIMEZONE says `offset`."""
    defined = vtimezone(tzid, offset) if zones is None else zones
    extra = f"RRULE:{rule}\r\n" if rule else ""
    return (
        f"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//mcp-hub tests//EN\r\n{defined}BEGIN:VEVENT\r\n"
        f"UID:{uid}@example.test\r\nDTSTAMP:20260901T000000Z\r\nDTSTART;TZID={tzid}:20261020T100000\r\n"
        f"DTEND;TZID={tzid}:20261020T110000\r\n{extra}SUMMARY:{uid}\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    ).encode()


HOSTILE = obj("+0500", "hostile")  # 10:00+05:00 == 07:00+02:00
LEGIT = obj("-0300", "legit")  # 10:00-03:00 == 15:00+02:00 (Zurich is +02:00 on 2026-10-20)


def starts(raw: bytes) -> list[str]:
    return [
        e.start.isoformat()
        for e in expand(raw, href="/h/", name="Home", start=START, end=END, zone=ZURICH, floating=ZURICH)
    ]


def cached(tzid: str) -> bool:
    """Whether icalendar resolves `tzid` from its process-wide cache (the provider does not know custom ids)."""
    return tzp.timezone(tzid) is not None


@pytest.mark.parametrize("order", ["hostile-first", "legit-first"])
def test_one_object_cannot_shift_another_within_one_call(order: str) -> None:
    from tests.unit.test_calendar_limits import source_for

    objects = (HOSTILE, LEGIT) if order == "hostile-first" else (LEGIT, HOSTILE)
    page = source_for(*objects).events(START, END, ZURICH, ZURICH)
    shown = {e.title: e.start.isoformat() for e in page.events}
    assert shown == {"hostile": "2026-10-20T07:00:00+02:00", "legit": "2026-10-20T15:00:00+02:00"}


def test_one_object_cannot_shift_another_across_calls() -> None:
    assert starts(HOSTILE) == ["2026-10-20T07:00:00+02:00"]
    assert starts(LEGIT) == ["2026-10-20T15:00:00+02:00"]
    assert starts(HOSTILE) == ["2026-10-20T07:00:00+02:00"]


def test_concurrent_expansions_keep_their_own_definitions() -> None:
    results: dict[str, list[str]] = {}
    barrier = threading.Barrier(2)

    def worker(name: str, raw: bytes) -> None:
        barrier.wait()
        found: list[str] = []
        for _ in range(20):
            found += starts(raw)
        results[name] = found

    threads = [threading.Thread(target=worker, args=a) for a in (("hostile", HOSTILE), ("legit", LEGIT))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert set(results["hostile"]) == {"2026-10-20T07:00:00+02:00"}
    assert set(results["legit"]) == {"2026-10-20T15:00:00+02:00"}


def test_an_iana_zone_cannot_be_redefined_by_an_object() -> None:
    assert starts(obj("+0500", "fake-zurich", tzid="Europe/Zurich")) == ["2026-10-20T10:00:00+02:00"]
    assert starts(obj("-0300", "real-zurich", tzid="Europe/Zurich")) == ["2026-10-20T10:00:00+02:00"]


@pytest.mark.parametrize("path", ["normal", "library-error", "refused", "deadline"])
def test_the_cache_is_empty_and_the_lock_free_after_every_exit_path(path: str) -> None:
    raw = {
        "normal": LEGIT,
        "library-error": obj("-0300", "broken", rule="FREQ=WEEKLY;BYDAY=XX"),
        "refused": obj("-0300", "refused", rule="FREQ=SECONDLY;COUNT=2"),
        "deadline": obj("-0300", "slow", rule="FREQ=DAILY;BYSETPOS=2"),
    }[path]
    ticks = [0.0]

    def clock() -> float:
        ticks[0] += 1e-5
        return ticks[0]

    try:
        caldav.expand(raw, href="/h/", name="Home", start=START, end=END, zone=ZURICH, floating=ZURICH, cpu_clock=clock)
        outcome = "ok"
    except ObjectSkippedError as exc:
        outcome = exc.reason
    except Exception:
        outcome = "error"
    assert (
        outcome
        == {"normal": "ok", "library-error": "error", "refused": "rule_refused", "deadline": "expansion_too_slow"}[path]
    )
    assert not cached(CUSTOM)
    assert not caldav.EXPANSION_LOCK.locked()


def test_many_unique_definitions_do_not_accumulate() -> None:
    for batch in range(20):
        zones = "".join(vtimezone(f"Probe Zone {batch}-{i}", "+0100") for i in range(20))
        assert starts(obj("+0100", f"many-{batch}", tzid=f"Probe Zone {batch}-0", zones=zones)) == [
            "2026-10-20T11:00:00+02:00"  # 10:00+01:00
        ]
    assert not any(cached(f"Probe Zone {batch}-{i}") for batch in range(20) for i in range(20))


def test_more_than_20_vtimezones_are_refused() -> None:
    zones = "".join(vtimezone(f"Probe Zone {i}", "+0100") for i in range(21))
    with pytest.raises(ObjectSkippedError) as caught:
        starts(obj("+0100", "many-zones", tzid="Probe Zone 0", zones=zones))
    assert caught.value.reason == "too_many_components"
    zones = "".join(vtimezone(f"Probe Zone {i}", "+0100") for i in range(20))
    assert len(starts(obj("+0100", "twenty-zones", tzid="Probe Zone 0", zones=zones))) == 1


async def test_instances_are_self_contained_after_the_cache_reset(secrets_dir: Path) -> None:
    class Custom:
        def __init__(self, account: Account, secrets_dir: Path) -> None:
            self.account = account

        def check(self) -> None: ...

        def events(self, start: datetime, end: datetime, zone: ZoneInfo, floating: ZoneInfo) -> CalendarPage:
            found = expand(LEGIT, href="/h/", name="Home", start=start, end=end, zone=zone, floating=floating)
            assert not cached(CUSTOM)  # the definition is gone before sorting, filtering and formatting run
            return CalendarPage(events=found, truncated=False)

    settings = load_settings({"HUB_SECRETS_DIR": str(secrets_dir)})
    ctx = HubContext(settings, load_registry(secrets_dir / "accounts.json"), StatusStore(), Adapters(calendar=Custom))
    for zone, expected in (("Europe/Zurich", "2026-10-20T15:00:00+02:00"), ("UTC", "2026-10-20T13:00:00+00:00")):
        result, _, _ = await run_get_events(
            ctx,
            start="2026-10-17T00:00:00+02:00",
            end="2026-11-02T00:00:00+01:00",
            account=None,
            timezone=zone,
            limit=None,
        )
        assert [i.start for i in result.items] == [expected]


def test_only_the_caldav_module_imports_the_calendar_libraries() -> None:
    # Scope note (D62): the guarded per-object entry point in providers/caldav.py is the only way into icalendar.
    src = Path(__file__).parents[2] / "src"
    offenders = [
        str(path.relative_to(src))
        for path in src.rglob("*.py")
        if path.name != "caldav.py"
        and re.search(r"^\s*(import|from)\s+(icalendar|recurring_ical_events|dateutil)\b", path.read_text(), re.M)
    ]
    assert offenders == []


def test_a_definition_cached_outside_the_guard_does_not_leak_into_an_object() -> None:
    # N8: the cache is also emptied BEFORE each object, against any other code that parses calendar data.
    bare = obj("+0000", "bare", zones="")  # uses the custom TZID without defining it
    reference = starts(bare)
    Calendar.from_ical(HOSTILE)  # outside expand(): icalendar caches "Hub Probe Zone" = +05:00 process-wide
    try:
        assert cached(CUSTOM)
        assert starts(bare) == reference
    finally:
        tzp.use_zoneinfo()


def test_objects_are_expanded_one_at_a_time(monkeypatch: pytest.MonkeyPatch) -> None:
    # N28: while object A (with its +05:00 definition) is being expanded, object B with the same TZID but no
    # definition must wait for the lock instead of seeing A's definition.
    bare = obj("+0000", "bare", zones="")
    reference = starts(bare)
    inside, release = threading.Event(), threading.Event()
    original = caldav._instances

    def blocking(calendar: Any, **kwargs: Any) -> Any:
        if any(str(c.get("UID", "")).startswith("hostile") for c in calendar.walk("VEVENT")):
            inside.set()
            release.wait(5)
        return original(calendar, **kwargs)

    monkeypatch.setattr(caldav, "_instances", blocking)
    results: dict[str, list[str]] = {}
    first = threading.Thread(target=lambda: results.__setitem__("hostile", starts(HOSTILE)))
    second = threading.Thread(target=lambda: results.__setitem__("bare", starts(bare)))
    first.start()
    assert inside.wait(5)
    second.start()
    second.join(0.3)
    assert second.is_alive()  # B waits for the lock while A is inside
    release.set()
    first.join(5)
    second.join(5)
    assert results == {"hostile": ["2026-10-20T07:00:00+02:00"], "bare": reference}


def _registry_size() -> int:
    import sys

    return sum(len(getattr(module, "__warningregistry__", {}) or {}) for module in list(sys.modules.values()))


def test_globally_unique_tzid_guesses_neither_warn_nor_grow_the_registry(caplog: pytest.LogCaptureFixture) -> None:
    # Delta review D7: one warning per distinct TZID text would grow the warnings registry and the log.
    import logging
    import warnings

    from icalendar.error import GloballyUniqueTZIDGuessed

    logging.captureWarnings(True)
    try:
        with warnings.catch_warnings(record=True) as seen, caplog.at_level(logging.DEBUG):
            warnings.simplefilter("always")  # worst case: every other warning is shown
            caldav.ignore_icalendar_tzid_guess_warnings()
            before = _registry_size()
            for i in range(10_000):
                assert tzp.timezone(f"/vendor-{i}/Europe/Zurich") is not None
            assert [w for w in seen if issubclass(w.category, GloballyUniqueTZIDGuessed)] == []
            assert _registry_size() == before
        assert not [r for r in caplog.records if "GloballyUnique" in r.getMessage() or r.name == "py.warnings"]
    finally:
        logging.captureWarnings(False)
        tzp.use_zoneinfo()  # timezone() cached the guessed zones under their primary ids
