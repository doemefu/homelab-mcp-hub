"""Legitimate real-world shapes that the D62 screens must let through complete (delta review of PR #11, D2)."""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from mcp_hub.providers.caldav import RawEvent, expand
from tests.support import ics

ZURICH = ZoneInfo("Europe/Zurich")
START = datetime(2026, 10, 17, tzinfo=ZURICH)
END = datetime(2026, 11, 17, tzinfo=ZURICH)
OUTLOOK_TZ = (
    "BEGIN:VTIMEZONE\r\nTZID:W. Europe Standard Time\r\nBEGIN:STANDARD\r\nDTSTART:16010101T030000\r\n"
    "TZOFFSETFROM:+0200\r\nTZOFFSETTO:+0100\r\nRRULE:FREQ=YEARLY;INTERVAL=1;BYDAY=-1SU;BYMONTH=10\r\nEND:STANDARD\r\n"
    "BEGIN:DAYLIGHT\r\nDTSTART:16010101T020000\r\nTZOFFSETFROM:+0100\r\nTZOFFSETTO:+0200\r\n"
    "RRULE:FREQ=YEARLY;INTERVAL=1;BYDAY=-1SU;BYMONTH=3\r\nEND:DAYLIGHT\r\nEND:VTIMEZONE\r\n"
)


def run(raw: bytes, start: datetime = START, end: datetime = END) -> list[RawEvent]:
    found = expand(raw, href="/h/", name="Home", start=start, end=end, zone=ZURICH, floating=ZURICH)
    return sorted(found, key=lambda e: e.sort_key)


def weekly_series(overrides: int, *, html_bytes: int = 0, first: datetime = datetime(2024, 1, 2, 9)) -> bytes:
    """A weekly Outlook-style series (Tuesdays 09:00 W. Europe) with `overrides` moved instances, each optionally
    carrying an X-ALT-DESC HTML body of `html_bytes`."""
    tz = "W. Europe Standard Time"
    fmt = "%Y%m%dT%H%M%S"
    html = f"X-ALT-DESC;FMTTYPE=text/html:<html><body>{'h' * html_bytes}</body></html>\r\n" if html_bytes else ""
    master = (
        f"BEGIN:VEVENT\r\nUID:outlook-series@example.test\r\nDTSTAMP:20260901T000000Z\r\n"
        f"DTSTART;TZID={tz}:{first.strftime(fmt)}\r\nDTEND;TZID={tz}:{(first + timedelta(hours=1)).strftime(fmt)}\r\n"
        f"RRULE:FREQ=WEEKLY;BYDAY=TU\r\nSUMMARY:Team meeting\r\n{html}END:VEVENT\r\n"
    )
    moved = "".join(
        f"BEGIN:VEVENT\r\nUID:outlook-series@example.test\r\nDTSTAMP:20260901T000000Z\r\n"
        f"RECURRENCE-ID;TZID={tz}:{(first + timedelta(weeks=i)).strftime(fmt)}\r\n"
        f"DTSTART;TZID={tz}:{(first + timedelta(weeks=i, hours=2)).strftime(fmt)}\r\n"
        f"DTEND;TZID={tz}:{(first + timedelta(weeks=i, hours=3)).strftime(fmt)}\r\n"
        f"SUMMARY:Team meeting (moved)\r\n{html}END:VEVENT\r\n"
        for i in range(overrides)
    )
    head = "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//mcp-hub tests//EN\r\n"
    return f"{head}{OUTLOOK_TZ}{master}{moved}END:VCALENDAR\r\n".encode()


def test_outlook_series_with_html_overrides_comes_back_complete() -> None:
    # 25 overrides with a 12 KB HTML body each: 311+ KiB raw, refused as object_too_large before.
    raw = weekly_series(25, html_bytes=12_000, first=datetime(2026, 9, 1, 9))
    assert len(raw) > 300 * 1024
    events = run(raw)
    assert [e.start.isoformat() for e in events] == [
        "2026-10-20T11:00:00+02:00",
        "2026-10-27T11:00:00+01:00",
        "2026-11-03T11:00:00+01:00",
        "2026-11-10T11:00:00+01:00",
    ]
    assert {e.title for e in events} == {"Team meeting (moved)"}


def test_a_long_series_with_600_overrides_comes_back_complete() -> None:
    events = run(weekly_series(600))  # 2024-01-02 + 600 weeks reaches 2035: every instance in the window is moved
    assert [e.start.isoformat() for e in events] == [
        "2026-10-20T11:00:00+02:00",
        "2026-10-27T11:00:00+01:00",
        "2026-11-03T11:00:00+01:00",
        "2026-11-10T11:00:00+01:00",
    ]


def test_an_apple_birthday_without_a_year_comes_back() -> None:
    march = datetime(2027, 3, 1, tzinfo=ZURICH), datetime(2027, 3, 31, tzinfo=ZURICH)
    [event] = run(ics.load("apple-birthday.ics"), *march)
    assert (event.all_day, event.start, event.end) == (True, date(2027, 3, 15), date(2027, 3, 16))
    assert event.recurring is True


def test_a_twice_daily_rule_returns_both_instances() -> None:
    raw = (
        b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//mcp-hub tests//EN\r\nBEGIN:VEVENT\r\nUID:twice@example.test\r\n"
        b"DTSTAMP:20260901T000000Z\r\nDTSTART;TZID=Europe/Zurich:20261018T070000\r\n"
        b"DTEND;TZID=Europe/Zurich:20261018T073000\r\nRRULE:FREQ=DAILY;BYHOUR=7,19\r\nSUMMARY:Medication\r\n"
        b"END:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    starts = [e.start.isoformat() for e in run(raw)]
    assert starts[:3] == ["2026-10-18T07:00:00+02:00", "2026-10-18T19:00:00+02:00", "2026-10-19T07:00:00+02:00"]
    assert len(starts) == 2 * 30
