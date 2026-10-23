"""IMAP `get_thread` caps, EXAMINE counts, key-search outcomes, soft-stop timing and log hygiene (spec 080 §5.2
`get_thread`, §5.4, §6.5, §9.7; WP13c T10, Design A, IR3, P11, P15, sec-G8).

Expected EXAMINE counts (Design A: a mailbox still selected is not examined again):
- iCloud-like `ref`: the location examines the four core folders (4); the seed is read in its mailbox when the
  location left another one selected (1); round (1) starts in the selected mailbox (3); each round-(2) iteration
  starts in the mailbox the last pass ended in (3); the body pass examines one mailbox per group (here 2).
- Gmail-like `ref` with `include_trash`: the location examines Trash and All Mail (2); the X-GM-THRID search runs in
  the still selected All Mail, then in Trash (1); the body pass goes back to All Mail (1).
- A `thread_key` has no location: a `headers` key starts with round (1) in four folders, a `gmail` key with All Mail.
"""

import json
import logging
from collections.abc import Iterator

import pytest

import mcp_hub.providers.imap_thread as imap_thread
from mcp_hub.ids import ThreadKey
from mcp_hub.providers.base import MAX_THREAD_MESSAGES, ProviderError, ThreadPage, log_provider_failure
from mcp_hub.providers.deadline import SOFT_STOP_SECONDS
from mcp_hub.providers.imap_thread import BODY_RESERVE_SECONDS, MAX_DEGRADED_LOGS
from tests.support.clock import FakeClock
from tests.support.imap_fake import FakeFolder, FakeImapServer, FakeMessage
from tests.support.imap_threads import (
    GMAIL_ALL,
    GMAIL_TRASH,
    INBOX_L,
    THREAD,
    clock_of,
    deadline,
    examined,
    folder,
    gmail,
    gmail_key,
    icloud,
    mid_ref,
    msg,
    ref,
    searches,
    stop_after,
    thread,
)
from tests.support.logcapture import Capture
from tests.support.logfields import allowed_fields

HARD = 20.0
CORE = ("INBOX", "Sent Messages", "Archive", "Deleted Messages")


@pytest.fixture
def hub_log() -> Iterator[Capture]:
    handler = Capture()
    logger = logging.getLogger("mcp_hub")
    logger.addHandler(handler)
    level = logger.level
    logger.setLevel(logging.DEBUG)
    yield handler
    logger.removeHandler(handler)
    logger.setLevel(level)


def exact(page: ThreadPage) -> bool:
    assert page.overview is not None
    return page.overview.message_count_exact


def count(page: ThreadPage) -> int:
    assert page.overview is not None
    return page.overview.message_count


def ids(page: ThreadPage) -> list[str | None]:
    return [member.summary.meta.message_id for member in page.members]


def a_thread() -> dict[str, list[FakeMessage]]:
    """R in INBOX, the owner's reply A in Sent, B (a reply to A) in INBOX."""
    return {
        "inbox": [
            msg(1, 300, "r", message_id="<r@example.test>"),
            msg(
                3,
                100,
                "Re: r",
                message_id="<b@example.test>",
                in_reply_to="<a@example.test>",
                references="<r@example.test> <a@example.test>",
            ),
        ],
        "sent": [
            msg(
                2,
                200,
                "Re: r",
                message_id="<a@example.test>",
                in_reply_to="<r@example.test>",
                references="<r@example.test>",
            )
        ],
    }


def a_gmail_thread() -> dict[str, list[FakeMessage]]:
    return {
        "all_mail": [
            msg(1, 300, "r", INBOX_L, thrid=THREAD, message_id="<r@example.test>"),
            msg(2, 200, "Re: r", b"\\Sent", thrid=THREAD, message_id="<a@example.test>"),
            msg(3, 100, "Re: r", INBOX_L, thrid=THREAD, message_id="<b@example.test>"),
        ]
    }


# --- EXAMINE counts (Design A) ---------------------------------------------------------------------------------


def test_examine_counts_design_a() -> None:
    """iCloud `ref`: at most 4 + 1 + 3 + 3 + body groups; Gmail `ref` with `include_trash`: locate 2 + Trash 1 + All
    Mail at most 1; a `thread_key` has no location."""
    server = icloud(**a_thread())
    page = thread(server, seed=ref("b@example.test", server))
    assert ids(page) == ["r@example.test", "a@example.test", "b@example.test"]
    locate, seed, round_one, round_two, body = 4, 1, 3, 3, 2
    assert len(examined(server)) == locate + seed + round_one + round_two + body == 13
    assert examined(server) == [
        "INBOX",
        "Archive",
        "Deleted Messages",
        "Sent Messages",  # the location
        "INBOX",  # the seed, read where the location found it
        "Sent Messages",
        "Archive",
        "Deleted Messages",  # round (1), started in the selected INBOX
        "INBOX",
        "Sent Messages",
        "Archive",  # round (2), iteration 1, started in the selected Deleted Messages
        "INBOX",
        "Sent Messages",  # the body pass: the group with the newest member first
    ]

    server = icloud(**a_thread())
    thread(server, key=ThreadKey("icloud", "headers", "r@example.test"))
    assert len(examined(server)) == 4 + 3 + 2  # round (1), round (2), body groups: no location, no seed read
    assert b"HEADER References" in searches(server)[0]  # the first search is round (1), not a location

    server = gmail(**a_gmail_thread())
    page = thread(server, seed=ref("b@example.test", server), include_trash=True)
    assert ids(page) == ["r@example.test", "a@example.test", "b@example.test"]
    assert examined(server) == [GMAIL_TRASH, GMAIL_ALL, GMAIL_TRASH, GMAIL_ALL]  # locate 2, Trash 1, All Mail 1

    server = gmail(**a_gmail_thread())
    thread(server, key=gmail_key(), include_trash=True)
    assert examined(server) == [GMAIL_ALL, GMAIL_TRASH, GMAIL_ALL]  # no location

    trashed = msg(5, 500, "r", thrid=THREAD, message_id="<t@example.test>")
    server = gmail(**a_gmail_thread(), trash=[trashed])
    page = thread(server, seed=mid_ref(server, GMAIL_TRASH, 5, 17), include_trash=True)
    assert ids(page) == ["t@example.test", "r@example.test", "a@example.test", "b@example.test"]
    # the seed read in Trash; the X-GM-THRID search in the still selected Trash, then All Mail; the body pass in the
    # still selected All Mail (newest group), then Trash
    assert examined(server) == [GMAIL_TRASH, GMAIL_ALL, GMAIL_TRASH]


# --- the 50-message cap ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("provider", ["icloud", "gmail"])
def test_fifty_members_kept_count_all(provider: str) -> None:
    """§5.2: 120 members found → the newest 50 are returned, `message_count` 120 and exact, `truncated` (P11: the cap
    sets `truncated`, not `message_count_exact`)."""
    root = msg(1, 1000, "root", INBOX_L, thrid=THREAD, message_id="<m0@example.test>")
    replies = [
        msg(
            1 + i,
            1000 - i,
            f"m{i}",
            INBOX_L,
            thrid=THREAD,
            message_id=f"<m{i}@example.test>",
            in_reply_to="<m0@example.test>",
            references="<m0@example.test>",
        )
        for i in range(1, 120)
    ]
    if provider == "icloud":
        server = icloud(inbox=[root, *replies])
        page = thread(server, key=ThreadKey("icloud", "headers", "m0@example.test"))
    else:
        server = gmail(all_mail=[root, *replies])
        page = thread(server, key=gmail_key())
    assert count(page) == 120
    assert exact(page) is True
    assert page.truncated is True
    assert len(page.members) == MAX_THREAD_MESSAGES == 50
    assert ids(page) == [f"m{i}@example.test" for i in range(70, 120)]  # the newest 50, oldest first
    assert all(not member.body.omitted for member in page.members)


# --- key searches: not_found, upstream_timeout or partial (IR3) ------------------------------------------------


def _fail_first_search_in(server: FakeImapServer, box: str) -> None:
    original = server.run
    state: dict[str, object] = {"box": None, "done": False}

    def run(name: str, argument: object = None) -> None:
        if name == "EXAMINE":
            state["box"] = argument
        if not state["done"] and name == "UID SEARCH" and state["box"] == box:
            state["done"] = True
            server.commands.append((name, argument))
            raise server.no(name)
        original(name, argument)

    server.run = run  # type: ignore[method-assign]


@pytest.mark.parametrize("source", ["headers", "gmail"])
@pytest.mark.parametrize("case", ["nothing, incomplete", "members, incomplete", "nothing, complete"])
def test_key_search_incomplete_is_upstream_timeout(source: str, case: str) -> None:
    """Q6 as refined by IR3: a key search that found nothing and did not complete → `upstream_timeout`; one that found
    members but did not complete → the members, `truncated`, not exact; a complete one that found nothing →
    `not_found`."""
    found = case.startswith("members")
    if source == "headers":
        key = ThreadKey("icloud", "headers", "r@example.test")
        members = a_thread() if found else {"inbox": [msg(9, 50, "other", message_id="<o@example.test>")]}
        server = icloud(**members)
        broken = "Archive"
    else:
        key = gmail_key()
        members = a_gmail_thread() if found else {"all_mail": [msg(9, 50, "other", thrid=THREAD + 1)]}
        server = gmail(**members)
        broken = GMAIL_TRASH
    if case.endswith("incomplete"):
        _fail_first_search_in(server, broken)
    if found:
        page = thread(server, key=key)
        assert ids(page) == ["r@example.test", "a@example.test", "b@example.test"]
        assert page.truncated is True
        assert exact(page) is False
        return
    with pytest.raises(ProviderError) as caught:
        thread(server, key=key)
    assert caught.value.code == ("upstream_timeout" if case.endswith("incomplete") else "not_found")


# --- iCloud latency ---------------------------------------------------------------------------------------------

LATENCY = {"EXAMINE": 0.2, "UID SEARCH": 0.6, "UID FETCH": 0.2}  # T0 probe: EXAMINE, SEARCH; a FETCH as an EXAMINE


def _timed(server: FakeImapServer) -> list[tuple[str, float]]:
    """Each command costs its iCloud latency on the server's clock; returns (command, start offset) per command."""
    clock = clock_of(server)
    started = clock()
    original = server.run
    log: list[tuple[str, float]] = []

    def run(name: str, argument: object = None) -> None:
        log.append((name, clock() - started))
        clock.advance(LATENCY.get(name, 0.0))
        original(name, argument)

    server.run = run  # type: ignore[method-assign]
    return log


def _chain(length: int) -> list[FakeMessage]:
    """c0 ← c1 ← … : each message names only its parent, in In-Reply-To (no References), so every link needs a
    round-(2) iteration."""
    out = [msg(1, 1000, "c0", message_id="<c0@example.test>")]
    for i in range(1, length):
        out.append(
            msg(1 + i, 1000 - i, f"c{i}", message_id=f"<c{i}@example.test>", in_reply_to=f"<c{i - 1}@example.test>")
        )
    return out


def test_icloud_latency_soft_stop_partial() -> None:
    """At iCloud latencies (0.2 s EXAMINE and FETCH, 0.6 s SEARCH) a `ref` whose chain needs three round-(2)
    iterations cannot search everything: the reserve stops the rounds, the result is partial (truncated, not exact)
    with the members found so far and their bodies, never an account error; no command but LOGOUT is sent after the
    soft stop and the call ends before the hard deadline."""
    server = icloud(inbox=_chain(5))
    log = _timed(server)
    page = thread(server, seed=ref("c0@example.test", server), call=deadline(server, soft=SOFT_STOP_SECONDS, hard=HARD))
    assert page.outcome == "ok"
    assert page.truncated is True
    assert exact(page) is False
    assert ids(page)[0] == "c0@example.test"
    assert 1 < len(page.members) < 5
    assert all(not member.body.omitted for member in page.members)  # the reserve left the body pass its time
    searched_until = max(at for name, at in log if name == "UID SEARCH")
    assert searched_until <= BODY_RESERVE_SECONDS  # no member search starts after the reserve (G2)
    assert [name for name, at in log if at >= SOFT_STOP_SECONDS] in ([], ["LOGOUT"])
    assert log[-1][1] < HARD


def test_icloud_latency_fifty_members_newest_bodies_first() -> None:
    """Rev 4.13 (T14): at iCloud latencies a thread of 50 members spends the 12 s reserve on its searches; the body
    pass then reads the newest slices first, so the newest body is read and a soft stop omits only older bodies
    (truncated), never an account error."""
    replies = [
        msg(
            1 + i,
            1000 - i,
            f"m{i}",
            message_id=f"<m{i}@example.test>",
            in_reply_to="<m0@example.test>",
            references="<m0@example.test>",
        )
        for i in range(1, 50)
    ]
    server = icloud(inbox=[msg(1, 1000, "m0", message_id="<m0@example.test>"), *replies])
    log = _timed(server)
    page = thread(
        server, seed=ref("m49@example.test", server), call=deadline(server, soft=SOFT_STOP_SECONDS, hard=HARD)
    )
    assert page.outcome == "ok"
    assert len(page.members) == 50
    omitted = [member.body.omitted for member in page.members]  # oldest first
    assert omitted[-1] is False  # the newest body is read
    assert omitted[0] is True  # the oldest was not reached before the soft stop
    assert omitted == sorted(omitted, reverse=True)  # only older bodies are missing
    assert page.truncated is True
    assert [name for name, at in log if at >= SOFT_STOP_SECONDS] in ([], ["LOGOUT"])
    assert log[-1][1] < HARD


# --- logs -----------------------------------------------------------------------------------------------------


def test_degraded_log_cap_twenty(hub_log: Capture) -> None:
    """sec-G8: at most MAX_DEGRADED_LOGS item_degraded lines per call, over every path (here 30 round hits without
    INTERNALDATE, Q9); the count is not exact."""
    undated = [
        msg(10 + i, None, f"u{i}", message_id=f"<u{i}@example.test>", in_reply_to="<r@example.test>") for i in range(30)
    ]
    server = icloud(inbox=[msg(1, 300, "r", message_id="<r@example.test>"), *undated])
    page = thread(server, seed=ref("r@example.test", server))
    assert ids(page) == ["r@example.test"]
    assert exact(page) is False
    assert page.truncated is False  # P11: a row without INTERNALDATE leaves the count inexact only
    degraded = [line for line in hub_log.lines if json.loads(line)["event"] == "item_degraded"]
    assert len(degraded) == MAX_DEGRADED_LOGS == 20
    assert {json.loads(line)["exception"] for line in degraded} == {"MissingInternalDate"}


LEAK_MID = "leak-canary-0001@example.test"
LEAK_SUBJECT = "Leak-Subject-Canary"
LEAK_FOLDER = "Leak-Archive-Name"
LEAK_ADDRESS = "leak-sender@example.test"
LEAK_BODY = "Leak-Body-Canary"


def _leaky_server() -> FakeImapServer:
    """An iCloud-like account whose archive has a hostile name and whose thread carries canary values."""
    sender = f"Leak <{LEAK_ADDRESS}>"
    root = msg(1, 300, LEAK_SUBJECT, sender=sender, message_id=f"<{LEAK_MID}>", text=LEAK_BODY.encode())
    reply = msg(
        2,
        200,
        f"Re: {LEAK_SUBJECT}",
        sender=sender,
        message_id="<reply@example.test>",
        in_reply_to=f"<{LEAK_MID}>",
        text=LEAK_BODY.encode(),
    )
    undated = msg(
        3, None, LEAK_SUBJECT, sender=sender, message_id="<undated@example.test>", in_reply_to=f"<{LEAK_MID}>"
    )
    folders: list[FakeFolder] = [
        folder("INBOX", (), [root]),
        folder("Sent Messages", (b"\\Sent",)),
        folder(LEAK_FOLDER, (b"\\Archive",), [reply, undated]),
        folder("Deleted Messages", (b"\\Trash",)),
    ]
    return FakeImapServer(folders, clock=FakeClock())


def test_imap_thread_logs_have_no_thread_values(hub_log: Capture, monkeypatch: pytest.MonkeyPatch) -> None:
    """P15, §9.7: over the item_degraded paths (a member row without INTERNALDATE, a body that fails to decode with
    an exception text full of canaries) and the failure paths (`not_found`, `upstream_timeout`, BAD, a soft stop), the
    hub logs only allowed fields and no Message-ID, subject, folder name, address or body. The canaries do occur in
    the scenario: in the result and in the exception text."""
    decode = imap_thread.decode_part

    def leaky_decode(data: bytes, part: object) -> str:
        if LEAK_BODY.encode() in data:
            raise ValueError(f"{LEAK_MID} {LEAK_SUBJECT} {LEAK_FOLDER} {LEAK_ADDRESS} {LEAK_BODY}")
        return decode(data, part)  # type: ignore[arg-type]

    monkeypatch.setattr(imap_thread, "decode_part", leaky_decode)
    server = _leaky_server()
    page = thread(server, seed=ref(LEAK_MID, server))
    assert ids(page) == [LEAK_MID, "reply@example.test"]
    assert page.members[0].summary.subject == LEAK_SUBJECT  # the canaries are in the result
    assert page.members[1].summary.meta.folder_name == LEAK_FOLDER
    assert page.members[0].summary.from_address == LEAK_ADDRESS
    assert all(member.body.omitted for member in page.members)  # both bodies failed with the canary text
    with pytest.raises(ValueError, match=LEAK_SUBJECT):
        leaky_decode(LEAK_BODY.encode(), None)

    failures: list[ProviderError] = []
    for build, call in (
        (_leaky_server, lambda s: thread(s, seed=ref("absent@example.test", s))),  # not_found
        (_leaky_server, lambda s: thread(s, key=ThreadKey("icloud", "headers", "absent@example.test"))),
        (_leaky_server, lambda s: thread(s, seed=mid_ref(s, LEAK_FOLDER, 9, 7))),  # unknown UID
    ):
        try:
            call(build())
        except ProviderError as error:
            failures.append(error)
    server = _leaky_server()
    server.fail["UID SEARCH"] = server.bad("UID SEARCH")
    with pytest.raises(ProviderError) as caught:
        thread(server, seed=ref(LEAK_MID, server))
    failures.append(caught.value)
    server = _leaky_server()
    _fail_first_search_in(server, "INBOX")
    with pytest.raises(ProviderError) as caught:
        thread(server, seed=ref(LEAK_MID, server))
    failures.append(caught.value)  # the location is incomplete: upstream_timeout
    server = _leaky_server()
    stop_after(server, 14)  # a soft stop right after the seed read: the seed alone, partial
    page = thread(server, seed=ref(LEAK_MID, server))
    assert page.truncated is True
    assert {error.code for error in failures} == {"not_found", "upstream_error", "upstream_timeout"}
    for error in failures:  # what the tool layer logs for a failed provider call
        log_provider_failure("icloud", "mail", error)

    events = [json.loads(line)["event"] for line in hub_log.lines]
    assert "item_degraded" in events
    assert "provider_call_failed" in events
    for line in hub_log.lines:
        record = json.loads(line)
        assert set(record) <= allowed_fields(record["event"])
    text = "\n".join(hub_log.lines) + "\n".join(message for _, _, message in hub_log.raw)
    for value in (LEAK_MID, LEAK_SUBJECT, LEAK_FOLDER, LEAK_ADDRESS, LEAK_BODY, "reply@example.test"):
        assert value not in text
