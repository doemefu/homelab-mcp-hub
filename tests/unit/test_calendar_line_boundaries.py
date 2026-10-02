"""The text the hub screens is the text the libraries evaluate (review of PR #15, G1).

The hub and icalendar split lines on CR/LF only; dateutil's tzical builds each custom zone with str.splitlines(),
which also splits on other characters. Layer 1 replaces those characters with a space before anything else reads
the object. Layer 2 lets dateutil evaluate only rule text the screens approved for this object."""

import contextlib
import inspect
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import dateutil.rrule
import dateutil.tz.tz
import icalendar
import pytest
import recurring_ical_events.series.rrule as library_rrule

from mcp_hub.providers import caldav
from mcp_hub.providers.caldav import ObjectSkippedError, RawEvent, expand
from tests.support import ics

ZURICH = ZoneInfo("Europe/Zurich")
START = datetime(2026, 10, 17, tzinfo=ZURICH)
END = datetime(2026, 11, 17, tzinfo=ZURICH)
CUSTOM = "Custom/Probe"
SEPARATORS = [chr(code) for code in (0x0B, 0x0C, 0x1C, 0x1D, 0x1E, 0x85, 0x2028, 0x2029)]
SMUGGLED = "RRULE:FREQ=SECONDLY"


def run(raw: bytes) -> list[RawEvent]:
    found = expand(raw, href="/h/", name="Home", start=START, end=END, zone=ZURICH, floating=ZURICH)
    return sorted(found, key=lambda e: e.sort_key)


def obj(*, comment: str = "COMMENT:x", summary: str = "SUMMARY:Probe", rule: str = "") -> bytes:
    """One event in a custom zone (built by icalendar through dateutil's tzical); `comment` is a whole content line
    inside the zone's STANDARD sub-component."""
    extra = f"RRULE:{rule}\r\n" if rule else ""
    return (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//mcp-hub tests//EN\r\n"
        f"BEGIN:VTIMEZONE\r\nTZID:{CUSTOM}\r\nBEGIN:STANDARD\r\nDTSTART:16010101T030000\r\nTZOFFSETFROM:+0200\r\n"
        f"TZOFFSETTO:+0100\r\nRRULE:FREQ=YEARLY;INTERVAL=1;BYDAY=-1SU;BYMONTH=10\r\n{comment}\r\nEND:STANDARD\r\n"
        "BEGIN:DAYLIGHT\r\nDTSTART:16010101T020000\r\nTZOFFSETFROM:+0100\r\nTZOFFSETTO:+0200\r\n"
        "RRULE:FREQ=YEARLY;INTERVAL=1;BYDAY=-1SU;BYMONTH=3\r\nEND:DAYLIGHT\r\nEND:VTIMEZONE\r\n"
        f"BEGIN:VEVENT\r\nUID:separator@example.test\r\nDTSTAMP:20260901T000000Z\r\n"
        f"DTSTART;TZID={CUSTOM}:20261020T100000\r\nDURATION:PT1H\r\n{extra}{summary}\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    ).encode()


@pytest.fixture
def parsed_rules(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every text dateutil's rule parser receives (event rules and the rules of the zones it builds)."""
    seen: list[str] = []
    original = dateutil.rrule._rrulestr._parse_rfc

    def spy(self: Any, s: str, **kwargs: Any) -> Any:
        seen.append(s)
        return original(self, s, **kwargs)

    monkeypatch.setattr(dateutil.rrule._rrulestr, "_parse_rfc", spy)
    return seen


def smuggled(seen: list[str]) -> bool:
    return any("SECONDLY" in text.upper() for text in seen)


# --- layer 1: normalise line boundaries before anything reads the object -------------------------------------------


def test_the_set_is_every_line_boundary_python_knows_besides_cr_and_lf() -> None:
    # A future Python that adds a separator to str.splitlines() makes this fail.
    derived = [c for c in map(chr, range(0x110000)) if len(("a" + c + "b").splitlines()) > 1]
    assert sorted(set(derived) - {"\r", "\n"}) == sorted(caldav.LINE_BOUNDARIES)
    assert sorted(caldav.LINE_BOUNDARIES) == sorted(SEPARATORS)


PLACES = {
    "zone-property": lambda sep: obj(comment=f"COMMENT:x{sep}{SMUGGLED}"),
    "zone-folded-line": lambda sep: obj(comment=f"COMMENT:x\r\n {sep}{SMUGGLED}"),
    "zone-quoted-parameter": lambda sep: obj(comment=f'COMMENT;X-A="a{sep}{SMUGGLED}":x'),
    "zone-name": lambda sep: obj(comment=f"TZNAME:CET{sep}{SMUGGLED}"),
}


@pytest.mark.parametrize("place", list(PLACES))
@pytest.mark.parametrize("sep", SEPARATORS, ids=[f"U+{ord(c):04X}" for c in SEPARATORS])
def test_a_separator_in_a_zone_does_not_reach_dateutil_as_a_rule(place: str, sep: str, parsed_rules: list[str]) -> None:
    events = run(PLACES[place](sep))
    assert [event.start.isoformat() for event in events] == ["2026-10-20T10:00:00+02:00"]
    assert parsed_rules  # the zone was built
    assert not smuggled(parsed_rules)


@pytest.mark.parametrize("sep", SEPARATORS, ids=[f"U+{ord(c):04X}" for c in SEPARATORS])
def test_a_separator_in_an_event_becomes_a_space(sep: str, parsed_rules: list[str]) -> None:
    [event] = run(obj(summary=f"SUMMARY:Team{sep}{SMUGGLED}"))
    assert event.title == f"Team {SMUGGLED}"
    assert not smuggled(parsed_rules)


@pytest.mark.parametrize("sep", SEPARATORS, ids=[f"U+{ord(c):04X}" for c in SEPARATORS])
def test_a_separator_inside_an_event_rule_cannot_split_it(sep: str, parsed_rules: list[str]) -> None:
    # dateutil splits a single rule on any whitespace. After normalisation the COUNT value reads "2 FREQ=SECONDLY";
    # icalendar drops the unreadable part, and dateutil receives the screened FREQ=DAILY.
    run(obj(rule=f"FREQ=DAILY;COUNT=2{sep}FREQ=SECONDLY"))
    assert "FREQ=DAILY" in parsed_rules
    assert not smuggled(parsed_rules)


# --- layer 2: dateutil evaluates only rule text the screens approved -------------------------------------------


@pytest.mark.parametrize("place", ["zone-property", "zone-folded-line", "zone-name"])
@pytest.mark.parametrize("sep", SEPARATORS, ids=[f"U+{ord(c):04X}" for c in SEPARATORS])
def test_the_guard_refuses_g1_without_normalisation(
    place: str, sep: str, monkeypatch: pytest.MonkeyPatch, parsed_rules: list[str]
) -> None:
    monkeypatch.setattr(caldav, "_normalise_line_boundaries", lambda ics: ics)  # layer 1 off
    with pytest.raises(ObjectSkippedError) as caught:
        run(PLACES[place](sep))
    assert caught.value.reason == "rule_refused"
    assert not smuggled(parsed_rules)


@pytest.mark.parametrize("sep", SEPARATORS, ids=[f"U+{ord(c):04X}" for c in SEPARATORS])
def test_without_normalisation_icalendar_refuses_a_separator_in_a_quoted_parameter(
    sep: str, monkeypatch: pytest.MonkeyPatch, parsed_rules: list[str]
) -> None:
    monkeypatch.setattr(caldav, "_normalise_line_boundaries", lambda ics: ics)  # layer 1 off
    # icalendar refuses the parameter, or dateutil's tzical the half line: the object is skipped as unparseable.
    with pytest.raises(ValueError, match=r"parameter|unpack"):
        run(PLACES["zone-quoted-parameter"](sep))
    assert not smuggled(parsed_rules)


def test_the_guard_refuses_a_zone_rule_the_screen_did_not_approve(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(caldav, "_screen_zones", lambda text: set())  # approves nothing
    with pytest.raises(ObjectSkippedError) as caught:
        run(obj())
    assert caught.value.reason == "rule_refused"


def test_the_guard_refuses_an_event_rule_the_screen_did_not_approve(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(caldav, "_screen", lambda calendar, window_end: set())  # approves nothing
    with pytest.raises(ObjectSkippedError) as caught:
        run(ics.load("dst-weekly.ics"))
    assert caught.value.reason == "rule_refused"


@pytest.mark.parametrize(
    ("text", "multi_line", "allowed"),
    [
        ("FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU", False, True),
        ("freq=yearly;bymonth=10;byday=-1su", False, True),
        ("BYDAY=-1SU;FREQ=YEARLY;BYMONTH=10", False, True),  # order does not matter
        ("FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU;UNTIL=20261231T000000Z", False, False),  # UNTIL was not approved
        ("FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU FREQ=SECONDLY", False, False),  # dateutil would read two rules
        ("FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU\r\n", False, True),  # dateutil splits on whitespace: one rule
        ("RRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU", False, True),
        ("FREQ=SECONDLY", False, False),
        ("DTSTART:16010101T030000\nRRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU", True, True),
        ("DTSTART:16010101T030000\nRRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU\nRRULE:FREQ=SECONDLY", True, False),
        ("DTSTART:16010101T030000\nFREQ=SECONDLY", True, False),  # a line without a colon is a rule to dateutil
        ("DTSTART:16010101T030000\nEXRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU", True, False),
        ("DTSTART:16010101T030000\nRDATE:20261025T030000\nEXDATE:20261025T030000", True, True),
        ("DTSTART:16010101T030000\nX-RULE:FREQ=SECONDLY", True, False),
    ],
)
def test_the_guard_reads_rule_text_as_dateutil_does(text: str, multi_line: bool, allowed: bool) -> None:
    approved = {caldav.canonical_rule("FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU")}
    if allowed:
        caldav.check_rule_text(text, approved, multi_line=multi_line)
    else:
        with pytest.raises(caldav.RuleNotApprovedError):
            caldav.check_rule_text(text, approved, multi_line=multi_line)


def test_the_until_value_the_library_reformats_is_accepted() -> None:
    # recurring-ical-events rewrites a local UNTIL to UTC and moves it to the end before calling dateutil again.
    approved = {caldav.canonical_rule("FREQ=WEEKLY;UNTIL=20261231;BYDAY=TU")}
    caldav.check_rule_text("FREQ=WEEKLY;BYDAY=TU;UNTIL=20261231T000000Z", approved, multi_line=False)
    with pytest.raises(caldav.RuleNotApprovedError):
        caldav.check_rule_text("FREQ=WEEKLY;BYDAY=TU;UNTIL=20261231T000000Z;X=1", approved, multi_line=False)


def test_the_guard_patch_points_are_what_the_pinned_libraries_use() -> None:
    # Fails loudly if a library upgrade moves or renames the calls the guard wraps.
    assert "rrulestr(rule_string, dtstart=self.start, cache=True)" in inspect.getsource(library_rrule)
    tzical_source = inspect.getsource(dateutil.tz.tz.tzical)
    assert "from dateutil import rrule" in tzical_source
    assert "rr = rrule.rrulestr(" in tzical_source
    assert "lines = s.splitlines()" in tzical_source
    assert "tzical(file).get()" in inspect.getsource(icalendar.timezone.zoneinfo.ZONEINFO._create_timezone)


def test_both_guards_run_and_are_removed_afterwards(monkeypatch: pytest.MonkeyPatch) -> None:
    checked: list[bool] = []
    original = caldav.check_rule_text

    def spy(text: str, approved: set[tuple[str, ...]], *, multi_line: bool) -> None:
        checked.append(multi_line)
        original(text, approved, multi_line=multi_line)

    monkeypatch.setattr(caldav, "check_rule_text", spy)
    assert len(run(obj(rule="FREQ=DAILY;COUNT=2"))) == 2
    assert set(checked) == {True, False}  # zone rules (multi-line) and the event rule
    assert dateutil.rrule.rrulestr is caldav.DATEUTIL_RRULESTR
    assert library_rrule.rrulestr is dateutil.rrule.rrulestr


@pytest.mark.parametrize(
    "name",
    [
        "zone-outlook-1601.ics",
        "zone-apple.ics",
        "zone-us-historical.ics",
        "zone-rdate-only.ics",
        "zone-bymonthday-sunday.ics",
        *ics.NAMES,
        "apple-birthday.ics",
    ],
)
def test_results_are_identical_with_the_guard(name: str) -> None:
    raw = ics.load(name)
    calendar = icalendar.Calendar.from_ical(raw)  # the libraries as shipped, outside the hub's guard
    reference = caldav._instances(calendar, href="/h/", name="Home", start=START, end=END, zone=ZURICH, floating=ZURICH)
    assert [e.sort_key for e in run(raw)] == sorted(e.sort_key for e in reference)


# --- the hunt for other differences between the screened text and the evaluated text --------------------------


def event_obj(lines: str) -> bytes:
    return (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//mcp-hub tests//EN\r\nBEGIN:VEVENT\r\nUID:hunt@example.test\r\n"
        f"DTSTAMP:20260901T000000Z\r\nDTSTART:20261020T100000Z\r\nDURATION:PT1H\r\n{lines}\r\nSUMMARY:h\r\n"
        "END:VEVENT\r\nEND:VCALENDAR\r\n"
    ).encode()


HUNT = {
    # icalendar unescapes TEXT values and escapes them again when it serialises the zone for dateutil.
    "escaped-newline-in-comment": obj(comment=f"COMMENT:x\\n{SMUGGLED}"),
    "escaped-capital-newline-in-tzname": obj(comment=f"TZNAME:CET\\N{SMUGGLED}"),
    "escaped-newline-in-x-property": obj(comment=f"X-NOTE:x\\n{SMUGGLED}"),
    "escaped-semicolon-in-zone-rule": obj(comment=r"RRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU\;FREQ=SECONDLY"),
    "escaped-semicolon-in-event-rule": event_obj(r"RRULE:FREQ=DAILY;COUNT=2\;FREQ=SECONDLY"),
    "escaped-comma-in-zone-rdate": obj(comment="RDATE:20261025T030000\\,20261026T030000"),
    # RFC 6868 caret encoding in parameter values.
    "caret-newline-in-quoted-parameter": obj(comment=f'COMMENT;X-A="a^n{SMUGGLED}":x'),
    "caret-newline-in-tzname-language": obj(comment=f"TZNAME;LANGUAGE=a^n{SMUGGLED}:CET"),
    "caret-newline-in-event-parameter": event_obj(f'SUMMARY;X-A="a^n{SMUGGLED}":x'),
    # Bare CR (XML turns it into LF before the hub sees it; the pre-screen treats it as a line end).
    "bare-cr-in-comment": obj(comment=f"COMMENT:x\r{SMUGGLED}"),
    "bare-cr-in-event-text": event_obj(f"SUMMARY:x\r{SMUGGLED}"),
    # Tab continuation, parameters, case, repeated properties, rules under other names.
    "tab-fold-in-zone-rule": obj(comment="RRULE:FREQ=YEARLY;BYMONTH=10;BY\r\n\tHOUR=1;BYDAY=-1SU"),
    "zone-rule-with-parameter": obj(comment="RRULE;X-A=1:FREQ=SECONDLY"),
    "lower-case-zone-rule": obj(comment="rrule:freq=secondly"),
    "second-dtstart-in-zone": obj(comment="DTSTART:20200101T000000"),
    "rule-under-an-x-name-in-zone": obj(comment="X-RRULE:FREQ=SECONDLY"),
    "event-rule-with-parameter": event_obj("RRULE;X-A=1:FREQ=SECONDLY"),
    "event-exrule": event_obj("RRULE:FREQ=DAILY;COUNT=2\r\nEXRULE:FREQ=SECONDLY"),
    "rule-under-an-x-name-in-event": event_obj("X-WR-RRULE:FREQ=SECONDLY"),
}


@pytest.mark.parametrize("guard", [True, False], ids=["guard-on", "guard-off"])
@pytest.mark.parametrize("case", list(HUNT))
def test_no_other_text_difference_reaches_dateutil_as_a_rule(
    case: str, guard: bool, monkeypatch: pytest.MonkeyPatch, parsed_rules: list[str]
) -> None:
    if not guard:  # the screens and icalendar's own escaping hold on their own too
        monkeypatch.setattr(caldav, "check_rule_text", lambda text, approved, *, multi_line: None)
    with contextlib.suppress(ObjectSkippedError, ValueError):  # refused, or unparseable for icalendar
        run(HUNT[case])
    assert not smuggled(parsed_rules)
