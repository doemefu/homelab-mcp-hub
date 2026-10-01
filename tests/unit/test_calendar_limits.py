"""Bounded recurrence expansion (spec 080 rev. 4.5 D62): screening, CPU deadline per object, caps, budget, order,
negative cache."""

import logging
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx2
import pytest

from mcp_hub.providers import caldav
from mcp_hub.providers.caldav import (
    MAX_INSTANCES_PER_CALL,
    MAX_INSTANCES_PER_OBJECT,
    OBJECT_CPU_SECONDS,
    CalDavCalendarSource,
    ObjectSkippedError,
    SlowObjectCache,
    expand,
)
from tests.support import ics
from tests.support.dav_transport import RecordingTransport, collection, dav, home_set, multistatus, principal, report
from tests.support.logfields import allowed_fields

ZURICH = ZoneInfo("Europe/Zurich")
START = datetime(2026, 10, 17, tzinfo=ZURICH)
END = datetime(2026, 11, 17, tzinfo=ZURICH)  # the widest allowed window (31 days)
PASSWORD = "unit-test-password"  # throwaway value for an in-process transport
NO_MATCH = [
    "FREQ=DAILY;BYMONTH=2;BYMONTHDAY=30",
    "FREQ=DAILY;BYSETPOS=2",
    "FREQ=DAILY;INTERVAL=7;BYDAY=MO",
    "FREQ=WEEKLY;BYMONTH=2;BYMONTHDAY=30",
    "FREQ=WEEKLY;BYDAY=MO;BYSETPOS=8",
    "FREQ=MONTHLY;BYMONTH=2;BYMONTHDAY=30",
    "FREQ=MONTHLY;BYDAY=MO;BYSETPOS=6",
    "FREQ=MONTHLY;BYDAY=1MO;BYMONTHDAY=20",
]


def stamp(value: datetime) -> str:
    return value.strftime("%Y%m%dT%H%M%SZ")


def obj(*lines: str, uid: str = "limit@example.test", start: str = "20261020T100000Z") -> bytes:
    """One VEVENT object; DTSTART is a Tuesday inside the window unless overridden."""
    body = "\r\n".join(lines)
    return (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//mcp-hub tests//EN\r\nBEGIN:VEVENT\r\n"
        f"UID:{uid}\r\nDTSTAMP:20260901T000000Z\r\nDTSTART:{start}\r\nDTEND:{start[:9]}235900Z\r\nSUMMARY:{uid}\r\n"
        + (body + "\r\n" if body else "")
        + "END:VEVENT\r\nEND:VCALENDAR\r\n"
    ).encode()


def single(n: int) -> bytes:
    return obj(uid=f"single-{n}@example.test", start=stamp(datetime(2026, 10, 18, 8) + timedelta(days=n % 20)))


def rdates(count: int, uid: str, minutes: int = 30) -> bytes:
    first = datetime(2026, 10, 21, 0)
    values = ",".join(stamp(first + timedelta(minutes=minutes * i)) for i in range(count))
    return obj(f"RDATE:{values}", uid=uid, start=stamp(first))


class FakeTime:
    """One fake clock for both the CPU deadline and the budget: every reading advances it by `step`."""

    def __init__(self, step: float = 1e-5) -> None:  # a normal object makes ~1,000-7,500 Python calls
        self.now, self.step = 0.0, step

    def __call__(self) -> float:
        self.now += self.step
        return self.now


def run(raw: bytes, **kwargs: Any) -> list[caldav.RawEvent]:
    return expand(raw, href="/h/", name="Home", start=START, end=END, zone=ZURICH, floating=ZURICH, **kwargs)


def source_for(*objects: bytes, **kwargs: Any) -> CalDavCalendarSource:
    """A CalDAV source whose single calendar answers the REPORT with `objects` (in this order)."""
    recorder = RecordingTransport(
        {
            ("PROPFIND", "https://dav.example.test:443/"): dav(principal("/p/")),
            ("PROPFIND", "https://dav.example.test:443/p/"): dav(home_set("/h/")),
            ("PROPFIND", "https://dav.example.test:443/h/"): dav(multistatus(collection("/h/home/", "Home"))),
            ("REPORT", "https://dav.example.test:443/h/home/"): dav(report(*objects)),
        }
    )

    def factory(username: str, password: str, timeout: float) -> httpx2.Client:
        return httpx2.Client(auth=(username, password), transport=recorder.transport(), trust_env=False)

    kwargs.setdefault("slow_objects", SlowObjectCache())
    return CalDavCalendarSource(
        "icloud",
        url="https://dav.example.test/",
        username="u",
        password=PASSWORD,
        include="all",
        client_factory=factory,
        **kwargs,
    )


@pytest.fixture
def expander_calls(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Counts calls into the expansion library."""
    calls: list[int] = []
    original = caldav.recurring_ical_events.of

    def spy(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(caldav.recurring_ical_events, "of", spy)
    return calls


def skipped(caplog: pytest.LogCaptureFixture) -> list[dict[str, object]]:
    lines = [r.fields for r in caplog.records if r.getMessage() == "calendar_object_skipped"]  # type: ignore[attr-defined]
    assert all(set(f) <= allowed_fields("calendar_object_skipped") for f in lines)
    return lines


# --- screening -----------------------------------------------------------------------------------------------------


EXDATES_1001 = ",".join(stamp(datetime(2027, 1, 1) + timedelta(days=i)) for i in range(1001))
REFUSED = {
    "hourly": (obj("RRULE:FREQ=HOURLY"), "rule_refused"),
    "minutely": (obj("RRULE:FREQ=MINUTELY;COUNT=10"), "rule_refused"),
    "secondly": (obj("RRULE:FREQ=SECONDLY;COUNT=10"), "rule_refused"),
    "two-rrules": (obj("RRULE:FREQ=DAILY;COUNT=2", "RRULE:FREQ=WEEKLY;COUNT=2"), "rule_refused"),
    "exrule": (obj("RRULE:FREQ=DAILY;COUNT=2", "EXRULE:FREQ=DAILY;COUNT=1"), "rule_refused"),
    "1001-rdates": (rdates(1001, "rdates-1001@example.test"), "too_many_dates"),
    "1001-exdates": (obj("RRULE:FREQ=DAILY", f"EXDATE:{EXDATES_1001}"), "too_many_dates"),
    "before-1900": (obj("RRULE:FREQ=YEARLY", start="18991231T100000Z"), "start_out_of_range"),
}


@pytest.mark.parametrize("case", list(REFUSED))
def test_screening_refuses_without_calling_the_expander(case: str, expander_calls: list[int]) -> None:
    raw, reason = REFUSED[case]
    with pytest.raises(ObjectSkippedError) as caught:
        run(raw)
    assert caught.value.reason == reason
    assert expander_calls == []


def test_screening_boundaries_are_allowed(expander_calls: list[int]) -> None:
    assert len(run(rdates(1000, "rdates-1000@example.test"))) == 1000
    assert len(run(obj("RRULE:FREQ=YEARLY", start="19000101T100000Z"))) == 0  # no instance in the window, but allowed
    exdates = ",".join(stamp(datetime(2027, 1, 1, 10) + timedelta(days=i)) for i in range(1000))
    assert len(run(obj("RRULE:FREQ=DAILY", f"EXDATE:{exdates}"))) == 28
    assert len(expander_calls) == 3


def test_refused_object_does_not_affect_the_others(caplog: pytest.LogCaptureFixture) -> None:
    hostile = obj("RRULE:FREQ=SECONDLY;COUNT=10", uid="hostile@example.test")
    with caplog.at_level(logging.WARNING, logger="mcp_hub"):
        page = source_for(hostile, ics.load("dst-weekly.ics")).events(START, END, ZURICH, ZURICH)
    assert [e.title for e in page.events] == ["Weekly DST"] * 3
    assert page.truncated is False  # a skipped object is not a truncation (D62)
    assert skipped(caplog) == [{"account": "icloud", "capability": "calendar", "outcome": "rule_refused"}]


# --- CPU deadline per object (D62 §1a) -----------------------------------------------------------------------------


@pytest.mark.parametrize("rule", NO_MATCH)
def test_no_match_rules_stop_at_the_cpu_deadline(rule: str) -> None:
    clock = FakeTime()
    with pytest.raises(ObjectSkippedError) as caught:
        run(obj(f"RRULE:{rule}"), cpu_clock=clock)
    assert caught.value.reason == "expansion_too_slow"
    assert OBJECT_CPU_SECONDS <= clock.now < OBJECT_CPU_SECONDS + 1.0


def test_a_real_no_match_rule_stops_within_the_cpu_deadline() -> None:
    started = time.monotonic()
    with pytest.raises(ObjectSkippedError) as caught:
        run(obj("RRULE:FREQ=DAILY;BYSETPOS=2"))
    assert caught.value.reason == "expansion_too_slow"
    assert time.monotonic() - started < 10 * OBJECT_CPU_SECONDS  # generous guard; without the deadline: ~30 s


def _previous_tracer(frame: Any, event: str, arg: Any) -> None:
    return None


@pytest.mark.parametrize(
    ("raw", "outcome"),
    [
        (ics.load("dst-weekly.ics"), "ok"),
        (ics.load("dst-weekly.ics").replace(b"RRULE:FREQ=WEEKLY;COUNT=3", b"RRULE:FREQ=WEEKLY;BYDAY=XX"), "error"),
        (obj("RRULE:FREQ=DAILY;BYSETPOS=2"), "expansion_too_slow"),
    ],
    ids=["normal", "library-error", "deadline"],
)
def test_trace_state_is_restored_on_every_path(raw: bytes, outcome: str) -> None:
    seen: list[object] = []

    def worker() -> None:
        sys.settrace(_previous_tracer)
        try:
            run(raw, cpu_clock=FakeTime())
            seen.append("ok")
        except ObjectSkippedError as exc:
            seen.append(exc.reason)
        except Exception:
            seen.append("error")
        finally:
            seen.append(sys.gettrace())
            sys.settrace(None)

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()
    assert seen == [outcome, _previous_tracer]


def test_the_hook_is_thread_local() -> None:
    main_trace = sys.gettrace()
    other: list[object] = []
    stop = threading.Event()

    def bystander() -> None:
        while not stop.is_set():
            other.append(sys.gettrace())
            run(ics.load("dst-weekly.ics"))

    slow_result: list[str] = []

    def hooked() -> None:
        try:
            run(obj("RRULE:FREQ=DAILY;BYSETPOS=2"))
        except ObjectSkippedError as exc:
            slow_result.append(exc.reason)

    threads = [threading.Thread(target=bystander), threading.Thread(target=hooked)]
    for thread in threads:
        thread.start()
    threads[1].join()
    stop.set()
    threads[0].join()
    assert slow_result == ["expansion_too_slow"]
    assert other
    assert set(other) == {None}  # the bystander thread never saw the hook
    assert sys.gettrace() is main_trace


def test_the_deadline_is_not_an_exception_subclass() -> None:
    # A broad `except Exception` in a library layer must not swallow it (D62 §1a).
    assert not issubclass(caldav._ExpansionTooSlow, Exception)
    assert issubclass(caldav._ExpansionTooSlow, BaseException)


# --- caps, budget and order ----------------------------------------------------------------------------------------


def test_instances_per_call_are_capped(caplog: pytest.LogCaptureFixture) -> None:
    objects = [rdates(1000, f"rdates-{n}@example.test") for n in range(3)]
    with caplog.at_level(logging.INFO, logger="mcp_hub"):
        page = source_for(*objects).events(START, END, ZURICH, ZURICH)
    assert len(page.events) == MAX_INSTANCES_PER_CALL
    assert page.truncated is True
    stopped = [r.fields for r in caplog.records if r.getMessage() == "expansion_stopped"]  # type: ignore[attr-defined]
    assert stopped == [{"account": "icloud", "capability": "calendar", "outcome": "instance_cap", "result_count": 2000}]
    assert set(stopped[0]) <= allowed_fields("expansion_stopped")


def test_instances_per_object_are_capped() -> None:
    raw = rdates(1000, "rdates-daily@example.test").replace(b"SUMMARY:", b"RRULE:FREQ=DAILY\r\nSUMMARY:")
    page = source_for(raw).events(START, END, ZURICH, ZURICH)
    assert len(page.events) == MAX_INSTANCES_PER_OBJECT
    assert page.truncated is True
    assert page.events == sorted(page.events, key=lambda e: e.sort_key)  # the first 1,000 by start


def test_expansion_time_budget_stops_with_truncated(caplog: pytest.LogCaptureFixture) -> None:
    clock = FakeTime(step=1.0)  # every reading of the budget clock moves time by 1 s
    objects = [single(n) for n in range(10)]
    with caplog.at_level(logging.INFO, logger="mcp_hub"):
        page = source_for(*objects, clock=clock).events(START, END, ZURICH, ZURICH)
    assert 0 < len(page.events) < 10
    assert page.truncated is True
    stopped = [r.fields for r in caplog.records if r.getMessage() == "expansion_stopped"]  # type: ignore[attr-defined]
    assert [s["outcome"] for s in stopped] == ["time_budget"]


def test_single_events_are_expanded_before_recurring_ones() -> None:
    clock = FakeTime()  # one fake time for the CPU deadline and the budget
    hostile = [obj(f"RRULE:{rule}", uid=f"hostile-{n}@example.test") for n, rule in enumerate(NO_MATCH[:4])]
    singles = [single(n) for n in range(50)]
    page = source_for(*hostile, *singles, clock=clock, cpu_clock=clock).events(START, END, ZURICH, ZURICH)
    assert len([e for e in page.events if e.uid.startswith("single-")]) == 50
    assert page.truncated is True  # the hostile objects used up the budget


def test_normal_fixtures_are_unaffected() -> None:
    page = source_for(*(ics.load(name) for name in ics.NAMES)).events(START, END, ZURICH, ZURICH)
    href = "https://dav.example.test/h/home/"
    direct = [replace(e, calendar_href=href) for name in ics.NAMES for e in run(ics.load(name))]
    assert sorted(page.events, key=lambda e: (e.sort_key, e.uid)) == sorted(direct, key=lambda e: (e.sort_key, e.uid))
    assert page.truncated is False


# --- negative cache --------------------------------------------------------------------------------------------------


def test_slow_objects_are_remembered_by_digest(expander_calls: list[int], caplog: pytest.LogCaptureFixture) -> None:
    cache = SlowObjectCache()
    slow = obj("RRULE:FREQ=DAILY;BYSETPOS=2", uid="slow@example.test")
    edited = slow.replace(b"SUMMARY:slow@example.test", b"SUMMARY:edited")
    with caplog.at_level(logging.WARNING, logger="mcp_hub"):
        source_for(slow, slow_objects=cache, cpu_clock=FakeTime()).events(START, END, ZURICH, ZURICH)
        assert len(expander_calls) == 1
        source_for(slow, slow_objects=cache, cpu_clock=FakeTime()).events(START, END, ZURICH, ZURICH)
        assert len(expander_calls) == 1  # second call: skipped from the cache, no second burn
        source_for(edited, slow_objects=cache, cpu_clock=FakeTime()).events(START, END, ZURICH, ZURICH)
        assert len(expander_calls) == 2  # an edited object is evaluated again
    assert [s["outcome"] for s in skipped(caplog)] == ["expansion_too_slow"] * 3


def test_slow_object_cache_is_bounded_and_evicts_the_oldest() -> None:
    cache = SlowObjectCache(size=1000)
    for n in range(1001):
        cache.add(f"{n:064x}")
    assert f"{0:064x}" not in cache
    assert f"{1:064x}" in cache
    assert f"{1000:064x}" in cache
    assert len(cache) == 1000


def test_slow_object_cache_is_thread_safe() -> None:
    cache = SlowObjectCache(size=100)

    def fill(offset: int) -> None:
        for n in range(1000):
            cache.add(f"{offset}-{n}")

    threads = [threading.Thread(target=fill, args=(t,)) for t in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(cache) == 100


# --- raw pre-screen and BY* screen (D62 final A and B) ---------------------------------------------------------------


@pytest.fixture
def parser_calls(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Counts calls into icalendar's parser."""
    calls: list[int] = []
    original = caldav.icalendar.Calendar.from_ical

    def spy(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(caldav.icalendar.Calendar, "from_ical", spy)
    return calls


def many_events(count: int) -> bytes:
    events = "".join(
        f"BEGIN:VEVENT\r\nUID:many-{n}@example.test\r\nDTSTAMP:20260901T000000Z\r\nDTSTART:20261020T100000Z\r\n"
        "DTEND:20261020T110000Z\r\nEND:VEVENT\r\n"
        for n in range(count)
    )
    return f"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//mcp-hub tests//EN\r\n{events}END:VCALENDAR\r\n".encode()


def padded(size: int) -> bytes:
    raw = obj()
    filler = b"DESCRIPTION:" + b"x" * (size - len(raw) - len(b"DESCRIPTION:\r\n")) + b"\r\n"
    return raw.replace(b"END:VEVENT", filler + b"END:VEVENT")


def folded_rdates(count: int) -> bytes:
    """RDATE values split over several RDATE lines and folded, so only a value count catches them."""
    values = [stamp(datetime(2026, 10, 21) + timedelta(minutes=30 * i)) for i in range(count)]
    lines = [f"RDATE:{','.join(values[i : i + 100])}" for i in range(0, count, 100)]
    folded = ["\r\n ".join(line[j : j + 70] for j in range(0, len(line), 70)) for line in lines]
    return obj(*folded, uid="folded@example.test", start="20261021T000000Z")


RAW_REFUSED = {
    "object-too-large": (padded(256 * 1024 + 1), "object_too_large"),
    "501-components": (many_events(501), "too_many_components"),
    "1001-rdates-folded": (folded_rdates(1001), "too_many_dates"),
    "1001-exdates-lowercase": (obj("RRULE:FREQ=DAILY", f"exdate:{EXDATES_1001}"), "too_many_dates"),
}


@pytest.mark.parametrize("case", list(RAW_REFUSED))
def test_raw_prescreen_refuses_before_parsing(case: str, parser_calls: list[int]) -> None:
    raw, reason = RAW_REFUSED[case]
    with pytest.raises(ObjectSkippedError) as caught:
        run(raw)
    assert caught.value.reason == reason
    assert parser_calls == []


def test_raw_prescreen_boundaries_are_allowed(parser_calls: list[int]) -> None:
    assert len(run(padded(256 * 1024))) == 1
    assert len(run(many_events(500))) == 500
    assert len(run(folded_rdates(1000))) == 1000
    assert len(parser_calls) == 3


@pytest.mark.parametrize(
    "rule",
    [
        "FREQ=DAILY;BYHOUR=0,1",
        "FREQ=DAILY;BYMINUTE=0,30",
        "FREQ=DAILY;BYSECOND=0,1",
        "FREQ=DAILY;BYHOUR=" + ",".join(map(str, range(24))) + ";BYMINUTE=" + ",".join(map(str, range(60))),
    ],
    ids=["two-hours", "two-minutes", "two-seconds", "review-19-shape"],
)
def test_time_of_day_lists_are_refused(rule: str, expander_calls: list[int]) -> None:
    with pytest.raises(ObjectSkippedError) as caught:
        run(obj(f"RRULE:{rule}"))
    assert caught.value.reason == "rule_refused"
    assert expander_calls == []


def test_rules_are_screened_on_every_component_including_overrides(expander_calls: list[int]) -> None:
    override = (
        "END:VEVENT\r\nBEGIN:VEVENT\r\nUID:limit@example.test\r\nDTSTAMP:20260901T000000Z\r\n"
        "RECURRENCE-ID:20261021T100000Z\r\nDTSTART:20261021T120000Z\r\nDTEND:20261021T130000Z\r\n"
        "RRULE:FREQ=DAILY;BYSECOND=0,1,2"
    )
    with pytest.raises(ObjectSkippedError) as caught:
        run(obj("RRULE:FREQ=DAILY;COUNT=5", override))
    assert caught.value.reason == "rule_refused"
    assert expander_calls == []


def test_single_values_for_time_of_day_parts_are_allowed() -> None:
    assert len(run(obj("RRULE:FREQ=DAILY;BYHOUR=9;BYMINUTE=30;BYSECOND=0"))) == 28


# --- swallowed deadline (D62 final C) --------------------------------------------------------------------------------


def _swallowing_expansion(clock: FakeTime) -> Callable[..., list[caldav.RawEvent]]:
    """Stands in for a library layer that catches everything, the injected deadline included."""

    def instances(*args: Any, **kwargs: Any) -> list[caldav.RawEvent]:
        try:
            while True:
                clock()  # a Python call: the hook fires and raises once the deadline has passed
        except BaseException:  # noqa: S110 - swallowing everything is the point of this test
            pass
        for _ in range(10):
            clock()  # more work after the swallowed exception, now without a trace function
        return []

    return instances


@pytest.mark.parametrize("failing", [False, True], ids=["returns", "raises-after"])
def test_a_swallowed_deadline_is_caught_after_the_call(monkeypatch: pytest.MonkeyPatch, failing: bool) -> None:
    clock = FakeTime()
    swallow = _swallowing_expansion(clock)

    def instances(*args: Any, **kwargs: Any) -> list[caldav.RawEvent]:
        swallow()
        if failing:
            raise ValueError("library error after the swallowed deadline")
        return []

    monkeypatch.setattr(caldav, "_instances", instances)
    previous = sys.gettrace()
    with pytest.raises(ObjectSkippedError) as caught:
        run(ics.load("dst-weekly.ics"), cpu_clock=clock)
    assert caught.value.reason == "expansion_too_slow"
    assert sys.gettrace() is previous


def test_a_swallowed_deadline_feeds_the_negative_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = FakeTime()
    monkeypatch.setattr(caldav, "_instances", _swallowing_expansion(clock))
    cache = SlowObjectCache()
    page = source_for(ics.load("dst-weekly.ics"), slow_objects=cache, cpu_clock=clock).events(
        START, END, ZURICH, ZURICH
    )
    assert page.events == []
    assert len(cache) == 1


def test_instances_of_one_object_share_their_text() -> None:
    # D62 E: 1,000 RDATE instances of one object with a 100 KB description must not hold 1,000 copies (95 MiB).
    raw = rdates(1000, "shared-text@example.test").replace(
        b"SUMMARY:", b"DESCRIPTION:" + b"x" * 100_000 + b"\r\nSUMMARY:"
    )
    events = run(raw)
    assert len(events) == 1000
    assert len({id(e.description) for e in events}) == 1
    assert len({id(e.title) for e in events}) == 1
    assert events[0].description == "x" * 100_000
