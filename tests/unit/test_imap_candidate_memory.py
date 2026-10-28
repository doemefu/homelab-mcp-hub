"""Pass 1 holds no full row list and keeps a bounded From per candidate (WP13d T8 reductions)."""

import gc
import logging
import weakref
from collections.abc import Iterator
from datetime import timedelta

import pytest

import mcp_hub.providers.imap as imap
from mcp_hub.providers.imap import Candidate
from mcp_hub.providers.imap_session import ImapSession
from tests.support.imap_fake import FakeImapClient, FakeImapServer, FakeMessage
from tests.support.logcapture import Capture
from tests.unit import test_imap_list_messages as lm
from tests.unit.test_imap_locate import _session, icloud, msg
from tests.unit.test_imap_search_messages import hit, squery, three


@pytest.fixture
def provider_log() -> Iterator[Capture]:
    handler = Capture()
    logger = logging.getLogger("mcp_hub.providers")
    logger.addHandler(handler)
    yield handler
    logger.removeHandler(handler)


def test_candidates_are_reduced_chunk_by_chunk(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each chunk of rows becomes `Candidate`s before the next chunk is fetched."""
    events: list[str] = []
    original = ImapSession.fetch_chunks

    def fetch_chunks(
        self: ImapSession, uids: object, items: object, **kw: object
    ) -> Iterator[dict[int, dict[bytes, object]]]:
        for number, rows in enumerate(original(self, uids, items, **kw)):  # type: ignore[arg-type]
            events.append(f"chunk{number}")
            yield rows

    real_aware = imap._aware
    monkeypatch.setattr(ImapSession, "fetch_chunks", fetch_chunks)
    monkeypatch.setattr(imap, "_aware", lambda value: (events.append("reduce"), real_aware(value))[1])
    inbox = [FakeMessage(u, lm.NOW - timedelta(seconds=2000 - u), headers=lm.hdr(f"m{u}")) for u in range(1, 1202)]
    server = lm.icloud(inbox=inbox)
    lm.run(server, lm.query(limit=1))
    assert [e for e in events if e.startswith("chunk")] == ["chunk0", "chunk1", "chunk2"]
    first_reduce_after_chunk1 = events.index("chunk1")
    assert events[:first_reduce_after_chunk1].count("reduce") == 500
    assert events.count("reduce") == 1201


def test_candidates_never_use_the_accumulating_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("pass 1 must use fetch_chunks")

    original = ImapSession.fetch

    def fetch(self: ImapSession, uids: object, items: object, **kw: object) -> dict[int, dict[bytes, object]]:
        if "BODYSTRUCTURE" not in items:  # type: ignore[operator]  # the candidate FETCH, not the summary one
            refuse()
        return original(self, uids, items, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(ImapSession, "fetch", fetch)
    page = lm.run(lm.icloud(inbox=[lm.msg(1, 5, "a")]), lm.query())
    assert [i.ref.uid for i in page.items] == [1]


def captured(monkeypatch: pytest.MonkeyPatch) -> list[Candidate]:
    seen: list[Candidate] = []
    original = imap.ImapMailbox._choose

    def choose(self: imap.ImapMailbox, candidates: list[Candidate], *args: object) -> object:
        seen.extend(candidates)
        return original(self, candidates, *args)  # type: ignore[arg-type]

    monkeypatch.setattr(imap.ImapMailbox, "_choose", choose)
    return seen


def test_candidate_from_is_capped_at_320_characters(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = captured(monkeypatch)
    hostile = "a" * 1500 + "@example.org"
    server = lm.icloud(inbox=[lm.msg(1, 5, "x", sender=f"<{hostile}>")])
    page = lm.run(server, lm.query("all"))
    assert [i.ref.uid for i in page.items] == [1]
    [candidate] = seen
    assert candidate.from_address == hostile[:320]


def test_candidate_from_under_the_cap_is_kept_whole_and_still_dedups() -> None:
    address = "b" * 290 + "@example.org"  # 302 characters
    mid = "<m1@example.org>"
    server = three(
        inbox=[hit(1, message_id=mid, sender=f"<{address}>")],
        sent=[lm.msg(21, 6, "Rechnung", sender=f"<{address}>", message_id=mid)],
    )
    page = lm.run(server, squery(folder="all"))
    assert [(i.ref.uid, i.meta.folder) for i in page.items] == [(21, "sent")]  # the sent copy replaces (M3)


def test_fetch_chunks_checks_the_soft_limit_before_each_chunk_after_the_first() -> None:
    server = icloud(inbox=[msg(uid, 5, "m") for uid in range(1, 6)])
    calls: list[int] = []
    session = _session(server)
    fetched = 0
    chunks = session.fetch_chunks([1, 2, 3, 4, 5], ["FLAGS"], chunk=2, soft_check=lambda: calls.append(fetched))
    for _ in chunks:
        fetched += 1
    assert calls == [1, 2]  # before chunk 2 and chunk 3, not before the first

    stopping = session.fetch_chunks(
        [1, 2, 3], ["FLAGS"], chunk=1, soft_check=lambda: (_ for _ in ()).throw(imap.SoftStop())
    )
    next(stopping)
    before = len([c for c in server.commands if c[0] == "UID FETCH"])
    with pytest.raises(imap.SoftStop):
        next(stopping)
    assert len([c for c in server.commands if c[0] == "UID FETCH"]) == before  # no command after the stop


class _Tracked(dict[int, dict[bytes, object]]):
    """A rows dict that can be weakly referenced."""


def test_pass_one_keeps_at_most_one_chunk_of_rows_alive(monkeypatch: pytest.MonkeyPatch) -> None:
    alive: list[int] = []
    refs: list[weakref.ref[_Tracked]] = []
    original = ImapSession.fetch_chunks

    def fetch_chunks(
        self: ImapSession, uids: object, items: object, **kw: object
    ) -> Iterator[dict[int, dict[bytes, object]]]:
        for plain in original(self, uids, items, **kw):  # type: ignore[arg-type]
            gc.collect()
            alive.append(sum(1 for ref in refs if ref() is not None))  # earlier chunks still referenced
            tracked = _Tracked(plain)
            refs.append(weakref.ref(tracked))
            del plain
            yield tracked
            del tracked

    monkeypatch.setattr(ImapSession, "fetch_chunks", fetch_chunks)
    inbox = [FakeMessage(u, lm.NOW - timedelta(seconds=2000 - u), headers=lm.hdr(f"m{u}")) for u in range(1, 1202)]
    lm.run(lm.icloud(inbox=inbox), lm.query(limit=1))
    assert len(alive) == 3
    assert alive == [0, 0, 0]


def chunked_hits() -> FakeImapServer:
    return lm.icloud(
        inbox=[FakeMessage(u, lm.NOW - timedelta(seconds=2000 - u), headers=lm.hdr("Rechnung")) for u in range(1, 1202)]
    )


def lose_in_second_candidate_chunk(server: FakeImapServer, kind: str) -> None:
    original = server.run
    seen = {"n": 0}

    def run_command(command: str, argument: object = None) -> None:
        if command == "UID FETCH" and argument[1] == lm.CANDIDATE:  # type: ignore[index]
            seen["n"] += 1
            if seen["n"] == 2:
                if kind == "chunk_no":
                    server.fail["UID FETCH"] = server.no("UID FETCH")
                else:
                    del server.folders[0].messages[777]
        original(command, argument)

    server.run = run_command  # type: ignore[method-assign]


@pytest.mark.parametrize("kind", ["chunk_no", "missing_uid"])
def test_search_second_candidate_chunk_loss_is_changed_during_call(kind: str) -> None:
    server = chunked_hits()
    lose_in_second_candidate_chunk(server, kind)
    page = lm.run(server, squery(limit=3))
    assert (page.complete, page.truncated) == (False, True)
    assert page.incomplete == {"changed_during_call"}


@pytest.mark.parametrize("kind", ["chunk_no", "missing_uid"])
def test_list_second_candidate_chunk_loss_stays_silent(kind: str) -> None:
    server = chunked_hits()
    lose_in_second_candidate_chunk(server, kind)
    page = lm.run(server, lm.query(limit=3))
    assert (page.complete, page.incomplete) == (True, frozenset())


@pytest.mark.parametrize("with_date", [True, False])
@pytest.mark.parametrize("search", [True, False])
def test_unsolicited_fetch_rows_are_not_candidates(
    with_date: bool, search: bool, monkeypatch: pytest.MonkeyPatch, provider_log: Capture
) -> None:
    """`* 7 FETCH (FLAGS ...)` pushed by the server (mail read elsewhere) is keyed by a sequence number: pass 1 asks
    for its UIDs and reads only those."""
    original = FakeImapClient.fetch

    def fetch(self: FakeImapClient, messages: object, items: object) -> dict[int, dict[bytes, object]]:
        rows = original(self, messages, items)  # type: ignore[arg-type]
        if items == lm.CANDIDATE:
            extra: dict[bytes, object] = {b"FLAGS": ()}
            if with_date:
                extra[b"INTERNALDATE"] = lm.NOW
            rows[7] = extra
        return rows

    monkeypatch.setattr(FakeImapClient, "fetch", fetch)
    server = three(inbox=[hit(1), hit(2, 6)])
    page = lm.run(server, squery() if search else lm.query())
    assert sorted(item.ref.uid for item in page.items) == [1, 2]
    assert provider_log.lines == []
    assert page.incomplete == frozenset()
    assert page.complete is True
