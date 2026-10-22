from datetime import UTC, datetime, timedelta, timezone

import httpx2
import pytest

from mcp_hub.providers import graph_status as gs
from mcp_hub.providers.folders import MailFolder
from mcp_hub.providers.graph_status import ThreadEntry, filter_batches, thread_facts

OWN = frozenset({"me@example.org"})
CONV = "conv-1"
T0 = datetime(2026, 10, 1, 12, 0, 0, tzinfo=UTC)


def _entry(
    graph_id: str,
    folder: MailFolder = "inbox",
    *,
    received: datetime | None = T0,
    sent: datetime | None = T0,
    message_id: str | None = None,
    sender: str | None = "peer@example.org",
    conversation: str = CONV,
) -> ThreadEntry:
    return ThreadEntry(graph_id, conversation, folder, received, sent, message_id, sender)


def _facts(entries: list[ThreadEntry], *, message_id: str | None = "x@example.org", copy_id: str = "X"):
    return thread_facts(copy_id, message_id, T0, CONV, entries, OWN)


def test_filters_render_exactly() -> None:
    assert gs.message_id_filter(["a@example.org"]) == "internetMessageId eq '<a@example.org>'"
    assert gs.message_id_filter(["a@x", "b@y"]) == "internetMessageId eq '<a@x>' or internetMessageId eq '<b@y>'"
    assert gs.conversation_filter(["a=", "b_-"]) == "conversationId eq 'a=' or conversationId eq 'b_-'"


@pytest.mark.parametrize("bad", ["a'b", "", "a b", "a" * 301, "a<b"])
def test_filter_values_refused(bad: str) -> None:
    with pytest.raises(ValueError, match="refused"):
        gs.message_id_filter([bad])


@pytest.mark.parametrize("bad", ["a'b", "", "a b", "a/b", "a" * 301])
def test_conversation_values_refused(bad: str) -> None:
    with pytest.raises(ValueError, match="refused"):
        gs.conversation_filter([bad])


def test_encoded_length_matches_the_wire() -> None:
    for text in ["internetMessageId eq '<a@example.org>'", gs.message_id_filter(["a#&+%=/?;@example.org"])]:
        request = httpx2.Request("GET", "https://graph.example.test/x", params={"$filter": text})
        raw = request.url.query.decode()
        assert raw.startswith("%24filter=") or raw.startswith("$filter=")
        assert gs.encoded_filter_length(text) == len(raw.split("=", 1)[1])


def _render(values: list[str]) -> str:
    return gs.conversation_filter(values)


def test_filter_batches_values_and_chars() -> None:
    values = [f"v{i}" for i in range(23)]
    assert [len(b) for b in filter_batches(values, _render)] == [10, 10, 3]
    assert filter_batches(["a", "b", "a", "c"], _render) == [["a", "b", "c"]]

    def render(vs: list[str]) -> str:
        return "x" * sum(len(v) for v in vs)

    at_cap = filter_batches(["a" * 900, "b" * 900], render, max_chars=1800)
    assert at_cap == [["a" * 900, "b" * 900]]
    assert len(filter_batches(["a" * 900, "b" * 901], render, max_chars=1800)) == 2

    ids = [f"{i}" * 300 for i in range(10)]
    batches = filter_batches(ids, gs.message_id_filter)
    assert sum(len(b) for b in batches) == 10
    assert all(gs.encoded_filter_length(gs.message_id_filter(b)) <= gs.MAX_FILTER_CHARS for b in batches if len(b) > 1)
    assert filter_batches(["#" * 300], gs.message_id_filter) == [["#" * 300]]


def test_filter_batches_refuses_any_invalid_value() -> None:
    for values in (["a'b", "ok"], ["ok", "a'b"], ["ok", "a b", "fine"]):
        with pytest.raises(ValueError, match="refused"):
            filter_batches(values, gs.message_id_filter)


def test_percent_message_id_fits_the_cap_alone() -> None:
    text = gs.message_id_filter(["%" * 300])
    assert gs.encoded_filter_length(text) <= gs.MAX_FILTER_CHARS
    assert filter_batches(["%" * 300], gs.message_id_filter) == [["%" * 300]]


def test_thread_facts_newer_incoming_in_archive_trash_and_other() -> None:
    for folder in ("archive", "trash", "other"):
        later = T0 + timedelta(hours=1)
        facts = _facts([_entry("a", folder, received=later, message_id="a@example.org")])
        assert facts.newer_incoming_at == later


def test_thread_facts_reply_strictly_after_receipt() -> None:
    first_reply = T0 + timedelta(seconds=1)
    late_reply = T0 + timedelta(hours=1, seconds=5)
    entries = [
        _entry("s0", "sent", sent=T0, message_id="r0@example.org"),
        _entry("s2", "sent", sent=late_reply, message_id="r2@example.org"),
        _entry("s1", "sent", sent=first_reply, message_id="r1@example.org"),
        _entry("s-before", "sent", sent=T0 - timedelta(seconds=1), message_id="r3@example.org"),
    ]
    facts = _facts(entries)
    assert facts.reply_at == first_reply
    assert facts.reply_message_id == "r1@example.org"
    assert facts.complete
    assert _facts(entries[:1]).reply_at is None


def test_thread_facts_self_excluded_by_id_and_message_id() -> None:
    later = T0 + timedelta(hours=1)
    entries = [
        _entry("X", "sent", sent=later, message_id="x@example.org"),
        _entry("other-copy", "archive", received=later, sent=later, message_id="x@example.org"),
        _entry("same-id", "inbox", received=later, message_id="y@example.org"),
    ]
    facts = _facts([entries[0], entries[1]])
    assert facts.reply_at is None
    assert facts.newer_incoming_at is None
    facts = thread_facts("same-id", None, T0, CONV, [entries[2]], OWN)
    assert facts.newer_incoming_at is None


def test_thread_facts_newer_incoming_skips_own_and_sent() -> None:
    t1, t2 = T0 + timedelta(hours=1), T0 + timedelta(hours=2)
    entries = [
        _entry("a", received=t1, message_id="a@example.org"),
        _entry("b", received=t2, message_id="b@example.org"),
        _entry("own", received=T0 + timedelta(hours=3), sender="ME@example.org", message_id="c@example.org"),
        _entry("sent", "sent", received=T0 + timedelta(hours=4), sent=T0 + timedelta(hours=4)),
        _entry("elsewhere", received=T0 + timedelta(hours=5), conversation="other"),
    ]
    assert _facts(entries).newer_incoming_at == t2


def test_thread_facts_equal_time_is_not_newer() -> None:
    entries = [_entry("a", received=T0), _entry("s", "sent", sent=T0, message_id="s@example.org")]
    facts = _facts(entries)
    assert facts.newer_incoming_at is None
    assert facts.reply_at is None


def test_thread_facts_mixed_offsets_compare_as_instants() -> None:
    plus2 = timezone(timedelta(hours=2))
    same_instant = T0.astimezone(plus2)
    later_local = (T0 + timedelta(seconds=1)).astimezone(plus2)
    assert _facts([_entry("a", received=same_instant)]).newer_incoming_at is None
    assert _facts([_entry("a", received=later_local)]).newer_incoming_at == T0 + timedelta(seconds=1)
    assert _facts([_entry("s", "sent", sent=later_local)]).reply_at == T0 + timedelta(seconds=1)


def test_thread_facts_own_reply_copy_in_inbox_not_newer() -> None:
    later = T0 + timedelta(hours=1)
    entries = [
        _entry("s", "sent", sent=later, message_id="reply@example.org"),
        _entry("back", received=later, sender="alias@example.net", message_id="reply@example.org"),
    ]
    facts = _facts(entries)
    assert facts.reply_at == later
    assert facts.newer_incoming_at is None


def test_thread_facts_outgoing_ref() -> None:
    later = T0 + timedelta(hours=1)
    entries = [
        _entry("X", "sent", sent=T0, received=T0, message_id="x@example.org"),
        _entry("own-later", "sent", sent=later, message_id="o@example.org"),
        _entry("theirs", received=later + timedelta(hours=1), message_id="t@example.org"),
    ]
    facts = _facts(entries)
    assert facts.reply_at == later
    assert facts.newer_incoming_at == later + timedelta(hours=1)


def test_thread_facts_unparsable_time_incomplete() -> None:
    later = T0 + timedelta(hours=1)
    bad_sent = _entry("s", "sent", sent=None, message_id="s@example.org")
    bad_received = _entry("i", received=None)
    good = _entry("g", received=later)
    for bad in (bad_sent, bad_received):
        facts = _facts([bad, good])
        assert not facts.complete
        assert facts.newer_incoming_at == later
    # a skipped self copy or another conversation never marks the ref incomplete
    assert _facts([_entry("X", received=None, sent=None), _entry("o", received=None, conversation="z")]).complete
