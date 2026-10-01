import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from mcp_hub.config import load_settings
from mcp_hub.errors import ToolError
from mcp_hub.health import StatusStore
from mcp_hub.providers import Adapters
from mcp_hub.providers.base import ProviderError
from mcp_hub.providers.caldav import CalendarPage, RawEvent, expand
from mcp_hub.registry import Account, load_registry
from mcp_hub.tools import HubContext
from mcp_hub.tools.calendar import run_get_events
from tests.support import ics

pytestmark = pytest.mark.anyio
FROM, TO = "2026-10-17T00:00:00+02:00", "2026-11-02T00:00:00+01:00"
HALF = len(ics.NAMES) // 2
EXPECTED_TITLES = [
    "Weekly DST",
    "Standup",
    "Floating",
    "Moved standup",
    "Standup",
    "Weekend away",
    "Weekly DST",
    "Invoice [link: evil.example.test] act now",
    "Call New York",
    "Workshop",
    "Workshop",
    "Weekly DST",
]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class FakeCalendar:
    def __init__(self, account: Account, sources: dict[str, tuple[str, ...] | Exception]) -> None:
        self.account, self.sources = account, sources

    def check(self) -> None: ...

    def events(self, start: datetime, end: datetime, zone: ZoneInfo, floating: ZoneInfo) -> CalendarPage:
        source = self.sources[self.account.id]
        if isinstance(source, Exception):
            raise source
        href = f"https://cal.example.test/{self.account.id}/home/"
        found = [
            event
            for name in source
            for event in expand(
                ics.load(name), href=href, name="Home", start=start, end=end, zone=zone, floating=floating
            )
        ]
        return CalendarPage(events=found, truncated=False)


def two_calendar_accounts(secrets_dir: Path) -> None:
    data = json.loads((secrets_dir / "accounts.json").read_text())
    icloud = next(a for a in data["accounts"] if a["id"] == "icloud")
    data["accounts"].append(icloud | {"id": "icloud2", "label": "iCloud 2"})
    (secrets_dir / "accounts.json").write_text(json.dumps(data))


def context(
    secrets_dir: Path, sources: dict[str, tuple[str, ...] | Exception], env: dict[str, str] | None = None
) -> HubContext:
    settings = load_settings({"HUB_SECRETS_DIR": str(secrets_dir)} | (env or {}))
    adapters = Adapters(calendar=lambda account, _dir: FakeCalendar(account, sources))
    return HubContext(settings, load_registry(secrets_dir / "accounts.json"), StatusStore(), adapters)


async def get(ctx: HubContext, **arguments: object) -> dict[str, object]:
    args: dict[str, object] = {"start": FROM, "end": TO, "account": None, "timezone": None, "limit": None}
    result, _, _ = await run_get_events(ctx, **(args | arguments))  # type: ignore[arg-type]
    return result.model_dump(mode="json")


async def test_get_events_merges_accounts_sorted_by_start(secrets_dir: Path) -> None:
    two_calendar_accounts(secrets_dir)
    ctx = context(secrets_dir, {"icloud": ics.NAMES[:HALF], "icloud2": ics.NAMES[HALF:]})
    result = await get(ctx)
    items = result["items"]
    assert isinstance(items, list)
    assert [i["untrusted"]["title"] for i in items] == EXPECTED_TITLES
    assert {i["account"] for i in items} == {"icloud", "icloud2"}
    assert result["truncated"] is False
    assert result["account_errors"] == []
    assert result["next_cursor"] is None
    weekend = next(i for i in items if i["all_day"])
    assert weekend["start_date"] == "2026-10-24"  # sorted by its local midnight, between 10-23 and 10-25


async def test_all_day_fields_and_timed_fields(secrets_dir: Path) -> None:
    ctx = context(secrets_dir, {"icloud": ("allday.ics", "cross-zone.ics")})
    allday, timed = (await get(ctx))["items"]  # type: ignore[misc]
    assert (allday["start"], allday["end"], allday["start_date"], allday["end_date"]) == (
        None,
        None,
        "2026-10-24",
        "2026-10-26",
    )
    assert allday["untrusted"]["original_timezone"] is None
    assert (timed["start"], timed["end"], timed["start_date"], timed["end_date"]) == (
        "2026-10-28T14:00:00+01:00",
        "2026-10-28T15:00:00+01:00",
        None,
        None,
    )
    assert timed["status"] == "tentative"
    assert timed["untrusted"]["original_timezone"] == "America/New_York"
    assert timed["recurring"] is False


async def test_timezone_argument_converts_timed_events(secrets_dir: Path) -> None:
    ctx = context(secrets_dir, {"icloud": ("floating.ics", "dst-weekly.ics")})
    items = (await get(ctx, timezone="UTC"))["items"]
    assert [i["start"] for i in items][:3] == [  # type: ignore[index]
        "2026-10-18T08:00:00+00:00",
        "2026-10-21T06:00:00+00:00",
        "2026-10-25T09:00:00+00:00",
    ]


async def test_third_party_fields_only_inside_untrusted_and_sanitised(secrets_dir: Path) -> None:
    ctx = context(secrets_dir, {"icloud": ("hostile.ics",)})
    [item] = (await get(ctx))["items"]  # type: ignore[misc]
    assert set(item) == {
        "id",
        "account",
        "all_day",
        "start",
        "end",
        "start_date",
        "end_date",
        "recurring",
        "status",
        "attendee_count",
        "untrusted",
    }
    untrusted = item["untrusted"]
    assert untrusted["title"] == "Invoice [link: evil.example.test] act now"
    assert len(untrusted["description"]) <= 500
    assert untrusted["description"].endswith(" [truncated]")
    assert untrusted["organizer_address"] == "organizer@example.test"
    assert untrusted["organizer_name"] == "Org Name"
    assert untrusted["calendar_name"] == "Home"
    assert untrusted["location"] == "Room 1"
    assert untrusted["original_timezone"] == "UTC"
    assert item["attendee_count"] == 2
    assert item["start"] == "2026-10-27T13:00:00+01:00"


async def test_capability_filtering_is_silent(secrets_dir: Path) -> None:
    for ref in ("gmail-username", "gmail-app-password"):
        (secrets_dir / ref).write_text("placeholder")
    ctx = context(secrets_dir, {"icloud": ("allday.ics",)})
    result = await get(ctx)
    assert result["account_errors"] == []
    assert [i["account"] for i in result["items"]] == ["icloud"]  # type: ignore[union-attr]
    with pytest.raises(ToolError) as caught:
        await get(ctx, account="gmail")
    assert caught.value.code == "capability_unavailable"


@pytest.mark.parametrize(
    "arguments",
    [
        {"end": FROM},
        {"start": TO, "end": FROM},
        {"end": "2026-11-16T23:00:01+01:00"},
        {"start": "2026-10-17T00:00:00"},
        {"end": "2026-11-02"},
        {"timezone": "Mars/Olympus"},
        {"timezone": "localtime"},
        {"limit": 0},
        {"limit": 201},
    ],
    ids=[
        "equal",
        "reversed",
        "window-31d-plus-1s",
        "no-offset",
        "date-only",
        "zone",
        "localtime",
        "limit-0",
        "limit-201",
    ],
)
async def test_invalid_arguments(secrets_dir: Path, arguments: dict[str, object]) -> None:
    ctx = context(secrets_dir, {"icloud": ()})
    with pytest.raises(ToolError) as caught:
        await get(ctx, **arguments)
    assert caught.value.code == "invalid_argument"


async def test_window_of_exactly_31_days_is_accepted(secrets_dir: Path) -> None:
    # 31 x 24 h across the DST change: 2026-10-17T00:00+02:00 + 744 h = 2026-11-16T23:00+01:00.
    ctx = context(secrets_dir, {"icloud": ()})
    assert (await get(ctx, end="2026-11-16T23:00:00+01:00"))["items"] == []


async def test_one_calendar_account_failing_does_not_fail_the_call(secrets_dir: Path) -> None:
    two_calendar_accounts(secrets_dir)
    ctx = context(secrets_dir, {"icloud": ("allday.ics",), "icloud2": ProviderError("too_large", "ResponseTooLarge")})
    result, accounts, outcome = await run_get_events(ctx, start=FROM, end=TO, account=None, timezone=None, limit=None)
    assert [i.account for i in result.items] == ["icloud"]
    assert [e.model_dump() for e in result.account_errors] == [
        {
            "account": "icloud2",
            "capability": "calendar",
            "code": "too_large",
            "message": "Provider response exceeded the inbound size limit",
        }
    ]
    assert (accounts, outcome) == (["icloud", "icloud2"], "partial")
    assert ctx.status.get("icloud2", "calendar").status == "error"
    assert ctx.status.get("icloud", "calendar").status == "ok"


async def test_limit_and_budget_set_truncated(secrets_dir: Path) -> None:
    ctx = context(secrets_dir, {"icloud": ics.NAMES})
    limited = await get(ctx, limit=5)
    assert len(limited["items"]) == 5  # type: ignore[arg-type]
    assert limited["truncated"] is True
    two_calendar_accounts(secrets_dir)
    small = context(secrets_dir, {"icloud": ics.NAMES, "icloud2": ics.NAMES}, {"HUB_RESPONSE_BUDGET_CHARS": "10000"})
    result, _, _ = await run_get_events(small, start=FROM, end=TO, account=None, timezone=None, limit=None)
    assert 0 < len(result.items) < 24
    assert result.truncated is True
    assert len(result.model_dump_json()) <= 10000


async def test_event_ids_are_opaque_and_distinct_per_instance(secrets_dir: Path) -> None:
    ctx = context(secrets_dir, {"icloud": ics.NAMES})
    ids = [i["id"] for i in (await get(ctx))["items"]]  # type: ignore[union-attr]
    assert len(ids) == len(set(ids)) == 12
    for value in ids:
        assert value.startswith("v1.")
        assert "example.test" not in value
        assert "standup" not in value
    again = [i["id"] for i in (await get(ctx))["items"]]  # type: ignore[union-attr]
    assert again == ids


class HostileCalendar:
    """Hundreds of instances with quote-heavy, maximal third-party fields (review 18 F2 invariant)."""

    def __init__(self, account: Account) -> None:
        self.account = account

    def check(self) -> None: ...

    def events(self, start: datetime, end: datetime, zone: ZoneInfo, floating: ZoneInfo) -> CalendarPage:
        quoted = '"\\' * 2000
        found = [
            RawEvent(
                calendar_href=f"https://cal.example.test/{self.account.id}/home/",
                calendar_name=quoted,
                uid=f"hostile-{n}@example.test",
                recurrence_id=str(n),
                all_day=False,
                start=start.astimezone(zone),
                end=end.astimezone(zone),
                sort_key=start,
                recurring=True,
                status="confirmed",
                attendee_count=10_000,
                title=quoted,
                location=quoted,
                description=quoted * 20,
                organizer_name=quoted,
                organizer_address=quoted,
                original_timezone="Europe/Zurich",
            )
            for n in range(400)
        ]
        return CalendarPage(events=found, truncated=False)


@pytest.mark.parametrize("budget", [10_000, 30_000, 70_000])
async def test_result_never_exceeds_the_budget(secrets_dir: Path, budget: int) -> None:
    two_calendar_accounts(secrets_dir)
    settings = load_settings({"HUB_SECRETS_DIR": str(secrets_dir), "HUB_RESPONSE_BUDGET_CHARS": str(budget)})
    adapters = Adapters(calendar=lambda account, _dir: HostileCalendar(account))
    ctx = HubContext(settings, load_registry(secrets_dir / "accounts.json"), StatusStore(), adapters)
    result, _, _ = await run_get_events(ctx, start=FROM, end=TO, account=None, timezone=None, limit=200)
    assert len(result.model_dump_json()) <= budget
    assert result.items
    assert result.truncated is True


async def test_invalid_utf8_in_an_event_still_serialises_strictly(secrets_dir: Path) -> None:
    broken = ics.load("allday.ics").replace(b"SUMMARY:Weekend away", b"SUMMARY:M\xfcller \xff\xfe away")

    class Mixed(FakeCalendar):
        def events(self, start: datetime, end: datetime, zone: ZoneInfo, floating: ZoneInfo) -> CalendarPage:
            href = "https://cal.example.test/icloud/home/"
            found = expand(broken, href=href, name="Home", start=start, end=end, zone=zone, floating=floating)
            return CalendarPage(events=found + super().events(start, end, zone, floating).events, truncated=False)

    settings = load_settings({"HUB_SECRETS_DIR": str(secrets_dir)})
    adapters = Adapters(calendar=lambda account, _dir: Mixed(account, {"icloud": ("dst-weekly.ics",)}))
    ctx = HubContext(settings, load_registry(secrets_dir / "accounts.json"), StatusStore(), adapters)
    result, _, _ = await run_get_events(ctx, start=FROM, end=TO, account=None, timezone=None, limit=None)
    text = result.model_dump_json()
    text.encode("utf-8")  # strict: no lone surrogates
    titles = [i["untrusted"]["title"] for i in json.loads(text)["items"]]
    assert titles.count("Weekly DST") == 3
    assert any(t.startswith("M") and t.endswith("away") for t in titles)


async def test_a_truncated_expansion_marks_the_result_truncated(secrets_dir: Path) -> None:
    class Cut(FakeCalendar):
        def events(self, start: datetime, end: datetime, zone: ZoneInfo, floating: ZoneInfo) -> CalendarPage:
            return CalendarPage(events=super().events(start, end, zone, floating).events, truncated=True)

    settings = load_settings({"HUB_SECRETS_DIR": str(secrets_dir)})
    adapters = Adapters(calendar=lambda account, _dir: Cut(account, {"icloud": ("allday.ics",)}))
    ctx = HubContext(settings, load_registry(secrets_dir / "accounts.json"), StatusStore(), adapters)
    result, _, outcome = await run_get_events(ctx, start=FROM, end=TO, account=None, timezone=None, limit=None)
    assert [i.untrusted.title for i in result.items] == ["Weekend away"]
    assert result.truncated is True  # spec 080 rev. 4.5 D62: a cap or the time budget stopped the expansion
    assert (result.account_errors, outcome) == ([], "ok")


async def test_event_ids_ignore_the_host_of_the_calendar_url(secrets_dir: Path) -> None:
    # Review 19 M14 / spec 080 rev. 4.4 D56: the id covers the calendar path, UID and recurrence id, not the host.
    class Host(FakeCalendar):
        def __init__(self, account: Account, host: str) -> None:
            super().__init__(account, {"icloud": ("dst-weekly.ics",)})
            self.host = host

        def events(self, start: datetime, end: datetime, zone: ZoneInfo, floating: ZoneInfo) -> CalendarPage:
            href = f"https://{self.host}/123/calendars/home/"
            found = expand(
                ics.load("dst-weekly.ics"), href=href, name="Home", start=start, end=end, zone=zone, floating=floating
            )
            return CalendarPage(events=found, truncated=False)

    ids = []
    for host in ("p01-caldav.icloud.com", "p42-caldav.icloud.com:443"):
        settings = load_settings({"HUB_SECRETS_DIR": str(secrets_dir)})
        adapters = Adapters(calendar=lambda account, _dir, h=host: Host(account, h))  # type: ignore[misc]
        ctx = HubContext(settings, load_registry(secrets_dir / "accounts.json"), StatusStore(), adapters)
        result, _, _ = await run_get_events(ctx, start=FROM, end=TO, account=None, timezone=None, limit=None)
        ids.append([i.id for i in result.items])
    assert ids[0] == ids[1]
    assert len(set(ids[0])) == 3
