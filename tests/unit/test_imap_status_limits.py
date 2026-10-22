"""IMAP `get_message_status` with 50 refs (spec 080 §5.4; WP13b T11): command counts, OR-tree sizes and the soft stop.

Expected counts (Design A, T0 rulings: no Gmail header reply search, Gmail replies from thread members):
- iCloud-like, 50 refs all in INBOX: the location step searches each core folder (INBOX, Archive, Trash, Sent) in
  ceil(50 / 20) = 3 batches, so 12 searches; the `other` mailboxes stay untouched because every ref is resolved by
  then. The reply step searches only `Sent` in ceil(50 / 10) = 5 batches. The thread step searches INBOX, Archive and
  Trash (the reply mailbox is done) in 5 batches each, so 15. EXAMINE: 4 for the location, plus 3 for the thread step,
  because the location left `Sent` selected and the reply step needs no EXAMINE.
- Gmail-like, 50 distinct thread ids: the location searches Trash and All Mail (3 batches each, 2 EXAMINEs); the
  reply and thread steps run in the still-selected All Mail in ceil(50 / 20) = 3 thread-id batches and send no
  header search."""

from collections.abc import Sequence

from mcp_hub.ids import StableRef
from mcp_hub.providers.base import MessageStatus
from mcp_hub.providers.imap_search import LOCATE_BATCH_VALUES, LOOKUP_BATCH_VALUES, MAX_OR_BYTES, MAX_THRID_VALUES
from tests.support.imap_fake import FakeImapServer
from tests.unit.test_imap_get_message import GMAIL_ALL, INBOX_L, clock_of, examined, gmail, msg, searches
from tests.unit.test_imap_status import ICLOUD, run, server_with

REFS = 50
SOFT, HARD = 16.0, 20.0


def ids(count: int = REFS, length: int = 0) -> list[str]:
    """Distinct ids; with `length`, each is exactly that long (300 is the longest id the hub accepts)."""
    tail = "@example.test"
    return [f"r{i:02d}-".ljust(length - len(tail), "x") + tail for i in range(count)]


def inbox_server(message_ids: Sequence[str], step: float = 0.0) -> FakeImapServer:
    return server_with(inbox=[msg(i + 1, 200, "x", message_id=f"<{x}>") for i, x in enumerate(message_ids)], step=step)


def keys(command: object) -> int:
    assert isinstance(command, bytes)
    return command.count(b"HEADER")


def split(server: FakeImapServer) -> tuple[list[bytes], list[bytes], list[bytes]]:
    """(location, reply, thread) SEARCH arguments of an iCloud-like run. The location searches carry the Message-ID
    key; the reply step runs right after them in the mailbox they left selected (no EXAMINE in between); every later
    SEARCH belongs to the thread step."""
    location: list[bytes] = []
    reply: list[bytes] = []
    thread: list[bytes] = []
    reply_open = False
    for name, argument in server.commands:
        if name == "EXAMINE":
            reply_open = False
        elif name == "UID SEARCH":
            assert isinstance(argument, bytes)
            if b"HEADER Message-ID" in argument:
                location.append(argument)
                reply_open = True
            elif reply_open:
                reply.append(argument)
            else:
                thread.append(argument)
    return location, reply, thread


def targets(message_ids: Sequence[str]) -> list[StableRef]:
    return [StableRef(ICLOUD, x) for x in message_ids]


def test_fifty_refs_batch_counts() -> None:
    server = inbox_server(ids())
    statuses = run(server, targets(ids()))
    assert [s.thread_checked for s in statuses] == [True] * REFS
    location, reply, thread = split(server)
    assert (len(location), len(reply), len(thread)) == (12, 5, 15)
    assert all(keys(c) <= LOCATE_BATCH_VALUES for c in location)
    assert all(keys(c) <= 2 * LOOKUP_BATCH_VALUES for c in reply + thread)  # 10 ids = 20 keys
    assert all(len(c) <= MAX_OR_BYTES for c in location + reply + thread)
    assert len(examined(server)) == 7


def test_long_message_ids_split_by_bytes() -> None:
    long_ids = ids(length=300)
    assert all(len(x) == 300 for x in long_ids)
    server = inbox_server(long_ids)
    run(server, targets(long_ids))
    location, reply, thread = split(server)
    assert len(location) > 12
    assert len(reply) > 5
    assert len(thread) > 15
    assert all(len(c) <= MAX_OR_BYTES for c in location + reply + thread)


def timed(step: float) -> tuple[FakeImapServer, list[MessageStatus], list[tuple[str, float]]]:
    server = inbox_server(ids(), step=step)
    clock = clock_of(server)
    started = clock()
    original = server.run
    log: list[tuple[str, float]] = []

    def record(command: str, argument: object = None) -> None:
        log.append((command, clock() - started))
        original(command, argument)

    server.run = record  # type: ignore[method-assign]
    return server, run(server, targets(ids()), soft=SOFT, hard=HARD), log


def test_fifty_refs_finish_before_soft_stop() -> None:
    """At 0.15 s per command the 45 commands of the run take about 7 s of fake time, far below the soft stop."""
    server, statuses, _ = timed(0.15)
    assert [s.thread_checked for s in statuses] == [True] * REFS
    assert clock_of(server)() - 1000.0 < SOFT


def test_fifty_refs_at_icloud_latency_degrade_without_account_error() -> None:
    """At 0.4 s per command the lookups cannot all run: the soft stop at 16 s leaves the tail unchecked, no refs are
    lost, and nothing but LOGOUT is sent after the stop."""
    server, statuses, log = timed(0.4)
    assert [s.state for s in statuses] == ["found"] * REFS
    checked = [s.thread_checked for s in statuses]
    assert set(checked) == {False, True}
    after = [name for name, at in log if at >= SOFT]
    assert after == ["LOGOUT"]
    assert clock_of(server)() - 1000.0 <= HARD


def test_gmail_fifty_refs_thread_batches() -> None:
    """Gmail: 50 distinct thread ids give 3 thread SEARCHes (20, 20, 10 keys) in All Mail and no header reply search;
    EXAMINE: Trash and All Mail only."""
    xs = ids()
    server = gmail(
        all_mail=[msg(i + 1, 200, "x", INBOX_L, thrid=7000 + i, message_id=f"<{x}>") for i, x in enumerate(xs)]
    )
    statuses = run(server, [StableRef("gmail", x) for x in xs])
    assert [s.thread_checked for s in statuses] == [True] * REFS
    thread = [c for c in searches(server) if b"X-GM-THRID" in c]  # type: ignore[operator]
    assert [c.count(b"X-GM-THRID") for c in thread] == [MAX_THRID_VALUES, MAX_THRID_VALUES, 10]  # type: ignore[attr-defined]
    assert all(len(c) <= MAX_OR_BYTES for c in thread)  # type: ignore[arg-type]
    assert not [c for c in searches(server) if b"In-Reply-To" in c or b"References" in c]  # type: ignore[operator]
    assert examined(server) == ["[Gmail]/Trash", GMAIL_ALL]
