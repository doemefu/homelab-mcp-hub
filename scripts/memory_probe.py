"""Memory and CPU probe for get_events and the Graph get_message under the pod's limits (review of PR #15, H1; L27).

Drives run_get_events through an in-process fake CalDAV server (no network): each calendar's REPORT body is built per
request from synthetic objects that fill the 5 MiB byte budget, so the process holds only what the adapter holds.
`--scenario graph-message` drives run_get_message for an `outlook` entry through an in-process Graph transport whose
message answer fills the 2 MiB message cap (non-ASCII body plus 5,000 recipients) and whose attachment answer fills the
1 MiB list cap; `--with-calendar <scenario>` runs that calendar scenario concurrently in the same process.
`--scenario status` drives ImapMailbox.message_status for 50 refs through an in-process IMAP fake: every location SEARCH
and every reply or thread SEARCH answers 500 hits whose header blocks are hostile (8 KiB address header for the 500-UID
locate chunks, 64 KiB lookup header for the 50-UID lookup chunks; one shared bytes object, so the fake holds
none of it).
`--scenario thread` drives run_get_thread for `icloud` accounts through an in-process IMAP fake (WP13c): the calls
alternate between a 51-message header-round thread (round (1) with 11 ids, three round-(2) iterations, 20 keys per
search, hit blocks of 64 KiB, 51 members with 64 KiB bodies and 300-part BODYSTRUCTUREs) and a subject-fallback thread
(500 hostile 8 KiB scan items per folder, 50 joined members); every search also answers 500 hostile hits per folder.
`--scenario thread-graph` drives run_get_thread for `--accounts` Graph accounts through an in-process transport: the
conversation list answers with a 2 MiB `uniqueBody` list or, alternately, a refused one and the 2 MiB fallback list, and
20 single reads of about 2 MiB follow (every fourth refused once, then retried with `uniqueBody` alone).
Prints peak RSS and CPU per call, then the cgroup's memory peak and events when they are readable.

Run inside the production image with the repository mounted read-only (see CONTRIBUTING.md, "Memory probe"):

    docker run --rm --memory=256m --memory-swap=256m --cpus=1 --read-only --tmpfs /tmp \\
      -v "$PWD":/work:ro -e PYTHONPATH=/app/src:/work mcp-hub:dev \\
      python /work/scripts/memory_probe.py --scenario nonascii-240k --calls 10
    ... python /work/scripts/memory_probe.py --scenario graph-message --with-calendar nonascii-240k --calls 10
    ... python /work/scripts/memory_probe.py --scenario status --accounts 2 --parallel 2 --calls 10
    ... python /work/scripts/memory_probe.py --scenario thread --accounts 2 --parallel 2 --calls 10
    ... python /work/scripts/memory_probe.py --scenario thread-graph --accounts 2 --parallel 2 --calls 10
"""

import argparse
import asyncio
import base64
import functools
import gc
import itertools
import json
import resource
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import anyio
import httpx2
from tests.support.clock import FakeClock
from tests.support.dav_transport import RecordingTransport, collection, dav, home_set, multistatus, principal, report
from tests.support.graph_fixtures import CONVERSATION, body_object, thread_item
from tests.support.graph_fixtures import INBOX_ID as THREAD_INBOX_ID
from tests.support.imap_fake import FakeFolder, FakeImapClient, FakeImapServer, FakeMessage
from tests.support.ms_transport import FAKE_CLIENT_ID, MsTransport, json_answer
from tests.support.token_fakes import KEY, FakeStore

from mcp_hub.config import load_settings
from mcp_hub.health import StatusStore
from mcp_hub.ids import GraphMessageRef, StableRef, encode_message_id, encode_ref
from mcp_hub.providers import Adapters
from mcp_hub.providers.caldav import MAX_REPORT_BYTES_PER_CALL, CalDavCalendarSource, SlowObjectCache
from mcp_hub.providers.deadline import CallDeadline
from mcp_hub.providers.folders import GRAPH_WELL_KNOWN, FolderMapCache
from mcp_hub.providers.graph import MAX_LIST_RESPONSE_BYTES, MAX_MESSAGE_RESPONSE_BYTES, Backoff, GraphMailbox
from mcp_hub.providers.graph_auth import AccessTokenCache, GraphTokenSource
from mcp_hub.providers.imap import ImapMailbox
from mcp_hub.providers.msidentity import GraphAccount
from mcp_hub.registry import Account, load_registry
from mcp_hub.tools import HubContext
from mcp_hub.tools.calendar import run_get_events
from mcp_hub.tools.mail import run_get_message
from mcp_hub.tools.mail_thread import run_get_thread

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


GRAPH = "https://graph.microsoft.com:443/v1.0"
GRAPH_ID = "AAMkPROBE0001="
INBOX_ID = "AQMkPROBEINBOX"


def graph_message_body() -> bytes:
    """2,097,000 bytes of message JSON: 5,000 recipients and a non-ASCII text body ("ä😀", 4-byte code points)."""
    recipients = [{"emailAddress": {"name": f"Recipient {i}", "address": f"r{i}@example.test"}} for i in range(5000)]
    head: dict[str, object] = {
        "id": GRAPH_ID,
        "receivedDateTime": "2026-10-17T08:00:00Z",
        "isRead": False,
        "hasAttachments": True,
        "from": {"emailAddress": {"name": "Probe", "address": "probe@example.test"}},
        "toRecipients": recipients,
        "ccRecipients": recipients[:100],
        "subject": "Probe " + EMOJI * 100,
        "parentFolderId": INBOX_ID,
    }
    raw = json.dumps(head | {"body": {"contentType": "text", "content": ""}}, ensure_ascii=False).encode()
    count = (MAX_MESSAGE_RESPONSE_BYTES - 152 - len(raw)) // len(("ä" + EMOJI).encode())
    body = json.dumps(
        head | {"body": {"contentType": "text", "content": ("ä" + EMOJI) * count}}, ensure_ascii=False
    ).encode()
    if len(body) > MAX_MESSAGE_RESPONSE_BYTES:
        raise SystemExit("probe message body exceeds the 2 MiB cap")
    return body


def graph_attachments_body() -> bytes:
    """Just below the 1 MiB list cap: as many attachment entries as fit, so every one is parsed."""
    entry = {"name": "file-" + EMOJI * 20 + ".pdf", "contentType": "application/pdf", "size": 1000, "isInline": False}
    one = len(json.dumps(entry, ensure_ascii=False).encode()) + 2
    body = json.dumps({"value": [entry] * ((MAX_LIST_RESPONSE_BYTES - 1000) // one)}, ensure_ascii=False).encode()
    if len(body) > MAX_LIST_RESPONSE_BYTES:
        raise SystemExit("probe attachment body exceeds the 1 MiB cap")
    return body


def graph_mailbox(account: Account, thread: bool = False) -> GraphMailbox:
    """Real GraphMailbox + GraphTokenSource; the refresh token lives in the in-memory fake store (no PostgreSQL).
    The seven well-known folder GETs of the D75 binding are answered: inbox with its id, the others 404 (no such
    folder); the folder ids are cached per mailbox as in the hub."""
    answers: dict[tuple[str, str], Callable[[httpx2.Request], httpx2.Response]] = {
        ("GET", f"{GRAPH}/me/mailFolders/{name}"): lambda r: httpx2.Response(404) for name in GRAPH_WELL_KNOWN
    }
    answers |= {
        ("GET", f"{GRAPH}/me/mailFolders/inbox"): lambda r: httpx2.Response(200, json={"id": INBOX_ID}),
        # Built per request, so nothing is held between calls.
        ("GET", f"{GRAPH}/me/messages/{GRAPH_ID}"): lambda r: httpx2.Response(200, content=graph_message_body()),
        ("GET", f"{GRAPH}/me/messages/{GRAPH_ID}/attachments"): lambda r: httpx2.Response(
            200, content=graph_attachments_body()
        ),
    }
    graph = thread_graph_transport() if thread else RecordingTransport(answers).transport()
    token = {"access_token": "AT-PROBE", "refresh_token": "RT-PROBE", "expires_in": 3600, "scope": "Mail.Read"}
    ms = MsTransport({"/consumers/oauth2/v2.0/token": [json_answer(200, token)]}).transport()
    store = FakeStore()
    store.sealed = store.cipher_.seal("RT-PROBE", account_id=account.id, provider=account.provider)  # bound to the id
    tokens = GraphTokenSource(
        GraphAccount(account.id, account.provider, "consumers", FAKE_CLIENT_ID, ("Mail.Read", "offline_access")),
        store,
        cache=AccessTokenCache(),
        client_factory=lambda timeout: httpx2.Client(transport=ms, trust_env=False),
    )
    return GraphMailbox(
        account.id,
        tokens,
        client_factory=lambda timeout: httpx2.Client(transport=graph, trust_env=False),
        backoff=Backoff(),
        folder_maps=FolderMapCache(),
    )


def context(
    accounts: int,
    scenario: str | None,
    calendars: int,
    graph: bool = False,
    *,
    imap: Callable[[str], ImapMailbox] | None = None,
    thread_graph: bool = False,
) -> HubContext:
    """`accounts` icloud accounts (calendar scenario and/or fake IMAP mailboxes) and, with `graph`, as many Graph
    accounts (`outlook`, `outlook1`, ...; one for graph-message)."""
    secrets = Path(tempfile.mkdtemp()) / "secrets"
    secrets.mkdir()
    example = json.loads((Path(__file__).parents[1] / "tests/fixtures/accounts.example.json").read_text())
    template = next(a for a in example["accounts"] if a["id"] == "icloud")
    graph_template = next(a for a in example["accounts"] if a["id"] == "outlook")
    icloud = [template | {"id": f"icloud{i}" if i else "icloud"} for i in range(accounts if scenario or imap else 0)]
    outlook = [
        graph_template | {"id": f"outlook{i}" if i else "outlook"}
        for i in range(accounts if thread_graph else 1 if graph else 0)
    ]
    example["accounts"] = icloud + outlook
    (secrets / "accounts.json").write_text(json.dumps(example))
    for ref in ("icloud-username", "icloud-app-password", "outlook-ms-client-id", "db-username", "db-password"):
        (secrets / ref).write_text("placeholder")
    (secrets / "token-encryption-key").write_text(base64.b64encode(KEY).decode())
    sources = {a["id"]: source(a["id"], scenario, calendars) for a in icloud} if scenario else {}
    registry = load_registry(secrets / "accounts.json")
    mailboxes = {a.id: graph_mailbox(a, thread_graph) for a in registry.accounts if a.id.startswith("outlook")}

    def mailbox(account: Account, directory: Path) -> ImapMailbox | GraphMailbox:
        if account.id in mailboxes:
            return mailboxes[account.id]
        if imap is None:
            raise SystemExit("no mailbox for this account")
        return imap(account.id)

    return HubContext(
        load_settings({"HUB_SECRETS_DIR": str(secrets)}),
        registry,
        StatusStore(),
        Adapters(mailbox=mailbox, calendar=lambda account, deadline: sources[account.id]),
    )


STATUS_REFS, STATUS_HITS = 50, 500


def status_server() -> tuple[FakeImapServer, list[StableRef]]:
    """INBOX holds the 50 refs (UIDs 1-50) and 500 hostile messages that every location and lookup SEARCH hits: they
    share one header block that names all 50 ids in Message-ID, In-Reply-To and References, carries an 8 KiB From and
    pads References to 64 KiB, so the locate items (8 KiB) and the lookup items (64 KiB) are always cut."""
    ids = [f"r{i:02d}@example.test" for i in range(STATUS_REFS)]
    named = " ".join(f"<{x}>" for x in ids)
    lines = [
        f"From: {'a' * 8000} <sender@example.test>",
        f"Message-ID: {named}",
        f"In-Reply-To: {named}",
        f"References: {named} {'x' * 66000}",
    ]
    hostile = ("\r\n".join(lines) + "\r\n\r\n").encode()
    received = datetime(2026, 10, 21, 7, 0, tzinfo=UTC)
    real = [
        FakeMessage(i + 1, received, headers=f"From: s@example.test\r\nMessage-ID: <{x}>\r\n\r\n".encode())
        for i, x in enumerate(ids)
    ]
    bulk = [FakeMessage(STATUS_REFS + 1 + i, received, headers=hostile) for i in range(STATUS_HITS)]
    inbox = FakeFolder("INBOX", (b"\\HasNoChildren",), messages={m.uid: m for m in real + bulk})
    sent = FakeFolder("Sent Messages", (b"\\HasNoChildren", b"\\Sent"), messages={})
    return FakeImapServer([inbox, sent], clock=FakeClock()), [StableRef("icloud", x) for x in ids]


def status_call(server: FakeImapServer, targets: list[StableRef]) -> str:
    clock = server.clock
    if clock is None:
        raise SystemExit("status server needs a clock")
    box = ImapMailbox(
        "icloud",
        host="imap.example.test",
        port=993,
        folder="INBOX",
        username="u",
        password="p",
        client_factory=lambda host, port, context, timeout: server.client(),
        own_addresses=frozenset({"owner@example.test"}),  # not the hostile sender: the From item is fetched
        folder_maps=FolderMapCache(clock=clock),
    )
    statuses = box.message_status(targets, CallDeadline(clock(), 16.0, 20.0, clock))
    checked = sum(s.thread_checked for s in statuses)
    return f"refs {len(statuses)}, found {sum(s.state == 'found' for s in statuses)}, thread_checked {checked}"


# --- get_thread (WP13c) ---------------------------------------------------------------------------------------------

OWNER = "owner@example.test"
THREAD_START = datetime(2026, 10, 21, 7, 0, tzinfo=UTC)
BULK_HITS, MEMBERS, SUBJECT_MEMBERS = 500, 51, 50
RECIPIENTS = ", ".join([OWNER] + [f"r{i}@example.test" for i in range(150)])
PAIR = "ä" + EMOJI  # 6 bytes of UTF-8
PLAIN_B64 = base64.b64encode(
    ((PAIR + " word ") * 14_000).encode()
)  # 64 KiB of it is fetched; the part is declared larger
HTML_BODY = ("<p>Hello <b>" + PAIR + "</b> &amp; <i>x</i></p>\n" * 7000).encode()
ATTACHMENT = (
    b"application", b"pdf", (b"name", b"f.pdf"), None, None, b"base64", 1000, None,
    (b"attachment", (b"filename", b"f.pdf")), None, None,
)  # fmt: skip


def hdr(*fields: tuple[str, str]) -> bytes:
    return ("".join(f"{name}: {value}\r\n" for name, value in fields) + "\r\n").encode()


def text_leaf(subtype: bytes, encoding: bytes, size: int) -> tuple[object, ...]:
    return (b"text", subtype, (b"charset", b"utf-8"), None, None, encoding, size, 100, None, None, None, None)


def mixed(first: object) -> tuple[object, ...]:
    """multipart/mixed: the text structure and 300 attachment parts (a maximal BODYSTRUCTURE line)."""
    return ([first, *([ATTACHMENT] * 300)], b"mixed", (b"boundary", b"b"), None, None, None)


STRUCTURES = (  # even UIDs: plain (base64) next to HTML; odd UIDs: HTML only
    mixed(([text_leaf(b"plain", b"base64", 200_000), text_leaf(b"html", b"7bit", 300_000)], b"alternative", None)),
    mixed(text_leaf(b"html", b"7bit", 300_000)),
)


def bulk_header(ids: list[str]) -> bytes:
    """A hit of every header and date SEARCH: a 9,000-character Subject (the 8 KiB scan item is cut inside it) and a
    64 KiB item cut inside the References, before the ids that made it match. The addresses are short: an 8 KiB
    address header costs about 1.5 ms of `email.utils.getaddresses` per hit (500 hits per SEARCH), which would
    exhaust the 12 s reserve and keep the probe from reaching the body pass."""
    named = " ".join(f"<{x}@example.test>" for x in ids)
    return hdr(
        ("From", "Bulk <bulk@example.test>"),
        ("To", OWNER),
        ("Subject", "y" * 9000),
        ("Message-ID", "<bulk@example.test>"),
        ("References", f"{'x' * 66000} {named}"),
    )


def member(
    uid: int, ident: str, minute: int, topic: str, *, parent: str | None, refs: list[str], sent: bool
) -> FakeMessage:
    fields = [
        ("From", OWNER if sent else "Peer <peer@example.test>"),
        ("To", "Peer <peer@example.test>" if sent else RECIPIENTS),
        ("Subject", topic),
        ("Message-ID", f"<{ident}@example.test>"),
    ]
    if parent is not None:
        fields.append(("In-Reply-To", f"<{parent}@example.test>"))
    if refs:
        fields.append(("References", " ".join(f"<{r}@example.test>" for r in refs)))
    return FakeMessage(
        uid,
        THREAD_START + timedelta(minutes=minute),
        headers=hdr(*fields),
        text=HTML_BODY if uid % 2 else PLAIN_B64,
        bodystructure=STRUCTURES[uid % 2],
    )


@dataclass
class ProbeFolder(FakeFolder):
    bulk: frozenset[int] = frozenset()  # the hostile hits: found by every header or date SEARCH without being matched


class ProbeClient(FakeImapClient):
    def search(self, criteria: bytes) -> list[int]:
        """The fake's evaluator over the real messages, plus every bulk UID for a header or date search (matching 500
        hostile blocks per SEARCH in the evaluator would cost seconds and measure the fake)."""
        folder = self._selected("UID SEARCH")
        bulk = folder.bulk if isinstance(folder, ProbeFolder) else frozenset()
        every = folder.messages
        folder.messages = {uid: m for uid, m in every.items() if uid not in bulk}
        try:
            hits = super().search(criteria)
        finally:
            folder.messages = every
        return sorted({*hits, *bulk}) if bulk and (b"HEADER" in criteria or b"SINCE" in criteria) else hits


class ProbeServer(FakeImapServer):
    def client(self) -> FakeImapClient:
        return ProbeClient(self)


def chain(n: int) -> tuple[str | None, list[str], bool]:
    """(In-Reply-To, References, in Sent) of member n of the header-round thread: 1-30 reach round (1) through their
    References root; 31-40, 41-45 and 46-51 follow one round-(2) iteration each by In-Reply-To; the keys of 46-51 are
    left after the third iteration (not exact)."""
    if n == 1:
        return None, [], False
    if n < 30:
        return "t1", ["t1"], False
    if n == 30:  # the seed: 31 References, of which round (1) searches the root and the newest nine
        return "t29", ["t1", *(f"x{i}" for i in range(1, 31))], False
    if n <= 40:
        return f"t{n - 30}", [], False
    if n <= 45:
        return f"t{n - 10}", [], True
    return f"t{41 + (n - 46) % 5}", [], False


def thread_server(kind: str) -> tuple[ProbeServer, StableRef]:
    """`rounds`: 51 members, 31 of them with a chain through the header rounds; `subject`: a seed without References
    and 50 other messages with its subject. Every folder also holds 500 bulk hits."""
    ids = [f"t{n}" for n in range(1, MEMBERS + 1)] + ["b0"]
    hostile = bulk_header(ids)  # one shared bytes object, so the fake holds none of it per hit
    bulk = {uid: FakeMessage(uid, THREAD_START, headers=hostile) for uid in range(1, BULK_HITS + 1)}
    inbox: dict[int, FakeMessage] = dict(bulk)
    sent: dict[int, FakeMessage] = {}
    if kind == "rounds":
        for n in range(1, MEMBERS + 1):
            parent, refs, outgoing = chain(n)
            target = sent if outgoing else inbox
            target[BULK_HITS + n] = member(
                BULK_HITS + n, f"t{n}", n, "Probe thread", parent=parent, refs=refs, sent=outgoing
            )
        seed = "t30@example.test"
    else:
        inbox[BULK_HITS + 1] = member(BULK_HITS + 1, "b0", 0, "Probe topic", parent=None, refs=[], sent=False)
        for k in range(1, SUBJECT_MEMBERS + 1):
            uid = BULK_HITS + 1 + k
            inbox[uid] = member(uid, f"s{k}", k, "Re: Probe topic", parent=None, refs=[], sent=False)
        seed = "b0@example.test"
    folders: list[FakeFolder] = [
        ProbeFolder("INBOX", (b"\\HasNoChildren",), messages=inbox, bulk=frozenset(bulk)),
        ProbeFolder("Sent Messages", (b"\\HasNoChildren", b"\\Sent"), messages=dict(bulk) | sent, bulk=frozenset(bulk)),
        ProbeFolder("Archive", (b"\\HasNoChildren", b"\\Archive"), messages=dict(bulk), bulk=frozenset(bulk)),
        ProbeFolder("Deleted Messages", (b"\\HasNoChildren", b"\\Trash"), messages=dict(bulk), bulk=frozenset(bulk)),
    ]
    return ProbeServer(folders, clock=FakeClock()), StableRef("icloud", seed)


def account_id(prefix: str, index: int) -> str:
    return f"{prefix}{index}" if index else prefix


def imap_factory(kind: Callable[[], str]) -> Callable[[str], ImapMailbox]:
    """A fresh fake server and mailbox per call (and a fresh folder map: the LIST is part of the call)."""

    def build(account: str) -> ImapMailbox:
        server, _ = thread_server(kind())
        clock = server.clock
        if clock is None:
            raise SystemExit("thread server needs a clock")
        return ImapMailbox(
            account,
            host="imap.example.test",
            port=993,
            folder="INBOX",
            username="u",
            password="p",
            client_factory=lambda host, port, context, timeout: server.client(),
            own_addresses=frozenset({OWNER}),
            folder_maps=FolderMapCache(clock=clock),
        )

    return build


GRAPH_ITEMS, GRAPH_LISTED, GRAPH_SEED = 51, 31, "AAMkMSG0051="
CAP = MAX_MESSAGE_RESPONSE_BYTES


@functools.cache
def graph_list(unique: bool) -> bytes:
    """The conversation list filled to just below the 2 MiB cap: 51 items, with `uniqueBody` for the 31 oldest (as
    HTML above the 64 KiB cut) or, without it, a long subject on every item."""

    def build(pairs: int) -> bytes:
        items = [
            thread_item(
                n,
                subject="Probe thread" + ("" if unique else PAIR * pairs),
                **(
                    {"uniqueBody": body_object(f"<p>{PAIR * pairs}</p>", "html")}
                    if unique and n <= GRAPH_LISTED
                    else {}
                ),
            )
            for n in range(1, GRAPH_ITEMS + 1)
        ]
        return json.dumps({"value": items}, ensure_ascii=False).encode()

    holders = GRAPH_LISTED if unique else GRAPH_ITEMS
    pairs = (CAP - 4096 - len(build(0))) // (len(PAIR.encode()) * holders)
    body = build(pairs)
    if len(body) > CAP:
        raise SystemExit("probe list body exceeds the 2 MiB cap")
    return body


@functools.cache
def graph_single(retry: bool) -> bytes:
    """One single read: `uniqueBody` and `body` of about 1 MiB each (the retry: `uniqueBody` alone, 2 MiB)."""
    pairs = (CAP - 4096) // (len(PAIR.encode()) * (1 if retry else 2))
    content = body_object(f"<p>{PAIR * pairs}</p>", "html")
    body = {"uniqueBody": content} if retry else {"uniqueBody": content, "body": content}
    return json.dumps(body, ensure_ascii=False).encode()


OVER_CAP = b"x" * (CAP + 1)


def thread_graph_transport() -> httpx2.MockTransport:
    """Folder ids (inbox only), the seed read, the conversation list (every second `uniqueBody` list refused as too
    large) and the single reads (every fourth `uniqueBody,body` read refused as too large)."""
    lists = itertools.count()

    def handle(request: httpx2.Request) -> httpx2.Response:
        path, select = request.url.path.removeprefix("/v1.0"), request.url.params.get("$select", "")
        if path.startswith("/me/mailFolders/"):
            found = path.endswith("/inbox")
            return httpx2.Response(200, json={"id": THREAD_INBOX_ID}) if found else httpx2.Response(404)
        if path == "/me/messages":
            if "uniqueBody" not in select:
                return httpx2.Response(200, content=graph_list(False))
            return httpx2.Response(200, content=OVER_CAP if next(lists) % 2 else graph_list(True))
        if path == f"/me/messages/{GRAPH_SEED}" and "conversationId" in select:
            seed = {"id": GRAPH_SEED, "conversationId": CONVERSATION, "parentFolderId": THREAD_INBOX_ID}
            return httpx2.Response(200, json=seed | {"isDraft": False, "receivedDateTime": "2026-10-20T06:51:00Z"})
        n = int(path.removeprefix("/me/messages/AAMkMSG")[:4])
        if select == "uniqueBody,body":
            return httpx2.Response(200, content=OVER_CAP if n % 4 == 0 else graph_single(False))
        if select == "uniqueBody":
            return httpx2.Response(200, content=graph_single(True))
        return httpx2.Response(404)

    return httpx2.MockTransport(handle)


async def thread_call(ctx: HubContext, args: argparse.Namespace, graph: bool, kind: str) -> list[str]:
    """`--accounts` accounts times `--parallel` concurrent get_thread calls; `--with-calendar` adds get_events."""
    notes: list[str] = []
    seed = "t30@example.test" if kind == "rounds" else "b0@example.test"

    async def thread(account: str) -> None:
        result = await run_get_thread(
            ctx,
            ref=None if graph else encode_ref("icloud", seed),
            message_id=encode_message_id(GraphMessageRef(account, GRAPH_SEED)) if graph else None,
            thread_key=None,
            account=None,
            max_chars_per_message=5000,
            include_trash=True,
        )
        omitted = sum(m.body_omitted for m in result.messages)
        notes.append(
            f"{account}: {len(result.messages)} messages, {result.summary.message_count} found "
            f"(exact {result.summary.message_count_exact}), truncated {result.truncated}, bodies omitted {omitted}"
        )

    async def events() -> None:
        notes.extend(await one_call(ctx, True, False))

    async with anyio.create_task_group() as tg:
        for i in range(args.accounts):
            for _ in range(args.parallel):
                tg.start_soon(thread, account_id("outlook" if graph else "icloud", i))
        if args.with_calendar:
            tg.start_soon(events)
    return notes


def thread_main(args: argparse.Namespace) -> None:
    graph = args.scenario == "thread-graph"
    kinds = itertools.cycle(("rounds", "subject"))
    kind = {"now": "rounds"}  # read by the mailbox factory: every call of a round runs the same thread
    ctx = context(
        args.accounts,
        args.with_calendar,
        args.calendars,
        graph=graph,
        imap=None if graph else imap_factory(lambda: kind["now"]),
        thread_graph=graph,
    )
    gc.collect()
    base = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (2**20 if sys.platform == "darwin" else 1024)
    print(f"baseline peak RSS {base:.0f} MiB (imports and fakes)", flush=True)
    for call in range(1, args.calls + 1):
        kind["now"] = next(kinds)
        cpu = time.process_time()
        started = time.monotonic()
        notes = asyncio.run(thread_call(ctx, args, graph, kind["now"]))
        gc.collect()
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (2**20 if sys.platform == "darwin" else 1024)
        print(
            f"call {call} ({'graph' if graph else kind['now']}): peak RSS {peak:.0f} MiB, "
            f"CPU {time.process_time() - cpu:.1f} s, wall {time.monotonic() - started:.1f} s: {notes[0]}",
            flush=True,
        )
    print(f"cgroup memory.peak {cgroup('memory.peak')}; memory.events {cgroup('memory.events')}", flush=True)


def cgroup(name: str) -> str:
    try:
        return Path("/sys/fs/cgroup", name).read_text().strip().replace("\n", ", ")
    except OSError:
        return "n/a"


async def one_call(ctx: HubContext, calendar: bool, graph: bool) -> list[str]:
    """One get_events and/or one get_message, concurrently; returns one summary per tool."""
    notes: list[str] = []

    async def events() -> None:
        result, _, _ = await run_get_events(
            ctx,
            start="2026-10-17T00:00:00+02:00",
            end="2026-11-16T23:00:00+01:00",
            account=None,
            timezone=None,
            limit=200,
        )
        notes.append(f"items {len(result.items)}, truncated {result.truncated}, skipped {result.skipped_objects}")

    async def message() -> None:
        result, _ = await run_get_message(
            ctx, message_id=encode_message_id(GraphMessageRef("outlook", GRAPH_ID)), max_chars=20000
        )
        notes.append(
            f"message body {len(result.untrusted.body)} chars, to {len(result.untrusted.to_addresses)}, "
            f"attachments {result.attachment_count}, body_truncated {result.body_truncated}"
        )

    async with anyio.create_task_group() as tg:
        if calendar:
            tg.start_soon(events)
        if graph:
            tg.start_soon(message)
    return notes


def status_worker(worker: tuple[FakeImapServer, list[StableRef]], notes: list[str], index: int) -> None:
    notes[index] = status_call(*worker)


def status_main(args: argparse.Namespace) -> None:
    """`--accounts` IMAP accounts times `--parallel` concurrent calls each, one worker thread per call with its own fake
    server; `--with-calendar` runs get_events for `--accounts` calendar accounts at the same time."""
    workers = [status_server() for _ in range(args.accounts * args.parallel)]
    ctx = context(args.accounts, args.with_calendar, args.calendars) if args.with_calendar else None
    gc.collect()
    base = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (2**20 if sys.platform == "darwin" else 1024)
    print(f"baseline peak RSS {base:.0f} MiB (imports and fakes)", flush=True)
    for call in range(1, args.calls + 1):
        cpu = time.process_time()
        notes = [""] * len(workers)
        threads = [threading.Thread(target=status_worker, args=(workers[i], notes, i)) for i in range(len(workers))]
        for thread in threads:
            thread.start()
        extra = asyncio.run(one_call(ctx, True, False)) if ctx else []
        for thread in threads:
            thread.join()
        gc.collect()
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (2**20 if sys.platform == "darwin" else 1024)
        print(
            f"call {call}: peak RSS {peak:.0f} MiB, CPU {time.process_time() - cpu:.1f} s, "
            f"{len(workers)} status workers: {notes[0]}" + "".join(f"; {n}" for n in extra),
            flush=True,
        )
    print(f"cgroup memory.peak {cgroup('memory.peak')}; memory.events {cgroup('memory.events')}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--scenario", choices=[*sorted(SCENARIOS), "graph-message", "status", "thread", "thread-graph"], required=True
    )
    parser.add_argument(
        "--with-calendar",
        choices=sorted(SCENARIOS),
        help="calendar scenario run next to graph-message, status, thread or thread-graph",
    )
    parser.add_argument("--accounts", type=int, default=1)
    parser.add_argument("--parallel", type=int, default=1, help="concurrent status or thread calls per account")
    parser.add_argument("--calendars", type=int, default=10)
    parser.add_argument("--calls", type=int, default=10)
    args = parser.parse_args()
    if args.scenario == "status":
        status_main(args)
        return
    if args.scenario in ("thread", "thread-graph"):
        thread_main(args)
        return
    graph = args.scenario == "graph-message"
    if args.with_calendar and not graph:
        parser.error("--with-calendar needs --scenario graph-message")
    calendar = args.with_calendar if graph else args.scenario
    ctx = context(args.accounts, calendar, args.calendars, graph=graph)
    for call in range(1, args.calls + 1):
        cpu = time.process_time()
        notes = asyncio.run(one_call(ctx, calendar is not None, graph))
        gc.collect()
        peak_mib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (2**20 if sys.platform == "darwin" else 1024)
        print(
            f"call {call}: peak RSS {peak_mib:.0f} MiB, CPU {time.process_time() - cpu:.1f} s, {'; '.join(notes)}",
            flush=True,
        )
    print(f"cgroup memory.peak {cgroup('memory.peak')}; memory.events {cgroup('memory.events')}", flush=True)


if __name__ == "__main__":
    main()
