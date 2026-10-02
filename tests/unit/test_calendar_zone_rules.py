"""Rules inside VTIMEZONE components are screened before parsing (spec 080 rev. 4.5 D62 B, PR #15 review F1).

icalendar builds every VTIMEZONE whose TZID zoneinfo does not know while it parses an object, through dateutil's
tzical, which compiles each STANDARD/DAYLIGHT rule with dateutil's occurrence cache on. Every UTC-offset lookup then
iterates that rule from the sub-component's DTSTART: a dense rule from 1601 cost 3-4 s and ~290 MB per object."""

import contextlib
import tracemalloc
from collections.abc import Callable
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import icalendar.timezone.zoneinfo
import pytest

from mcp_hub.providers import caldav
from mcp_hub.providers.caldav import ObjectSkippedError, RawEvent, expand
from tests.support import ics

ZURICH = ZoneInfo("Europe/Zurich")
START = datetime(2026, 10, 17, tzinfo=ZURICH)
END = datetime(2026, 11, 17, tzinfo=ZURICH)
CUSTOM = "Custom/Probe"
HOURS = ",".join(map(str, range(24)))
DENSE = f"FREQ=YEARLY;BYMONTH={','.join(map(str, range(1, 13)))};BYMONTHDAY={','.join(map(str, range(1, 32)))}"
DENSE += f";BYHOUR={HOURS}"
OUTLOOK_RULE = "FREQ=YEARLY;INTERVAL=1;BYDAY=-1SU;BYMONTH=10"


def run(raw: bytes, start: datetime = START, end: datetime = END) -> list[RawEvent]:
    found = expand(raw, href="/h/", name="Home", start=start, end=end, zone=ZURICH, floating=ZURICH)
    return sorted(found, key=lambda e: e.sort_key)


def zone(rule_line: str, *, tzid: str = CUSTOM, start: str = "16010101T030000", begin: str = "BEGIN:VTIMEZONE") -> str:
    """A VTIMEZONE whose STANDARD sub-component carries `rule_line` (a whole content line) and whose DAYLIGHT
    sub-component carries Outlook's March rule."""
    return (
        f"{begin}\r\nTZID:{tzid}\r\nBEGIN:STANDARD\r\nDTSTART:{start}\r\nTZOFFSETFROM:+0200\r\nTZOFFSETTO:+0100\r\n"
        f"{rule_line}\r\nEND:STANDARD\r\nBEGIN:DAYLIGHT\r\nDTSTART:16010101T020000\r\nTZOFFSETFROM:+0100\r\n"
        "TZOFFSETTO:+0200\r\nRRULE:FREQ=YEARLY;INTERVAL=1;BYDAY=-1SU;BYMONTH=3\r\nEND:DAYLIGHT\r\nEND:VTIMEZONE\r\n"
    )


def obj(zones: str, *, tzid: str = CUSTOM, rule: str = "", inner: str = "") -> bytes:
    """One event on 2026-10-20 10:00 in `tzid` (optionally recurring), after `zones`."""
    extra = f"RRULE:{rule}\r\n" if rule else ""
    return (
        f"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//mcp-hub tests//EN\r\n{zones}BEGIN:VEVENT\r\n"
        f"UID:zone@example.test\r\nDTSTAMP:20260901T000000Z\r\nDTSTART;TZID={tzid}:20261020T100000\r\n"
        f"DURATION:PT1H\r\n{extra}{inner}SUMMARY:Zone probe\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    ).encode()


def peak_of(call: Callable[[], object]) -> int:
    tracemalloc.start()
    try:
        with contextlib.suppress(ObjectSkippedError):
            call()
        return tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


@pytest.fixture
def parser_calls(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Counts icalendar parses; icalendar builds the zones while it parses."""
    calls: list[int] = []
    original = caldav.icalendar.Calendar.from_ical

    def spy(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(caldav.icalendar.Calendar, "from_ical", spy)
    return calls


@pytest.fixture
def zone_builds(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Counts the zones icalendar builds with dateutil's tzical (the only place a VTIMEZONE rule is evaluated)."""
    calls: list[int] = []
    original = icalendar.timezone.zoneinfo.tzical

    def spy(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(icalendar.timezone.zoneinfo, "tzical", spy)
    return calls


# --- the reviewer's F1 shapes: refused before parsing, on bounded memory -------------------------------------------

F1_SHAPES = {
    "dense-from-1601": obj(zone(f"RRULE:{DENSE}")),
    "dense-with-a-daily-event": obj(zone(f"RRULE:{DENSE}"), rule="FREQ=DAILY"),
    "dense-from-2000": obj(zone(f"RRULE:{DENSE}", start="20000101T000000")),
    "hourly": obj(zone("RRULE:FREQ=HOURLY")),
    "minutely-from-1970": obj(zone("RRULE:FREQ=MINUTELY", start="19700101T000000")),
    "easter": obj(zone(f"RRULE:FREQ=YEARLY;BYEASTER=0;BYHOUR={HOURS}")),
}


@pytest.mark.parametrize("case", list(F1_SHAPES))
def test_the_review_shapes_are_refused_before_parsing(case: str, parser_calls: list[int]) -> None:
    with pytest.raises(ObjectSkippedError) as caught:
        run(F1_SHAPES[case])
    assert caught.value.reason == "rule_refused"
    assert parser_calls == []


@pytest.mark.parametrize("case", list(F1_SHAPES))
def test_the_review_shapes_cost_bounded_memory(case: str) -> None:
    assert peak_of(lambda: run(F1_SHAPES[case])) < 2 * 1024 * 1024  # was 26-71 MiB under tracemalloc, ~290 MB RSS


# --- the rule shape real zones use -------------------------------------------------------------------------------

REFUSED_ZONE_RULES = {
    "byhour": f"RRULE:{OUTLOOK_RULE};BYHOUR=1",
    "byminute": f"RRULE:{OUTLOOK_RULE};BYMINUTE=0",
    "bysecond": f"RRULE:{OUTLOOK_RULE};BYSECOND=0",
    "bysetpos": f"RRULE:{OUTLOOK_RULE};BYSETPOS=-1",
    "byweekno": "RRULE:FREQ=YEARLY;BYMONTH=10;BYWEEKNO=43",
    "byyearday": "RRULE:FREQ=YEARLY;BYMONTH=10;BYYEARDAY=300",
    "byeaster": "RRULE:FREQ=YEARLY;BYMONTH=10;BYEASTER=0",
    "x-name": f"RRULE:{OUTLOOK_RULE};X-NAME=1",
    "monthly": "RRULE:FREQ=MONTHLY;BYMONTH=10;BYDAY=-1SU",
    "daily": "RRULE:FREQ=DAILY;BYMONTH=10",
    "interval-2": "RRULE:FREQ=YEARLY;INTERVAL=2;BYDAY=-1SU;BYMONTH=10",
    "no-bymonth": "RRULE:FREQ=YEARLY;BYDAY=-1SU",
    "two-months": "RRULE:FREQ=YEARLY;BYDAY=-1SU;BYMONTH=3,10",
    "two-weekdays": "RRULE:FREQ=YEARLY;BYDAY=1SU,-1SU;BYMONTH=10",
    "eight-month-days": "RRULE:FREQ=YEARLY;BYMONTH=10;BYMONTHDAY=1,2,3,4,5,6,7,8",
    "repeated-part": "RRULE:FREQ=YEARLY;BYMONTH=10;BYMONTH=3;BYDAY=-1SU",
    "part-smuggled-into-byday": "RRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU,BYEASTER=0",
    "unparseable": "RRULE:garbage",
    "lower-case": "rrule:freq=yearly;bymonth=10;byday=-1su;byhour=1",
    "parameter": f"RRULE;X-A=1:{OUTLOOK_RULE};BYHOUR=1",
    "quoted-colon-parameter": f'RRULE;X-A="a:b":{OUTLOOK_RULE};BYHOUR=1',
    "unbalanced-quote-parameter": f'RRULE;X-A="a:{OUTLOOK_RULE}',
    "folded": "RRULE:FREQ=YEARLY;INTERVAL=1;BYDAY=-1SU;BYMONTH=10;BY\r\n HOUR=1",
    "blank-line-fold": "RRULE:FREQ=YEARLY;INTERVAL=1;BYDAY=-1SU;BYMONTH=10;BY\r\n\r\n HOUR=1",
    "two-rules": f"RRULE:{OUTLOOK_RULE}\r\nRRULE:FREQ=YEARLY;BYDAY=1SU;BYMONTH=11",
    "exrule": f"RRULE:{OUTLOOK_RULE}\r\nEXRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=1SU",
}


@pytest.mark.parametrize("case", list(REFUSED_ZONE_RULES))
def test_zone_rules_outside_the_real_world_shape_are_refused(case: str, parser_calls: list[int]) -> None:
    with pytest.raises(ObjectSkippedError) as caught:
        run(obj(zone(REFUSED_ZONE_RULES[case])))
    assert caught.value.reason == "rule_refused"
    assert parser_calls == []


@pytest.mark.parametrize(
    "raw",
    [
        obj(zone(f"RRULE:{DENSE}", begin="begin;x-a=1:vtimezone")),
        obj(zone(f"RRULE:{DENSE}", begin='BEGIN;X-A="a:b":VTIMEZONE')),
        obj("", inner=zone(f"RRULE:{DENSE}")),  # icalendar builds a VTIMEZONE nested in an event too
        obj(zone(f"RRULE:{DENSE}", tzid="Europe/Zurich"), tzid="Europe/Zurich"),  # never built, still screened
    ],
    ids=["begin-lower-case-with-parameter", "begin-quoted-colon-parameter", "nested-in-the-event", "iana-tzid"],
)
def test_every_vtimezone_is_screened(raw: bytes, parser_calls: list[int]) -> None:
    with pytest.raises(ObjectSkippedError) as caught:
        run(raw)
    assert caught.value.reason == "rule_refused"
    assert parser_calls == []


@pytest.mark.parametrize(
    "rule_line",
    [
        f"RRULE:{OUTLOOK_RULE}",
        "RRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=SU;BYMONTHDAY=25,26,27,28,29,30,31",
        "RRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU;UNTIL=20991231T000000Z;WKST=MO",
        "RRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU;COUNT=500",
        "RRULE:FREQ=YEARLY;BYMONTH=10;BYMONTHDAY=25",
        "RRULE:FREQ=YEARLY;BYMONTH=10",
        "rrule:freq=yearly;interval=1;bymonth=10;byday=-1su",
    ],
)
def test_the_real_world_zone_rule_shape_is_allowed(rule_line: str) -> None:
    assert len(run(obj(zone(rule_line)))) == 1


def test_zone_rule_dates_are_limited_per_vtimezone() -> None:
    assert caldav.MAX_ZONE_RECURRENCE_DATES == 200

    def rdates(count: int) -> bytes:
        values = ",".join(f"{1700 + i}1027T030000" for i in range(count))
        return obj(zone(f"RDATE:{values}"))

    assert len(run(rdates(200))) == 1
    with pytest.raises(ObjectSkippedError) as caught:
        run(rdates(201))
    assert caught.value.reason == "too_many_dates"


def test_zone_rule_dates_count_towards_the_object_limit() -> None:
    values = ",".join(f"{1700 + i}1027T030000" for i in range(150))
    zones = "".join(zone(f"RDATE:{values}", tzid=f"Custom/Zone{i}") for i in range(7))  # 1,050 dates
    with pytest.raises(ObjectSkippedError) as caught:
        run(obj(zones, tzid="Custom/Zone0"))
    assert caught.value.reason == "too_many_dates"


def test_zone_sub_components_count_towards_the_component_limit() -> None:
    standard = "BEGIN:STANDARD\r\nDTSTART:19700101T000000\r\nTZOFFSETFROM:+0100\r\nTZOFFSETTO:+0100\r\nEND:STANDARD\r\n"

    def zones(count: int) -> str:
        return f"BEGIN:VTIMEZONE\r\nTZID:{CUSTOM}\r\n{standard * count}END:VTIMEZONE\r\n"

    assert len(run(obj(zones(1000)))) == 1
    with pytest.raises(ObjectSkippedError) as caught:
        run(obj(zones(1001)))
    assert caught.value.reason == "too_many_components"


def sub_components(rule: str, count: int, start: str = "16010101T000000", tzid: str = CUSTOM) -> str:
    subs = "".join(
        f"BEGIN:STANDARD\r\nDTSTART:{start}\r\nTZOFFSETFROM:+0100\r\nTZOFFSETTO:+0100\r\nRRULE:{rule}\r\nEND:STANDARD\r\n"
        for _ in range(count)
    )
    return f"BEGIN:VTIMEZONE\r\nTZID:{tzid}\r\n{subs}END:VTIMEZONE\r\n"


def test_the_rules_of_one_vtimezone_are_held_to_the_iteration_limit_up_to_the_year_9999() -> None:
    # A UTC-offset lookup iterates from DTSTART to the looked-up time, and an event may lie in 9999: 1601-9999 is
    # 8,399 years of one transition each. Two such rules fit (16,798), three do not (25,197).
    assert len(run(obj(sub_components(OUTLOOK_RULE, 2)))) == 1
    for zones in (
        sub_components(OUTLOOK_RULE, 3),
        sub_components(OUTLOOK_RULE, 3) + sub_components(OUTLOOK_RULE, 1, tzid="Custom/Other"),  # not only the last
    ):
        with pytest.raises(ObjectSkippedError) as caught:
            run(obj(zones))
        assert caught.value.reason == "rule_refused"


def test_many_late_rules_cannot_add_up_through_a_far_lookup() -> None:
    # Review of the F1 fix: 999 rules from 2026, cheap up to the window but +57 MiB for one event in the year 9999.
    rule = "FREQ=YEARLY;BYMONTH=1;BYMONTHDAY=1,2,3,4,5,6,7"
    raw = obj(sub_components(rule, 999, start="20260101T000000")).replace(b":20261020T100000", b":99991020T100000")
    with pytest.raises(ObjectSkippedError) as caught:
        run(raw)
    assert caught.value.reason == "rule_refused"


# Below the iteration limit, so only the shape rule itself refuses (review of PR #15, G3).


def test_a_second_rule_in_one_sub_component_is_refused() -> None:
    zone_text = sub_components(OUTLOOK_RULE, 1, start="20200101T000000").replace(
        f"RRULE:{OUTLOOK_RULE}", f"RRULE:{OUTLOOK_RULE}\r\nRRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=-1SU"
    )  # 2 x 7,980 occurrences up to 9999
    with pytest.raises(ObjectSkippedError) as caught:
        run(obj(zone_text))
    assert caught.value.reason == "rule_refused"


def test_more_than_seven_month_days_are_refused() -> None:
    rule = "FREQ=YEARLY;BYMONTH=10;BYMONTHDAY=1,2,3,4,5,6,7,8"
    with pytest.raises(ObjectSkippedError) as caught:
        run(obj(sub_components(rule, 1, start="99900101T000000")))  # 10 years x 8
    assert caught.value.reason == "rule_refused"


def test_the_rule_icalendar_serialises_is_checked_too() -> None:
    # The raw value has one BYMONTH entry ("10 BYDAY=-1SU"); icalendar drops the unreadable part, so the text the
    # time-zone builder evaluates is FREQ=YEARLY without BYMONTH.
    with pytest.raises(ObjectSkippedError) as caught:
        run(obj(sub_components("FREQ=YEARLY;BYMONTH=10 BYDAY=-1SU", 1, start="99900101T000000")))
    assert caught.value.reason == "rule_refused"


def test_the_limit_applies_per_vtimezone() -> None:
    # Outlook adds one VTIMEZONE per zone the event uses (start and end in different zones).
    zones = sub_components(OUTLOOK_RULE, 2) + sub_components(OUTLOOK_RULE, 2, tzid="Custom/Other")
    assert len(run(obj(zones))) == 1


@pytest.mark.parametrize(
    ("rule", "per_year"),
    [
        ("FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU", 1),
        ("FREQ=YEARLY;BYMONTH=3;BYMONTHDAY=8,9,10,11,12,13,14;BYDAY=SU", 1),  # one week: one Sunday
        ("FREQ=YEARLY;BYMONTH=3;BYMONTHDAY=1,8,15,22,29;BYDAY=SU", 5),  # all may be Sundays
        ("FREQ=YEARLY;BYMONTH=3;BYMONTHDAY=1,-7;BYDAY=SU", 2),
        ("FREQ=YEARLY;BYMONTH=3;BYDAY=SU", 5),
        ("FREQ=YEARLY;BYMONTH=3;BYMONTHDAY=1,2,3", 3),
        ("FREQ=YEARLY;BYMONTH=3", 1),
    ],
)
def test_zone_rule_occurrences_per_year(rule: str, per_year: int) -> None:
    from datetime import date

    from icalendar import vRecur

    assert caldav.zone_iteration_bound(vRecur.from_ical(rule), date(2000, 1, 1), date(2009, 12, 31)) == 10 * per_year


def test_the_zone_bound_never_underestimates_dateutil() -> None:
    import random

    import dateutil.rrule
    from icalendar import vRecur

    generator = random.Random(15)  # noqa: S311 - a seeded generator for reproducible test rules
    days = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]
    for _ in range(600):
        parts = [f"FREQ=YEARLY;BYMONTH={generator.randint(1, 12)}"]
        choice = generator.randrange(3)
        if choice == 1:
            parts.append(f"BYDAY={generator.choice(['', '1', '2', '-1', '-2', '5'])}{generator.choice(days)}")
        elif choice == 2:
            parts.append(f"BYDAY={generator.choice(days)}")
        if generator.randrange(2):
            values = generator.sample([*range(1, 32), *range(-31, 0)], generator.randint(1, 7))
            parts.append(f"BYMONTHDAY={','.join(map(str, values))}")
        count = generator.randint(1, 60) if generator.randrange(4) == 0 else None
        start = datetime(generator.randint(1900, 2050), 1, 1)
        end = datetime(2100, 12, 31, 23, 59)
        # dateutil stops at UNTIL (a COUNT rule is the first COUNT of those); without it a rule that never matches
        # would iterate to the year 9999.
        found = list(dateutil.rrule.rrulestr(";".join(parts) + f";UNTIL={end:%Y%m%dT%H%M%S}", dtstart=start))
        actual = min(len(found), count) if count else len(found)
        text = ";".join(parts + ([f"COUNT={count}"] if count else []))
        bound = caldav.zone_iteration_bound(vRecur.from_ical(text), start.date(), end.date())
        assert actual <= bound, text


# --- real zones stay correct ----------------------------------------------------------------------------------


def local(zone_name: str, *stamps: datetime) -> list[str]:
    """`stamps` as wall-clock times in the IANA zone `zone_name`, shown in Zurich."""
    return [stamp.replace(tzinfo=ZoneInfo(zone_name)).astimezone(ZURICH).isoformat() for stamp in stamps]


def starts(name: str, start: datetime = START, end: datetime = END) -> list[str]:
    return [event.start.isoformat() for event in run(ics.load(name), start, end)]


def test_an_outlook_zone_from_1601_is_built_and_correct(zone_builds: list[int]) -> None:
    tuesdays = [datetime(2026, 10, 20, 9), datetime(2026, 10, 27, 9), datetime(2026, 11, 3, 9)]
    assert starts("zone-outlook-1601.ics") == local("Europe/Berlin", *tuesdays, datetime(2026, 11, 10, 9))
    assert zone_builds == [1]


def test_an_apple_zone_is_correct() -> None:
    days = [datetime(2026, 10, 24, 10), datetime(2026, 10, 25, 10), datetime(2026, 10, 26, 10)]
    assert starts("zone-apple.ics") == local("Europe/Zurich", *days)


def test_a_us_zone_with_historical_rules_is_built_and_correct(zone_builds: list[int]) -> None:
    mondays = [datetime(2026, 10, day, 9) for day in (19, 26)] + [datetime(2026, 11, day, 9) for day in (2, 9, 16)]
    assert starts("zone-us-historical.ics") == local("America/New_York", *mondays)
    # 2005: the old rule (last Sunday of October) still applies, through the sub-components with UNTIL.
    old = (datetime(2005, 10, 17, tzinfo=ZURICH), datetime(2005, 11, 8, tzinfo=ZURICH))
    then = [datetime(2005, 10, 17, 9), datetime(2005, 10, 24, 9), datetime(2005, 10, 31, 9), datetime(2005, 11, 7, 9)]
    assert starts("zone-us-historical.ics", *old) == local("America/New_York", *then)
    assert len(zone_builds) == 2


def test_a_transition_list_zone_is_built_and_correct(zone_builds: list[int]) -> None:
    days = [datetime(2026, 10, 23, 10), datetime(2026, 10, 24, 10), datetime(2026, 10, 25, 10)]
    assert starts("zone-rdate-only.ics") == local("Europe/London", *days, datetime(2026, 10, 26, 10))
    assert zone_builds  # twice: tzical refuses X-TZINFO and icalendar retries without X- properties


def test_an_older_month_day_rule_is_built_and_correct(zone_builds: list[int]) -> None:
    mondays = [datetime(2026, 10, 26, 9), datetime(2026, 11, 2, 9), datetime(2026, 11, 9, 9)]
    assert starts("zone-bymonthday-sunday.ics") == local("America/New_York", *mondays)
    assert zone_builds == [1]


# --- layer 2: icalendar never builds a VTIMEZONE whose TZID zoneinfo knows -------------------------------------


@pytest.mark.parametrize("tzid", ["Europe/Zurich", "/Europe/Zurich"])
def test_a_hostile_rule_under_an_iana_tzid_is_never_evaluated(
    tzid: str, monkeypatch: pytest.MonkeyPatch, zone_builds: list[int]
) -> None:
    # The zone screen is switched off to pin the library behaviour underneath it: zoneinfo's own data is used.
    monkeypatch.setattr(caldav, "_screen_zones", lambda text: set())
    raw = obj(zone(f"RRULE:{DENSE}", tzid=tzid), tzid=tzid)
    assert peak_of(lambda: run(raw)) < 2 * 1024 * 1024
    assert [event.start.isoformat() for event in run(raw)] == ["2026-10-20T10:00:00+02:00"]
    assert zone_builds == []


def test_a_custom_tzid_is_built(zone_builds: list[int]) -> None:
    # The counterpart of the test above, so the spy is known to see the builds.
    assert len(run(obj(zone(f"RRULE:{OUTLOOK_RULE}")))) == 1
    assert zone_builds == [1]
