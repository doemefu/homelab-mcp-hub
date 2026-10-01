"""Radicale test helpers (spec 080 §10.2). Test data only; seeding uses httpx2 against the local container."""

import os
import time
import uuid
from xml.sax.saxutils import escape

import httpx2
import pytest

from mcp_hub.providers.caldav import CalDavCalendarSource
from tests.support import greenmail, ics

URL = "http://127.0.0.1:5232/"
LOGIN = "hub-cal"
RUN = uuid.uuid4().hex[:8]  # calendar names carry the run token, so reruns against one container stay isolated


def password() -> str:
    """Generated per run by scripts/provider_services.sh up (shared credentials file)."""
    return greenmail.passwords()[LOGIN]


def _client() -> httpx2.Client:
    return httpx2.Client(auth=(LOGIN, password()), trust_env=False, timeout=120)  # the 6 MiB seed is slow


def require() -> None:
    """Skip unless HUB_PROVIDER_TESTS=1; then fail (not skip) if Radicale does not answer within 60 s."""
    if os.environ.get("HUB_PROVIDER_TESTS") != "1":
        pytest.skip("provider tests need scripts/provider_services.sh up and HUB_PROVIDER_TESTS=1")
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            with _client() as client:
                if client.request("PROPFIND", f"{URL}{LOGIN}/", headers={"Depth": "0"}).status_code == 207:
                    return
        except httpx2.TransportError:
            pass
        time.sleep(1)
    pytest.fail("Radicale did not become ready")


def calendar_name(label: str) -> str:
    return f"it-{RUN}-{label}"


def seed(name: str, objects: list[bytes]) -> str:
    """Create a calendar with display name `name` and PUT each object; returns the calendar URL."""
    calendar = f"{URL}{LOGIN}/{uuid.uuid4().hex[:12]}/"
    body = (
        '<?xml version="1.0" encoding="utf-8"?><C:mkcalendar xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">'
        f"<D:set><D:prop><D:displayname>{escape(name)}</D:displayname></D:prop></D:set></C:mkcalendar>"
    )
    with _client() as client:
        created = client.request(
            "MKCALENDAR", calendar, content=body.encode(), headers={"Content-Type": "application/xml"}
        )
        assert created.status_code == 201, created.status_code
        for n, data in enumerate(objects):
            put = client.put(f"{calendar}{n}.ics", content=data, headers={"Content-Type": "text/calendar"})
            assert put.status_code == 201, put.status_code
    return calendar


def seed_fixtures(name: str, files: tuple[str, ...] = ics.NAMES) -> str:
    return seed(name, [ics.load(f) for f in files])


def source(include: list[str], password_override: str | None = None, url: str = URL) -> CalDavCalendarSource:
    return CalDavCalendarSource(
        "icloud", url=url, username=LOGIN, password=password_override or password(), include=include
    )
