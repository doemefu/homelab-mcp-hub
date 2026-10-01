"""Read-only CalDAV adapter (spec 080 rev. 4.4 §5.2 get_events, §5.4, §6.1, D58).

Discovery (principal -> calendar home -> calendars) and one calendar-query REPORT per calendar, all through httpx2
with the credential-destination rule, read as streams capped at 5 MiB; expansion on the client with
recurring-ical-events. Blocking; the tool layer runs it through providers.base.run_blocking. Returns unsanitised text.

This is the only module that imports icalendar, recurring-ical-events or dateutil (a unit test enforces it). `expand()`
is the single guarded entry point for parsing and expanding one calendar object (pre-screen, rule screen, per-object
time-zone isolation, CPU deadline, caps; spec 080 rev. 4.5 D62); the ICS-feed adapter of a later story must use it.
"""

import hashlib
import logging
import re
import sys
import threading
import time as clocks
import xml.etree.ElementTree as ET
import xml.parsers.expat
from collections import OrderedDict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from types import FrameType
from typing import Final, Literal, NoReturn, cast
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo

import httpx2
import icalendar
import recurring_ical_events
from icalendar.timezone import tzp

from mcp_hub.logging import log_event
from mcp_hub.providers.base import MAX_HTTP_RESPONSE_BYTES, PROVIDER_TIMEOUT_SECONDS, ProviderError, read_capped
from mcp_hub.sanitize import FIELD_LIMITS, validate_timezone

_DAV: Final = "{DAV:}"
_CALDAV: Final = "{urn:ietf:params:xml:ns:caldav}"
REPLACEMENT_CHARACTER: Final = "\ufffd"
_XML_FORBIDDEN: Final = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]")
MAX_XML_DEPTH: Final = 32  # a multistatus is about 8 levels deep
MAX_XML_ELEMENTS: Final = 100_000  # per response (spec 080 rev. 4.5, review 19 F2)
_XML_ENCODING: Final = re.compile(rb"\s*<\?xml[^>]*?encoding\s*=\s*[\"']([A-Za-z0-9._-]+)[\"']")
RAW_TEXT_FACTOR: Final = 4  # third-party text is cut to 4x its output field limit at extraction (worker thread)
_WIDEN: Final = timedelta(days=1)  # query and expansion window widened on both sides (spec 080 rev. 4.4 §5.4)
# Bounded expansion (spec 080 rev. 4.5 D62). Each calendar object is screened before the expansion library sees it,
# expanded under a CPU deadline, and the whole call is capped by instance counts and a cooperative time budget.
ALLOWED_FREQUENCIES: Final = frozenset({"YEARLY", "MONTHLY", "WEEKLY", "DAILY"})
MAX_RECURRENCE_DATES: Final = 1000  # RDATE values and EXDATE values, each counted per object
MAX_OBJECT_BYTES: Final = 256 * 1024  # one calendar object, checked before parsing
MAX_COMPONENTS_PER_OBJECT: Final = 500  # VEVENT components (master and overrides) per object
MAX_TIMEZONES_PER_OBJECT: Final = 20  # VTIMEZONE components per object (real objects carry one to three)
_TIME_OF_DAY_PARTS: Final = ("BYHOUR", "BYMINUTE", "BYSECOND")  # at most one value each
EARLIEST_START: Final = date(1900, 1, 1)
MAX_INSTANCES_PER_OBJECT: Final = 1000
MAX_INSTANCES_PER_CALL: Final = 2000  # per account and call
OBJECT_CPU_SECONDS: Final = 2.0  # thread CPU time per object (parse, screening, expansion)
EXPANSION_BUDGET_SECONDS: Final = 5.0  # monotonic, per account and call, checked between objects
SLOW_OBJECT_CACHE_SIZE: Final = 1000
# REPORT bodies held per account and call (discovery bodies have their own 5 MiB cap and do not count). Calendars are
# fetched sequentially in path order; the cap drops whole calendars from the end of that order (D62).
MAX_REPORT_BYTES_PER_CALL: Final = 10 * 1024 * 1024
_FOLD: Final = re.compile(rb"\r?\n[ \t]")
_RECURRING_LINE: Final = re.compile(rb"(?im)^(?:RRULE|RDATE)[;:]")
_BEGIN_VEVENT: Final = re.compile(rb"(?im)^BEGIN:VEVENT[ \t]*\r?$")
_BEGIN_VTIMEZONE: Final = re.compile(rb"(?im)^BEGIN:VTIMEZONE[ \t]*\r?$")
_DATE_LINE: Final = re.compile(rb"(?im)^(RDATE|EXDATE)[;:][^\r\n]*")


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


def _parse(raw: bytes) -> ET.Element:
    builder = ET.TreeBuilder()
    parser = xml.parsers.expat.ParserCreate(namespace_separator="}")
    parser.SetParamEntityParsing(xml.parsers.expat.XML_PARAM_ENTITY_PARSING_NEVER)
    parser.StartDoctypeDeclHandler = _refuse
    parser.EntityDeclHandler = _refuse
    parser.UnparsedEntityDeclHandler = _refuse
    parser.ExternalEntityRefHandler = _refuse
    depth = elements = 0

    def start(name: str, attrs: dict[str, str]) -> None:
        nonlocal depth, elements
        depth, elements = depth + 1, elements + 1
        if depth > MAX_XML_DEPTH or elements > MAX_XML_ELEMENTS:  # a 5 MiB body could build a tree of ~600 MB
            raise _RefusedError
        builder.start(_clark(name), {_clark(key): value for key, value in attrs.items()})

    def end(name: str) -> None:
        nonlocal depth
        depth -= 1
        builder.end(_clark(name))

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = builder.data
    parser.Parse(raw, True)
    return builder.close()


def _declares_utf8(raw: bytes) -> bool:
    """UTF-8 by declaration or by default (no UTF-16/32 byte-order mark, no other declared encoding)."""
    if raw.startswith((b"\xff\xfe", b"\xfe\xff", b"\x00\x00\xfe\xff")):
        return False
    declared = _XML_ENCODING.match(raw.removeprefix(b"\xef\xbb\xbf"))
    return declared is None or declared.group(1).lower() in (b"utf-8", b"utf8")


def _repaired(raw: bytes) -> bytes | None:
    """The UTF-8 document with invalid bytes and XML 1.0-forbidden code points (C0 controls except TAB, LF and CR;
    U+FFFE; U+FFFF) replaced by U+FFFD, or None when it is not UTF-8 or nothing needs repair."""
    if not _declares_utf8(raw):
        return None
    fixed = _XML_FORBIDDEN.sub(REPLACEMENT_CHARACTER, raw.decode("utf-8", "replace")).encode("utf-8")
    return fixed if fixed != raw else None


def parse_xml(raw: bytes, *, account: str | None = None) -> ET.Element:
    """Stdlib-only hardened parse (D58): expat itself refuses any DOCTYPE and any entity declaration, whatever the
    document encoding (UTF-16 included); parameter entities are never parsed; no external resource is ever fetched;
    depth and element count are bounded. The tree is built with ElementTree's TreeBuilder.

    A UTF-8 document that is not well-formed only because of invalid bytes or XML-forbidden characters is repaired
    (U+FFFD) and parsed exactly once more by the same refusing parser, so one broken object does not cost the whole
    calendar (spec 080 rev. 4.5, review 19 F4). A refusal is never retried; other encodings get no retry."""
    try:
        return _parse(raw)
    except _RefusedError:
        raise ProviderError("upstream_error", "XmlRefused") from None
    except (xml.parsers.expat.ExpatError, AssertionError):
        repaired = _repaired(raw)
    if repaired is not None:
        try:
            tree = _parse(repaired)
        except (_RefusedError, xml.parsers.expat.ExpatError, AssertionError):
            pass
        else:
            if account is not None:
                log_event(_log, logging.WARNING, "xml_encoding_repaired", account=account, capability="calendar")
            return tree
    raise ProviderError("upstream_error", "XmlRefused")


def parse_multistatus(raw: bytes, *, account: str | None = None) -> list[bytes]:
    """calendar-data of every propstat with status 200."""
    found = []
    for propstat in parse_xml(raw, account=account).iter(f"{_DAV}propstat"):
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


def _first(component: icalendar.cal.Component, name: str) -> object:
    """The first value of a property that may repeat (two SUMMARY lines give a list, review 19 F7)."""
    value = component.get(name)
    return value[0] if isinstance(value, list) and value else value


def _cut(value: object, field: str) -> str:
    """str(value), at most RAW_TEXT_FACTOR x the output field limit: the tool layer never cleans more than that."""
    return str(value)[: RAW_TEXT_FACTOR * FIELD_LIMITS[field]]


def _text(component: icalendar.Event, name: str, field: str, strings: dict[int, tuple[object, str]]) -> str | None:
    """The property as str, cut before it is shared. Instances of one object share the library's value objects;
    converting each only once keeps memory at the object's size instead of size x instances (D62 E)."""
    value = _first(component, name)
    if value is None:
        return None
    known = strings.get(id(value))
    if known is None:
        known = strings[id(value)] = (value, _cut(value, field))  # the value is kept alive, so its id stays unique
    return known[1]


def _overlaps(low: datetime, high: datetime, start: datetime, end: datetime) -> bool:
    return low < end and (high > start or (high == low and low >= start))  # zero-length at `from` counts


class ObjectSkippedError(Exception):
    """A calendar object the hub does not expand; `reason` is a fixed outcome for the log (spec 080 rev. 4.5 D62)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _ExpansionTooSlow(BaseException):
    """Raised by the trace hook. A BaseException, so no `except Exception` inside icalendar, recurring-ical-events or
    dateutil can swallow it; caught only at the per-object boundary in `_expand_object`."""


@dataclass(frozen=True, slots=True)
class CalendarPage:
    events: list[RawEvent]
    truncated: bool  # a cap or the time budget stopped the expansion


class SlowObjectCache:
    """Digests of objects that hit the CPU deadline, so a hostile object costs its 2 s once per process, not on every
    call. Bounded, oldest first out, in memory only; holds SHA-256 digests of the raw calendar data, nothing else."""

    def __init__(self, size: int = SLOW_OBJECT_CACHE_SIZE) -> None:
        self._size = size
        self._digests: OrderedDict[str, None] = OrderedDict()
        self._lock = threading.Lock()

    def add(self, digest: str) -> None:
        with self._lock:
            self._digests[digest] = None
            while len(self._digests) > self._size:
                self._digests.popitem(last=False)

    def __contains__(self, digest: object) -> bool:
        with self._lock:
            return digest in self._digests

    def __len__(self) -> int:
        with self._lock:
            return len(self._digests)


SLOW_OBJECTS: Final = SlowObjectCache()
# icalendar caches every VTIMEZONE whose TZID zoneinfo does not know in one process-wide map, first writer wins and
# never evicted: one object could shift another object's (or account's) events, and unique TZIDs grow it without
# bound. Each object is therefore parsed and expanded alone, with that cache emptied before and after (spec 080
# rev. 4.5 D62, "time-zone definitions are isolated per calendar object"). Held for one object only, never across
# network I/O; the CPU deadline inside bounds how long it is held.
EXPANSION_LOCK: Final = threading.Lock()


def _isolated[T](func: Callable[[], T]) -> T:
    with EXPANSION_LOCK:
        tzp.use_zoneinfo()  # public API: a fresh, empty time-zone cache
        try:
            return func()
        finally:
            tzp.use_zoneinfo()  # every exit path: normal, library error, refusal, injected deadline


def _values(value: object) -> list[object]:
    return value if isinstance(value, list) else ([] if value is None else [value])


def _date_count(component: icalendar.cal.Component, name: str) -> int:
    return sum(len(getattr(value, "dts", [value])) for value in _values(component.get(name)))


def _prescreen(ics: bytes) -> None:
    """Cheap checks on the raw object, before icalendar parses it (spec 080 rev. 4.5 D62 A). Values are counted by
    their separators on the unfolded lines; a comma inside a parameter only makes the count larger (refuses earlier)."""
    if len(ics) > MAX_OBJECT_BYTES:
        raise ObjectSkippedError("object_too_large")
    text = _FOLD.sub(b"", ics)
    if len(_BEGIN_VEVENT.findall(text)) > MAX_COMPONENTS_PER_OBJECT:
        raise ObjectSkippedError("too_many_components")
    if len(_BEGIN_VTIMEZONE.findall(text)) > MAX_TIMEZONES_PER_OBJECT:
        raise ObjectSkippedError("too_many_components")
    counts = {b"RDATE": 0, b"EXDATE": 0}
    for line in _DATE_LINE.finditer(text):
        counts[line.group(1).upper()] += line.group(0).count(b",") + 1
    if max(counts.values()) > MAX_RECURRENCE_DATES:
        raise ObjectSkippedError("too_many_dates")


def _screen(calendar: icalendar.Calendar) -> None:
    """Refuse shapes whose expansion cost is unbounded, by inspection only, before the expansion library runs: with
    FREQ at least daily and at most one value per time-of-day part, a rule yields at most a few dozen instances in a
    widened 31-day window (spec 080 rev. 4.5 D62 B). One object holds one series: one UID (one per CalDAV resource,
    RFC 4791 §4.1) and at most one component with an RRULE (the master; overrides never carry one)."""
    components = calendar.walk("VEVENT")
    if len({str(c.get("UID", "")) for c in components}) > 1 or sum("RRULE" in c for c in components) > 1:
        raise ObjectSkippedError("rule_refused")
    for component in components:
        rules = _values(component.get("RRULE"))
        if len(rules) > 1 or "EXRULE" in component:
            raise ObjectSkippedError("rule_refused")
        for rule in rules:
            frequencies = {str(value).upper() for value in _values(cast(dict[str, object], rule).get("FREQ"))}
            if len(frequencies) != 1 or not frequencies <= ALLOWED_FREQUENCIES:
                raise ObjectSkippedError("rule_refused")
            parts = cast(dict[str, object], rule)
            if any(len(_values(parts.get(part))) > 1 for part in _TIME_OF_DAY_PARTS):
                raise ObjectSkippedError("rule_refused")
        if max(_date_count(component, "RDATE"), _date_count(component, "EXDATE")) > MAX_RECURRENCE_DATES:
            raise ObjectSkippedError("too_many_dates")
        if "DTSTART" in component:
            begin = cast(date, component.decoded("DTSTART"))
            if (begin.date() if isinstance(begin, datetime) else begin) < EARLIEST_START:
                raise ObjectSkippedError("start_out_of_range")


def _with_cpu_deadline[T](func: Callable[[], T], seconds: float, cpu_clock: Callable[[], float]) -> T:
    """Run `func` in this thread with a call-level trace hook that raises once `seconds` of thread CPU time are used.

    Why a trace hook (spec 080 rev. 4.5 D62 §1a): dateutil's rrule iterates from DTSTART and loops without yielding
    for rules that never match, so neither instance caps nor checks between objects bound one object's CPU time, and
    a worker thread cannot be killed. The hook sees every Python call of the expansion and stops it. It is
    thread-local, CPython-specific and replaces a debugger's or coverage tool's trace function while it runs (the
    previous one is restored on every path). A library layer that catches the injected BaseException also removes
    the hook (CPython drops a raising trace function), so the CPU time is read once more after the call on every path.
    The clock is read on every call event: sampling every 16 events saved only about a third of the 0.4 ms per object.
    Rejected alternative: a killable subprocess per expansion (memory in a
    256 Mi pod, start-up cost per call)."""
    deadline = cpu_clock() + seconds
    previous = sys.gettrace()

    def hook(frame: FrameType, event: str, arg: object) -> None:
        if cpu_clock() > deadline:
            raise _ExpansionTooSlow  # CPython removes a raising trace function; `finally` restores the previous one
        return None  # call events only, no line tracing

    sys.settrace(hook)
    try:
        result = func()
    except Exception:
        if cpu_clock() > deadline:  # the injected exception was swallowed and turned into another one
            raise _ExpansionTooSlow from None
        raise
    finally:
        sys.settrace(previous)  # never threading.settrace: that would reach every new thread
    if cpu_clock() > deadline:  # the injected exception was swallowed and the call still returned
        raise _ExpansionTooSlow
    return result


def _expand_object(
    ics: bytes,
    *,
    href: str,
    name: str | None,
    start: datetime,
    end: datetime,
    zone: ZoneInfo,
    floating: ZoneInfo,
    cpu_seconds: float,
    cpu_clock: Callable[[], float],
) -> tuple[list[RawEvent], bool]:
    """The object's instances sorted by start, at most MAX_INSTANCES_PER_OBJECT, and whether that cap cut them."""

    def work() -> list[RawEvent]:
        _prescreen(ics)
        calendar = icalendar.Calendar.from_ical(ics)
        _screen(calendar)
        return _instances(calendar, href=href, name=name, start=start, end=end, zone=zone, floating=floating)

    try:
        found = _isolated(lambda: _with_cpu_deadline(work, cpu_seconds, cpu_clock))  # lock outside, hook inside
    except _ExpansionTooSlow:
        raise ObjectSkippedError("expansion_too_slow") from None
    found.sort(key=lambda event: event.sort_key)
    return found[:MAX_INSTANCES_PER_OBJECT], len(found) > MAX_INSTANCES_PER_OBJECT


def expand(
    ics: bytes,
    *,
    href: str,
    name: str | None,
    start: datetime,
    end: datetime,
    zone: ZoneInfo,
    floating: ZoneInfo,
    cpu_seconds: float = OBJECT_CPU_SECONDS,
    cpu_clock: Callable[[], float] = clocks.thread_time,
) -> list[RawEvent]:
    """Every instance of one calendar object overlapping [start, end): RRULE, RDATE, EXDATE and overrides expanded,
    cancelled ones omitted; timed events in `zone`, floating times placed in `floating`, all-day events as dates.
    Raises ObjectSkippedError for refused shapes and objects that exceed the CPU deadline (spec 080 rev. 4.5 D62)."""
    found, _ = _expand_object(
        ics,
        href=href,
        name=name,
        start=start,
        end=end,
        zone=zone,
        floating=floating,
        cpu_seconds=cpu_seconds,
        cpu_clock=cpu_clock,
    )
    return found


def _recurring(ics: bytes) -> bool:
    """RRULE or RDATE present (folded lines joined); decides only the expansion order, so a property name that
    appears inside a text value merely moves that object back."""
    return _RECURRING_LINE.search(_FOLD.sub(b"", ics)) is not None


def _instances(
    calendar: icalendar.Calendar,
    *,
    href: str,
    name: str | None,
    start: datetime,
    end: datetime,
    zone: ZoneInfo,
    floating: ZoneInfo,
) -> list[RawEvent]:
    recurring_uids = {
        str(c.get("UID", "")) for c in calendar.walk("VEVENT") if {"RRULE", "RDATE", "RECURRENCE-ID"} & set(c)
    }
    # A cancelled master cancels the whole series, its overrides included (review 19 F11).
    cancelled_uids = {
        str(c.get("UID", ""))
        for c in calendar.walk("VEVENT")
        if "RECURRENCE-ID" not in c and str(c.get("STATUS", "")).upper() == "CANCELLED"
    }
    # A broken series raises, so the caller skips this object and logs calendar_object_skipped (spec 080 §10.2).
    query = recurring_ical_events.of(calendar, skip_bad_series=False)
    events: list[RawEvent] = []
    strings: dict[int, tuple[object, str]] = {}
    for component in query.between(start.astimezone(UTC) - _WIDEN, end.astimezone(UTC) + _WIDEN):
        if component.name != "VEVENT" or str(component.get("STATUS", "CONFIRMED")).upper() == "CANCELLED":
            continue
        if str(component.get("UID", "")) in cancelled_uids:
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
        organizer = cast(icalendar.vCalAddress | None, _first(component, "ORGANIZER"))
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
                title=_text(component, "SUMMARY", "title", strings),
                location=_text(component, "LOCATION", "location", strings),
                description=_text(component, "DESCRIPTION", "description", strings),
                organizer_name=(
                    _cut(organizer.params["CN"], "organizer_name")
                    if organizer is not None and "CN" in organizer.params
                    else None
                ),
                organizer_address=(
                    _cut(re.sub(r"(?i)^mailto:", "", str(organizer)), "address") if organizer is not None else None
                ),
                original_timezone=None if all_day else _zone_name(begin),
            )
        )
    return events


_NS: Final = 'xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav"'
PRINCIPAL_BODY: Final = (
    f'<?xml version="1.0" encoding="utf-8"?><D:propfind {_NS}><D:prop><D:current-user-principal/></D:prop></D:propfind>'
)
HOME_BODY: Final = (
    f'<?xml version="1.0" encoding="utf-8"?><D:propfind {_NS}><D:prop><C:calendar-home-set/></D:prop></D:propfind>'
)
LIST_BODY: Final = (
    f'<?xml version="1.0" encoding="utf-8"?><D:propfind {_NS}><D:prop><D:resourcetype/><D:displayname/>'
    "<C:supported-calendar-component-set/></D:prop></D:propfind>"
)
REPORT_BODY: Final = (
    f'<?xml version="1.0" encoding="utf-8"?><C:calendar-query {_NS}>'
    "<D:prop><D:getetag/><C:calendar-data/></D:prop>"
    '<C:filter><C:comp-filter name="VCALENDAR"><C:comp-filter name="VEVENT">'
    '<C:time-range start="{start}" end="{end}"/>'
    "</C:comp-filter></C:comp-filter></C:filter></C:calendar-query>"
)
_ICLOUD_HOST: Final = "caldav.icloud.com"
_ICLOUD_PARTITION: Final = re.compile(r"^p[0-9]{1,3}-caldav\.icloud\.com$")  # ASCII digits only
_MAX_REDIRECTS: Final = 3
_REDIRECTS: Final = frozenset({301, 302, 307, 308})
_log = logging.getLogger("mcp_hub.providers.caldav")

ClientFactory = Callable[[str, str, float], httpx2.Client]


@dataclass(frozen=True, slots=True)
class CalendarRef:
    href: str
    name: str | None


_DEFAULT_PORTS: Final = {"https": 443, "http": 80}


def _origin(url: str) -> tuple[str, str, int] | None:
    """(scheme, host, port) with the scheme's default port filled in; None when the URL has no usable host or port."""
    parts = urlsplit(url)
    try:
        port = parts.port
    except ValueError:  # e.g. "+443" or "443.evil.example": refused, never guessed
        return None
    if not parts.hostname or parts.scheme not in _DEFAULT_PORTS:
        return None
    return parts.scheme, parts.hostname, port if port is not None else _DEFAULT_PORTS[parts.scheme]


def allowed_host(configured: str, target: str) -> bool:
    """Credentials go only to the configured scheme/host/port, or - for iCloud - to its pNN-caldav partition hosts
    on port 443 (spec 080 rev. 4.4 §6.1, D58). Checked before every request, including redirects and discovered
    hrefs. An explicit default port equals no port (iCloud returns absolute hrefs with ":443"); any other port must
    match the configured one. The registry only accepts https URLs, so targets are https in production."""
    a, b = _origin(configured), _origin(target)
    if a is None or b is None or a[0] != b[0]:
        return False
    if a == b:
        return True
    return a[:2] == ("https", _ICLOUD_HOST) and b[2] == 443 and bool(_ICLOUD_PARTITION.fullmatch(b[1]))


class _OverBudgetError(Exception):
    """A REPORT body would exceed the remaining byte budget of the call (not the 5 MiB per-response cap)."""


def _read_within(chunks: Iterable[bytes], budget: int) -> bytes:
    """The body if it fits `budget`; beyond it the bytes are dropped and the rest drained without storing, up to
    the 5 MiB per-response cap, only to tell `too_large` (one response too big) from the call's byte budget."""
    buffer = bytearray()
    total = 0
    for chunk in chunks:
        total += len(chunk)
        if total > MAX_HTTP_RESPONSE_BYTES:
            raise ProviderError("too_large", "ResponseTooLarge")
        if total <= budget:
            buffer += chunk
        elif buffer:
            buffer = bytearray()  # never parse a partial multistatus
    if total > budget:
        raise _OverBudgetError
    return bytes(buffer)


def default_client(username: str, password: str, timeout: float) -> httpx2.Client:
    return httpx2.Client(auth=(username, password), timeout=timeout, trust_env=False, follow_redirects=False)


def _provider_error(exc: Exception) -> ProviderError:
    if isinstance(exc, ProviderError):
        return exc
    cause = type(exc).__name__
    if isinstance(exc, TimeoutError | httpx2.TimeoutException):
        return ProviderError("upstream_timeout", cause)
    if isinstance(exc, OSError | httpx2.TransportError):
        return ProviderError("unreachable", cause)
    return ProviderError("upstream_error", cause)


def _stamp(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def _href(element: ET.Element | None, base: str) -> str | None:
    node = element.find(f"{_DAV}href") if element is not None else None
    return urljoin(base, node.text.strip()) if node is not None and node.text else None


class CalDavCalendarSource:
    def __init__(
        self,
        account_id: str,
        *,
        url: str,
        username: str,
        password: str,
        include: Literal["all"] | list[str],
        timeout: float = PROVIDER_TIMEOUT_SECONDS,
        client_factory: ClientFactory = default_client,
        clock: Callable[[], float] = clocks.monotonic,
        cpu_clock: Callable[[], float] = clocks.thread_time,
        slow_objects: SlowObjectCache = SLOW_OBJECTS,
    ) -> None:
        self._account, self._url, self._include = account_id, url, include
        self._username, self._password, self._timeout = username, password, timeout
        self._factory = client_factory
        self._clock, self._cpu_clock, self._slow_objects = clock, cpu_clock, slow_objects
        self._deadline = float("inf")  # set per call; no new request after it (review 19 F12)

    def _client(self) -> httpx2.Client:
        self._deadline = self._clock() + self._timeout
        return self._factory(self._username, self._password, self._timeout)

    def _request(
        self, client: httpx2.Client, method: str, url: str, body: str, depth: str, budget: int | None = None
    ) -> tuple[str, bytes]:
        """One DAV request; follows at most 3 redirects by hand, each target checked with allowed_host first."""
        for _ in range(_MAX_REDIRECTS + 1):
            if self._clock() > self._deadline:  # the caller has given up: stop sending
                raise ProviderError("upstream_timeout", "CallDeadline")
            if not allowed_host(self._url, url):
                raise ProviderError("upstream_error", "ForeignHost")
            # Uncompressed bodies only: the 5 MiB cap must apply before any decoding (review 19 F3).
            headers = {"Depth": depth, "Content-Type": "application/xml; charset=utf-8", "Accept-Encoding": "identity"}
            with client.stream(method, url, content=body.encode(), headers=headers) as response:
                if response.status_code in _REDIRECTS and "location" in response.headers:
                    url = urljoin(url, response.headers["location"])
                    continue
                if response.status_code in (401, 403):
                    raise ProviderError("auth_expired", "HttpUnauthorized")
                if response.status_code != 207:
                    raise ProviderError("upstream_error", "UnexpectedStatus")
                if response.headers.get("content-encoding", "identity").strip().lower() not in ("", "identity"):
                    raise ProviderError("upstream_error", "ContentEncoding")
                if budget is not None:
                    return url, _read_within(response.iter_bytes(), budget)
                return url, read_capped(response.iter_bytes())
        raise ProviderError("upstream_error", "TooManyRedirects")

    def _principal(self, client: httpx2.Client) -> str:
        base, raw = self._request(client, "PROPFIND", self._url, PRINCIPAL_BODY, "0")
        principal = _href(parse_xml(raw, account=self._account).find(f".//{_DAV}current-user-principal"), base)
        if principal is None:
            raise ProviderError("upstream_error", "NoPrincipal")
        return principal

    def _calendars(self, client: httpx2.Client) -> list[CalendarRef]:
        principal = self._principal(client)
        base, raw = self._request(client, "PROPFIND", principal, HOME_BODY, "0")
        home = _href(parse_xml(raw, account=self._account).find(f".//{_CALDAV}calendar-home-set"), base)
        if home is None:
            raise ProviderError("upstream_error", "NoCalendarHome")
        base, raw = self._request(client, "PROPFIND", home, LIST_BODY, "1")
        found = []
        for response in parse_xml(raw, account=self._account).iter(f"{_DAV}response"):
            href = _href(response, base)
            if href is None or response.find(f".//{_DAV}resourcetype/{_CALDAV}calendar") is None:
                continue
            components = response.find(f".//{_CALDAV}supported-calendar-component-set")
            if components is not None and not any(
                comp.get("name", "").upper() == "VEVENT" for comp in components.iter(f"{_CALDAV}comp")
            ):
                continue  # task-only list: no REPORT spent on it; kept when the property is absent
            name = response.findtext(f".//{_DAV}displayname")
            found.append(CalendarRef(href=href, name=_cut(name.strip(), "calendar_name") if name else None))
        return found

    def check(self) -> None:
        """Status check (spec 080 §7.4): one PROPFIND for current-user-principal."""
        try:
            with self._client() as client:
                self._principal(client)
        except Exception as exc:
            raise _provider_error(exc) from None

    def calendars(self) -> list[CalendarRef]:
        try:
            with self._client() as client:
                return self._calendars(client)
        except Exception as exc:
            raise _provider_error(exc) from None

    def events(self, start: datetime, end: datetime, zone: ZoneInfo, floating: ZoneInfo) -> CalendarPage:
        try:
            with self._client() as client:
                selected = [c for c in self._calendars(client) if self._include == "all" or c.name in self._include]
                selected.sort(key=lambda calendar: urlsplit(calendar.href).path)  # the byte cap cuts from the end
                body = REPORT_BODY.format(start=_stamp(start - _WIDEN), end=_stamp(end + _WIDEN))
                objects: list[tuple[CalendarRef, bytes]] = []
                remaining = MAX_REPORT_BYTES_PER_CALL
                stopped: str | None = None
                for calendar in selected:  # sequential: the byte accounting needs no locking
                    try:
                        _, raw = self._request(client, "REPORT", calendar.href, body, "1", budget=remaining)
                    except _OverBudgetError:
                        stopped = "byte_cap"  # this calendar is dropped whole; no further calendar is requested
                        break
                    remaining -= len(raw)
                    objects += [(calendar, ics) for ics in parse_multistatus(raw, account=self._account)]
            return self._expand_all(objects, start, end, zone, floating, stopped=stopped)
        except Exception as exc:
            raise _provider_error(exc) from None

    def _expand_all(
        self,
        objects: list[tuple[CalendarRef, bytes]],
        start: datetime,
        end: datetime,
        zone: ZoneInfo,
        floating: ZoneInfo,
        stopped: str | None = None,
    ) -> CalendarPage:
        """Single events first, recurring objects after them, so a hostile series cannot crowd out single events;
        stops at MAX_INSTANCES_PER_CALL or EXPANSION_BUDGET_SECONDS (spec 080 rev. 4.5 D62)."""
        objects = sorted(objects, key=lambda pair: _recurring(pair[1]))  # stable: response order kept otherwise
        events: list[RawEvent] = []
        started = self._clock()
        for calendar, ics in objects:
            if self._clock() - started > EXPANSION_BUDGET_SECONDS:
                stopped = stopped or "time_budget"
                break
            digest = hashlib.sha256(ics).hexdigest()
            if digest in self._slow_objects:
                self._skipped(outcome="expansion_too_slow")
                continue
            try:  # one broken or refused calendar object never fails the account (spec 080 §10.2)
                found, cut = _expand_object(
                    ics,
                    href=calendar.href,
                    name=calendar.name,
                    start=start,
                    end=end,
                    zone=zone,
                    floating=floating,
                    cpu_seconds=OBJECT_CPU_SECONDS,
                    cpu_clock=self._cpu_clock,
                )
            except ObjectSkippedError as exc:
                if exc.reason == "expansion_too_slow":
                    self._slow_objects.add(digest)
                self._skipped(outcome=exc.reason)
                continue
            except Exception as exc:
                self._skipped(exception=type(exc).__name__)
                continue
            if cut:
                stopped = stopped or "instance_cap"
            room = MAX_INSTANCES_PER_CALL - len(events)
            if len(found) > room:
                events += found[:room]
                stopped = stopped or "instance_cap"
                break
            events += found
        if stopped is not None:
            log_event(
                _log,
                logging.INFO,
                "expansion_stopped",
                account=self._account,
                capability="calendar",
                outcome=stopped,
                result_count=len(events),
            )
        return CalendarPage(events=events, truncated=stopped is not None)

    def _skipped(self, **fields: str) -> None:
        log_event(
            _log, logging.WARNING, "calendar_object_skipped", account=self._account, capability="calendar", **fields
        )
