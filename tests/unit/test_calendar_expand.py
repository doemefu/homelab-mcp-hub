from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from mcp_hub.providers.base import ProviderError
from mcp_hub.providers.caldav import RawEvent, expand, parse_multistatus, parse_xml
from tests.support import ics

ZURICH, UTC_ZONE = ZoneInfo("Europe/Zurich"), ZoneInfo("UTC")
START = datetime(2026, 10, 17, tzinfo=ZURICH)
END = datetime(2026, 11, 2, tzinfo=ZURICH)


def run(name: str, zone: ZoneInfo = ZURICH, start: datetime = START, end: datetime = END) -> list[RawEvent]:
    return expand(
        ics.load(name),
        href="https://cal.example.test/1/",
        name="Home",
        start=start,
        end=end,
        zone=zone,
        floating=ZURICH,
    )


def test_weekly_series_across_the_dst_change() -> None:
    assert [e.start.isoformat() for e in run("dst-weekly.ics")] == [
        "2026-10-18T10:00:00+02:00",
        "2026-10-25T10:00:00+01:00",
        "2026-11-01T10:00:00+01:00",
    ]
    assert [e.start.isoformat() for e in run("dst-weekly.ics", UTC_ZONE)][:2] == [
        "2026-10-18T08:00:00+00:00",
        "2026-10-25T09:00:00+00:00",
    ]
    assert all(e.recurring for e in run("dst-weekly.ics"))


def test_exdate_override_and_cancelled_override() -> None:
    events = run("standup-overrides.ics")
    assert [(e.start.isoformat(), e.title) for e in events] == [
        ("2026-10-19T09:00:00+02:00", "Standup"),
        ("2026-10-21T11:00:00+02:00", "Moved standup"),
        ("2026-10-23T09:00:00+02:00", "Standup"),
    ]
    assert len({e.recurrence_id for e in events}) == 3
    assert all(e.recurring for e in events)


def test_cancelled_event_and_window_edge_are_omitted() -> None:
    assert run("cancelled.ics") == []
    assert run("window-edge.ics") == []


def test_all_day_event_is_returned_as_dates() -> None:
    [event] = run("allday.ics")
    assert event.all_day
    assert (event.start, event.end) == (date(2026, 10, 24), date(2026, 10, 26))
    assert event.recurring is False
    assert event.original_timezone is None


def test_cross_zone_tentative_event() -> None:
    [event] = run("cross-zone.ics")
    assert (event.start.isoformat(), event.end.isoformat()) == (
        "2026-10-28T14:00:00+01:00",
        "2026-10-28T15:00:00+01:00",
    )
    assert (event.status, event.original_timezone) == ("tentative", "America/New_York")


def test_floating_time_uses_the_default_zone() -> None:
    [event] = run("floating.ics")
    assert event.start.isoformat() == "2026-10-21T08:00:00+02:00"
    [event] = run("floating.ics", UTC_ZONE)
    assert event.start.isoformat() == "2026-10-21T06:00:00+00:00"
    assert event.original_timezone is None


def test_rdate_adds_an_instance_with_the_series_duration() -> None:
    assert [(e.start.isoformat(), e.end.isoformat()) for e in run("rdate.ics")] == [
        ("2026-10-29T15:00:00+01:00", "2026-10-29T16:00:00+01:00"),
        ("2026-10-30T17:00:00+01:00", "2026-10-30T18:00:00+01:00"),
    ]
    assert all(e.recurring for e in run("rdate.ics"))


def test_organizer_attendees_and_raw_texts() -> None:
    [event] = run("hostile.ics")
    assert (event.organizer_name, event.organizer_address, event.attendee_count) == (
        "Org Name",
        "organizer@example.test",
        2,
    )
    assert event.original_timezone == "UTC"
    assert event.start.isoformat() == "2026-10-27T13:00:00+01:00"
    assert event.status == "confirmed"


def test_all_fixtures_give_the_twelve_instances_in_order() -> None:
    events = [e for name in ics.NAMES for e in run(name)]
    events.sort(key=lambda e: e.sort_key)
    assert [e.title for e in events] == [
        "Weekly DST",
        "Standup",
        "Floating",
        "Moved standup",
        "Standup",
        "Weekend away",
        "Weekly DST",
        "Invoice\u200b https://evil.example.test/p?x=1 act now",
        "Call New York",
        "Workshop",
        "Workshop",
        "Weekly DST",
    ]


def test_multistatus_returns_calendar_data_of_200_propstats_only() -> None:
    raw = (
        b'<?xml version="1.0"?><D:multistatus xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">'
        b"<D:response><D:href>/a.ics</D:href><D:propstat><D:prop><C:calendar-data>BEGIN:VCALENDAR\r\nEND:VCALENDAR"
        b"</C:calendar-data></D:prop><D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response>"
        b"<D:response><D:href>/b.ics</D:href><D:propstat><D:prop><C:calendar-data/></D:prop>"
        b"<D:status>HTTP/1.1 404 Not Found</D:status></D:propstat></D:response></D:multistatus>"
    )
    # XML end-of-line handling turns CRLF into LF; icalendar accepts both.
    assert parse_multistatus(raw) == [b"BEGIN:VCALENDAR\nEND:VCALENDAR"]


@pytest.mark.parametrize(
    "bad",
    [
        b"<not-xml",
        b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><x>&a;</x>',
        '<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE x [<!ENTITY a "aaaa">]><x>&a;</x>'.encode("utf-16"),
        b'<?xml version="1.0"?><!DOCTYPE x SYSTEM "file:///etc/passwd"><x/>',
    ],
    ids=["malformed", "entity", "utf16-entity", "system-dtd"],
)
def test_multistatus_refuses_dtds_entities_and_malformed_xml(bad: bytes) -> None:
    with pytest.raises(ProviderError) as caught:
        parse_multistatus(bad)
    assert caught.value.code == "upstream_error"


@pytest.mark.parametrize(
    ("raw", "limit"),
    [
        (b"<a>" * 33 + b"</a>" * 33, "depth"),
        (b"<r>" + b"<e/>" * 100_000 + b"</r>", "elements"),
    ],
    ids=["depth-33", "100001-elements"],
)
def test_xml_depth_and_element_count_are_bounded(raw: bytes, limit: str) -> None:
    with pytest.raises(ProviderError) as caught:
        parse_xml(raw)
    assert (caught.value.code, caught.value.cause) == ("upstream_error", "XmlRefused")


def test_xml_at_the_limits_is_accepted() -> None:
    assert parse_xml(b"<a>" * 32 + b"</a>" * 32).tag == "a"
    assert len(parse_xml(b"<r>" + b"<e/>" * 99_999 + b"</r>")) == 99_999


def test_a_deeply_nested_5_mib_document_is_refused_early() -> None:
    # Review 19 F2: 5 MiB of nested elements built a 583 MB tree before.
    depth = 5 * 1024 * 1024 // 7
    raw = b"<a>" * depth + b"</a>" * depth  # well-formed: only the depth limit can refuse it
    with pytest.raises(ProviderError):
        parse_xml(raw)


def test_an_event_starting_exactly_at_the_window_end_is_excluded() -> None:
    # Review 19 M11: overlap is start < to. 18:00 New York (EST) == 2026-11-01T23:00Z == END.
    raw = (
        ics.load("cross-zone.ics")
        .replace(b"20261028T090000", b"20261101T180000")
        .replace(b"20261028T100000", b"20261101T190000")
    )

    def expand_until(end: datetime) -> list[RawEvent]:
        return expand(raw, href="/h/", name="Home", start=START, end=end, zone=ZURICH, floating=ZURICH)

    assert expand_until(END) == []
    assert len(expand_until(END + timedelta(seconds=1))) == 1
