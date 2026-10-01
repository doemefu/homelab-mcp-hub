"""Read-only CalDAV adapter (spec 080 rev. 4.4 §5.2 get_events, §5.4, §6.1, D58).

Discovery (principal -> calendar home -> calendars) and one calendar-query REPORT per calendar, all through httpx2
with the credential-destination rule, read as streams capped at 5 MiB; expansion on the client with
recurring-ical-events. Blocking; the tool layer runs it through providers.base.run_blocking. Returns unsanitised text.
"""

import re
import xml.etree.ElementTree as ET
import xml.parsers.expat
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Final, Literal, NoReturn, cast
from zoneinfo import ZoneInfo

import icalendar
import recurring_ical_events

from mcp_hub.providers.base import ProviderError
from mcp_hub.sanitize import validate_timezone

_DAV: Final = "{DAV:}"
_CALDAV: Final = "{urn:ietf:params:xml:ns:caldav}"
_WIDEN: Final = timedelta(days=1)  # query and expansion window widened on both sides (spec 080 rev. 4.4 §5.4)


@dataclass(frozen=True, slots=True)
class RawEvent:
    calendar_href: str
    calendar_name: str | None
    uid: str
    recurrence_id: str
    all_day: bool
    start: datetime | date  # timed: aware, in the requested zone; all-day: date
    end: datetime | date  # all-day: exclusive date
    sort_key: datetime
    recurring: bool
    status: Literal["confirmed", "tentative"]
    attendee_count: int
    title: str | None
    location: str | None
    description: str | None
    organizer_name: str | None
    organizer_address: str | None
    original_timezone: str | None


class _RefusedError(Exception):
    pass


def _refuse(*_: object) -> NoReturn:
    raise _RefusedError


def _clark(name: str) -> str:
    return "{" + name if "}" in name else name  # expat "ns}local" -> ElementTree "{ns}local"


def parse_xml(raw: bytes) -> ET.Element:
    """Stdlib-only hardened parse (D58): expat itself refuses any DOCTYPE and any entity declaration, whatever the
    document encoding (UTF-16 included); parameter entities are never parsed; no external resource is ever fetched.
    The tree is built with ElementTree's TreeBuilder."""
    builder = ET.TreeBuilder()
    parser = xml.parsers.expat.ParserCreate(namespace_separator="}")
    parser.SetParamEntityParsing(xml.parsers.expat.XML_PARAM_ENTITY_PARSING_NEVER)
    parser.StartDoctypeDeclHandler = _refuse
    parser.EntityDeclHandler = _refuse
    parser.UnparsedEntityDeclHandler = _refuse
    parser.ExternalEntityRefHandler = _refuse
    parser.StartElementHandler = lambda name, attrs: builder.start(
        _clark(name), {_clark(key): value for key, value in attrs.items()}
    )
    parser.EndElementHandler = lambda name: builder.end(_clark(name))
    parser.CharacterDataHandler = builder.data
    try:
        parser.Parse(raw, True)
        return builder.close()
    except (_RefusedError, xml.parsers.expat.ExpatError, AssertionError):
        raise ProviderError("upstream_error", "XmlRefused") from None


def parse_multistatus(raw: bytes) -> list[bytes]:
    """calendar-data of every propstat with status 200."""
    found = []
    for propstat in parse_xml(raw).iter(f"{_DAV}propstat"):
        if " 200 " not in f"{propstat.findtext(f'{_DAV}status', '')} ":
            continue
        data = propstat.find(f"{_DAV}prop/{_CALDAV}calendar-data")
        if data is not None and data.text:
            found.append(data.text.encode())
    return found


def _aware(value: datetime, floating: ZoneInfo) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=floating)


def _zone_name(value: date) -> str | None:
    """ "UTC" for a UTC DTSTART, null for floating times; otherwise a validated IANA name (spec 080 §5.2)."""
    if not isinstance(value, datetime) or value.tzinfo is None:
        return None
    if value.tzinfo is UTC or (value.utcoffset() == timedelta(0) and value.tzname() == "UTC"):
        return "UTC"
    return validate_timezone(getattr(value.tzinfo, "key", None))


def _end(component: icalendar.Event, begin: date) -> date:
    if "DTEND" in component:
        return cast(date, component.decoded("DTEND"))
    if "DURATION" in component:
        return cast(date, begin + component.decoded("DURATION"))
    return begin if isinstance(begin, datetime) else begin + timedelta(days=1)


def _text(component: icalendar.Event, name: str) -> str | None:
    value = component.get(name)
    return str(value) if value is not None else None


def _overlaps(low: datetime, high: datetime, start: datetime, end: datetime) -> bool:
    return low < end and (high > start or (high == low and low >= start))  # zero-length at `from` counts


def expand(
    ics: bytes, *, href: str, name: str | None, start: datetime, end: datetime, zone: ZoneInfo, floating: ZoneInfo
) -> list[RawEvent]:
    """Every instance overlapping [start, end): RRULE, RDATE, EXDATE and overrides expanded, cancelled ones omitted;
    timed events in `zone`, floating times placed in `floating`, all-day events as dates."""
    calendar = icalendar.Calendar.from_ical(ics)
    recurring_uids = {
        str(c.get("UID", "")) for c in calendar.walk("VEVENT") if {"RRULE", "RDATE", "RECURRENCE-ID"} & set(c)
    }
    query = recurring_ical_events.of(calendar, skip_bad_series=True)
    events: list[RawEvent] = []
    for component in query.between(start.astimezone(UTC) - _WIDEN, end.astimezone(UTC) + _WIDEN):
        if component.name != "VEVENT" or str(component.get("STATUS", "CONFIRMED")).upper() == "CANCELLED":
            continue
        begin = cast(date, component.decoded("DTSTART"))
        finish = _end(component, begin)
        all_day = not isinstance(begin, datetime)
        shown_start: datetime | date
        shown_end: datetime | date
        if all_day:
            low, high = datetime.combine(begin, time(), zone), datetime.combine(finish, time(), zone)
            shown_start, shown_end = begin, finish
        else:
            low, high = _aware(cast(datetime, begin), floating), _aware(cast(datetime, finish), floating)
            shown_start, shown_end = low.astimezone(zone), high.astimezone(zone)
        if not _overlaps(low, high, start, end):
            continue
        recurrence = component.get("RECURRENCE-ID")
        rid = recurrence.dt if recurrence is not None else begin
        if isinstance(rid, datetime) and rid.tzinfo is not None:
            rid = rid.astimezone(UTC)
        organizer = component.get("ORGANIZER")
        attendees = component.get("ATTENDEE")
        uid = str(component.get("UID", ""))
        events.append(
            RawEvent(
                calendar_href=href,
                calendar_name=name,
                uid=uid,
                recurrence_id=rid.isoformat(),
                all_day=all_day,
                start=shown_start,
                end=shown_end,
                sort_key=low.astimezone(UTC),
                recurring=uid in recurring_uids,
                status="tentative" if str(component.get("STATUS", "")).upper() == "TENTATIVE" else "confirmed",
                attendee_count=len(attendees) if isinstance(attendees, list) else int(attendees is not None),
                title=_text(component, "SUMMARY"),
                location=_text(component, "LOCATION"),
                description=_text(component, "DESCRIPTION"),
                organizer_name=(
                    str(organizer.params["CN"]) if organizer is not None and "CN" in organizer.params else None
                ),
                organizer_address=re.sub(r"(?i)^mailto:", "", str(organizer)) if organizer is not None else None,
                original_timezone=None if all_day else _zone_name(begin),
            )
        )
    return events
