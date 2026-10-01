"""Read-only CalDAV adapter (spec 080 rev. 4.4 §5.2 get_events, §5.4, §6.1, D58).

Discovery (principal -> calendar home -> calendars) and one calendar-query REPORT per calendar, all through httpx2
with the credential-destination rule, read as streams capped at 5 MiB; expansion on the client with
recurring-ical-events. Blocking; the tool layer runs it through providers.base.run_blocking. Returns unsanitised text.
"""

import logging
import re
import xml.etree.ElementTree as ET
import xml.parsers.expat
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Final, Literal, NoReturn, cast
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo

import httpx2
import icalendar
import recurring_ical_events

from mcp_hub.logging import log_event
from mcp_hub.providers.base import PROVIDER_TIMEOUT_SECONDS, ProviderError, read_capped
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
    # A broken series raises, so the caller skips this object and logs calendar_object_skipped (spec 080 §10.2).
    query = recurring_ical_events.of(calendar, skip_bad_series=False)
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
_ICLOUD_PARTITION: Final = re.compile(r"^p\d{1,3}-caldav\.icloud\.com$")
_MAX_REDIRECTS: Final = 3
_REDIRECTS: Final = frozenset({301, 302, 307, 308})
_log = logging.getLogger("mcp_hub.providers.caldav")

ClientFactory = Callable[[str, str, float], httpx2.Client]


@dataclass(frozen=True, slots=True)
class CalendarRef:
    href: str
    name: str | None


def allowed_host(configured: str, target: str) -> bool:
    """Credentials go only to the configured scheme/host/port, or - for iCloud - to its pNN-caldav partition hosts
    on port 443 (spec 080 rev. 4.4 §6.1, D58). Checked before every request, including redirects and discovered
    hrefs."""
    a, b = urlsplit(configured), urlsplit(target)
    if a.scheme != b.scheme or not b.hostname:
        return False
    if (a.hostname, a.port) == (b.hostname, b.port):
        return True
    return (
        a.hostname == _ICLOUD_HOST
        and a.scheme == "https"
        and b.port in (None, 443)
        and bool(_ICLOUD_PARTITION.fullmatch(b.hostname))
    )


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
    ) -> None:
        self._account, self._url, self._include = account_id, url, include
        self._username, self._password, self._timeout = username, password, timeout
        self._factory = client_factory

    def _request(self, client: httpx2.Client, method: str, url: str, body: str, depth: str) -> tuple[str, bytes]:
        """One DAV request; follows at most 3 redirects by hand, each target checked with allowed_host first."""
        for _ in range(_MAX_REDIRECTS + 1):
            if not allowed_host(self._url, url):
                raise ProviderError("upstream_error", "ForeignHost")
            headers = {"Depth": depth, "Content-Type": "application/xml; charset=utf-8"}
            with client.stream(method, url, content=body.encode(), headers=headers) as response:
                if response.status_code in _REDIRECTS and "location" in response.headers:
                    url = urljoin(url, response.headers["location"])
                    continue
                if response.status_code in (401, 403):
                    raise ProviderError("auth_expired", "HttpUnauthorized")
                if response.status_code != 207:
                    raise ProviderError("upstream_error", "UnexpectedStatus")
                return url, read_capped(response.iter_bytes())
        raise ProviderError("upstream_error", "TooManyRedirects")

    def _principal(self, client: httpx2.Client) -> str:
        base, raw = self._request(client, "PROPFIND", self._url, PRINCIPAL_BODY, "0")
        principal = _href(parse_xml(raw).find(f".//{_DAV}current-user-principal"), base)
        if principal is None:
            raise ProviderError("upstream_error", "NoPrincipal")
        return principal

    def _calendars(self, client: httpx2.Client) -> list[CalendarRef]:
        principal = self._principal(client)
        base, raw = self._request(client, "PROPFIND", principal, HOME_BODY, "0")
        home = _href(parse_xml(raw).find(f".//{_CALDAV}calendar-home-set"), base)
        if home is None:
            raise ProviderError("upstream_error", "NoCalendarHome")
        base, raw = self._request(client, "PROPFIND", home, LIST_BODY, "1")
        found = []
        for response in parse_xml(raw).iter(f"{_DAV}response"):
            href = _href(response, base)
            if href is None or response.find(f".//{_DAV}resourcetype/{_CALDAV}calendar") is None:
                continue
            components = response.find(f".//{_CALDAV}supported-calendar-component-set")
            if components is not None and not any(
                comp.get("name", "").upper() == "VEVENT" for comp in components.iter(f"{_CALDAV}comp")
            ):
                continue  # task-only list: no REPORT spent on it; kept when the property is absent
            name = response.findtext(f".//{_DAV}displayname")
            found.append(CalendarRef(href=href, name=name.strip() if name else None))
        return found

    def check(self) -> None:
        """Status check (spec 080 §7.4): one PROPFIND for current-user-principal."""
        try:
            with self._factory(self._username, self._password, self._timeout) as client:
                self._principal(client)
        except Exception as exc:
            raise _provider_error(exc) from None

    def calendars(self) -> list[CalendarRef]:
        try:
            with self._factory(self._username, self._password, self._timeout) as client:
                return self._calendars(client)
        except Exception as exc:
            raise _provider_error(exc) from None

    def events(self, start: datetime, end: datetime, zone: ZoneInfo, floating: ZoneInfo) -> list[RawEvent]:
        try:
            with self._factory(self._username, self._password, self._timeout) as client:
                selected = [c for c in self._calendars(client) if self._include == "all" or c.name in self._include]
                body = REPORT_BODY.format(start=_stamp(start - _WIDEN), end=_stamp(end + _WIDEN))
                events: list[RawEvent] = []
                for calendar in selected:
                    _, raw = self._request(client, "REPORT", calendar.href, body, "1")
                    for ics in parse_multistatus(raw):
                        try:  # one broken calendar object never fails the account (spec 080 §10.2)
                            events += expand(
                                ics,
                                href=calendar.href,
                                name=calendar.name,
                                start=start,
                                end=end,
                                zone=zone,
                                floating=floating,
                            )
                        except Exception as exc:
                            log_event(
                                _log,
                                logging.WARNING,
                                "calendar_object_skipped",
                                account=self._account,
                                capability="calendar",
                                exception=type(exc).__name__,
                            )
                return events
        except Exception as exc:
            raise _provider_error(exc) from None
