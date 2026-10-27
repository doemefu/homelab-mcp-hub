"""D85 through the tool with the real adapters: `truncated` with `next_cursor: null` always comes with an
`incomplete_accounts` or `account_errors` entry. Every scenario drives a real `ImapMailbox` (fake server) or
`GraphMailbox` (mock transport) and must truncate, so the invariant is never checked against an empty case."""

from collections.abc import Callable
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

import mcp_hub.providers.imap as imap
from mcp_hub.providers import Adapters
from mcp_hub.providers.graph import GraphMailbox
from mcp_hub.providers.imap import ImapMailbox
from tests.support.clock import FakeClock
from tests.support.graph_fixtures import SENT_ID
from tests.support.imap_fake import FakeMessage
from tests.unit import test_imap_list_messages as lm
from tests.unit import test_imap_search_messages as ism
from tests.unit.test_graph_history import INBOX_ID, Server, mailbox, values
from tests.unit.test_graph_search import NOW as GRAPH_NOW
from tests.unit.test_graph_search import block, hit, searching, throttled_after
from tests.unit.test_search_messages_tool import Fake, context, incomplete, run

pytestmark = pytest.mark.anyio
Box = ImapMailbox | GraphMailbox
Scenario = Callable[[pytest.MonkeyPatch], tuple[Box, dict[str, object]]]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def imap_examine_no(_: pytest.MonkeyPatch) -> tuple[Box, dict[str, object]]:
    server = ism.three(inbox=[ism.hit(1)])
    server.fail["EXAMINE"] = server.no("EXAMINE")
    return lm.box(server), {"query": "rechnung"}


def imap_search_badcharset(_: pytest.MonkeyPatch) -> tuple[Box, dict[str, object]]:
    server = ism.three(inbox=[ism.hit(1, sender="Müller <m@example.org>")])
    server.badcharset = True
    return lm.box(server), {"query": "Müller"}


def imap_pass_one_fetch_no(_: pytest.MonkeyPatch) -> tuple[Box, dict[str, object]]:
    server = ism.three(inbox=[ism.hit(1), ism.hit(2, 6)])
    original = server.run

    def run_command(command: str, argument: object = None) -> None:
        if command == "UID FETCH" and argument[1] == lm.CANDIDATE:  # type: ignore[index]
            server.fail["UID FETCH"] = server.no("UID FETCH")
        original(command, argument)

    server.run = run_command  # type: ignore[method-assign]
    return lm.box(server), {"query": "rechnung", "folder": "inbox"}  # `all` also fetches the dedup headers


def imap_pass_two_fetch_no(_: pytest.MonkeyPatch) -> tuple[Box, dict[str, object]]:
    server = ism.three(inbox=[ism.hit(1)])
    original = server.run

    def run_command(command: str, argument: object = None) -> None:
        if command == "UID FETCH" and lm.SUMMARY_HEADER_ITEM in argument[1]:  # type: ignore[index]
            server.fail["UID FETCH"] = server.no("UID FETCH")
        original(command, argument)

    server.run = run_command  # type: ignore[method-assign]
    return lm.box(server), {"query": "rechnung"}


def imap_candidate_cap(monkeypatch: pytest.MonkeyPatch) -> tuple[Box, dict[str, object]]:
    monkeypatch.setattr(imap, "MAX_SEARCH_CANDIDATES", 1)
    inbox = [FakeMessage(u, ism.NOW - timedelta(minutes=u), headers=lm.hdr("Rechnung")) for u in (1, 2, 3)]
    return lm.box(ism.three(inbox=inbox)), {"query": "rechnung"}


def graph_full_answer(_: pytest.MonkeyPatch) -> tuple[Box, dict[str, object]]:
    hits = [hit(n, GRAPH_NOW - timedelta(minutes=n)) for n in range(1, 101)]
    return mailbox(searching(*hits), clock=FakeClock()), {"query": "budget", "folder": "inbox"}


def graph_stuck_second(_: pytest.MonkeyPatch) -> tuple[Box, dict[str, object]]:
    same = [hit(n, GRAPH_NOW) for n in range(101)]
    server = Server({INBOX_ID: values(*same)})
    return mailbox(server, clock=FakeClock()), {"sender": "x", "folder": "inbox"}


def graph_throttled_read_on(_: pytest.MonkeyPatch) -> tuple[Box, dict[str, object]]:
    blocks = {
        INBOX_ID: [block(GRAPH_NOW - timedelta(hours=2 * k), 3, k) for k in range(4)],
        SENT_ID: [block(GRAPH_NOW - timedelta(hours=2 * k), 3, k + 10) for k in range(4)],
    }
    server, clock = throttled_after(2, blocks)
    return mailbox(server, clock=clock), {"sender": "rare person", "folder": "all"}


# name -> (scenario, account, reasons, a cursor is issued)
SCENARIOS: dict[str, tuple[Scenario, str, set[str], bool]] = {
    "imap-examine-no": (imap_examine_no, "icloud", {"search_refused"}, False),
    "imap-badcharset": (imap_search_badcharset, "icloud", {"search_refused"}, False),
    "imap-pass-one-fetch-no": (imap_pass_one_fetch_no, "icloud", {"changed_during_call"}, False),
    "imap-pass-two-fetch-no": (imap_pass_two_fetch_no, "icloud", {"changed_during_call"}, False),
    "imap-candidate-cap": (imap_candidate_cap, "icloud", {"result_cap"}, False),
    "graph-full-answer": (graph_full_answer, "outlook", {"result_cap"}, True),
    "graph-stuck-second": (graph_stuck_second, "outlook", {"result_cap"}, False),
    "graph-throttled-read-on": (graph_throttled_read_on, "outlook", {"time_limit"}, False),
}


@pytest.mark.parametrize("name", sorted(SCENARIOS))
async def test_truncated_without_cursor_is_explained_by_the_real_adapters(
    secrets_dir: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    scenario, account, reasons, cursor = SCENARIOS[name]
    box, arguments = scenario(monkeypatch)
    ctx = context(secrets_dir, Fake())
    ctx = replace(ctx, adapters=Adapters(mailbox=lambda target, _dir: box))
    result, _, _ = await run(ctx, account=account, **arguments)
    assert result.truncated, name  # every scenario truncates: the invariant is never checked on an empty case
    assert {reason for who, reason in incomplete(result) if who == account} == reasons
    assert (result.next_cursor is not None) == cursor
    assert result.next_cursor is not None or result.incomplete_accounts or result.account_errors
