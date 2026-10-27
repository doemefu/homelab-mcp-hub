"""Graph `search_messages` (spec 080 §5.2, §6.5; WP13d T5): the hub filters and the filter-only list path."""

import base64
import hashlib
import logging
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import httpx2
import pytest

import mcp_hub
from mcp_hub.cursors import Position
from mcp_hub.errors import ToolError
from mcp_hub.ids import GraphMessageRef, SearchHitRef, encode_message_id, location_id
from mcp_hub.providers.base import FolderPage, ListQuery, ProviderError, SearchTerms, order_key
from mcp_hub.providers.deadline import CallDeadline
from mcp_hub.providers.folders import GraphFolderIds
from mcp_hub.providers.graph import (
    EXTENDED_EXPAND,
    FILTER_READ_REQUESTS,
    READ_ON_SECONDS,
    SEARCH_EXPAND,
    SUMMARY_SELECT,
    GraphMailbox,
    _search_requests,
    graph_filters_match,
    list_filter,
)
from tests.support.clock import FakeClock
from tests.support.graph_fixtures import (
    ARCHIVE_ID,
    FOLDER_IDS,
    INBOX_ID,
    JUNK_ID,
    OUTBOX_ID,
    SEARCH_FACTS,
    SENT_ID,
    TRASH_ID,
    sender,
)
from tests.support.logcapture import Capture
from tests.unit.test_graph_history import SINCE, Server, item, js, mailbox, values

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)


def terms(
    *, words: tuple[str, ...] = (), sender: str | None = None, recipient: str | None = None, subject: str | None = None
) -> SearchTerms:
    return SearchTerms(words, sender, recipient, subject)


def squery(search: SearchTerms, folder: str = "inbox", limit: int = 20) -> ListQuery:
    return ListQuery(folder, SINCE, False, None, limit, None, search)  # type: ignore[arg-type]


def found(box: GraphMailbox, q: ListQuery, clock: FakeClock) -> FolderPage:
    return box.list_messages(q, CallDeadline.start(clock))


# --- hub filters (MF-D2, MF-R1, MF-R3, SR-2) -----------------------------------------------------------------------


def raw(**extra: object) -> dict[str, object]:
    return {"from": sender("jm@example.org", "Jürg Müller"), "subject": "Hello", "toRecipients": []} | extra


@pytest.mark.parametrize(
    ("needle", "expected"),
    [("MÜLLER", True), ("müller", True), ("muller", False), ("example.org", True), ("Müller jm", False)],
)
def test_sender_filter_table(needle: str, expected: bool) -> None:
    assert graph_filters_match(raw(), terms(sender=needle)) is expected


def test_sender_dn_address_is_not_searched() -> None:
    dn = {"from": sender("/O=EXAMPLE/OU=X/CN=RECIPIENTS/CN=abc", "owner@example.org")}
    assert graph_filters_match(raw(**dn), terms(sender="owner@"))
    assert not graph_filters_match(raw(**dn), terms(sender="recipients"))


def test_recipient_filter_all_entries_never_cc() -> None:
    to = [sender(f"r{n}@example.org", f"R{n}") for n in range(11)] + [sender("x@example.org", "Zora Last")]
    assert graph_filters_match(raw(toRecipients=to), terms(recipient="zora"))
    cc = {"ccRecipients": [sender("c@example.org", "Zora Last")]}
    assert not graph_filters_match(raw(**cc), terms(recipient="zora"))


@pytest.mark.parametrize(
    ("subject", "needle", "expected"),
    [
        ("Re: Straße", "STRASSE", True),
        ("In​voice", "invoice", True),
        ("see https://example.org/abc?x=1", "abc?x", True),
        ("x" * 5000 + "needle", "needle", False),
        ('Re: "Budget" Q4', 'Re: "Budget" Q4', True),
        ("Hello", "world", False),
    ],
)
def test_subject_filter_table(subject: str, needle: str, expected: bool) -> None:
    assert graph_filters_match(raw(subject=subject), terms(subject=needle)) is expected


def test_subject_cap_pinned_at_4000_characters() -> None:
    subject = "y" * 3990 + "needle"
    assert graph_filters_match(raw(subject=subject), terms(subject="needle"))
    assert not graph_filters_match(raw(subject="y" * 4000 + "needle"), terms(subject="needle"))


@pytest.mark.parametrize(
    ("name", "needle"),
    [('Doe "JD" John', "Doe JD John"), ("a\\b", "a\\b"), ("Müller", "Müller")],
)
def test_name_quote_backslash_and_nfd(name: str, needle: str) -> None:
    assert graph_filters_match(raw(**{"from": sender("a@example.org", name)}), terms(sender=needle))


@pytest.mark.parametrize("needle", [".*", "a+(b", "[x]", "%", "_"])
def test_needles_are_literal_text(needle: str) -> None:
    assert not graph_filters_match(raw(subject="abc xyz"), terms(subject=needle))
    assert graph_filters_match(raw(subject=f"a {needle} b"), terms(subject=needle))


def test_words_are_not_checked_by_the_hub() -> None:
    assert graph_filters_match(raw(), terms(words=("absent",)))


# --- filter-only path ------------------------------------------------------------------------------------------------


def filtered_run(n: int, start: datetime, count: int, who: str, hits: set[int]) -> list[object]:
    """`count` items one minute apart; the offsets in `hits` come from `who`, the others from someone else."""
    return [
        item(
            n + i,
            start - timedelta(minutes=i),
            **{"from": sender(f"s{n + i}@example.org", who if i in hits else "Other")},
        )
        for i in range(count)
    ]


def test_filter_only_uses_list_request_and_sends_no_value() -> None:
    clock = FakeClock()
    server = Server({INBOX_ID: values(*filtered_run(1, NOW, 5, "Canary Needle", {0, 2}))})
    page = found(mailbox(server, clock=clock), squery(terms(sender="canary needle")), clock)
    (request,) = server.lists()
    assert request.url.path.endswith(f"/mailFolders/{INBOX_ID}/messages")
    assert "$search" not in request.url.params
    assert request.url.params["$top"] == "101"
    assert "$orderby" in request.url.params
    assert len(page.items) == 2
    assert not page.more
    assert page.complete
    raw_url = str(request.url).lower()
    for part in ("canary", "needle", "canary%20needle", "canary+needle"):
        assert part not in raw_url  # GR3: no filter value reaches the URL


def full_answers(server: Server, folder_id: str, answers: list[list[object]]) -> None:
    calls = iter(answers)
    server.routes[f"/v1.0/me/mailFolders/{folder_id}/messages"] = lambda request: js({"value": next(calls)})(request)


def block(start: datetime, hit_at: int | None, tag: int) -> list[object]:
    """101 items, one per minute from `start` downwards, a full answer; `hit_at` is the offset of the one match."""
    return [
        item(
            tag * 1000 + i,
            start - timedelta(minutes=i),
            **{"from": sender(f"s{i}@example.org", "Rare Person" if i == hit_at else "Other")},
        )
        for i in range(101)
    ]


def test_filter_only_reads_on_until_the_answer_is_not_full() -> None:
    clock = FakeClock()
    server = Server()
    a, b, c = (block(NOW - timedelta(hours=2 * k), 3, k) for k in range(3))
    d = filtered_run(9000, NOW - timedelta(hours=7), 10, "Rare Person", {1})
    full_answers(server, INBOX_ID, [a, b, c, d])
    page = found(mailbox(server, clock=clock), squery(terms(sender="rare person")), clock)
    requests = server.lists()
    assert len(requests) == 4
    assert len(page.items) == 4
    assert not page.more
    assert page.frontier is None
    assert page.complete
    for number in (1, 2, 3):  # the next request starts at the second of the last item of the answer before
        bound = NOW - timedelta(hours=2 * (number - 1)) - timedelta(minutes=100)
        assert requests[number].url.params["$filter"] == list_filter(SINCE, bound, False)


def test_filter_only_request_cap_and_frontier_of_the_last_answer() -> None:
    clock = FakeClock()
    server = Server()
    answers = [block(NOW - timedelta(hours=2 * k), None, k) for k in range(FILTER_READ_REQUESTS + 1)]
    full_answers(server, INBOX_ID, answers)
    page = found(mailbox(server, clock=clock), squery(terms(sender="rare person")), clock)
    assert len(server.lists()) == FILTER_READ_REQUESTS == 5
    assert page.items == []
    assert page.more
    assert page.complete
    assert not page.incomplete
    last = answers[FILTER_READ_REQUESTS - 1]
    assert page.frontier is not None
    assert page.frontier[0] == int((NOW - timedelta(hours=2 * 4) - timedelta(minutes=100)).timestamp())
    assert last  # the fifth answer decides the frontier


def test_filter_only_stops_reading_at_limit_matches() -> None:
    clock = FakeClock()
    server = Server()
    answers = [block(NOW - timedelta(hours=2 * k), 0, k) for k in range(6)]
    full_answers(server, INBOX_ID, answers)
    page = found(mailbox(server, clock=clock), squery(terms(sender="rare person"), limit=2), clock)
    assert len(server.lists()) == 2
    assert len(page.items) == 2
    assert page.more


def test_filter_only_stuck_second_is_result_cap() -> None:
    clock = FakeClock()
    same = [item(i, NOW) for i in range(101)]
    server = Server({INBOX_ID: values(*same)})
    page = found(mailbox(server, clock=clock), squery(terms(sender="x")), clock)
    assert page.truncated
    assert not page.complete
    assert page.incomplete == {"result_cap"}
    assert page.frontier is None


def test_filter_only_soft_stop_is_time_limit_and_keeps_what_was_read() -> None:
    clock = FakeClock()
    server = Server()
    first = block(NOW, 1, 1)

    def handler(request: httpx2.Request) -> httpx2.Response:
        clock.advance(17)  # the next folder's first request is past the soft stop
        return js({"value": first})(request)

    server.routes[f"/v1.0/me/mailFolders/{INBOX_ID}/messages"] = handler
    page = found(mailbox(server, clock=clock), squery(terms(sender="rare person"), "all"), clock)
    assert page.incomplete == {"time_limit"}
    assert page.truncated
    assert not page.complete
    assert len(page.items) == 1


def test_soft_stop_while_building_folder_ids_is_time_limit() -> None:
    clock = FakeClock()
    server = Server({INBOX_ID: values()})

    def slow_inbox_id(request: httpx2.Request) -> httpx2.Response:
        clock.advance(17)
        return js({"id": INBOX_ID})(request)

    server.routes["/v1.0/me/mailFolders/inbox"] = slow_inbox_id
    page = found(mailbox(server, clock=clock), squery(terms(sender="x")), clock)
    assert page.incomplete == {"time_limit"}
    assert not page.complete


def test_filter_only_keeps_immutable_ids_and_all_folders() -> None:
    clock = FakeClock()
    mine = [item(1, NOW, SENT_ID) | {"toRecipients": [sender("z@example.org", "Zed")]}]
    server = Server({INBOX_ID: values(), ARCHIVE_ID: values(), SENT_ID: values(*mine)})
    page = found(mailbox(server, clock=clock), squery(terms(recipient="zed"), folder="all"), clock)
    assert [i.ref.graph_id for i in page.items] == ["AAMkMSG0001="]  # type: ignore[union-attr]
    assert page.items[0].meta.direction == "outgoing"


# --- `$search` path (T5a) ---------------------------------------------------------------------------------------------

SEARCH_PATH = "/v1.0/me/messages"


def hit(n: int, at: datetime, parent: str = INBOX_ID, **extra: object) -> dict[str, object]:
    """A `$search` hit: its returned id is a regular id (not the immutable one the list returns)."""
    return item(n, at, parent, id=f"REGULAR{n:05d}=") | extra


def searching(*hits: object, **extra: object) -> Server:
    server = Server()
    server.routes[SEARCH_PATH] = values(*hits, **extra)
    return server


def swords(*words: str, **kw: str | None) -> SearchTerms:
    return terms(words=words, **kw)  # type: ignore[arg-type]


def searches(server: Server) -> list[httpx2.Request]:
    return [r for r in server.requests if r.url.path == SEARCH_PATH]


def test_search_fixture_shapes_match_facts() -> None:
    assert SEARCH_FACTS is not None
    assert (SEARCH_FACTS["p8_status"] == 200 and SEARCH_FACTS["p8_extended_present"] is True) == SEARCH_EXPAND
    assert SEARCH_FACTS["p8_mailbox_wide"] is True  # the probed request is the shipped shape
    assert SEARCH_FACTS["p8_ids_immutable"] is False  # decision A: hits carry no id


@pytest.mark.parametrize("folder", ["all", "inbox", "sent", "archive", "trash"])
def test_search_request_shape_one_get_for_every_folder(folder: str) -> None:
    clock = FakeClock()
    server = searching(hit(1, NOW))
    found(mailbox(server, clock=clock), squery(swords("Budget", "Q4"), folder), clock)
    (request,) = searches(server)
    assert request.method == "GET"
    assert dict(request.url.params) == {
        "$search": '"budget q4"',
        "$top": "100",
        "$select": SUMMARY_SELECT,
        "$expand": EXTENDED_EXPAND,
    }
    assert request.headers["prefer"] == 'IdType="ImmutableId", outlook.body-content-type="text"'
    assert "consistencylevel" not in request.headers
    assert not {"$filter", "$orderby"} & set(request.url.params)
    raw_value = _raw_search_value(request)
    assert raw_value.count("%22") == 2
    assert not any(c in raw_value.upper() for c in ("%25", "%26", "%2B", "%2A", "%3A"))


def _raw_search_value(request: httpx2.Request) -> str:
    return next(p for p in request.url.query.decode().split("&") if p.startswith(("%24search=", "$search=")))


def test_search_wire_value_of_hostile_words_has_no_special_escapes() -> None:
    clock = FakeClock()
    server = searching(hit(1, NOW))
    words = ("a%3Ab", "x&y", "report*", "+x", "from\uff1ax", "100%")
    found(mailbox(server, clock=clock), squery(swords(*words)), clock)
    raw_value = _raw_search_value(searches(server)[0])
    assert raw_value.count("%22") == 2
    assert not any(c in raw_value.upper() for c in ("%25", "%26", "%2B", "%2A", "%3A"))


def test_search_request_shape_function_is_the_one_builder() -> None:
    ids = GraphFolderIds("outlook", FOLDER_IDS)  # type: ignore[arg-type]
    path, params, allowed = _search_requests(ids, squery(swords("x1"), "all"))
    assert path == "/me/messages"
    assert allowed == {INBOX_ID, ARCHIVE_ID, SENT_ID}
    assert set(params) == {"$search", "$top", "$select", "$expand"}
    assert _search_requests(ids, squery(swords("x1"), "trash"))[2] == {TRASH_ID}


def _bound_words(length: int) -> tuple[str, ...]:
    """8 distinct words of 4 comma-separated tokens: 32 tokens, `length` encoded characters of `$search`."""
    letters = length - 6 - 31  # two %22, 31 separators of one "+"
    sizes = [letters // 32 + (1 if i < letters % 32 else 0) for i in range(32)]
    tokens = [chr(ord("a") + i // 4) + "x" * (sizes[i] - 1) for i in range(32)]
    return tuple(",".join(tokens[i * 4 : i * 4 + 4]) for i in range(8))


def test_search_text_bound_1500_sends_one_request_and_one_more_none() -> None:
    clock = FakeClock()
    server = searching(hit(1, NOW))
    box = mailbox(server, clock=clock)
    found(box, squery(terms(words=_bound_words(1500))), clock)
    assert len(searches(server)) == 1
    assert len(_raw_search_value(searches(server)[0]).split("=", 1)[1]) == 1500  # measured on the recorded request
    with pytest.raises(ToolError) as caught:
        found(box, squery(terms(words=_bound_words(1501))), clock)
    assert caught.value.code == "invalid_argument"
    assert len(searches(server)) == 1


def test_search_filter_values_never_reach_a_url() -> None:
    clock = FakeClock()
    server = searching(hit(1, NOW, **{"from": sender("a@example.org", "Jürg Canaryfrom")}))
    q = squery(swords("alpha", sender="Jürg Canaryfrom", recipient="Canaryto", subject="Canarysubj"))
    found(mailbox(server, clock=clock), q, clock)
    urls = " ".join(str(r.url) + r.url.raw_path.decode() for r in server.requests).lower()
    for canary in ("canaryfrom", "canaryto", "canarysubj", "j%c3%bcrg", "jürg", "j%25c3"):
        assert canary not in urls


def test_search_empty_graph_text_is_invalid_argument_before_any_request() -> None:
    clock = FakeClock()
    server = searching()
    with pytest.raises(ToolError):
        found(mailbox(server, clock=clock), squery(swords("--", "***")), clock)
    assert server.requests == []


def test_search_orders_by_received_newest_first_and_drops_the_window() -> None:
    clock = FakeClock()
    old = SINCE - timedelta(seconds=1)
    server = searching(hit(1, NOW - timedelta(days=2)), hit(2, NOW), hit(3, NOW - timedelta(days=1)), hit(4, old))
    page = found(mailbox(server, clock=clock), squery(swords("budget")), clock)
    assert [i.meta.message_id for i in page.items] == ["m2@example.test", "m3@example.test", "m1@example.test"]


def test_search_parent_mismatch_and_drafts_dropped() -> None:
    clock = FakeClock()
    server = searching(
        hit(1, NOW, "JUNKPARENT"),
        hit(2, NOW, ARCHIVE_ID),  # another allowed folder is kept for `all`, dropped for `inbox` below
        hit(3, NOW, isDraft=True),
        hit(4, NOW, INBOX_ID),
        hit(5, NOW, parentFolderId=None),
    )
    page = found(mailbox(server, clock=clock), squery(swords("budget"), "inbox"), clock)
    assert [i.meta.message_id for i in page.items] == ["m4@example.test"]
    assert [r.url.path for r in server.lists()] == [SEARCH_PATH]


def test_search_folder_filter() -> None:
    clock = FakeClock()
    hits = (hit(1, NOW, INBOX_ID), hit(2, NOW, ARCHIVE_ID), hit(3, NOW, SENT_ID), hit(4, NOW, TRASH_ID))
    expected = {"all": {1, 2, 3}, "inbox": {1}, "sent": {3}, "archive": {2}, "trash": {4}}
    for folder, numbers in expected.items():
        page = found(mailbox(searching(*hits), clock=clock), squery(swords("budget"), folder), clock)
        got = {int(i.meta.message_id[1:].split("@")[0]) for i in page.items if i.meta.message_id}
        assert got == numbers, folder


def test_search_full_answer_sets_result_cap_and_short_answer_is_complete() -> None:
    clock = FakeClock()
    hits = [hit(n, NOW - timedelta(minutes=n)) for n in range(100)]
    full = found(mailbox(searching(*hits), clock=clock), squery(swords("budget"), limit=50), clock)
    assert full.incomplete == {"result_cap"}
    assert full.truncated
    assert full.complete
    assert full.more
    short = found(mailbox(searching(*hits[:99]), clock=clock), squery(swords("budget"), limit=50), clock)
    assert not short.incomplete
    assert not short.truncated


def test_search_cap_counts_dropped_hits() -> None:
    clock = FakeClock()
    hits = [hit(n, NOW - timedelta(minutes=n), JUNK_ID if n < 60 else INBOX_ID) for n in range(100)]
    page = found(mailbox(searching(*hits), clock=clock), squery(swords("budget"), limit=50), clock)
    assert page.incomplete == {"result_cap"}
    assert len(page.items) == 40


def test_search_next_link_is_result_cap_and_never_requested() -> None:
    clock = FakeClock()
    server = searching(hit(1, NOW), hit(2, NOW), hit(3, NOW), **{"@odata.nextLink": "https://example.test/next"})
    page = found(mailbox(server, clock=clock), squery(swords("budget")), clock)
    assert page.incomplete == {"result_cap"}
    assert len(server.requests) == len(server.folder_gets()) + 1


def test_search_too_large_halves_once() -> None:
    clock = FakeClock()
    big = {"value": [hit(0, NOW, subject="x" * 1_100_000)]}
    small = {"value": [hit(n, NOW - timedelta(minutes=n)) for n in range(50)]}
    calls: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(request.url.params["$top"])
        return js(big if len(calls) == 1 else small)(request)

    server = Server()
    server.routes[SEARCH_PATH] = handler
    page = found(mailbox(server, clock=clock), squery(swords("budget"), limit=50), clock)
    assert calls == ["100", "50"]
    assert page.incomplete == {"result_cap"}
    assert len(page.items) == 50
    server = Server()
    server.routes[SEARCH_PATH] = js(big)
    with pytest.raises(ProviderError) as caught:
        found(mailbox(server, clock=clock), squery(swords("budget")), clock)
    assert caught.value.code == "too_large"
    assert len(searches(server)) == 2


def test_search_pages_within_the_hits_with_identical_requests() -> None:
    clock = FakeClock()
    hits = [hit(n, NOW - timedelta(minutes=n)) for n in range(60)]
    server = searching(*hits)
    box = mailbox(server, clock=clock)
    position: Position | None = None
    pages: list[FolderPage] = []
    for _ in range(3):
        q = ListQuery("inbox", SINCE, False, None, 20, position, swords("budget"))
        pages.append(found(box, q, clock))
        last = pages[-1].items[-1]
        position = Position(*order_key(last))
    assert [p.more for p in pages] == [True, True, False]
    keys = [order_key(i) for p in pages for i in p.items]
    assert len(keys) == len(set(keys)) == 60
    assert keys == sorted(keys, reverse=True)
    assert len({tuple(sorted(r.url.params.items())) for r in searches(server)}) == 1


@pytest.mark.parametrize("status", [404, 400])
def test_search_rejected_is_an_account_error_without_retry(status: int) -> None:
    clock = FakeClock()
    server = Server()
    server.routes[SEARCH_PATH] = lambda request: httpx2.Response(status)
    with pytest.raises(ProviderError) as caught:
        found(mailbox(server, clock=clock), squery(swords("secretword")), clock)
    assert (caught.value.code, caught.value.cause) == ("upstream_error", "SearchRejected")
    assert len(searches(server)) == 1  # the log line is pinned by the DEBUG canary test (the adapter logs nothing)


def test_folder_id_failure_keeps_its_cause() -> None:
    clock = FakeClock()
    server = searching(hit(1, NOW))
    server.routes["/v1.0/me/mailFolders/inbox"] = lambda request: httpx2.Response(400)
    with pytest.raises(ProviderError) as caught:
        found(mailbox(server, clock=clock), squery(swords("budget")), clock)
    assert (caught.value.code, caught.value.cause) == ("upstream_error", "UnexpectedStatus")
    assert searches(server) == []


def test_search_soft_stop_before_the_request_is_time_limit() -> None:
    clock = FakeClock()
    server = searching(hit(1, NOW))

    def slow_outbox(request: httpx2.Request) -> httpx2.Response:
        clock.advance(17)
        return js({"id": OUTBOX_ID})(request)

    server.routes["/v1.0/me/mailFolders/outbox"] = slow_outbox
    page = found(mailbox(server, clock=clock), squery(swords("budget")), clock)
    assert page.incomplete == {"time_limit"}
    assert page.truncated
    assert not page.complete
    assert searches(server) == []


def test_search_applies_the_hub_filters_per_hit() -> None:
    clock = FakeClock()
    other = {"from": sender("o@example.org", "Other")}
    server = searching(hit(1, NOW, **{"from": sender("m@example.org", "Mina Rare")}), hit(2, NOW, **other))
    page = found(mailbox(server, clock=clock), squery(swords("budget", sender="rare")), clock)
    assert [i.meta.message_id for i in page.items] == ["m1@example.test"]


def test_search_direction_filter() -> None:
    clock = FakeClock()
    server = searching(hit(1, NOW, INBOX_ID), hit(2, NOW, SENT_ID))
    q = ListQuery("all", SINCE, False, "outgoing", 20, None, swords("budget"))
    page = found(mailbox(server, clock=clock), q, clock)
    assert [i.meta.message_id for i in page.items] == ["m2@example.test"]


# --- owner decision A: hits carry no id ---------------------------------------------------------------------------


def test_search_hit_id_null_and_ref_present() -> None:
    clock = FakeClock()
    server = searching(hit(1, NOW), hit(2, NOW - timedelta(minutes=1), SENT_ID))
    page = found(mailbox(server, clock=clock), squery(swords("budget"), "all"), clock)
    assert len(page.items) == 2
    for summary in page.items:
        assert location_id(summary.ref) is None
        assert summary.meta.message_id is not None
    assert page.items[1].meta.direction == "outgoing"


def test_search_hit_without_message_id_has_neither_id_nor_ref() -> None:
    clock = FakeClock()
    server = searching(hit(1, NOW, internetMessageId=None))
    (summary,) = found(mailbox(server, clock=clock), squery(swords("budget")), clock).items
    assert location_id(summary.ref) is None
    assert summary.meta.message_id is None


def test_search_k_is_the_would_be_g_id_hash_and_stable_across_pages() -> None:
    clock = FakeClock()
    hits = [hit(n, NOW - timedelta(minutes=n)) for n in range(4)]
    box = mailbox(searching(*hits), clock=clock)
    first = found(box, squery(swords("budget"), limit=2), clock)
    again = found(box, squery(swords("budget"), limit=2), clock)
    for summary in first.items:
        assert isinstance(summary.ref, SearchHitRef)
        expected = hashlib.sha256(encode_message_id(GraphMessageRef("outlook", summary.ref.graph_id)).encode())
        assert order_key(summary)[1] == expected.hexdigest()[:16]
    assert [order_key(i) for i in first.items] == [order_key(i) for i in again.items]
    last = first.items[-1]
    second = found(box, ListQuery("inbox", SINCE, False, None, 2, Position(*order_key(last)), swords("budget")), clock)
    assert [order_key(i) for i in second.items] == [
        order_key(i) for i in found(box, squery(swords("budget")), clock).items[2:]
    ]


def test_search_regular_id_never_exposed_or_logged_or_requested() -> None:
    clock = FakeClock()
    capture = Capture()
    logger = logging.getLogger("mcp_hub")
    logger.addHandler(capture)
    logger.setLevel(logging.DEBUG)
    server = searching(hit(1, NOW), hit(2, NOW, receivedDateTime="garbage"), hit(3, NOW, **{"from": ["bad"]}))
    try:
        page = found(mailbox(server, clock=clock), squery(swords("budget")), clock)
    finally:
        logger.removeHandler(capture)
    exposure = " ".join([repr(page), *capture.lines, *(str(r.url) for r in server.requests)])
    for n in (1, 2, 3):
        regular = f"REGULAR{n:05d}="
        assert regular not in exposure
        assert base64.urlsafe_b64encode(regular.encode()).decode().rstrip("=") not in exposure
        assert regular not in " ".join(r.url.path for r in server.requests)
    for summary in page.items:
        with pytest.raises(ValueError, match="never an id"):
            encode_message_id(summary.ref)


def test_search_dedup_by_the_returned_id_not_the_message_id() -> None:
    clock = FakeClock()
    twin_a = hit(1, NOW)
    twin_b = hit(2, NOW, SENT_ID, internetMessageId="<m1@example.test>")
    server = searching(twin_a, twin_a, twin_b)
    page = found(mailbox(server, clock=clock), squery(swords("budget"), "all"), clock)
    assert len(page.items) == 2
    assert {i.meta.message_id for i in page.items} == {"m1@example.test"}
    assert {i.meta.direction for i in page.items} == {"incoming", "outgoing"}


def test_filter_only_keeps_real_ids() -> None:
    clock = FakeClock()
    server = Server({INBOX_ID: values(item(1, NOW))})
    (summary,) = found(mailbox(server, clock=clock), squery(terms(sender="sender 1")), clock).items
    assert location_id(summary.ref) is not None
    assert not isinstance(summary.ref, SearchHitRef)


# --- keyset stability (graph-advisor 02-graph-brief.md §11) ----------------------------------------------------------


def _pages(box: GraphMailbox, clock: FakeClock, limit: int) -> list[FolderPage]:
    pages: list[FolderPage] = []
    position: Position | None = None
    while True:
        page = found(box, ListQuery("inbox", SINCE, False, None, limit, position, swords("budget")), clock)
        pages.append(page)
        if not page.more:
            return pages
        position = Position(*order_key(page.items[-1]))


def test_search_same_regular_ids_over_three_pages_repeat_and_skip_nothing() -> None:
    clock = FakeClock()
    hits = [hit(n, NOW - timedelta(minutes=n)) for n in range(30)]
    pages = _pages(mailbox(searching(*hits), clock=clock), clock, 10)
    assert len(pages) == 3
    got = [i.meta.message_id for p in pages for i in p.items]
    assert got == [f"m{n}@example.test" for n in range(30)]


def test_search_hit_whose_id_changes_between_pages_appears_once() -> None:
    clock = FakeClock()
    first = [hit(n, NOW - timedelta(minutes=n)) for n in range(4)]
    calls = 0

    def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        calls += 1
        moved = hit(3, NOW - timedelta(minutes=3))
        if calls > 1:
            moved["id"] = "REGULARCHANGED="  # the same message, a new regular id, below the cursor second
        return js({"value": [*first[:3], moved]})(request)

    server = Server()
    server.routes[SEARCH_PATH] = handler
    pages = _pages(mailbox(server, clock=clock), clock, 2)
    got = [i.meta.message_id for p in pages for i in p.items]
    assert got == [f"m{n}@example.test" for n in range(4)]
    assert calls == 2


# --- review fixes (G5-1, G5-3, G5-4, G5-5, T5-S1, T5-S3, T5-S6, G5-11) ------------------------------------------------


@pytest.mark.parametrize(
    "module", ["mcp_hub.search", "mcp_hub.providers.graph", "mcp_hub.ids", "mcp_hub.tools.mail_search"]
)
def test_module_imports_first_in_a_fresh_interpreter(module: str) -> None:
    src = Path(mcp_hub.__file__).resolve().parent.parent
    code = f"import sys; sys.path.insert(0, {str(src)!r}); import {module}"
    done = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, check=False)
    assert done.returncode == 0, done.stderr.decode()[-400:]


def test_filter_only_read_on_stops_at_the_budget_with_a_cursor_and_the_cursor_continues() -> None:
    clock = FakeClock()
    server = Server()
    for folder_id, k in ((INBOX_ID, 0), (ARCHIVE_ID, 10), (SENT_ID, 20)):
        blocks = iter(block(NOW - timedelta(hours=2 * n), None, k + n) for n in range(6))

        def handler(
            request: httpx2.Request,
            blocks: "iter[list[object]]" = blocks,  # type: ignore[valid-type]
        ) -> httpx2.Response:
            clock.advance(3)
            return js({"value": next(blocks)})(request)

        server.routes[f"/v1.0/me/mailFolders/{folder_id}/messages"] = handler
    box = mailbox(server, clock=clock)
    page = found(box, squery(terms(sender="rare person"), "all"), clock)
    per_folder = [sum(1 for r in server.lists() if f"/{fid}/" in r.url.path) for fid in (INBOX_ID, ARCHIVE_ID, SENT_ID)]
    assert per_folder[0] == 3  # 0 s, 3 s, 6 s start below the budget; after 9 s no new read-on request
    assert per_folder[1] >= 1
    assert per_folder[2] >= 1
    assert READ_ON_SECONDS == 8.0
    assert page.more
    assert page.frontier is not None
    assert page.complete
    assert not page.incomplete  # not time_limit: the cursor continues
    # the cursor continues where the frontier says
    server2 = Server({INBOX_ID: values(), ARCHIVE_ID: values(), SENT_ID: values()})
    q = ListQuery("all", SINCE, False, None, 20, Position(*page.frontier), terms(sender="rare person"))
    found(mailbox(server2, clock=FakeClock()), q, FakeClock())
    bound = datetime.fromtimestamp(page.frontier[0], UTC)
    assert {r.url.params["$filter"] for r in server2.lists()} == {list_filter(SINCE, bound, False)}


def test_filter_only_read_on_counts_distinct_matches() -> None:
    clock = FakeClock()
    server = Server()
    a1 = block(NOW, 100, 1)  # the only match is the last item: the frontier second
    repeated = a1[100]
    a2 = [repeated, *block(NOW - timedelta(hours=2), None, 2)[:100]]  # repeats it, no new match
    a3 = filtered_run(9000, NOW - timedelta(hours=4), 5, "Rare Person", {2})
    full_answers(server, INBOX_ID, [a1, a2, a3])
    page = found(mailbox(server, clock=clock), squery(terms(sender="rare person"), limit=2), clock)
    assert len(server.lists()) == 3
    assert len(page.items) == 2
    assert not page.more


def too_large_first(clock: FakeClock, size_ok: int, big_at: set[int] | None = None) -> tuple[Server, list[str]]:
    """Every request is answered too large with `$top=101`, and full with `$top=50` (or at the `big_at` calls)."""
    server = Server()
    tops: list[str] = []
    calls = 0

    def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        calls += 1
        tops.append(request.url.params["$top"])
        too_big = request.url.params["$top"] == "101" if big_at is None else calls in big_at
        if too_big:
            return js({"value": [item(0, NOW, subject="x" * 1_100_000)]})(request)
        start = NOW - timedelta(hours=2 * calls)
        top = int(request.url.params["$top"])
        return js({"value": [item(calls * 1000 + i, start - timedelta(minutes=i)) for i in range(min(top, size_ok))]})(
            request
        )

    server.routes[f"/v1.0/me/mailFolders/{INBOX_ID}/messages"] = handler
    return server, tops


def test_filter_only_halving_retry_counts_toward_the_request_cap() -> None:
    clock = FakeClock()
    server, tops = too_large_first(clock, 50)
    page = found(mailbox(server, clock=clock), squery(terms(sender="rare person")), clock)
    assert len(server.lists()) == FILTER_READ_REQUESTS == 5  # 2 GETs for the first answer, then $top=50 carried on
    assert tops[:2] == ["101", "50"]
    assert set(tops[2:]) == {"50"}
    assert page.more
    assert page.complete


def test_filter_only_a_late_too_large_answer_stops_reading_on_without_an_error() -> None:
    clock = FakeClock()
    server = Server()
    sizes: list[int] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        sizes.append(1)
        n = len(sizes)
        if n == 5:  # the fifth GET could only be answered by a sixth: it stops instead
            return js({"value": [item(0, NOW, subject="x" * 1_100_000)]})(request)
        return js({"value": block(NOW - timedelta(hours=2 * n), None, n)})(request)

    server.routes[f"/v1.0/me/mailFolders/{INBOX_ID}/messages"] = handler
    page = found(mailbox(server, clock=clock), squery(terms(sender="rare person")), clock)
    assert len(server.lists()) == 5
    assert page.more
    assert page.frontier is not None
    assert page.frontier[0] == int((NOW - timedelta(hours=2 * 4) - timedelta(minutes=100)).timestamp())
    assert page.complete


def test_placeholder_never_matches_a_filter() -> None:
    clock = FakeClock()
    odd = hit(2, NOW, subject=["not text"], **{"from": "garbage"})
    server = searching(odd)
    assert found(mailbox(server, clock=clock), squery(swords("budget", sender="x")), clock).items == []
    kept = found(mailbox(searching(odd), clock=clock), squery(swords("budget")), clock).items
    assert len(kept) == 1
    assert location_id(kept[0].ref) is None


def test_by_id_paths_refuse_a_search_hit_reference() -> None:
    clock = FakeClock()
    server = Server()
    box = mailbox(server, clock=clock)
    ref = SearchHitRef("outlook", "REGULAR00001=")
    with pytest.raises(ProviderError) as caught:
        box.get_message(ref, CallDeadline.start(clock))
    assert caught.value.code == "not_found"
    from mcp_hub.providers.base import ThreadQuery

    with pytest.raises(ProviderError):
        box.get_thread(ThreadQuery(ref, None, False, 1000), CallDeadline.start(clock))
    box.message_status([ref], CallDeadline.start(clock))
    assert not any("REGULAR" in r.url.path for r in server.requests)


def test_graph_search_logs_no_canary_at_debug() -> None:
    from mcp_hub.logging import HUB_LOGGER, PINNED_WARNING, configure_logging
    from mcp_hub.providers.base import item_hash

    names = ["", HUB_LOGGER, *PINNED_WARNING]
    saved = {n: (logging.getLogger(n).level, list(logging.getLogger(n).handlers)) for n in names}
    capture = Capture()
    clock = FakeClock()
    ids = [f"REGULARzqxcanary{n}=" for n in (1, 2, 3)]
    server = searching(
        hit(1, NOW, id=ids[0]),
        hit(2, NOW, id=ids[1], receivedDateTime="garbage"),
        hit(3, NOW, id=ids[2], **{"from": ["bad"]}),
    )
    rejected = Server()
    rejected.routes[SEARCH_PATH] = lambda request: httpx2.Response(400)
    box = mailbox(server, clock=clock)
    try:
        configure_logging("DEBUG")
        logging.getLogger().addHandler(capture)
        words = ("zqxcanaryquery", "zqxcanaryübung")
        filters = {"sender": "zqxcanarysender", "recipient": "zqxcanaryrecipient", "subject": "zqxcanarysubject"}
        found(box, squery(swords(*words, **filters)), clock)  # type: ignore[arg-type]
        with pytest.raises(ProviderError):
            found(mailbox(rejected, clock=clock), squery(swords(*words)), clock)
        found(mailbox(Server({INBOX_ID: values(item(1, NOW))}), clock=clock), squery(terms(**filters)), clock)  # type: ignore[arg-type]
        levels = {n: logging.getLogger(n).getEffectiveLevel() for n in PINNED_WARNING}
    finally:
        logging.getLogger().removeHandler(capture)
        for n, (level, handlers) in saved.items():
            logger = logging.getLogger(n)
            logger.setLevel(level)
            logger.handlers[:] = handlers
    assert all(level >= logging.WARNING for level in levels.values())
    assert not [r for r in capture.raw if r[0].startswith(("httpx2", "httpcore2"))]
    needles = [*words, *filters.values(), *ids]
    for needle in [*needles]:
        needles += [
            quote(needle),
            base64.urlsafe_b64encode(needle.encode()).decode().rstrip("="),
            repr(needle.encode()),
        ]
    for regular in ids:  # the realistic leak forms of the id: the would-be v1. text and its item hash
        would_be = encode_message_id(GraphMessageRef(box._account, regular))  # type: ignore[attr-defined]
        needles += [would_be, item_hash(would_be)]
    text = " ".join([*capture.lines, *(m for _, _, m in capture.raw)])
    for needle in needles:
        assert needle not in text, needle
    assert "zqxcanary" not in text.lower()
    assert not [line for line in capture.lines if '"item"' in line]
