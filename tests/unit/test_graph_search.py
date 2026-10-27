"""Graph `search_messages` (spec 080 §5.2, §6.5; WP13d T5): the hub filters and the filter-only list path."""

from datetime import UTC, datetime, timedelta

import httpx2
import pytest

from mcp_hub.providers.base import FolderPage, ListQuery, SearchTerms
from mcp_hub.providers.deadline import CallDeadline
from mcp_hub.providers.graph import FILTER_READ_REQUESTS, GraphMailbox, graph_filters_match
from tests.support.clock import FakeClock
from tests.support.graph_fixtures import ARCHIVE_ID, INBOX_ID, SENT_ID, sender
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
    assert "lt" in requests[1].url.params["$filter"]


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
        clock.advance(17)  # the next request is past the soft stop
        return js({"value": first})(request)

    server.routes[f"/v1.0/me/mailFolders/{INBOX_ID}/messages"] = handler
    page = found(mailbox(server, clock=clock), squery(terms(sender="rare person")), clock)
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
