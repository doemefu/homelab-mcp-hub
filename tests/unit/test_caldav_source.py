import gzip
import logging
import re
from datetime import datetime
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import httpx2
import pytest
from pytest_httpserver import HTTPServer

from mcp_hub.providers.base import MAX_HTTP_RESPONSE_BYTES, ProviderError
from mcp_hub.providers.caldav import CalDavCalendarSource, allowed_host
from tests.support import ics
from tests.support.dav_transport import (
    RecordingTransport,
    collection,
    dav,
    home_set,
    multistatus,
    principal,
    report,
)
from tests.support.logfields import allowed_fields

ZURICH = ZoneInfo("Europe/Zurich")
START = datetime(2026, 10, 17, tzinfo=ZURICH)
END = datetime(2026, 11, 2, tzinfo=ZURICH)
XML = "application/xml; charset=utf-8"
PASSWORD = "unit-test-password"  # throwaway value for a local test server


def serve_discovery(httpserver: HTTPServer, listing: bytes | None = None) -> None:
    httpserver.expect_request("/", method="PROPFIND", headers={"Depth": "0"}).respond_with_data(
        principal("/p/"), status=207, content_type=XML
    )
    httpserver.expect_request("/p/", method="PROPFIND", headers={"Depth": "0"}).respond_with_data(
        home_set("/h/"), status=207, content_type=XML
    )
    if listing is None:
        listing = multistatus(
            collection("/h/", None, calendar=False),
            collection("/h/home/", "Home", components=("VEVENT", "VTODO")),
            collection("/h/work/", "Work"),
            collection("/h/inbox/", "Inbox", calendar=False),
        )
    httpserver.expect_request("/h/", method="PROPFIND", headers={"Depth": "1"}).respond_with_data(
        listing, status=207, content_type=XML
    )


def source(httpserver: HTTPServer, include: list[str] | str = "all", **kwargs: object) -> CalDavCalendarSource:
    return CalDavCalendarSource(
        "icloud",
        url=httpserver.url_for("/"),
        username="hub-cal",
        password=PASSWORD,
        include=include,  # type: ignore[arg-type]
        **kwargs,  # type: ignore[arg-type]
    )


def requests(httpserver: HTTPServer) -> list[tuple[str, str]]:
    return [(request.method, request.path) for request, _ in httpserver.log]


def code_of(call: object) -> ProviderError:
    with pytest.raises(ProviderError) as caught:
        call()  # type: ignore[operator]
    return caught.value


def test_discovery_follows_principal_home_and_lists_calendars(httpserver: HTTPServer) -> None:
    serve_discovery(httpserver)
    found = source(httpserver).calendars()
    assert [(c.name, c.href) for c in found] == [
        ("Home", httpserver.url_for("/h/home/")),
        ("Work", httpserver.url_for("/h/work/")),
    ]
    assert all(request.headers.get("Authorization") for request, _ in httpserver.log)


def test_events_reports_each_selected_calendar_and_expands(httpserver: HTTPServer) -> None:
    serve_discovery(httpserver)
    httpserver.expect_request("/h/home/", method="REPORT").respond_with_data(
        report(ics.load("dst-weekly.ics")), status=207, content_type=XML
    )
    events = source(httpserver, ["Home"]).events(START, END, ZURICH, ZURICH).events
    assert [e.calendar_name for e in events] == ["Home", "Home", "Home"]
    [(sent, _)] = [(r, s) for r, s in httpserver.log if r.method == "REPORT"]
    assert sent.path == "/h/home/"
    assert sent.headers["Depth"] == "1"
    assert sent.headers["Content-Type"] == XML
    assert b'<C:time-range start="20261015T220000Z" end="20261102T230000Z"/>' in sent.get_data()


def test_report_above_limit_is_too_large(httpserver: HTTPServer) -> None:
    serve_discovery(httpserver)
    httpserver.expect_request("/h/home/", method="REPORT").respond_with_data(
        b"x" * (MAX_HTTP_RESPONSE_BYTES + 1), status=207, content_type=XML
    )
    assert code_of(lambda: source(httpserver, ["Home"]).events(START, END, ZURICH, ZURICH).events).code == "too_large"


def test_discovery_response_above_limit_is_too_large(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/", method="PROPFIND").respond_with_data(
        b"x" * (MAX_HTTP_RESPONSE_BYTES + 1), status=207, content_type=XML
    )
    assert code_of(source(httpserver).calendars).code == "too_large"


@pytest.mark.parametrize("status", [401, 403])
def test_http_401_is_auth_expired(httpserver: HTTPServer, status: int) -> None:
    httpserver.expect_request("/", method="PROPFIND").respond_with_data(b"", status=status)
    assert code_of(source(httpserver).check).code == "auth_expired"
    httpserver.clear()
    serve_discovery(httpserver)
    httpserver.expect_request("/h/home/", method="REPORT").respond_with_data(b"", status=status)
    assert code_of(lambda: source(httpserver).events(START, END, ZURICH, ZURICH).events).code == "auth_expired"


def test_http_500_is_upstream_error(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/", method="PROPFIND").respond_with_data(b"", status=500)
    assert code_of(source(httpserver).check).code == "upstream_error"


def test_same_site_redirect_is_followed_with_a_limit(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/", method="PROPFIND").respond_with_data(b"", status=301, headers={"Location": "/dav/"})
    httpserver.expect_request("/dav/", method="PROPFIND").respond_with_data(
        principal("/p/"), status=207, content_type=XML
    )
    source(httpserver).check()
    assert requests(httpserver) == [("PROPFIND", "/"), ("PROPFIND", "/dav/")]
    httpserver.clear()
    for n in range(4):
        httpserver.expect_request(f"/r{n}/", method="PROPFIND").respond_with_data(
            b"", status=307, headers={"Location": f"/r{n + 1}/"}
        )
    chained = CalDavCalendarSource(
        "icloud", url=httpserver.url_for("/r0/"), username="hub-cal", password=PASSWORD, include="all"
    )
    error = code_of(chained.check)
    assert (error.code, error.cause) == ("upstream_error", "TooManyRedirects")
    assert len(httpserver.log) == 4  # the original request plus three redirects, never a fifth


@pytest.mark.parametrize("position", ["redirect", "principal", "home", "calendar"])
def test_foreign_hosts_are_refused_before_any_request(httpserver: HTTPServer, position: str) -> None:
    # Same server, other host name: a request would reach it, so the log proves it was never sent.
    foreign = httpserver.url_for("/foreign/").replace("localhost", "127.0.0.1")
    assert "127.0.0.1" in foreign
    httpserver.expect_request("/foreign/").respond_with_data(principal("/p/"), status=207, content_type=XML)
    if position == "redirect":
        httpserver.expect_request("/", method="PROPFIND").respond_with_data(
            b"", status=302, headers={"Location": foreign}
        )
    elif position == "principal":
        httpserver.expect_request("/", method="PROPFIND").respond_with_data(
            principal(foreign), status=207, content_type=XML
        )
    else:
        httpserver.expect_request("/", method="PROPFIND").respond_with_data(
            principal("/p/"), status=207, content_type=XML
        )
        if position == "home":
            httpserver.expect_request("/p/", method="PROPFIND").respond_with_data(
                home_set(foreign), status=207, content_type=XML
            )
        else:
            serve_discovery(httpserver, multistatus(collection(foreign, "Home")))
    error = code_of(lambda: source(httpserver).events(START, END, ZURICH, ZURICH).events)
    assert (error.code, error.cause) == ("upstream_error", "ForeignHost")
    assert ("PROPFIND", "/foreign/") not in requests(httpserver)
    assert ("REPORT", "/foreign/") not in requests(httpserver)


def test_icloud_partition_hosts_are_allowed() -> None:
    icloud = "https://caldav.icloud.com/"
    assert allowed_host(icloud, "https://p42-caldav.icloud.com/123/calendars/")
    assert allowed_host(icloud, "https://p42-caldav.icloud.com:443/123/calendars/")
    assert allowed_host(icloud, "https://caldav.icloud.com/123/principal/")
    for target in (
        "https://p42-caldav.icloud.com.evil.example/",
        "https://caldav.icloud.com.evil.example/",
        "http://caldav.icloud.com/",
        "https://other.icloud.com/",
        "https://p42-caldav.icloud.com:8443/",
        "https://p1234-caldav.icloud.com/",
    ):
        assert not allowed_host(icloud, target), target
    other = "https://dav.example.test:8443/"
    assert allowed_host(other, "https://dav.example.test:8443/cal/")
    for target in ("https://dav.example.test/", "http://dav.example.test:8443/", "https://p42-caldav.icloud.com/"):
        assert not allowed_host(other, target), target


@pytest.mark.parametrize(
    ("raised", "code"),
    [(httpx2.ConnectError("x"), "unreachable"), (httpx2.ReadTimeout("x"), "upstream_timeout")],
    ids=["connect", "timeout"],
)
def test_error_mapping(raised: Exception, code: str) -> None:
    def fail(request: httpx2.Request) -> httpx2.Response:
        raise raised

    def factory(username: str, password: str, timeout: float) -> httpx2.Client:
        return httpx2.Client(auth=(username, password), transport=httpx2.MockTransport(fail))

    caldav = CalDavCalendarSource(
        "icloud",
        url="https://dav.example.test/",
        username="u",
        password=PASSWORD,
        include="all",
        client_factory=factory,
    )
    assert code_of(caldav.check).code == code


def test_parse_refusal_is_upstream_error(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/", method="PROPFIND").respond_with_data(
        b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><x>&a;</x>', status=207, content_type=XML
    )
    assert code_of(source(httpserver).check).code == "upstream_error"


def test_check_is_one_principal_lookup(httpserver: HTTPServer) -> None:
    serve_discovery(httpserver)
    source(httpserver).check()
    assert requests(httpserver) == [("PROPFIND", "/")]
    assert b"current-user-principal" in httpserver.log[0][0].get_data()


def test_task_only_collections_are_skipped(httpserver: HTTPServer) -> None:
    serve_discovery(
        httpserver,
        multistatus(collection("/h/tasks/", "Tasks", components=("VTODO",)), collection("/h/home/", "Home")),
    )
    httpserver.expect_request("/h/home/", method="REPORT").respond_with_data(report(), status=207, content_type=XML)
    assert source(httpserver).events(START, END, ZURICH, ZURICH).events == []
    assert [path for method, path in requests(httpserver) if method == "REPORT"] == ["/h/home/"]


def test_icloud_shaped_discovery_crosses_to_the_partition_host() -> None:
    calendars = "https://p42-caldav.icloud.com:443/123/calendars/"

    def run(home: str) -> tuple[RecordingTransport, list[object] | ProviderError]:
        recorder = RecordingTransport(
            {
                ("PROPFIND", "https://caldav.icloud.com:443/"): dav(
                    principal("https://caldav.icloud.com:443/123/principal/")
                ),
                ("PROPFIND", "https://caldav.icloud.com:443/123/principal/"): dav(home_set(home)),
                ("PROPFIND", "https://p42-caldav.icloud.com:443/123/calendars/"): dav(
                    multistatus(collection("/123/calendars/home/", "Home", components=("VEVENT",)))
                ),
                ("REPORT", "https://p42-caldav.icloud.com:443/123/calendars/home/"): dav(
                    report(ics.load("dst-weekly.ics"))
                ),
            }
        )

        def factory(username: str, password: str, timeout: float) -> httpx2.Client:
            return httpx2.Client(auth=(username, password), transport=recorder.transport(), trust_env=False)

        caldav = CalDavCalendarSource(
            "icloud",
            url="https://caldav.icloud.com/",
            username="u",
            password=PASSWORD,
            include="all",
            client_factory=factory,
        )
        try:
            return recorder, list(caldav.events(START, END, ZURICH, ZURICH).events)
        except ProviderError as exc:
            return recorder, exc

    recorder, events = run(calendars)
    assert isinstance(events, list)
    assert len(events) == 3
    assert {(host, port) for host, port, *_ in recorder.seen} == {
        ("caldav.icloud.com", 443),
        ("p42-caldav.icloud.com", 443),
    }
    assert all(has_auth for *_, has_auth in recorder.seen)
    recorder, failed = run("https://p42-caldav.icloud.com.evil.example/123/calendars/")
    assert isinstance(failed, ProviderError)
    assert (failed.code, failed.cause) == ("upstream_error", "ForeignHost")
    assert all(host != "p42-caldav.icloud.com.evil.example" for host, *_ in recorder.seen)


def test_broken_calendar_objects_are_skipped_individually(
    httpserver: HTTPServer, caplog: pytest.LogCaptureFixture
) -> None:
    valid = ics.load("dst-weekly.ics")
    broken_start = valid.replace(
        b"DTSTART;TZID=Europe/Zurich:20261018T100000", b"DTSTART;TZID=Europe/Zurich:not-a-date"
    )
    broken_rule = valid.replace(b"RRULE:FREQ=WEEKLY;COUNT=3", b"RRULE:FREQ=SOMETIMES;COUNT=x")
    serve_discovery(httpserver)
    httpserver.expect_request("/h/home/", method="REPORT").respond_with_data(
        report(broken_start, broken_rule, valid), status=207, content_type=XML
    )
    with caplog.at_level(logging.WARNING, logger="mcp_hub"):
        events = source(httpserver, ["Home"]).events(START, END, ZURICH, ZURICH).events
    assert len(events) == 3
    skipped = [r for r in caplog.records if r.getMessage() == "calendar_object_skipped"]
    assert len(skipped) == 2
    for record in skipped:
        fields = record.fields  # type: ignore[attr-defined]
        assert set(fields) == {"account", "capability", "exception"}
        assert (fields["account"], fields["capability"]) == ("icloud", "calendar")
        assert set(fields) <= allowed_fields("calendar_object_skipped")


def raw_report(*objects: bytes, prolog: bytes = b'<?xml version="1.0" encoding="utf-8"?>') -> bytes:
    """A multistatus built from raw bytes (report() would need valid UTF-8)."""
    parts = b"".join(
        b"<D:response><D:href>/o%d.ics</D:href><D:propstat><D:prop><C:calendar-data>%s</C:calendar-data></D:prop>"
        b"<D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response>" % (n, o)
        for n, o in enumerate(objects)
    )
    return (
        prolog + b'<D:multistatus xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">' + parts + b"</D:multistatus>"
    )


def invalid_summary() -> bytes:
    return ics.load("allday.ics").replace(b"SUMMARY:Weekend away", b"SUMMARY:M\xfcller \xff away")


def test_invalid_utf8_in_a_multistatus_is_replaced_once(
    httpserver: HTTPServer, caplog: pytest.LogCaptureFixture
) -> None:
    serve_discovery(httpserver)
    httpserver.expect_request("/h/home/", method="REPORT").respond_with_data(
        raw_report(invalid_summary(), ics.load("dst-weekly.ics")), status=207, content_type=XML
    )
    with caplog.at_level(logging.INFO, logger="mcp_hub"):
        events = source(httpserver, ["Home"]).events(START, END, ZURICH, ZURICH).events
    titles = sorted(e.title or "" for e in events)
    assert titles == ["M\ufffdller \ufffd away", "Weekly DST", "Weekly DST", "Weekly DST"]
    repaired = [r for r in caplog.records if r.getMessage() == "xml_encoding_repaired"]
    assert len(repaired) == 1
    assert repaired[0].fields == {"account": "icloud", "capability": "calendar"}  # type: ignore[attr-defined]


_PRINCIPAL = (
    b'<D:multistatus xmlns:D="DAV:"><D:response><D:href>/</D:href><D:propstat><D:prop>'
    b"<D:displayname>\xff</D:displayname><D:current-user-principal><D:href>%s</D:href></D:current-user-principal>"
    b"</D:prop><D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response></D:multistatus>"
)


def test_the_utf8_retry_parses_a_principal_with_invalid_bytes(httpserver: HTTPServer) -> None:
    body = b'<?xml version="1.0" encoding="utf-8"?>' + _PRINCIPAL % b"/p/"
    httpserver.expect_request("/", method="PROPFIND").respond_with_data(body, status=207, content_type=XML)
    source(httpserver).check()  # the retry succeeds: same document shape as the refusal cases below


@pytest.mark.parametrize(
    "body",
    [
        # Each would yield a valid principal if the retry accepted a document type or entity declaration.
        b'<?xml version="1.0" encoding="utf-8"?><!DOCTYPE x [<!ENTITY a "/p/">]>' + _PRINCIPAL % b"&a;",
        b'<?xml version="1.0"?><!DOCTYPE x SYSTEM "file:///etc/passwd">' + _PRINCIPAL % b"/p/",
        b'<?xml version="1.0" encoding="utf-8"?><!DOCTYPE x>' + _PRINCIPAL % b"/p/",
        # Still malformed after the replacement; non-UTF-8 declarations get no retry.
        b'<?xml version="1.0" encoding="utf-8"?>' + (_PRINCIPAL % b"/p/").replace(b"</D:prop>", b"<D:prop>"),
        b'<?xml version="1.0" encoding="US-ASCII"?>' + _PRINCIPAL % b"/p/",
    ],
    ids=["entity", "system-dtd", "doctype", "still-malformed", "declared-ascii"],
)
def test_the_utf8_retry_is_no_bypass(httpserver: HTTPServer, body: bytes) -> None:
    httpserver.expect_request("/", method="PROPFIND").respond_with_data(body, status=207, content_type=XML)
    error = code_of(source(httpserver).check)
    assert (error.code, error.cause) == ("upstream_error", "XmlRefused")


def test_default_port_is_normalised_before_the_host_comparison() -> None:
    # iCloud returns absolute hrefs with an explicit :443 (review 19 F6).
    assert allowed_host("https://caldav.icloud.com/", "https://caldav.icloud.com:443/123/principal/")
    assert allowed_host("https://caldav.icloud.com:443/", "https://caldav.icloud.com/123/principal/")
    assert allowed_host("https://dav.example.test/", "https://dav.example.test:443/cal/")
    for target in (
        "https://caldav.icloud.com:8443/",
        "https://p42-caldav.icloud.com:444/",
        "http://caldav.icloud.com:443/",
        "https://caldav.icloud.com:+443/",
    ):
        assert not allowed_host("https://caldav.icloud.com/", target), target
    assert not allowed_host("https://dav.example.test/", "https://dav.example.test:8443/cal/")


HOSTILE_HREFS = [
    "//evil.example.test/x",
    "https://evil.example.test\\@caldav.icloud.com/",
    "https://evil.example.test#@caldav.icloud.com/",
    "https://evil.example.test?@caldav.icloud.com/",
    "https://caldav.icloud.com:443@evil.example.test/",
    "https://caldav.icloud.com%2F@evil.example.test/",
    "https://caldav.icloud.com%00@evil.example.test/",
    "https://a@b@evil.example.test/",
    "https://caldav.icloud.com@evil.example.test/",
    "https://evil.example.test\t.caldav.icloud.com/",
    "https://evil.example.test\n.caldav.icloud.com/",
    "https://caldav.icloud.com./",
    "https://CALDAV.ICLOUD.COM/",
    "https://p\u0661-caldav.icloud.com/",
    "https://p\uff14\uff12-caldav.icloud.com/",
    "https://169.254.169.254/",
    "https://[::1]/",
    "https://127.0.0.1/",
    "https://10.0.0.1/",
    "https://caldav.icloud.com:0443/",
    "https://caldav.icloud.com:+443/",
    "https://p42-caldav.icloud.com.evil.example.test/",
    "https://evil.example.test/p42-caldav.icloud.com/",
    "https://p42-caldav.icloud.com:443.evil.example.test/",
    "http://p42-caldav.icloud.com/",
    "https://xp42-caldav.icloud.com/",
    "https://p42-caldav-icloud.com/",
    "https://p4242-caldav.icloud.com/",
]


@pytest.mark.parametrize("href", HOSTILE_HREFS)
def test_an_accepted_href_is_the_host_httpx2_will_contact(href: str) -> None:
    # Differential check: whenever allowed_host accepts a URL, httpx2's own parser must see an allowed host on 443.
    target = urljoin("https://caldav.icloud.com/123/principal/", href)
    if not allowed_host("https://caldav.icloud.com/", target):
        return
    try:
        url = httpx2.URL(target)
    except Exception:
        return  # httpx2 refuses the URL: nothing is sent
    assert url.scheme == "https"
    assert url.port in (None, 443)
    assert url.host == "caldav.icloud.com" or re.fullmatch(r"p[0-9]{1,3}-caldav\.icloud\.com", url.host)


def test_partition_host_digits_are_ascii_only() -> None:
    for host in ("p\u0661", "p\uff14\uff12", "p\u0664\u0662"):
        assert not allowed_host("https://caldav.icloud.com/", f"https://{host}-caldav.icloud.com/"), host


def test_every_request_asks_for_an_uncompressed_body(httpserver: HTTPServer) -> None:
    serve_discovery(httpserver)
    httpserver.expect_request("/h/home/", method="REPORT").respond_with_data(report(), status=207, content_type=XML)
    source(httpserver, ["Home"]).events(START, END, ZURICH, ZURICH)
    assert len(httpserver.log) == 4  # three discovery PROPFINDs, one REPORT
    assert {request.headers.get("Accept-Encoding") for request, _ in httpserver.log} == {"identity"}


@pytest.mark.parametrize("encoding", ["gzip", "deflate", "br", "gzip, identity"])
def test_a_compressed_response_is_refused(httpserver: HTTPServer, encoding: str) -> None:
    # Review 19 F3: a 200 KiB gzip bomb decompressed one 64 KiB chunk at a time to ~220 MB before the cap applied.
    body = gzip.compress(principal("/p/")) if encoding.startswith("gzip") else principal("/p/")
    httpserver.expect_request("/", method="PROPFIND").respond_with_data(
        body, status=207, content_type=XML, headers={"Content-Encoding": encoding}
    )
    error = code_of(source(httpserver).check)
    assert (error.code, error.cause) == ("upstream_error", "ContentEncoding")


def test_identity_content_encoding_is_accepted(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/", method="PROPFIND").respond_with_data(
        principal("/p/"), status=207, content_type=XML, headers={"Content-Encoding": "identity"}
    )
    source(httpserver).check()
