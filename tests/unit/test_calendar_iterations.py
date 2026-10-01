"""Occurrences iterated from DTSTART are bounded before expansion (confirmation review of PR #11, C1): dateutil keeps
every occurrence it iterates from DTSTART (60-90 bytes each), and the CPU deadline does not bound memory."""

import random
import tracemalloc
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import icalendar
import pytest
from dateutil.rrule import rrulestr

from mcp_hub.providers import caldav
from mcp_hub.providers.caldav import MAX_ITERATED_OCCURRENCES, ObjectSkippedError, expand, iteration_bound
from tests.support import ics

ZURICH = ZoneInfo("Europe/Zurich")
START = datetime(2026, 10, 17, tzinfo=ZURICH)
END = datetime(2026, 11, 17, tzinfo=ZURICH)
WINDOW_END = date(2026, 11, 19)  # the widened window end plus a day of time-zone slack
HOURS = ",".join(map(str, range(24)))
EVERY_DAY = "BYMONTH=1,2,3,4,5,6,7,8,9,10,11,12;BYMONTHDAY=" + ",".join(map(str, range(1, 32)))


def obj(rule: str, start: str) -> bytes:
    dtstart = f"DTSTART;VALUE=DATE:{start}" if len(start) == 8 else f"DTSTART:{start}"
    return (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//mcp-hub tests//EN\r\nBEGIN:VEVENT\r\nUID:iter@example.test\r\n"
        f"DTSTAMP:20260901T000000Z\r\n{dtstart}\r\nRRULE:{rule}\r\nSUMMARY:Iterations\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    ).encode()


def run(raw: bytes) -> list[Any]:
    return expand(raw, href="/h/", name="Home", start=START, end=END, zone=ZURICH, floating=ZURICH)


@pytest.fixture
def expander_calls(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    calls: list[int] = []
    original = caldav.recurring_ical_events.of

    def spy(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(caldav.recurring_ical_events, "of", spy)
    return calls


def test_the_limit_is_20000() -> None:
    assert MAX_ITERATED_OCCURRENCES == 20_000


@pytest.mark.parametrize(
    ("rule", "start"),
    [
        (
            f"FREQ=YEARLY;{EVERY_DAY};BYHOUR={HOURS}",
            "18000101T000000Z",
        ),
        (
            f"FREQ=YEARLY;{EVERY_DAY};BYHOUR={HOURS}",
            "15000101T000000Z",
        ),
        (f"FREQ=MONTHLY;BYMONTHDAY={','.join(map(str, range(1, 32)))};BYHOUR={HOURS}", "16040101T000000Z"),
        (f"FREQ=DAILY;BYHOUR={HOURS}", "19000101T000000Z"),
        (f"FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR,SA,SU;BYHOUR={HOURS}", "19000101T000000Z"),
    ],
    ids=[
        "yearly-everyhour-1800",
        "yearly-everyhour-1500",
        "monthly-everyhour-1604",
        "daily-everyhour-1900",
        "weekly-everyhour-1900",
    ],
)
def test_rules_that_iterate_too_many_occurrences_are_refused_before_expansion(
    rule: str, start: str, expander_calls: list[int]
) -> None:
    raw = obj(rule, start)
    tracemalloc.start()
    try:
        with pytest.raises(ObjectSkippedError) as caught:
            run(raw)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert caught.value.reason == "rule_refused"
    assert expander_calls == []
    assert peak < 20 * 1024 * 1024  # review 19 C1: 195-441 MB before


@pytest.mark.parametrize(
    ("rule", "start"),
    [
        ("FREQ=YEARLY", "16040315"),  # Apple's year-less birthday
        ("FREQ=DAILY;BYHOUR=7,19", "20200101T070000Z"),
        ("FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR", "19900101T090000Z"),
        ("FREQ=MONTHLY;BYDAY=1MO", "19500102T090000Z"),
        ("FREQ=YEARLY;BYMONTH=5;BYDAY=-1MO", "19500529T090000Z"),
        ("FREQ=DAILY", "20000101T090000Z"),
    ],
    ids=[
        "apple-birthday-1604",
        "twice-daily-2020",
        "weekdays-1990",
        "first-monday-1950",
        "last-monday-may-1950",
        "daily-2000",
    ],
)
def test_legitimate_long_series_pass(rule: str, start: str) -> None:
    run(obj(rule, start))  # no ObjectSkippedError


@pytest.mark.parametrize(
    ("rule", "start"),
    [("FREQ=DAILY", "19700101T090000Z"), (f"FREQ=DAILY;BYHOUR={HOURS}", "20240101T000000Z")],
    ids=["daily-since-1970", "hourly-like-since-2024"],
)
def test_accepted_trade_off_refusals(rule: str, start: str, expander_calls: list[int]) -> None:
    # Documented in INTERFACES: a daily series older than about 54 years, and other rules whose bound exceeds 20,000.
    with pytest.raises(ObjectSkippedError) as caught:
        run(obj(rule, start))
    assert caught.value.reason == "rule_refused"
    assert expander_calls == []


@pytest.mark.parametrize(
    ("rule", "start", "most"),
    [
        ("FREQ=MONTHLY;BYDAY=1MO", date(1950, 1, 2), 925),
        ("FREQ=YEARLY;BYMONTH=5;BYDAY=-1MO", date(1950, 5, 29), 78),
        ("FREQ=YEARLY;BYDAY=20MO", date(1950, 5, 15), 78),
        ("FREQ=MONTHLY;BYDAY=MO,WE", date(2020, 1, 6), 83 * 10),
        ("FREQ=YEARLY;BYDAY=FR", date(2020, 1, 3), 7 * 53),
        ("FREQ=YEARLY;BYMONTH=1,7;BYDAY=MO,2TU", date(2020, 1, 6), 7 * 2 * 6),
    ],
    ids=["monthly-ordinal", "yearly-month-ordinal", "yearly-ordinal", "monthly-plain", "yearly-plain", "yearly-mixed"],
)
def test_byday_bounds(rule: str, start: date, most: int) -> None:
    assert iteration_bound(icalendar.vRecur.from_ical(rule), start, WINDOW_END) <= most


def test_the_bound_for_apple_birthdays_is_small() -> None:
    [component] = icalendar.Calendar.from_ical(ics.load("apple-birthday.ics")).walk("VEVENT")
    bound = iteration_bound(component["RRULE"], date(1604, 3, 15), WINDOW_END)
    assert 420 <= bound <= 430


def _values_for(rng: random.Random, part: str) -> str:
    days = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]
    pick = {
        "BYSECOND": lambda: [str(rng.randint(0, 59))],
        "BYMINUTE": lambda: [str(rng.randint(0, 59))],
        "BYHOUR": lambda: [str(rng.randint(0, 23)) for _ in range(rng.randint(1, 8))],
        "BYMONTHDAY": lambda: [str(rng.choice([*range(1, 32), *range(-31, 0)])) for _ in range(rng.randint(1, 6))],
        "BYYEARDAY": lambda: [str(rng.choice([*range(1, 367), *range(-366, 0)])) for _ in range(rng.randint(1, 6))],
        "BYWEEKNO": lambda: [str(rng.choice([*range(1, 54), *range(-53, 0)])) for _ in range(rng.randint(1, 4))],
        "BYMONTH": lambda: [str(rng.randint(1, 12)) for _ in range(rng.randint(1, 6))],
        "BYSETPOS": lambda: [str(rng.choice([1, 2, 3, 5, -1, -2, -3])) for _ in range(rng.randint(1, 3))],
        "BYDAY": lambda: [
            (f"{rng.choice([1, 2, 3, 4, 5, -1, -2, 10, 20, 52, -10])}{d}" if rng.random() < 0.4 else d)
            for d in (rng.choice(days) for _ in range(rng.randint(1, 5)))
        ],
    }[part]()
    return ",".join(pick)  # duplicates within a list are deliberate


def _random_rule(rng: random.Random) -> tuple[str, datetime]:
    freq = rng.choice(["YEARLY", "MONTHLY", "WEEKLY", "DAILY"])
    years_back = {"YEARLY": 80, "MONTHLY": 40, "WEEKLY": 8, "DAILY": 4}[freq]
    start = datetime(2026, 10, 20, rng.randint(0, 23)) - timedelta(days=rng.randint(0, 365 * years_back))
    parts = [f"FREQ={freq}"]
    for part in (
        "BYMONTH",
        "BYWEEKNO",
        "BYYEARDAY",
        "BYMONTHDAY",
        "BYDAY",
        "BYHOUR",
        "BYMINUTE",
        "BYSECOND",
        "BYSETPOS",
    ):
        if rng.random() < 0.3:
            parts.append(f"{part}={_values_for(rng, part)}")
    if rng.random() < 0.3:
        parts.append(f"INTERVAL={rng.randint(2, 6)}")
    if rng.random() < 0.15:
        parts.append(f"WKST={rng.choice(['MO', 'SU', 'WE'])}")
    if rng.random() < 0.2:
        parts.append(f"COUNT={rng.randint(1, 5000)}")
    elif rng.random() < 0.2:
        parts.append("UNTIL=" + (start + timedelta(days=rng.randint(0, 365 * years_back))).strftime("%Y%m%dT%H%M%SZ"))
    return ";".join(parts), start


def test_the_bound_is_never_below_the_real_number_of_occurrences(capsys: pytest.CaptureFixture[str]) -> None:
    """For every generated rule that PASSES the screen: bound >= the occurrences dateutil iterates to the window end."""
    import time
    from collections import Counter

    rng = random.Random(20261001)  # noqa: S311 - a seeded generator for reproducible test rules
    screened, compared, partial, invalid = Counter(), Counter(), Counter(), Counter()
    window_end = datetime.combine(WINDOW_END, datetime.min.time())
    for _ in range(3_000):
        text, start = _random_rule(rng)
        freq = text.split(";")[0].split("=")[1]
        raw = obj(text, start.strftime("%Y%m%dT%H%M%SZ"))
        try:
            caldav._screen(icalendar.Calendar.from_ical(caldav._prescreen(raw)), WINDOW_END)
        except ObjectSkippedError:
            screened[freq] += 1
            continue
        except Exception:
            invalid[freq] += 1  # icalendar cannot parse it: the hub skips such an object as broken
            continue
        bound = iteration_bound(icalendar.vRecur.from_ical(text), start.date(), WINDOW_END)
        try:
            real = rrulestr(text.replace("Z", ""), dtstart=start)
        except Exception:
            invalid[freq] += 1
            continue
        count = 0

        def counting(real: Any = real, bound: int = bound) -> None:
            nonlocal count
            for occurrence in real:
                if occurrence > window_end or count > bound:
                    return
                count += 1

        try:  # rules that never match make dateutil iterate to year 9999: stop counting after 0.1 s CPU
            caldav._with_cpu_deadline(counting, 0.1, time.thread_time)
            compared[freq] += 1
        except caldav._ExpansionTooSlow:
            partial[freq] += 1  # every occurrence counted so far is still checked against the bound
        except Exception:
            invalid[freq] += 1  # dateutil itself fails (e.g. IndexError); the hub skips such an object
        assert count <= bound, (text, start, count, bound)
    with capsys.disabled():
        print(
            f"\npassing rules fully compared {dict(compared)}; partially {dict(partial)}; refused by the screen "
            f"{dict(screened)}; unparseable {dict(invalid)}"
        )
    assert sum(compared.values()) >= 1_000
    assert min(compared[f] for f in ("YEARLY", "MONTHLY", "WEEKLY", "DAILY")) >= 150
