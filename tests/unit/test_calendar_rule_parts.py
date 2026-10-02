"""Only RFC 5545 rule parts, and only where RFC 5545 allows them per frequency (second confirmation pass of PR #11:
E1 BYEASTER, E2 MONTHLY with BYYEARDAY/BYWEEKNO, E5 INTERVAL pinned)."""

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from mcp_hub.providers import caldav
from mcp_hub.providers.caldav import ObjectSkippedError, expand

ZURICH = ZoneInfo("Europe/Zurich")
START = datetime(2026, 10, 17, tzinfo=ZURICH)
END = datetime(2026, 11, 17, tzinfo=ZURICH)
HOURS = ",".join(map(str, range(24)))


def obj(rule: str, start: str = "20261020T100000Z", rule_line: str | None = None) -> bytes:
    line = rule_line if rule_line is not None else f"RRULE:{rule}"
    return (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//mcp-hub tests//EN\r\nBEGIN:VEVENT\r\nUID:parts@example.test\r\n"
        f"DTSTAMP:20260901T000000Z\r\nDTSTART:{start}\r\n{line}\r\nSUMMARY:Parts\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
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


@pytest.mark.parametrize(
    "raw",
    [
        obj(f"FREQ=YEARLY;BYEASTER={','.join(map(str, range(-180, 181)))};BYHOUR={HOURS}", "17000101T000000Z"),
        obj("FREQ=YEARLY;BYEASTER=0"),
        obj("FREQ=YEARLY;byeaster=0"),
        obj("FREQ=YEARLY;RSCALE=GREGORIAN;SKIP=OMIT"),
        obj("FREQ=DAILY;X-NAME=1"),
        obj("FREQ=DAILY;BYWEEKDAY=MO"),
        obj("", rule_line="RRULE;X-PARAM=1:FREQ=YEARLY;BYEASTER=0"),
    ],
    ids=[
        "review-e1-shape",
        "byeaster",
        "lower-case-byeaster",
        "rscale-skip",
        "x-part",
        "dateutil-byweekday",
        "with-parameter",
    ],
)
def test_only_rfc5545_rule_parts_are_allowed(raw: bytes, expander_calls: list[int]) -> None:
    with pytest.raises(ObjectSkippedError) as caught:
        run(raw)
    assert caught.value.reason == "rule_refused"
    assert expander_calls == []


@pytest.mark.parametrize(
    "rule",
    [
        "FREQ=MONTHLY;BYYEARDAY=1,100,200",
        "FREQ=MONTHLY;BYWEEKNO=1,20",
        "FREQ=WEEKLY;BYYEARDAY=1",
        "FREQ=DAILY;BYYEARDAY=1",
        "FREQ=WEEKLY;BYWEEKNO=1",
        "FREQ=DAILY;BYWEEKNO=1",
        "FREQ=WEEKLY;BYMONTHDAY=1",
        f"FREQ=MONTHLY;BYYEARDAY={','.join(map(str, range(1, 367)))};BYHOUR={HOURS}",
    ],
    ids=[
        "monthly-byyearday",
        "monthly-byweekno",
        "weekly-byyearday",
        "daily-byyearday",
        "weekly-byweekno",
        "daily-byweekno",
        "weekly-bymonthday",
        "review-e2-shape",
    ],
)
def test_parts_rfc5545_forbids_for_the_frequency_are_refused(rule: str, expander_calls: list[int]) -> None:
    with pytest.raises(ObjectSkippedError) as caught:
        run(obj(rule, "19580101T000000Z"))
    assert caught.value.reason == "rule_refused"
    assert expander_calls == []


@pytest.mark.parametrize(
    "rule",
    [
        "FREQ=YEARLY;BYWEEKNO=20;BYDAY=MO",
        "FREQ=YEARLY;BYYEARDAY=1,-1",
        "FREQ=MONTHLY;BYMONTHDAY=1,-1;BYSETPOS=1",
        "FREQ=WEEKLY;BYDAY=MO,WE;WKST=SU;INTERVAL=2",
        "FREQ=DAILY;BYMONTH=1,2;BYMONTHDAY=3;BYHOUR=7,19;BYMINUTE=30;BYSECOND=0",
        "FREQ=YEARLY;COUNT=3",
        "FREQ=WEEKLY;UNTIL=20271231T000000Z",
    ],
)
def test_allowed_parts_and_combinations_pass(rule: str) -> None:
    run(obj(rule))  # no ObjectSkippedError


def test_duplicate_rule_parts_are_judged_as_dateutil_receives_them() -> None:
    # icalendar keeps the last value of a repeated part and the hub checks the string dateutil receives, so a small
    # first value cannot hide a large last one: 24 per day from 2016 exceeds the bound.
    with pytest.raises(ObjectSkippedError) as caught:
        run(obj("", "20160104T000000Z", rule_line=f"RRULE:FREQ=DAILY;BYHOUR=1;BYHOUR={HOURS}"))
    assert caught.value.reason == "rule_refused"


def test_the_interval_division_is_applied() -> None:
    # E5 (mutation N45): DAILY since 1960 is ~24,400 iterations (refused), every third day ~8,100 (accepted).
    with pytest.raises(ObjectSkippedError):
        run(obj("FREQ=DAILY", "19600101T090000Z"))
    run(obj("FREQ=DAILY;INTERVAL=3", "19600101T090000Z"))
