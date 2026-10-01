"""Structural safeguard (second confirmation pass of PR #11): recurring-ical-events builds its dateutil rules with
cache=True, so dateutil keeps every occurrence it iterates from DTSTART. The hub switches that cache off for its own
calls, inside the guarded per-object section, so memory stays flat and only CPU grows (bounded by the deadline)."""

import contextlib
import inspect
import random
import tracemalloc
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import dateutil.rrule
import icalendar
import pytest
import recurring_ical_events.series.rrule as library_rrule

from mcp_hub.providers import caldav
from mcp_hub.providers.caldav import ObjectSkippedError, expand
from tests.support import ics
from tests.unit.test_calendar_iterations import _random_rule
from tests.unit.test_calendar_shapes import weekly_series

ZURICH = ZoneInfo("Europe/Zurich")
START = datetime(2026, 10, 17, tzinfo=ZURICH)
END = datetime(2026, 11, 17, tzinfo=ZURICH)
HOURS = ",".join(map(str, range(24)))


def obj(rule: str, start: str) -> bytes:
    dtstart = f"DTSTART;VALUE=DATE:{start}" if len(start) == 8 else f"DTSTART:{start}"
    return (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//mcp-hub tests//EN\r\nBEGIN:VEVENT\r\nUID:cache@example.test\r\n"
        f"DTSTAMP:20260901T000000Z\r\n{dtstart}\r\nRRULE:{rule}\r\nSUMMARY:Cache\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    ).encode()


def ours(raw: bytes) -> list[str]:
    return sorted(
        e.sort_key.isoformat()
        for e in expand(raw, href="/h/", name="H", start=START, end=END, zone=ZURICH, floating=ZURICH)
    )


def reference(raw: bytes) -> list[str]:
    """The library as shipped (cache=True), outside the hub's guard, with the hub's own window filter applied."""
    calendar = icalendar.Calendar.from_ical(raw)
    with_cache = caldav._instances(calendar, href="/h/", name="H", start=START, end=END, zone=ZURICH, floating=ZURICH)
    return sorted(e.sort_key.isoformat() for e in with_cache)


def test_the_patch_point_is_what_the_pinned_library_uses() -> None:
    # Fails loudly if a library upgrade moves or renames what the hub patches.
    assert library_rrule.rrulestr is dateutil.rrule.rrulestr
    assert library_rrule.rruleset is dateutil.rrule.rruleset
    source = inspect.getsource(library_rrule)
    assert "rrulestr(rule_string, dtstart=self.start, cache=True)" in source
    assert "rruleset(cache=True)" in source


def test_no_occurrence_cache_is_built_during_expansion(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[bool] = []
    rule_init, set_init = dateutil.rrule.rrule.__init__, dateutil.rrule.rruleset.__init__

    def recording_rule(self: Any, *args: Any, cache: bool = False, **kwargs: Any) -> None:
        seen.append(cache)
        rule_init(self, *args, cache=cache, **kwargs)

    def recording_set(self: Any, cache: bool = False) -> None:
        seen.append(cache)
        set_init(self, cache=cache)

    monkeypatch.setattr(dateutil.rrule.rrule, "__init__", recording_rule)
    monkeypatch.setattr(dateutil.rrule.rruleset, "__init__", recording_set)
    ours(ics.load("dst-weekly.ics"))
    assert seen
    assert not any(seen)
    assert library_rrule.rrulestr is dateutil.rrule.rrulestr  # restored after the object
    assert library_rrule.rruleset is dateutil.rrule.rruleset


def test_the_cache_is_restored_on_every_exit_path() -> None:
    for raw in (obj("FREQ=SECONDLY;COUNT=2", "20261020T100000Z"), obj("FREQ=WEEKLY;BYDAY=XX", "20261020T100000Z")):
        with pytest.raises(ObjectSkippedError):
            ours(raw)
        assert library_rrule.rrulestr is dateutil.rrule.rrulestr
        assert library_rrule.rruleset is dateutil.rrule.rruleset


@pytest.mark.parametrize(
    "raw",
    [
        *(ics.load(name) for name in ics.NAMES),
        ics.load("apple-birthday.ics"),
        weekly_series(600),
        obj("FREQ=DAILY;BYHOUR=7,19", "20200101T070000Z"),
        obj("FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR", "19900101T090000Z"),
        obj("FREQ=MONTHLY;BYDAY=1MO", "19500102T090000Z"),
        obj("FREQ=YEARLY;BYMONTH=5;BYDAY=-1MO", "19500529T090000Z"),
        obj("FREQ=DAILY", "20000101T090000Z"),
    ],
)
def test_results_are_identical_without_the_cache(raw: bytes) -> None:
    assert ours(raw) == reference(raw)


def test_results_are_identical_on_the_property_corpus() -> None:
    rng = random.Random(20261002)  # noqa: S311 - a seeded generator for reproducible test rules
    compared = 0
    for _ in range(600):
        text, start = _random_rule(rng)
        raw = obj(text, start.strftime("%Y%m%dT%H%M%SZ"))
        try:
            got = ours(raw)
        except Exception:  # noqa: S112 - refused, too slow or broken objects are not part of the comparison
            continue
        assert got == reference(raw), text
        compared += 1
        if compared == 300:
            break
    assert compared == 300


def test_memory_stays_flat_without_the_screen(monkeypatch: pytest.MonkeyPatch) -> None:
    # With the occurrence screen DISABLED, the review's 1800 shape iterated ~1.9 M occurrences into dateutil's cache
    # (187 MiB RSS). Without the cache only CPU grows; the 4 s deadline bounds that.
    monkeypatch.setattr(caldav, "_screen", lambda calendar, window_end: None)
    raw = obj(
        f"FREQ=YEARLY;BYMONTH=1,2,3,4,5,6,7,8,9,10,11,12;BYMONTHDAY={','.join(map(str, range(1, 32)))};BYHOUR={HOURS}",
        "18000101T000000Z",
    )
    tracemalloc.start()
    try:
        with contextlib.suppress(ObjectSkippedError):
            ours(raw)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert peak < 20 * 1024 * 1024
