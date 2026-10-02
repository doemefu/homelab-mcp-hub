"""Memory and CPU probe for get_events under the pod's limits (review of PR #15, H1).

Drives run_get_events through an in-process fake CalDAV server (no network): each calendar's REPORT body is built per
request from synthetic objects that fill the 5 MiB byte budget, so the process holds only what the adapter holds.
Prints peak RSS and CPU per call, then the cgroup's memory peak and events when they are readable.

Run inside the production image with the repository mounted read-only (see CONTRIBUTING.md, "Memory probe"):

    docker run --rm --memory=256m --memory-swap=256m --cpus=1 --read-only --tmpfs /tmp \\
      -v "$PWD":/work:ro -e PYTHONPATH=/app/src:/work mcp-hub:dev \\
      python /work/scripts/memory_probe.py --scenario nonascii-240k --calls 10
"""

import argparse
import asyncio
import gc
import json
import resource
import sys
import tempfile
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

import httpx2
from tests.support.dav_transport import RecordingTransport, collection, dav, home_set, multistatus, principal, report

from mcp_hub.config import load_settings
from mcp_hub.health import StatusStore
from mcp_hub.providers import Adapters
from mcp_hub.providers.caldav import MAX_REPORT_BYTES_PER_CALL, CalDavCalendarSource, SlowObjectCache
from mcp_hub.registry import load_registry
from mcp_hub.tools import HubContext
from mcp_hub.tools.calendar import run_get_events

BASE = "https://dav.example.test:443"
HEAD = "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//mcp-hub probe//EN\r\n"
EN_DASH, EMOJI, ONE_DOT_LEADER = chr(0x2013), chr(0x1F600), chr(0x2027)
OUTLOOK_STANDARD = "FREQ=YEARLY;INTERVAL=1;BYDAY=-1SU;BYMONTH=10"
OUTLOOK_DAYLIGHT = "FREQ=YEARLY;INTERVAL=1;BYDAY=-1SU;BYMONTH=3"
HOURS = ",".join(map(str, range(24)))


def stamp(value: datetime) -> str:
    return value.strftime("%Y%m%dT%H%M%SZ")


def vevent(uid: str, extra: str = "", start: str = "20261017T100000Z", description: int = 0) -> str:
    text = f"DESCRIPTION:{'x' * description}\r\n" if description else ""
    return (
        f"BEGIN:VEVENT\r\nUID:{uid}\r\nDTSTAMP:20260901T000000Z\r\nDTSTART:{start}\r\nDTEND:{start[:9]}235900Z\r\n"
        f"{text}{extra}END:VEVENT\r\n"
    )


def sub(name: str, rule: str, start: str = "16010101T030000", extra: str = "") -> str:
    return (
        f"BEGIN:{name}\r\nDTSTART:{start}\r\nTZOFFSETFROM:+0200\r\nTZOFFSETTO:+0100\r\nRRULE:{rule}\r\n{extra}"
        f"END:{name}\r\n"
    )


def zone_event(k: int, tzid: str, subs: str, when: str = "20261020T100000", rule: str = "", more: str = "") -> str:
    recurring = f"RRULE:{rule}\r\n" if rule else ""
    return (
        f"BEGIN:VTIMEZONE\r\nTZID:{tzid}\r\n{subs}END:VTIMEZONE\r\n{more}BEGIN:VEVENT\r\nUID:z{k}@example.test\r\n"
        f"DTSTAMP:20260901T000000Z\r\nDTSTART;TZID={tzid}:{when}\r\nDURATION:PT1H\r\n{recurring}SUMMARY:Probe\r\n"
        "END:VEVENT\r\n"
    )


def outlook(comment: str = "") -> str:
    extra = f"COMMENT:{comment}\r\n" if comment else ""
    return sub("STANDARD", OUTLOOK_STANDARD, extra=extra) + sub("DAYLIGHT", OUTLOOK_DAYLIGHT, "16010101T020000")


def overrides(k: int, count: int, description: int) -> str:
    uid = f"series{k}@example.test"
    first = datetime(2026, 10, 17, 10)
    return vevent(uid, "RRULE:FREQ=DAILY\r\n") + "".join(
        vevent(
            uid,
            f"RECURRENCE-ID:{stamp(first + timedelta(days=i))}\r\n",
            start=stamp(first + timedelta(days=i, hours=1)),
            description=description,
        )
        for i in range(count)
    )


def far_zones(k: int) -> str:
    """20 zones at the iteration limit, each looked up in the year 9999 through an override (CPU deadline)."""
    zones = "".join(f"BEGIN:VTIMEZONE\r\nTZID:Z{k}-{j}\r\n{outlook()}END:VTIMEZONE\r\n" for j in range(20))
    moved = "".join(
        f"BEGIN:VEVENT\r\nUID:far{k}@example.test\r\nDTSTAMP:20260901T000000Z\r\n"
        f"RECURRENCE-ID;TZID=Z{k}-0:999910{20 + j // 10:02d}T1{j % 10}0000\r\n"
        f"DTSTART;TZID=Z{k}-{j}:99991020T100000\r\nDURATION:PT1H\r\nSUMMARY:o\r\nEND:VEVENT\r\n"
        for j in range(1, 20)
    )
    return zones + (
        f"BEGIN:VEVENT\r\nUID:far{k}@example.test\r\nDTSTAMP:20260901T000000Z\r\nDTSTART;TZID=Z{k}-0:99991020T100000\r\n"
        "DURATION:PT1H\r\nRRULE:FREQ=DAILY;COUNT=40\r\nSUMMARY:z\r\nEND:VEVENT\r\n" + moved
    )


DENSE = (
    f"FREQ=YEARLY;BYMONTH={','.join(map(str, range(1, 13)))};BYMONTHDAY={','.join(map(str, range(1, 32)))};"
    f"BYHOUR={HOURS}"
)
SCENARIOS: dict[str, Callable[[int], str]] = {
    # Legitimate large single events; ASCII baseline and the same with one non-ASCII character in the title.
    "ascii-240k": lambda k: vevent(f"a{k}@example.test", "SUMMARY:Team - weekly\r\n", "20261020T100000Z", 240_000),
    "nonascii-240k": lambda k: vevent(
        f"n{k}@example.test", f"SUMMARY:Team {EN_DASH} weekly\r\n", "20261020T100000Z", 240_000
    ),
    # Many small single events, each with its own custom zone (built by icalendar while it parses).
    "zone-ascii": lambda k: zone_event(k, f"Custom/Z{k}", outlook("plain")),
    "zone-u2027": lambda k: zone_event(k, f"Custom/Z{k}", outlook(f"x{ONE_DOT_LEADER}y")),
    "zone-emoji": lambda k: zone_event(k, f"Custom/Z{k}", outlook(f"x{EMOJI}y")),
    # The worst objects that pass every screen.
    "series-999-1mib": lambda k: overrides(k, 999, 850),
    "rdates-1000-1mib": lambda k: vevent(
        f"r{k}@example.test",
        "RDATE:" + ",".join(stamp(datetime(2026, 10, 21) + timedelta(minutes=30 * i)) for i in range(1000)) + "\r\n",
        description=1_000_000,
    ),
    "zone-1000-subs": lambda k: zone_event(
        k,
        f"Custom/Z{k}",
        "".join(sub("STANDARD", "FREQ=YEARLY;BYMONTH=1;COUNT=1", f"{2000 + i % 20}0101T000000") for i in range(999))
        + sub("DAYLIGHT", OUTLOOK_DAYLIGHT, "16010101T020000"),
        when="20261017T090000",
        rule="FREQ=DAILY",
    ),
    "zone-20-far": far_zones,
    # Refused before parsing (review F1 shape).
    "zone-dense-refused": lambda k: zone_event(
        k, f"Custom/Z{k}", sub("STANDARD", DENSE) + sub("DAYLIGHT", OUTLOOK_DAYLIGHT, "16010101T020000")
    ),
}


def report_body(scenario: str) -> bytes:
    """As many objects as fit one REPORT within the per-call byte budget."""
    build = SCENARIOS[scenario]
    one = len(HEAD) + len(build(0)) + 300
    count = max(1, int(MAX_REPORT_BYTES_PER_CALL * 0.97) // one)
    return report(*((HEAD + build(k) + "END:VCALENDAR\r\n").encode() for k in range(count)))


def source(account: str, scenario: str, calendars: int) -> CalDavCalendarSource:
    answers = {
        ("PROPFIND", f"{BASE}/"): dav(principal("/p/")),
        ("PROPFIND", f"{BASE}/p/"): dav(home_set("/h/")),
        ("PROPFIND", f"{BASE}/h/"): dav(multistatus(*(collection(f"/h/c{i}/", f"C{i}") for i in range(calendars)))),
    }
    for i in range(calendars):  # built per request, so nothing is held between calls
        answers[("REPORT", f"{BASE}/h/c{i}/")] = lambda request: dav(report_body(scenario))(request)
    transport = RecordingTransport(answers).transport()
    return CalDavCalendarSource(
        account,
        url="https://dav.example.test/",
        username="probe",
        password="probe",
        include="all",
        client_factory=lambda user, password, timeout: httpx2.Client(
            auth=(user, password), transport=transport, trust_env=False
        ),
        slow_objects=SlowObjectCache(),
    )


def context(accounts: int, scenario: str, calendars: int) -> HubContext:
    secrets = Path(tempfile.mkdtemp()) / "secrets"
    secrets.mkdir()
    example = json.loads((Path(__file__).parents[1] / "tests/fixtures/accounts.example.json").read_text())
    template = next(a for a in example["accounts"] if a["id"] == "icloud")
    example["accounts"] = [template | {"id": f"icloud{i}" if i else "icloud"} for i in range(accounts)]
    (secrets / "accounts.json").write_text(json.dumps(example))
    for ref in ("icloud-username", "icloud-app-password"):
        (secrets / ref).write_text("placeholder")
    sources = {a["id"]: source(a["id"], scenario, calendars) for a in example["accounts"]}
    return HubContext(
        load_settings({"HUB_SECRETS_DIR": str(secrets)}),
        load_registry(secrets / "accounts.json"),
        StatusStore(),
        Adapters(calendar=lambda account, deadline: sources[account.id]),
    )


def cgroup(name: str) -> str:
    try:
        return Path("/sys/fs/cgroup", name).read_text().strip().replace("\n", ", ")
    except OSError:
        return "n/a"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--scenario", choices=sorted(SCENARIOS), required=True)
    parser.add_argument("--accounts", type=int, default=1)
    parser.add_argument("--calendars", type=int, default=10)
    parser.add_argument("--calls", type=int, default=10)
    args = parser.parse_args()
    ctx = context(args.accounts, args.scenario, args.calendars)
    for call in range(1, args.calls + 1):
        cpu = time.process_time()
        result, _, _ = asyncio.run(
            run_get_events(
                ctx,
                start="2026-10-17T00:00:00+02:00",
                end="2026-11-16T23:00:00+01:00",
                account=None,
                timezone=None,
                limit=200,
            )
        )
        gc.collect()
        peak_mib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (2**20 if sys.platform == "darwin" else 1024)
        print(
            f"call {call}: peak RSS {peak_mib:.0f} MiB, CPU {time.process_time() - cpu:.1f} s, "
            f"items {len(result.items)}, truncated {result.truncated}, skipped {result.skipped_objects}",
            flush=True,
        )
    print(f"cgroup memory.peak {cgroup('memory.peak')}; memory.events {cgroup('memory.events')}", flush=True)


if __name__ == "__main__":
    main()
