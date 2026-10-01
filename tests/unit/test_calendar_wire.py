import json
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from starlette.testclient import TestClient

from mcp_hub.app import create_app
from mcp_hub.config import Settings
from mcp_hub.logging import configure_logging
from mcp_hub.providers import Adapters
from mcp_hub.providers.caldav import CalendarPage, expand
from mcp_hub.registry import Account, load_registry
from tests.support import ics
from tests.support.mcp import body, modern
from tests.support.tokens import TokenFactory


class Calendar:
    def check(self) -> None: ...

    def events(self, start: datetime, end: datetime, zone: ZoneInfo, floating: ZoneInfo) -> CalendarPage:
        found = expand(
            ics.load("dst-weekly.ics"),
            href="https://cal.example.test/1/",
            name="Home",
            start=start,
            end=end,
            zone=zone,
            floating=floating,
        )
        return CalendarPage(events=found, truncated=False)


def fake_calendar(account: Account, secrets_dir: Path) -> Calendar:
    return Calendar()


@pytest.fixture
def wired(settings: Settings, secrets_dir: Path) -> Iterator[TestClient]:
    configure_logging(settings.log_level)
    hub = create_app(settings, load_registry(secrets_dir / "accounts.json"), adapters=Adapters(calendar=fake_calendar))
    with TestClient(hub.public, base_url="https://mcp.furchert.ch") as client:
        yield client


def test_get_events_schema_uses_from_and_to(wired: TestClient, tokens: TokenFactory) -> None:
    tools = {t["name"]: t for t in body(modern(wired, tokens.mint(), "tools/list"))["result"]["tools"]}
    tool = tools["get_events"]
    assert set(tool["inputSchema"]["properties"]) == {"from", "to", "account", "timezone", "limit"}
    assert sorted(tool["inputSchema"]["required"]) == ["from", "to"]
    assert tool["annotations"]["readOnlyHint"] is True
    assert tool["annotations"]["destructiveHint"] is False
    assert "outputSchema" in tool
    assert "untrusted" in tool["description"]


def test_get_events_call_over_the_wire(wired: TestClient, tokens: TokenFactory) -> None:
    arguments = {"from": "2026-10-17T00:00:00+02:00", "to": "2026-11-02T00:00:00+01:00", "timezone": "UTC"}
    params = {"name": "get_events", "arguments": arguments}
    result = body(modern(wired, tokens.mint(), "tools/call", params, name="get_events"))["result"]
    assert result["isError"] is False
    assert json.loads(result["content"][0]["text"]) == result["structuredContent"]
    assert [i["start"] for i in result["structuredContent"]["items"]] == [
        "2026-10-18T08:00:00+00:00",
        "2026-10-25T09:00:00+00:00",
        "2026-11-01T09:00:00+00:00",
    ]


def test_get_events_without_offset_is_invalid_argument(wired: TestClient, tokens: TokenFactory) -> None:
    params = {"name": "get_events", "arguments": {"from": "2026-10-17T00:00:00", "to": "2026-10-18T00:00:00+02:00"}}
    result = body(modern(wired, tokens.mint(), "tools/call", params, name="get_events"))["result"]
    assert result["isError"] is True
    assert json.loads(result["content"][0]["text"])["code"] == "invalid_argument"
