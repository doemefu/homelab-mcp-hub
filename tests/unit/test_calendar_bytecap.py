"""REPORT byte budget per account and call (spec 080 rev. 4.5 D62): at most 5 MiB of REPORT bodies, enforced while
reading; the cap drops whole calendars from the end of a deterministic (path-sorted) order."""

import logging
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx2
import pytest

from mcp_hub.providers.base import MAX_HTTP_RESPONSE_BYTES, ProviderError
from mcp_hub.providers.caldav import MAX_REPORT_BYTES_PER_CALL, CalDavCalendarSource, SlowObjectCache
from tests.support.dav_transport import RecordingTransport, collection, dav, home_set, multistatus, principal, report
from tests.support.logfields import allowed_fields

ZURICH = ZoneInfo("Europe/Zurich")
START = datetime(2026, 10, 17, tzinfo=ZURICH)
END = datetime(2026, 11, 2, tzinfo=ZURICH)
MIB = 1024 * 1024


def calendar_data(name: str, size: int) -> bytes:
    """A REPORT body of about `size` bytes: single events with large descriptions (each object < 256 KiB)."""
    objects: list[bytes] = []
    total = 0
    n = 0
    while total < size - 250_000:
        description = b"x" * 200_000
        objects.append(
            b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//mcp-hub tests//EN\r\nBEGIN:VEVENT\r\n"
            + f"UID:{name}-{n}@example.test\r\nDTSTAMP:20260901T000000Z\r\nDTSTART:20261020T100000Z\r\n".encode()
            + b"DTEND:20261020T110000Z\r\nSUMMARY:"
            + name.encode()
            + b"\r\nDESCRIPTION:"
            + description
            + b"\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
        )
        total += len(objects[-1])
        n += 1
    return report(*objects)


def source_with(
    calendars: dict[str, bytes], listing_order: list[str] | None = None
) -> tuple[CalDavCalendarSource, RecordingTransport]:
    names = listing_order or list(calendars)
    answers: dict[tuple[str, str], Any] = {
        ("PROPFIND", "https://dav.example.test:443/"): dav(principal("/p/")),
        ("PROPFIND", "https://dav.example.test:443/p/"): dav(home_set("/h/")),
        ("PROPFIND", "https://dav.example.test:443/h/"): dav(multistatus(*(collection(f"/h/{n}/", n) for n in names))),
    }
    for name, body in calendars.items():
        answers[("REPORT", f"https://dav.example.test:443/h/{name}/")] = dav(body)
    recorder = RecordingTransport(answers)

    def factory(username: str, password: str, timeout: float) -> httpx2.Client:
        return httpx2.Client(auth=(username, password), transport=recorder.transport(), trust_env=False)

    caldav = CalDavCalendarSource(
        "icloud",
        url="https://dav.example.test/",
        username="u",
        password="unit-test-password",  # throwaway value for an in-process transport
        include="all",
        client_factory=factory,
        slow_objects=SlowObjectCache(),
    )
    return caldav, recorder


def reports(recorder: RecordingTransport) -> list[str]:
    return [path for _, _, method, path, _ in recorder.seen if method == "REPORT"]


def test_the_budget_is_5_mib_per_account_and_call() -> None:
    assert MAX_REPORT_BYTES_PER_CALL == 5 * MIB
    assert MAX_REPORT_BYTES_PER_CALL == MAX_HTTP_RESPONSE_BYTES  # one maximal response uses the whole budget


def test_the_byte_cap_drops_whole_calendars_from_the_end(caplog: pytest.LogCaptureFixture) -> None:
    calendars = {name: calendar_data(name, 2 * MIB) for name in ("a", "b", "c", "d")}
    caldav, recorder = source_with(calendars)
    with caplog.at_level(logging.INFO, logger="mcp_hub"):
        page = caldav.events(START, END, ZURICH, ZURICH)
    # a and b fit (about 4 MiB); c would cross the remaining 1 MiB: requested, aborted, dropped whole; d never requested
    assert reports(recorder) == ["/h/a/", "/h/b/", "/h/c/"]
    assert {e.title for e in page.events} == {"a", "b"}
    assert page.truncated is True
    stopped = [r.fields for r in caplog.records if r.getMessage() == "expansion_stopped"]  # type: ignore[attr-defined]
    assert [s["outcome"] for s in stopped] == ["byte_cap"]
    assert all(set(s) <= allowed_fields("expansion_stopped") for s in stopped)


def test_calendars_are_queried_in_path_order() -> None:
    calendars = {name: calendar_data(name, 2 * MIB) for name in ("c", "a", "b")}
    caldav, recorder = source_with(calendars, listing_order=["c", "a", "b"])
    page = caldav.events(START, END, ZURICH, ZURICH)
    assert reports(recorder) == ["/h/a/", "/h/b/", "/h/c/"]
    assert {e.title for e in page.events} == {"a", "b"}  # the same subset on every call


def test_a_single_response_above_5_mib_stays_too_large() -> None:
    calendars = {"a": calendar_data("a", 1 * MIB), "b": calendar_data("b", 6 * MIB)}
    caldav, _ = source_with(calendars)
    with pytest.raises(ProviderError) as caught:
        caldav.events(START, END, ZURICH, ZURICH)
    assert caught.value.code == "too_large"


def test_a_normal_account_is_unaffected(caplog: pytest.LogCaptureFixture) -> None:
    calendars = {name: calendar_data(name, MIB + MIB // 2) for name in ("a", "b", "c")}
    caldav, recorder = source_with(calendars)
    with caplog.at_level(logging.INFO, logger="mcp_hub"):
        page = caldav.events(START, END, ZURICH, ZURICH)
    assert reports(recorder) == ["/h/a/", "/h/b/", "/h/c/"]
    assert {e.title for e in page.events} == {"a", "b", "c"}
    assert page.truncated is False
    assert not [r for r in caplog.records if r.getMessage() == "expansion_stopped"]


def test_discovery_bodies_do_not_count_against_the_budget() -> None:
    # A 4.5 MiB listing (padded with non-calendar collections) and two 2 MiB calendars: REPORTs stay below 5 MiB.
    padding = [collection(f"/h/x{i}/", "pad " + "y" * 1500, calendar=False) for i in range(2_900)]
    calendars = {name: calendar_data(name, 2 * MIB) for name in ("a", "b")}
    answers_listing = multistatus(collection("/h/a/", "a"), collection("/h/b/", "b"), *padding)
    assert 4 * MIB < len(answers_listing) < 5 * MIB
    caldav, recorder = source_with(calendars)
    recorder.answers[("PROPFIND", "https://dav.example.test:443/h/")] = dav(answers_listing)
    page = caldav.events(START, END, ZURICH, ZURICH)
    assert {e.title for e in page.events} == {"a", "b"}
    assert page.truncated is False
