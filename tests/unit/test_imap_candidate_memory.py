"""Pass 1 holds no full row list and keeps a bounded From per candidate (WP13d T8 reductions)."""

from collections.abc import Iterator
from datetime import timedelta

import pytest

import mcp_hub.providers.imap as imap
from mcp_hub.providers.imap import Candidate
from mcp_hub.providers.imap_session import ImapSession
from tests.support.imap_fake import FakeMessage
from tests.unit import test_imap_list_messages as lm
from tests.unit.test_imap_search_messages import hit, squery, three


def test_candidates_are_reduced_chunk_by_chunk(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each chunk of rows becomes `Candidate`s before the next chunk is fetched."""
    events: list[str] = []
    original = ImapSession.fetch_chunks

    def fetch_chunks(
        self: ImapSession, uids: object, items: object, *, chunk: int
    ) -> Iterator[dict[int, dict[bytes, object]]]:
        for number, rows in enumerate(original(self, uids, items, chunk=chunk)):  # type: ignore[arg-type]
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
