"""Graph `list_messages` (spec 080 §5.5, §6.5): well-known folder ids, extended properties, keyset paging, soft stop."""

import json
import logging
from collections.abc import Callable, Iterator, Mapping, Sequence
from datetime import UTC, datetime, timedelta, timezone
from urllib.parse import parse_qs

import httpx2
import pytest

from mcp_hub.cursors import Position
from mcp_hub.ids import GraphMessageRef, StableRef, ThreadKey, location_k
from mcp_hub.logging import configure_logging
from mcp_hub.providers import graph
from mcp_hub.providers.base import (
    UNDECODABLE_NOTE,
    FolderPage,
    ListQuery,
    MailDetail,
    MailFlags,
    MailMeta,
    ProviderError,
    order_key,
)
from mcp_hub.providers.deadline import CallDeadline
from mcp_hub.providers.folders import FolderMapCache, GraphFolderIds, ListFolder
from mcp_hub.providers.graph import (
    ANSWERED_VERBS,
    EXTENDED_EXPAND,
    FALLBACK_SELECT,
    FOLDER_ID,
    GRAPH_LIST_TOP,
    MESSAGE_SELECT,
    SUMMARY_SELECT,
    WELL_KNOWN_FOR,
    Backoff,
    GraphMailbox,
    _answered,
    graph_meta,
    list_filter,
)
from mcp_hub.providers.headers import MAX_REFERENCES
from tests.support.clock import FakeClock
from tests.support.graph_fixtures import (
    ARCHIVE_ID,
    FOLDER_IDS,
    INBOX_ID,
    SENT_ID,
    T1B_EXTENDED,
    T1B_FACTS,
    T1B_FIELDS,
    FakeTokens,
    extended,
    message,
)
from tests.support.logcapture import Capture
from tests.support.logfields import allowed_fields

Handler = Callable[[httpx2.Request], httpx2.Response]
SINCE = datetime(2026, 10, 7, 7, 0, tzinfo=UTC)
LATER = datetime(2026, 10, 20, 6, 0, tzinfo=UTC)
OWN = frozenset({"owner@example.test"})
TOP_K = "f" * 16  # the frontier covers its whole second (ruling, T15 fix round 1)


def js(body: object, status: int = 200) -> Handler:
    return lambda request: httpx2.Response(
        status, content=json.dumps(body).encode(), headers={"Content-Type": "application/json"}
    )


def values(*items: object, **extra: object) -> Handler:
    return js({"value": list(items)} | extra)


def item(n: int, at: datetime, folder_id: str = INBOX_ID, **extra: object) -> dict[str, object]:
    return message(n, **({"receivedDateTime": at.strftime("%Y-%m-%dT%H:%M:%SZ"), "parentFolderId": folder_id} | extra))


def run_of(n: int, start: datetime, count: int, folder_id: str = INBOX_ID, **extra: object) -> list[object]:
    """`count` items one minute apart, newest first, as Graph orders them."""
    return [item(n + i, start - timedelta(minutes=i), folder_id, **extra) for i in range(count)]


class Server:
    """Graph by request path: the well-known folder GETs answer FOLDER_IDS, list paths answer what a test sets."""

    def __init__(
        self,
        lists: dict[str, Handler] | None = None,
        *,
        folders: Mapping[str, object] | None = None,
        missing: tuple[str, ...] = (),
    ) -> None:
        self.requests: list[httpx2.Request] = []
        self.routes: dict[str, Handler] = {}
        for name, folder_id in (FOLDER_IDS if folders is None else folders).items():
            if name not in missing:
                self.routes[f"/v1.0/me/mailFolders/{name}"] = js({"id": folder_id})
        for folder_id, handler in (lists or {}).items():
            self.routes[f"/v1.0/me/mailFolders/{folder_id}/messages"] = handler

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        handler = self.routes.get(request.url.path)
        return handler(request) if handler is not None else httpx2.Response(404)

    def client(self, timeout: float) -> httpx2.Client:
        return httpx2.Client(transport=httpx2.MockTransport(self.handle), trust_env=False, follow_redirects=False)

    def lists(self) -> list[httpx2.Request]:
        return [r for r in self.requests if r.url.path.endswith("/messages")]

    def folder_gets(self) -> list[str]:
        return [r.url.path.rsplit("/", 1)[1] for r in self.requests if not r.url.path.endswith("/messages")]


def mailbox(
    server: Server,
    *,
    clock: FakeClock | None = None,
    tokens: FakeTokens | None = None,
    own: frozenset[str] = OWN,
    maps: FolderMapCache | None = None,
) -> GraphMailbox:
    clock = clock or FakeClock()
    return GraphMailbox(
        "outlook",
        tokens or FakeTokens(),
        client_factory=server.client,
        clock=clock,
        backoff=Backoff(),
        own_addresses=own,
        folder_maps=maps or FolderMapCache(clock=clock),
    )


def query(
    folder: ListFolder = "inbox",
    *,
    unread_only: bool = False,
    direction: str | None = None,
    limit: int = 20,
    position: Position | None = None,
) -> ListQuery:
    # direction is a plain str here so tests can pass it as a keyword; ListQuery types it as the Direction literal
    return ListQuery(folder, SINCE, unread_only, direction, limit, position)  # type: ignore[arg-type]


def listed(box: GraphMailbox, q: ListQuery, clock: FakeClock) -> FolderPage:
    return box.list_messages(q, CallDeadline.start(clock))


def ids(page: FolderPage) -> list[str]:
    return [i.ref.graph_id for i in page.items if isinstance(i.ref, GraphMessageRef)]


def position_of(graph_id: str, at: datetime) -> Position:
    return Position(int(at.timestamp()), location_k(GraphMessageRef("outlook", graph_id)))


# --- folder ids --------------------------------------------------------------------------------------------------


def test_folder_ids_built_once_per_hour() -> None:
    clock = FakeClock()
    maps = FolderMapCache(clock=clock)
    server = Server({INBOX_ID: values()})
    box = mailbox(server, clock=clock, maps=maps)
    listed(box, query(), clock)
    listed(box, query(), clock)
    assert server.folder_gets() == list(FOLDER_IDS)  # the seven well-known names, once
    folder_requests = [r for r in server.requests if not r.url.path.endswith("/messages")]
    assert all(dict(r.url.params) == {"$select": "id"} for r in folder_requests)
    assert all(r.headers["prefer"] == graph.PREFER for r in folder_requests)
    cached = maps.get("outlook")
    assert isinstance(cached, GraphFolderIds)
    assert dict(cached.ids) == FOLDER_IDS
    clock.advance(3599)
    listed(box, query(), clock)
    assert len(server.folder_gets()) == 7
    clock.advance(1)
    listed(box, query(), clock)
    assert server.folder_gets() == list(FOLDER_IDS) * 2


def test_missing_archive_404() -> None:
    clock = FakeClock()
    maps = FolderMapCache(clock=clock)
    server = Server({INBOX_ID: values(), SENT_ID: values()}, missing=("archive",))
    box = mailbox(server, clock=clock, maps=maps)
    page = listed(box, query("archive"), clock)
    assert page == FolderPage(items=[], more=False, truncated=False, complete=True)
    assert server.lists() == []
    cached = maps.get("outlook")
    assert isinstance(cached, GraphFolderIds)
    assert cached.ids["archive"] is None
    listed(box, query("all"), clock)
    assert [r.url.path for r in server.lists()] == [
        f"/v1.0/me/mailFolders/{INBOX_ID}/messages",
        f"/v1.0/me/mailFolders/{SENT_ID}/messages",
    ]


@pytest.mark.parametrize("name", ["inbox", "junkemail"])
@pytest.mark.parametrize("bad", ["a/../b", "x" * 513, "", 5, None, "a b", "a\n"])
def test_folder_id_validation(name: str, bad: object) -> None:
    clock = FakeClock()
    server = Server({INBOX_ID: values()}, folders=FOLDER_IDS | {name: bad})
    with pytest.raises(ProviderError) as info:
        listed(mailbox(server, clock=clock), query(), clock)
    assert info.value.code == "upstream_error"
    assert server.lists() == []


def test_valid_folder_id_is_percent_encoded_in_the_path() -> None:
    clock = FakeClock()
    odd = "AQMk=a-b_c"
    server = Server({odd: values()}, folders=FOLDER_IDS | {"inbox": odd})
    listed(mailbox(server, clock=clock), query(), clock)
    [request] = server.lists()
    assert request.url.raw_path.split(b"?")[0] == b"/v1.0/me/mailFolders/AQMk%3Da-b_c/messages"
    assert FOLDER_ID.fullmatch("x" * 512) is not None
    assert FOLDER_ID.fullmatch("x" * 513) is None


# --- request shape -----------------------------------------------------------------------------------------------


def test_constants_verbatim() -> None:
    assert SUMMARY_SELECT == (
        "id,receivedDateTime,isRead,hasAttachments,from,subject,bodyPreview,isDraft,internetMessageId,"
        "conversationId,parentFolderId,toRecipients,ccRecipients,flag,sentDateTime"
    )
    assert EXTENDED_EXPAND == (
        "singleValueExtendedProperties($filter=id eq 'Integer 0x1081' or id eq 'String 0x1042' "
        "or id eq 'String 0x1039')"
    )
    assert GRAPH_LIST_TOP == 101
    assert frozenset({102, 103}) == ANSWERED_VERBS
    assert WELL_KNOWN_FOR == {
        "inbox": ("inbox",),
        "sent": ("sentitems",),
        "archive": ("archive",),
        "trash": ("deleteditems",),
        "all": ("inbox", "archive", "sentitems"),
    }


def test_list_request_shape() -> None:
    clock = FakeClock()
    server = Server({INBOX_ID: values()})
    box = mailbox(server, clock=clock)
    listed(box, query(), clock)
    listed(box, query(unread_only=True, position=Position(int(LATER.timestamp()), "0" * 16)), clock)
    first, later = server.lists()
    assert dict(first.url.params) == {
        "$filter": "receivedDateTime ge 2026-10-07T07:00:00Z",
        "$orderby": "receivedDateTime desc",
        "$top": "101",
        "$select": SUMMARY_SELECT,
        "$expand": EXTENDED_EXPAND,
    }
    assert later.url.params["$filter"] == (
        "receivedDateTime ge 2026-10-07T07:00:00Z and receivedDateTime lt 2026-10-20T06:00:01Z and isRead eq false"
    )
    for request in (first, later):
        assert request.method == "GET"
        assert request.headers["prefer"] == 'IdType="ImmutableId", outlook.body-content-type="text"'
        assert request.headers["accept-encoding"] == "identity"


def test_list_filter_shapes() -> None:
    zurich = timezone(timedelta(hours=2))
    assert list_filter(datetime(2026, 10, 7, 9, 0, 0, 999_999, tzinfo=zurich), None, False) == (
        "receivedDateTime ge 2026-10-07T07:00:00Z"
    )
    assert list_filter(SINCE, LATER, False) == (
        "receivedDateTime ge 2026-10-07T07:00:00Z and receivedDateTime lt 2026-10-20T06:00:01Z"
    )
    assert list_filter(SINCE, None, True) == "receivedDateTime ge 2026-10-07T07:00:00Z and isRead eq false"


# --- item fields -------------------------------------------------------------------------------------------------

IDS = GraphFolderIds("outlook", FOLDER_IDS)  # type: ignore[arg-type]  # fixture keys are str, not GraphWellKnown


@pytest.mark.parametrize(
    ("entries", "answered"),
    [
        ([{"id": "Integer 0x1081", "value": "102"}], True),
        ([{"id": "integer 0x1081", "value": "102"}], True),  # G7: ids compared case-insensitively
        ([{"id": "INTEGER 0X1081", "value": "103"}], True),
        ([{"id": "Integer 0x1081", "value": "104"}], False),
        ([{"id": "Integer 0x1081", "value": "abc"}], False),
        ([{"id": "Integer 0x1081", "value": " 102"}], False),
        ([{"id": "Integer 0x1081", "value": "-102"}], False),
        ([{"id": "Integer 0x1081", "value": 102}], False),  # values arrive as strings; a number is not answered
        ([{"id": "Integer 0x1081", "value": "9" * 5000}], False),
        ([{"id": "Integer 0x1080", "value": "102"}], False),
        (["not an object", {"id": 5, "value": "102"}], False),
    ],
)
def test_answered_from_last_verb(entries: list[object], answered: bool) -> None:
    meta = graph_meta(message(1, singleValueExtendedProperties=entries), IDS, OWN, "outlook")
    assert meta.flags.answered is answered


def test_extended_fields() -> None:
    raw = message(
        1,
        singleValueExtendedProperties=[
            {"id": "integer 0x1081", "value": "102"},
            {"id": "STRING 0x1042", "value": "<parent@example.test>"},
            {"id": "String 0x1039", "value": "<root@example.test> <bad id@x> <parent@example.test> <m1@example.test>"},
        ],
        flag={"flagStatus": "flagged"},
    )
    meta = graph_meta(raw, IDS, OWN, "outlook")
    assert meta.folder == "inbox"
    assert meta.folder_name is None
    assert (meta.flags.answered, meta.flags.flagged) == (True, True)
    assert meta.direction == "incoming"
    assert meta.message_id == "m1@example.test"
    assert meta.in_reply_to == "parent@example.test"
    assert meta.references == ("root@example.test", "parent@example.test")  # own id and invalid tokens skipped
    assert meta.thread == ThreadKey("outlook", "graph", "AAQkCONV0001=")
    answered = graph_meta(message(2, singleValueExtendedProperties=extended(answered="abc")), IDS, OWN, "outlook")
    assert answered.flags.answered is False
    many = " ".join(f"<r{n}@example.test>" for n in range(MAX_REFERENCES + 5))
    long = graph_meta(message(3, singleValueExtendedProperties=extended(references=many)), IDS, OWN, "outlook")
    assert long.references == tuple(f"r{n}@example.test" for n in range(5, MAX_REFERENCES + 5))  # the newest kept


def test_extended_fields_through_the_list() -> None:
    clock = FakeClock()
    raw = item(1, LATER, singleValueExtendedProperties=extended("103", "<p@example.test>", "<r@example.test>"))
    page = listed(mailbox(Server({INBOX_ID: values(raw)}), clock=clock), query(), clock)
    [summary] = page.items
    assert (summary.meta.flags.answered, summary.meta.in_reply_to, summary.meta.references) == (
        True,
        "p@example.test",
        ("r@example.test",),
    )
    assert summary.to_addresses == ("owner@example.test",)
    assert summary.unread is True


def test_direction_and_folder_roles() -> None:
    sent = graph_meta(message(1, parentFolderId=SENT_ID), IDS, frozenset(), "outlook")
    assert (sent.folder, sent.direction) == ("sent", "outgoing")
    own_from = message(2, **{"from": {"emailAddress": {"address": "Owner+news@Example.test"}}})
    assert graph_meta(own_from, IDS, OWN, "outlook").direction == "outgoing"
    assert graph_meta(message(3, parentFolderId=ARCHIVE_ID), IDS, OWN, "outlook").folder == "archive"
    assert graph_meta(message(4, parentFolderId="AQMkOTHER"), IDS, OWN, "outlook").folder == "other"
    for parent in (FOLDER_IDS["junkemail"], FOLDER_IDS["drafts"], FOLDER_IDS["outbox"], None):
        with pytest.raises(ProviderError) as info:
            graph_meta(message(5, parentFolderId=parent), IDS, OWN, "outlook")
        assert info.value.code == "not_found"


@pytest.mark.parametrize("missing", ["absent", None, "garbage", {"id": "Integer 0x1081", "value": "102"}])
def test_missing_extended_properties_is_unknown(missing: object) -> None:
    raw = item(1, LATER)
    if missing != "absent":
        raw["singleValueExtendedProperties"] = missing
    assert "singleValueExtendedProperties" not in item(1, LATER)
    clock = FakeClock()
    page = listed(mailbox(Server({INBOX_ID: values(raw)}), clock=clock), query(), clock)
    [summary] = page.items
    assert (summary.meta.flags.answered, summary.meta.in_reply_to, summary.meta.references) == (False, None, ())
    assert summary.meta.message_id == "m1@example.test"
    assert (page.truncated, page.complete) == (False, True)


def test_drafts_dropped_per_item() -> None:
    clock = FakeClock()
    entries = [
        item(1, LATER, isDraft=True),
        item(2, LATER - timedelta(minutes=1)),
        item(3, LATER - timedelta(minutes=2), isDraft=False),
    ]
    page = listed(mailbox(Server({INBOX_ID: values(*entries)}), clock=clock), query(), clock)
    assert ids(page) == ["AAMkMSG0002=", "AAMkMSG0003="]


def test_excluded_parent_dropped_per_item() -> None:
    clock = FakeClock()
    entries = [
        item(1, LATER, FOLDER_IDS["junkemail"]),
        item(2, LATER, parentFolderId=None),
        item(3, LATER),
        item(4, LATER, parentFolderId=""),  # not a folder id: no parent (Sec8, D75 fail closed)
        item(5, LATER, parentFolderId="a/../b"),
    ]
    page = listed(mailbox(Server({INBOX_ID: values(*entries)}), clock=clock), query(), clock)
    assert ids(page) == ["AAMkMSG0003="]


@pytest.mark.parametrize("conversation", ["a/b", "x" * 301, 5, None, ""])
def test_conversation_id_outside_pattern_degrades_thread_only(conversation: object) -> None:
    clock = FakeClock()
    raw = item(1, LATER, conversationId=conversation)
    page = listed(mailbox(Server({INBOX_ID: values(raw)}), clock=clock), query(), clock)
    [summary] = page.items
    assert summary.meta.thread is None
    assert summary.meta.message_id == "m1@example.test"
    assert summary.subject == "Subject 1"


@pytest.mark.parametrize(
    ("folder", "folder_id", "direction"), [("sent", SENT_ID, "outgoing"), ("inbox", INBOX_ID, "incoming")]
)
def test_degraded_item_keeps_folder_and_direction(
    monkeypatch: pytest.MonkeyPatch, folder: ListFolder, folder_id: str, direction: str
) -> None:
    def broken(*args: object) -> object:
        raise ValueError("undecodable")

    monkeypatch.setattr(graph, "graph_meta", broken)
    clock = FakeClock()
    page = listed(mailbox(Server({folder_id: values(item(1, LATER, folder_id))}), clock=clock), query(folder), clock)
    [summary] = page.items
    assert summary.snippet_text == UNDECODABLE_NOTE
    assert (summary.meta.folder, summary.meta.direction) == (folder, direction)


def test_unread_only_and_since_refiltered_locally() -> None:
    clock = FakeClock()
    entries = [item(1, LATER, isRead=True), item(2, LATER), item(3, SINCE - timedelta(seconds=1))]
    page = listed(mailbox(Server({INBOX_ID: values(*entries)}), clock=clock), query(unread_only=True), clock)
    assert ids(page) == ["AAMkMSG0002="]


# --- keyset paging -----------------------------------------------------------------------------------------------


def test_position_drops_items_at_or_above() -> None:
    clock = FakeClock()
    a, b = "AAMkMSG0001=", "AAMkMSG0002="
    high, low = sorted((a, b), key=lambda g: location_k(GraphMessageRef("outlook", g)), reverse=True)
    older = LATER - timedelta(minutes=5)
    entries = [
        item(9, LATER + timedelta(seconds=30)),  # above the bound: Graph should not send it; dropped locally anyway
        item(int(high[7:11]), LATER),
        item(int(low[7:11]), LATER),
        item(3, older),
    ]
    page = listed(
        mailbox(Server({INBOX_ID: values(*entries)}), clock=clock), query(position=position_of(high, LATER)), clock
    )
    assert ids(page) == [low, "AAMkMSG0003="]
    assert (page.more, page.truncated, page.complete) == (False, False, True)


def test_full_page_without_item_below_position_stops_truncated() -> None:
    clock = FakeClock()
    stuck = [item(100 + n, LATER) for n in range(GRAPH_LIST_TOP)]  # more than 100 messages in one second
    server = Server({INBOX_ID: values(*stuck), ARCHIVE_ID: values(item(1, LATER - timedelta(hours=1)))})
    page = listed(
        mailbox(server, clock=clock), query("all", position=Position(int(LATER.timestamp()), "0" * 16)), clock
    )
    assert page.items == []
    assert (page.more, page.truncated, page.complete) == (False, True, False)  # no cursor past a stuck folder
    assert [r.url.path for r in server.lists()] == [f"/v1.0/me/mailFolders/{INBOX_ID}/messages"]  # account stops


def test_full_answer_inside_the_position_second_is_stuck() -> None:
    """Items after the position, but all in its second: a frontier there cannot move the cursor on (>= $top)."""
    clock = FakeClock()
    dense = [item(100 + n, LATER) for n in range(GRAPH_LIST_TOP)]
    middle = sorted((str(e["id"]) for e in dense), key=lambda g: location_k(GraphMessageRef("outlook", g)))[50]
    server = Server({INBOX_ID: values(*dense)})
    page = listed(mailbox(server, clock=clock), query(position=position_of(middle, LATER)), clock)
    assert page.items == []
    assert (page.more, page.truncated, page.complete, page.frontier) == (False, True, False, None)


def test_stuck_later_folder_of_all_keeps_earlier_items_without_cursor() -> None:
    clock = FakeClock()
    stuck = [item(100 + n, LATER, ARCHIVE_ID) for n in range(GRAPH_LIST_TOP)]  # one second, no position needed
    server = Server(
        {INBOX_ID: values(item(1, LATER - timedelta(hours=2))), ARCHIVE_ID: values(*stuck), SENT_ID: values()}
    )
    page = listed(mailbox(server, clock=clock), query("all"), clock)
    assert ids(page) == ["AAMkMSG0001="]
    assert (page.more, page.truncated, page.complete) == (False, True, False)
    assert [r.url.path.split("/")[4] for r in server.lists()] == [INBOX_ID, ARCHIVE_ID]  # sent never requested


def test_full_page_or_next_link_sets_more_and_is_not_followed() -> None:
    clock = FakeClock()
    server = Server({INBOX_ID: values(*run_of(1, LATER, GRAPH_LIST_TOP))})
    page = listed(mailbox(server, clock=clock), query(limit=20), clock)
    assert len(page.items) == 20
    assert (page.more, page.truncated, page.complete) == (True, False, True)
    assert page.frontier == (int((LATER - timedelta(minutes=GRAPH_LIST_TOP - 1)).timestamp()), TOP_K)
    link = "https://evil.example.test/v1.0/me/mailFolders/x/messages?$skip=101"
    older = LATER - timedelta(minutes=1)
    server = Server({INBOX_ID: values(item(1, LATER), item(2, older), **{"@odata.nextLink": link})})
    page = listed(mailbox(server, clock=clock), query(), clock)
    assert ids(page) == ["AAMkMSG0001="]  # the frontier second is read again on the next page
    assert page.more is True
    assert page.frontier == (int(older.timestamp()), TOP_K)
    assert len(server.lists()) == 1
    assert {r.url.host for r in server.requests} == {"graph.microsoft.com"}


def test_full_page_is_the_frontier_for_other_folders() -> None:
    """The cursor continues after the full page's last item, so no older item of another folder may come first."""
    clock = FakeClock()
    inbox = run_of(1, LATER, GRAPH_LIST_TOP)
    frontier = LATER - timedelta(minutes=GRAPH_LIST_TOP - 1)
    sent = [item(500, frontier + timedelta(seconds=30), SENT_ID), item(501, frontier - timedelta(minutes=1), SENT_ID)]
    server = Server({INBOX_ID: values(*inbox), ARCHIVE_ID: values(), SENT_ID: values(*sent)})
    page = listed(mailbox(server, clock=clock), query("all", limit=50), clock)
    assert len(page.items) == 50
    assert page.more is True
    assert page.frontier == (int(frontier.timestamp()), TOP_K)
    assert all(order_key(i)[0] > int(frontier.timestamp()) for i in page.items)
    wide = listed(mailbox(server, clock=clock), query("all", direction="outgoing", limit=50), clock)
    assert ids(wide) == ["AAMkMSG0500="]  # sent 501 lies below the inbox frontier: left for the next page
    page = listed(mailbox(server, clock=clock), query("sent"), clock)
    assert ids(page) == ["AAMkMSG0500=", "AAMkMSG0501="]  # a single folder: no frontier from another folder
    small = listed(
        mailbox(server, clock=clock),
        query("all", limit=50, position=Position(int((frontier + timedelta(minutes=2)).timestamp()), "0" * 16)),
        clock,
    )
    assert ids(small) == ["AAMkMSG0100=", "AAMkMSG0500="]  # nothing at or below the frontier second
    assert small.frontier == (int(frontier.timestamp()), TOP_K)


def test_zero_item_direction_page_reports_more_and_frontier() -> None:
    clock = FakeClock()
    server = Server({INBOX_ID: values(*run_of(1, LATER, GRAPH_LIST_TOP))})
    page = listed(mailbox(server, clock=clock), query(direction="outgoing"), clock)
    assert page.items == []
    assert (page.more, page.truncated, page.complete) == (True, False, True)
    assert page.frontier == (int((LATER - timedelta(minutes=GRAPH_LIST_TOP - 1)).timestamp()), TOP_K)


class GraphStore:
    """A folder answering like Graph: newest first, `lt` bound honoured, at most `$top` entries."""

    def __init__(self, entries: Sequence[object]) -> None:
        self.entries = [e for e in entries if isinstance(e, dict)]  # already in Graph's answer order

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        flt = request.url.params["$filter"]
        bound = flt.split("receivedDateTime lt ")[1].split(" ")[0] if " lt " in flt else None
        kept = [e for e in self.entries if bound is None or str(e["receivedDateTime"]) < bound]
        return values(*kept[: int(request.url.params["$top"])])(request)


def page_to_the_end(box: GraphMailbox, clock: FakeClock, *, limit: int = 50) -> list[str]:
    """Pages as the tool layer does for one account: the cursor at the last item, else at the page's frontier."""
    seen: list[str] = []
    position: Position | None = None
    for _ in range(20):
        page = listed(box, query(limit=limit, position=position), clock)
        assert page.complete
        seen += ids(page)
        if not page.more:
            return seen
        if page.items:
            position = Position(*order_key(page.items[-1]))
        else:
            assert page.frontier is not None
            position = Position(*page.frontier)
    raise AssertionError("paging did not end")


def test_frontier_below_the_position_second_progresses() -> None:
    """The cursor sits on the smallest-k item of second P; the `lt P+1s` answer re-reads P and the next occupied
    second F fills the rest of $top. F < P, so the frontier (F, "f"*16) progresses: not stuck."""
    clock = FakeClock()
    p_second, f_second = LATER, LATER - timedelta(seconds=5)
    in_p = [item(1000 + n, p_second) for n in range(60)]
    in_f = [item(2000 + n, f_second) for n in range(41)]  # fewer than $top: the next page reads it whole
    smallest = min((str(e["id"]) for e in in_p), key=lambda g: location_k(GraphMessageRef("outlook", g)))
    box = mailbox(Server({INBOX_ID: GraphStore(in_p + in_f + [item(3000, LATER - timedelta(hours=1))])}), clock=clock)
    page = listed(box, query(limit=50, position=position_of(smallest, p_second)), clock)
    assert page.items == []
    assert (page.more, page.truncated, page.complete) == (True, False, True)
    assert page.frontier == (int(f_second.timestamp()), TOP_K)
    following = listed(box, query(limit=50, position=Position(*page.frontier)), clock)
    assert sorted(ids(following)) == sorted([str(e["id"]) for e in in_f] + ["AAMkMSG3000="])
    assert following.more is False


def test_paging_through_dense_seconds_loses_nothing() -> None:
    """Regression (fix round 2): 60 messages in S1, 41 in S1 - 5 s and 1 an hour older, limit 50."""
    clock = FakeClock()
    s1 = LATER
    entries = (
        [item(1000 + n, s1) for n in range(60)]
        + [item(2000 + n, s1 - timedelta(seconds=5)) for n in range(41)]
        + [item(3000, s1 - timedelta(hours=1))]
    )
    box = mailbox(Server({INBOX_ID: GraphStore(entries)}), clock=clock)
    seen = page_to_the_end(box, clock)
    assert len(seen) == len(set(seen)) == 102
    assert set(seen) == {str(e["id"]) for e in entries}


def test_frontier_second_cut_by_top_returns_both_messages_across_pages() -> None:
    """$top cuts between two messages of one second: both are returned, over two pages (no same-second gap)."""
    clock = FakeClock()
    second = LATER - timedelta(minutes=GRAPH_LIST_TOP - 1)
    pair = sorted(
        (f"AAMkMSG{n:04d}=" for n in (300, 301)), key=lambda g: location_k(GraphMessageRef("outlook", g))
    )  # Graph lists the smaller k first, so $top cuts off the one with the larger k
    own = {"from": {"emailAddress": {"address": "owner@example.test"}}}
    entries = run_of(1, LATER, GRAPH_LIST_TOP - 1) + [item(int(g[7:11]), second, INBOX_ID, **own) for g in pair]
    store = GraphStore(entries)
    box = mailbox(Server({INBOX_ID: store}), clock=clock)
    first = listed(box, query(direction="outgoing"), clock)
    assert first.items == []  # the listed copy sits in the frontier second: not returned yet
    assert first.frontier == (int(second.timestamp()), TOP_K)
    assert first.more is True
    later = listed(box, query(direction="outgoing", position=Position(*first.frontier)), clock)
    assert sorted(ids(later)) == sorted(pair)
    assert later.more is False


def _over_cap(request: httpx2.Request) -> httpx2.Response:
    return httpx2.Response(200, content=b'{"value": [' + b" " * (graph.MAX_LIST_RESPONSE_BYTES + 1) + b"]}")


def test_over_cap_answer_retried_once_with_top_halved() -> None:
    clock = FakeClock()
    answers = iter([_over_cap, values(item(1, LATER))])
    server = Server({INBOX_ID: lambda request: next(answers)(request)})
    page = listed(mailbox(server, clock=clock), query(), clock)
    assert ids(page) == ["AAMkMSG0001="]
    assert [r.url.params["$top"] for r in server.lists()] == ["101", "50"]
    first, second = (dict(r.url.params) for r in server.lists())
    assert {k: v for k, v in first.items() if k != "$top"} == {k: v for k, v in second.items() if k != "$top"}
    server = Server({INBOX_ID: _over_cap})
    with pytest.raises(ProviderError) as info:
        listed(mailbox(server, clock=clock), query(), clock)
    assert info.value.code == "too_large"
    assert [r.url.params["$top"] for r in server.lists()] == ["101", "50"]


@pytest.mark.parametrize(("count", "more"), [(50, True), (49, False)])
def test_effective_top_after_halving_drives_more(count: int, more: bool) -> None:
    clock = FakeClock()
    answers = iter([_over_cap, values(*run_of(1, LATER, count))])
    server = Server({INBOX_ID: lambda request: next(answers)(request)})
    page = listed(mailbox(server, clock=clock), query(limit=50), clock)
    assert len(page.items) == 49  # a full answer of 50 leaves its last (frontier) second for the next page
    assert page.more is more
    assert (page.frontier is not None) is more


# --- folders, direction, soft stop -------------------------------------------------------------------------------


def test_all_queries_three_folders_and_merges() -> None:
    clock = FakeClock()
    server = Server(
        {
            INBOX_ID: values(item(1, LATER), item(2, LATER - timedelta(minutes=30))),
            ARCHIVE_ID: values(item(3, LATER - timedelta(minutes=10), ARCHIVE_ID)),
            SENT_ID: values(item(4, LATER - timedelta(minutes=20), SENT_ID)),
        }
    )
    page = listed(mailbox(server, clock=clock), query("all"), clock)
    assert [r.url.path.split("/")[4] for r in server.lists()] == [INBOX_ID, ARCHIVE_ID, SENT_ID]
    assert ids(page) == ["AAMkMSG0001=", "AAMkMSG0003=", "AAMkMSG0004=", "AAMkMSG0002="]
    assert [(i.meta.folder, i.meta.direction) for i in page.items] == [
        ("inbox", "incoming"),
        ("archive", "incoming"),
        ("sent", "outgoing"),
        ("inbox", "incoming"),
    ]
    assert page.items == sorted(page.items, key=order_key, reverse=True)
    assert (page.more, page.truncated, page.complete) == (False, False, True)
    limited = listed(mailbox(server, clock=clock), query("all", limit=2), clock)
    assert ids(limited) == ["AAMkMSG0001=", "AAMkMSG0003="]
    assert limited.more is True


@pytest.mark.parametrize(("folder", "folder_id"), [("sent", SENT_ID), ("trash", FOLDER_IDS["deleteditems"])])
def test_single_folders_use_their_well_known_id(folder: ListFolder, folder_id: str) -> None:
    clock = FakeClock()
    server = Server({folder_id: values(item(1, LATER, folder_id))})
    page = listed(mailbox(server, clock=clock), query(folder), clock)
    assert [r.url.path for r in server.lists()] == [f"/v1.0/me/mailFolders/{folder_id}/messages"]
    assert [i.meta.folder for i in page.items] == [folder]


def test_direction_filter_per_page() -> None:
    clock = FakeClock()
    entries: list[object] = run_of(1, LATER, GRAPH_LIST_TOP)
    for n in (5, 40):
        # run_of returns list[object]; every entry is a dict built by item()
        entries[n] = dict(entries[n]) | {"from": {"emailAddress": {"address": "owner@example.test"}}}  # type: ignore[call-overload]
    page = listed(mailbox(Server({INBOX_ID: values(*entries)}), clock=clock), query(direction="outgoing"), clock)
    assert ids(page) == ["AAMkMSG0006=", "AAMkMSG0041="]
    assert page.more is True  # fewer than `limit` items while more exist
    incoming = listed(mailbox(Server({INBOX_ID: values(*entries)}), clock=clock), query(direction="incoming"), clock)
    assert len(incoming.items) == 20
    assert all(i.meta.direction == "incoming" for i in incoming.items)


def test_soft_stop_before_second_folder_is_partial() -> None:
    clock = FakeClock()

    def slow_inbox(request: httpx2.Request) -> httpx2.Response:
        clock.advance(17.0)  # the answer arrives after the 16 s soft stop
        return values(item(1, LATER))(request)

    server = Server({INBOX_ID: slow_inbox, ARCHIVE_ID: values(), SENT_ID: values()})
    page = listed(mailbox(server, clock=clock), query("all"), clock)
    assert ids(page) == ["AAMkMSG0001="]
    assert (page.truncated, page.complete) == (True, False)
    assert len(server.lists()) == 1


def test_soft_stop_while_building_folder_ids_is_an_empty_partial_page() -> None:
    clock = FakeClock()
    server = Server({INBOX_ID: values(item(1, LATER))})

    def slow_inbox_id(request: httpx2.Request) -> httpx2.Response:
        clock.advance(17.0)
        return js({"id": INBOX_ID})(request)

    server.routes["/v1.0/me/mailFolders/inbox"] = slow_inbox_id
    page = listed(mailbox(server, clock=clock), query(), clock)
    assert page == FolderPage(items=[], more=False, truncated=True, complete=False)
    assert server.folder_gets() == ["inbox"]


class SpyTokens(FakeTokens):
    def __init__(self, clock: FakeClock) -> None:
        super().__init__()
        self.clock = clock
        self.calls: list[tuple[float, float]] = []  # (now, deadline passed)

    def access_token(self, deadline: float) -> str:
        self.calls.append((self.clock(), deadline))
        return super().access_token(deadline)


def test_no_token_refresh_after_soft_stop() -> None:
    clock = FakeClock()
    tokens = SpyTokens(clock)

    def unauthorized_late(request: httpx2.Request) -> httpx2.Response:
        clock.advance(17.0)
        return httpx2.Response(401, content=b"{}")

    server = Server({INBOX_ID: unauthorized_late})
    deadline = CallDeadline.start(clock)
    page = mailbox(server, clock=clock, tokens=tokens).list_messages(query(), deadline)
    assert tokens.calls == [(deadline.started, deadline.hard_at)]  # the refresh after the 401 is never started
    assert tokens.invalidated == 1
    assert page == FolderPage(items=[], more=False, truncated=True, complete=False)

    late = SpyTokens(clock)
    server = Server({INBOX_ID: values(item(1, LATER))})
    deadline = CallDeadline.start(clock)
    clock.advance(16.5)
    page = mailbox(server, clock=clock, tokens=late).list_messages(query(), deadline)
    assert late.calls == []
    assert server.requests == []
    assert (page.truncated, page.complete) == (True, False)


def test_soft_stop_in_check_and_get_message_is_upstream_timeout() -> None:
    clock = FakeClock()
    tokens = SpyTokens(clock)
    box = mailbox(Server(), clock=clock, tokens=tokens)
    for call in (box.check, lambda d: box.get_message(GraphMessageRef("outlook", "AAMkMSG0001="), d)):
        deadline = CallDeadline.start(clock)
        clock.advance(16.5)
        with pytest.raises(ProviderError) as info:
            call(deadline)
        assert (info.value.code, info.value.cause) == ("upstream_timeout", "SoftStop")
    assert tokens.calls == []


def test_unread_inbox_query_filters_on_read_state() -> None:
    clock = FakeClock()
    server = Server({INBOX_ID: values(item(1, LATER), item(2, LATER - timedelta(minutes=1), isRead=True))})
    page = mailbox(server, clock=clock).list_messages(
        ListQuery("inbox", SINCE, True, None, 20), CallDeadline.start(clock)
    )
    assert [i.ref for i in page.items] == [GraphMessageRef("outlook", "AAMkMSG0001=")]
    assert page.more is False
    [request] = server.lists()
    assert request.url.params["$filter"] == "receivedDateTime ge 2026-10-07T07:00:00Z and isRead eq false"
    assert not hasattr(graph, "INBOX_IDS")
    assert not hasattr(GraphMailbox, "_inbox_id")


# --- wire hygiene ------------------------------------------------------------------------------------------------


def test_every_request_is_get_to_graph_host() -> None:
    clock = FakeClock()
    server = Server(
        {
            INBOX_ID: values(item(1, LATER)),
            ARCHIVE_ID: values(item(2, LATER, ARCHIVE_ID)),
            SENT_ID: values(item(3, LATER, SENT_ID)),
        }
    )
    listed(mailbox(server, clock=clock), query("all"), clock)
    assert len(server.requests) == 7 + 3
    for request in server.requests:
        assert (request.method, request.url.scheme, request.url.host, request.url.port) == (
            "GET",
            "https",
            "graph.microsoft.com",
            None,
        )
        assert request.url.path.startswith("/v1.0/me/mailFolders/")


def test_logs_carry_no_filter_values_or_folder_ids() -> None:
    configure_logging("DEBUG")
    capture = Capture()
    logging.getLogger().addHandler(capture)
    try:
        clock = FakeClock()
        entries = [
            item(1, LATER, singleValueExtendedProperties=extended("102", "<irt@example.test>", "<refs@example.test>")),
            {"id": "AAMk/../BAD", "subject": "SUBJECT-SENTINEL"},
            item(2, LATER, receivedDateTime="garbage"),
        ]
        server = Server({INBOX_ID: values(*entries), ARCHIVE_ID: values(), SENT_ID: values()})
        above = Position(int((LATER + timedelta(minutes=1)).timestamp()), "0" * 16)
        page = listed(mailbox(server, clock=clock), query("all", unread_only=True, position=above), clock)
    finally:
        logging.getLogger().removeHandler(capture)
    assert len(page.items) == 1
    events = [json.loads(line) for line in capture.lines]
    assert [e["event"] for e in events].count("item_degraded") == 2
    assert "folder_map_refreshed" in [e["event"] for e in events]
    for event in events:
        assert set(event) <= allowed_fields(event["event"])
    joined = "\n".join(capture.lines + [m for _, _, m in capture.raw])
    for secret in (
        *FOLDER_IDS.values(),
        "receivedDateTime",
        "isRead",
        "2026-10-07T07",
        "2026-10-20T06",
        "AAQkCONV",
        "example.test",
        "SUBJECT-SENTINEL",
        "BAD",
        "graph.microsoft.com",
        "AT-SENTINEL",
    ):
        assert secret not in joined


# --- get_message: D75 binding, isDraft, lookup by ref (T16) ------------------------------------------------------

REF_PATH = "/v1.0/me/messages"
TRASH_ID = FOLDER_IDS["deleteditems"]
JUNK_ID = FOLDER_IDS["junkemail"]
OTHER_ID = "AQMkOTHERFOLDER"
MID9 = "m9@example.test"


def message_path(graph_id: str) -> str:
    return f"{REF_PATH}/{graph_id}"


def graph_id(n: int) -> str:
    return f"AAMkMSG{n:04d}="


def detail_body(n: int, folder_id: str = INBOX_ID, **extra: object) -> dict[str, object]:
    return message(n, **({"parentFolderId": folder_id, "body": {"contentType": "text", "content": "Hello"}} | extra))


def copy_of(n: int, at: datetime, folder_id: str, **extra: object) -> dict[str, object]:
    """A lookup hit: a copy of MID9 received at `at` in `folder_id`."""
    return item(n, at, folder_id, **({"internetMessageId": f"<{MID9}>"} | extra))


def message_server(*bodies: dict[str, object], lookup: Handler | None = None) -> Server:
    server = Server()
    for body in bodies:
        server.routes[message_path(str(body["id"]))] = js(body)
    if lookup is not None:
        server.routes[REF_PATH] = lookup
    return server


def primed(clock: FakeClock, folder_ids: GraphFolderIds = IDS) -> FolderMapCache:
    """A folder-id map read by an earlier call."""
    maps = FolderMapCache(clock=clock)
    maps.put(folder_ids)
    return maps


def folder_reads(server: Server) -> int:
    return sum(
        1
        for r in server.requests
        if r.url.path.startswith("/v1.0/me/mailFolders/") and not r.url.path.endswith("/messages")
    )


def opened(server: Server) -> list[str]:
    return [r.url.path.rsplit("/", 1)[1] for r in server.requests if r.url.path.startswith(REF_PATH + "/")]


def by_id(box: GraphMailbox, graph_id_text: str, clock: FakeClock) -> MailDetail:
    return box.get_message(GraphMessageRef("outlook", graph_id_text), CallDeadline.start(clock))


def by_ref(box: GraphMailbox, message_id: str, clock: FakeClock) -> MailDetail:
    return box.get_message(StableRef("outlook", message_id), CallDeadline.start(clock))


def test_message_in_archive_allowed() -> None:
    clock = FakeClock()
    server = message_server(detail_body(1, ARCHIVE_ID))
    detail = by_id(mailbox(server, clock=clock), graph_id(1), clock)
    assert detail.ref == GraphMessageRef("outlook", graph_id(1))
    assert detail.meta.folder == "archive"
    assert detail.body_text == "Hello"
    assert folder_reads(server) == 7  # read in this call: never read a second time


@pytest.mark.parametrize("name", ["junkemail", "drafts", "outbox"])
def test_parent_junk_drafts_outbox_not_found_after_one_rebuild(name: str) -> None:
    clock = FakeClock()
    fresh = message_server(detail_body(1, FOLDER_IDS[name]))
    with pytest.raises(ProviderError) as info:
        by_id(mailbox(fresh, clock=clock), graph_id(1), clock)
    assert (info.value.code, info.value.cause) == ("not_found", "ExcludedFolder")
    assert folder_reads(fresh) == 7  # the map was read in this call: no re-read before the refusal

    maps = primed(clock)
    server = message_server(detail_body(1, FOLDER_IDS[name]))
    box = mailbox(server, clock=clock, maps=maps)
    with pytest.raises(ProviderError) as info:
        by_id(box, graph_id(1), clock)
    assert (info.value.code, info.value.cause) == ("not_found", "ExcludedFolder")
    assert folder_reads(server) == 7  # a map from an earlier call is read once more before the refusal
    clock.advance(59)
    with pytest.raises(ProviderError) as info:
        by_id(box, graph_id(1), clock)
    assert info.value.code == "not_found"
    assert folder_reads(server) == 7  # Sec16: at most one forced re-read per 60 s per account
    clock.advance(1)
    with pytest.raises(ProviderError):
        by_id(box, graph_id(1), clock)
    assert folder_reads(server) == 14


def test_is_draft_not_found() -> None:
    clock = FakeClock()
    server = message_server(detail_body(1, isDraft=True))
    with pytest.raises(ProviderError) as info:
        by_id(mailbox(server, clock=clock), graph_id(1), clock)
    assert (info.value.code, info.value.cause) == ("not_found", "Draft")
    assert folder_reads(server) == 0
    assert opened(server) == [graph_id(1)]


def test_unknown_parent_is_other() -> None:
    clock = FakeClock()
    maps = primed(clock)
    server = message_server(detail_body(1, OTHER_ID))
    box = mailbox(server, clock=clock, maps=maps)
    assert by_id(box, graph_id(1), clock).meta.folder == "other"
    assert folder_reads(server) == 7  # the cached map did not know the parent: one re-read
    assert by_id(box, graph_id(1), clock).meta.folder == "other"
    assert folder_reads(server) == 7  # within 60 s the cached map answers (Sec16)

    # A stale map: the re-read shows that the unknown parent is the junk folder now.
    clock.advance(60)
    maps.put(GraphFolderIds("outlook", FOLDER_IDS | {"junkemail": "AQMkOLDJUNK"}))  # type: ignore[arg-type]
    moved = message_server(detail_body(1, JUNK_ID))
    with pytest.raises(ProviderError) as info:
        by_id(mailbox(moved, clock=clock, maps=maps), graph_id(1), clock)
    assert (info.value.code, info.value.cause) == ("not_found", "ExcludedFolder")
    assert folder_reads(moved) == 7


def test_ref_lookup_request_shape() -> None:
    clock = FakeClock()
    server = message_server(detail_body(1), lookup=values(item(1, LATER)))
    detail = by_ref(mailbox(server, clock=clock), "m1@example.test", clock)
    assert detail.ref == GraphMessageRef("outlook", graph_id(1))
    assert [r.url.path for r in server.requests] == [
        REF_PATH,
        *(f"/v1.0/me/mailFolders/{name}" for name in FOLDER_IDS),
        message_path(graph_id(1)),
    ]
    lookup, message_get = server.requests[0], server.requests[-1]
    assert lookup.method == "GET"
    assert dict(lookup.url.params) == {
        "$filter": "internetMessageId eq '<m1@example.test>'",
        "$select": SUMMARY_SELECT,
        "$top": "50",
    }
    assert lookup.headers["prefer"] == graph.PREFER
    assert dict(message_get.url.params) == {"$select": MESSAGE_SELECT, "$expand": EXTENDED_EXPAND}
    assert graph.REF_LOOKUP_TOP == 50


def test_ref_filter_special_characters_byte_identical() -> None:
    clock = FakeClock()
    odd = "a+&#%=;/?@x"
    server = message_server(lookup=values())
    with pytest.raises(ProviderError):
        by_ref(mailbox(server, clock=clock), odd, clock)
    [request] = server.requests
    assert request.url.path == REF_PATH  # nothing of the value reaches the path
    # The recorded wire bytes (D74): every special character percent-encoded inside the one $filter parameter.
    assert b"%24filter=internetMessageId+eq+%27%3Ca%2B%26%23%25%3D%3B%2F%3F%40x%3E%27&" in request.url.query
    decoded = parse_qs(request.url.query.decode("ascii"), keep_blank_values=True, strict_parsing=True)
    assert decoded["$filter"] == ["internetMessageId eq '<a+&#%=;/?@x>'"]
    assert request.url.params["$filter"] == "internetMessageId eq '<a+&#%=;/?@x>'"


def test_ref_lookup_choice() -> None:
    clock = FakeClock()
    hits = [
        copy_of(1, LATER, INBOX_ID),  # a later copy in the inbox
        copy_of(2, LATER - timedelta(hours=1), ARCHIVE_ID),  # the earliest acceptable copy
        copy_of(3, LATER - timedelta(hours=2), JUNK_ID),  # excluded folder
        copy_of(4, LATER - timedelta(hours=3), FOLDER_IDS["outbox"]),  # excluded folder
        copy_of(5, LATER - timedelta(hours=4), INBOX_ID, isDraft=True),  # a draft
        item(6, LATER - timedelta(hours=5), INBOX_ID, internetMessageId="<M9@example.test>"),  # case differs
        item(7, LATER - timedelta(hours=6), INBOX_ID, internetMessageId="<m9@example.test.x>"),  # another id
        copy_of(8, LATER - timedelta(hours=7), INBOX_ID, receivedDateTime="garbage"),  # no usable time
    ]
    server = message_server(detail_body(2, ARCHIVE_ID), lookup=values(*hits))
    detail = by_ref(mailbox(server, clock=clock), MID9, clock)
    assert detail.ref == GraphMessageRef("outlook", graph_id(2))
    assert detail.meta.folder == "archive"
    assert opened(server) == [graph_id(2)]

    # Equal received_at: the order inbox, archive, sent, trash, other decides (S-e), whatever order Graph answers in.
    ranked = [INBOX_ID, ARCHIVE_ID, SENT_ID, TRASH_ID, OTHER_ID]
    for start in range(len(ranked)):
        tied = [copy_of(10 + n, LATER, folder_id) for n, folder_id in enumerate(ranked)][start:]
        server = message_server(
            *(detail_body(10 + n, folder_id) for n, folder_id in enumerate(ranked)), lookup=values(*reversed(tied))
        )
        assert by_ref(mailbox(server, clock=clock), MID9, clock).ref == GraphMessageRef("outlook", graph_id(10 + start))


def test_ref_lookup_returned_id_pattern_checked() -> None:
    clock = FakeClock()
    forged = copy_of(1, LATER - timedelta(hours=1), INBOX_ID, id="a/b")
    server = message_server(detail_body(2), lookup=values(forged, copy_of(2, LATER, INBOX_ID)))
    assert by_ref(mailbox(server, clock=clock), MID9, clock).ref == GraphMessageRef("outlook", graph_id(2))
    assert opened(server) == [graph_id(2)]
    for bad in ("a/b", "x" * 301, "", 5, None):
        alone = message_server(lookup=values(copy_of(1, LATER, INBOX_ID, id=bad)))
        with pytest.raises(ProviderError) as info:
            by_ref(mailbox(alone, clock=clock), MID9, clock)
        assert (info.value.code, info.value.cause) == ("not_found", "RefNotFound")
        assert [r.url.path for r in alone.requests] == [REF_PATH]


def test_ref_not_found() -> None:
    clock = FakeClock()
    server = message_server(lookup=values())
    with pytest.raises(ProviderError) as info:
        by_ref(mailbox(server, clock=clock), MID9, clock)
    assert (info.value.code, info.value.cause) == ("not_found", "RefNotFound")
    assert [r.url.path for r in server.requests] == [REF_PATH]  # no folder ids, no message read

    # Only unacceptable hits on a page that is not full: a complete search, so not_found.
    only_junk = message_server(lookup=values(copy_of(1, LATER, JUNK_ID), copy_of(2, LATER, INBOX_ID, isDraft=True)))
    with pytest.raises(ProviderError) as info:
        by_ref(mailbox(only_junk, clock=clock), MID9, clock)
    assert (info.value.code, info.value.cause) == ("not_found", "RefNotFound")
    assert opened(only_junk) == []


def _late_401(clock: FakeClock) -> Handler:
    def answer(request: httpx2.Request) -> httpx2.Response:
        clock.advance(17.0)  # the soft stop passes while the lookup runs; the retry after the 401 never starts
        return httpx2.Response(401, content=b"{}")

    return answer


def _deadline_mid_body(clock: FakeClock) -> Handler:
    def answer(request: httpx2.Request) -> httpx2.Response:
        def body() -> Iterator[bytes]:
            yield b'{"value": ['
            clock.advance(25.0)  # the 20 s call deadline passes between two chunks
            yield b"]}"

        return httpx2.Response(200, content=body())

    return answer


def _status(status: int) -> Callable[[FakeClock], Handler]:
    return lambda clock: js({"error": {"code": "x"}}, status)


def _raw(content: bytes) -> Callable[[FakeClock], Handler]:
    return lambda clock: lambda request: httpx2.Response(200, content=content)


def _connect_error(clock: FakeClock) -> Handler:
    def fail(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("c")

    return fail


_FULL_OF_JUNK = values(*(copy_of(n, LATER, JUNK_ID) for n in range(50)))  # the lookup $top


@pytest.mark.parametrize(
    ("make", "code", "cause"),
    [
        pytest.param(_late_401, "upstream_timeout", "SoftStop", id="soft-deadline"),
        pytest.param(_deadline_mid_body, "upstream_timeout", "CallDeadline", id="hard-deadline"),
        pytest.param(_raw(b"x" * 1_100_000), "upstream_timeout", "IncompleteSearch", id="too-large-after-halving"),
        pytest.param(_status(400), "upstream_timeout", "IncompleteSearch", id="400"),
        pytest.param(_status(404), "upstream_timeout", "IncompleteSearch", id="404-collection"),
        pytest.param(_status(500), "upstream_timeout", "IncompleteSearch", id="500"),
        pytest.param(_status(502), "upstream_timeout", "IncompleteSearch", id="502"),
        pytest.param(_status(504), "upstream_timeout", "IncompleteSearch", id="504"),
        pytest.param(_raw(b"{not json"), "upstream_timeout", "IncompleteSearch", id="malformed-json"),
        pytest.param(_raw(b'{"value": 5}'), "upstream_timeout", "IncompleteSearch", id="value-not-a-list"),
        pytest.param(lambda clock: _FULL_OF_JUNK, "upstream_timeout", "IncompleteSearch", id="full-page-no-hit"),
        pytest.param(
            lambda clock: values(copy_of(1, LATER, JUNK_ID), **{"@odata.nextLink": f"https://{graph.GRAPH_HOST}/x"}),
            "upstream_timeout",
            "IncompleteSearch",
            id="next-link-no-hit",
        ),
        pytest.param(_status(401), "auth_expired", "GraphUnauthorized", id="401-twice"),
        pytest.param(_connect_error, "unreachable", "ConnectError", id="unreachable"),
        pytest.param(_status(429), "upstream_error", "Throttled", id="429"),
        pytest.param(_status(503), "upstream_error", "Throttled", id="503"),
        pytest.param(_status(403), "upstream_error", "Forbidden", id="403"),
    ],
)
def test_ref_lookup_failure_mapping(make: Callable[[FakeClock], Handler], code: str, cause: str) -> None:
    clock = FakeClock()
    server = message_server(lookup=make(clock))
    with pytest.raises(ProviderError) as info:
        by_ref(mailbox(server, clock=clock), MID9, clock)
    assert (info.value.code, info.value.cause) == (code, cause)  # never not_found (B6)
    assert opened(server) == []
    assert all(r.url.host == graph.GRAPH_HOST and r.url.path != "/x" for r in server.requests)  # no nextLink GET


def test_ref_lookup_over_cap_retried_once_with_top_halved() -> None:
    clock = FakeClock()
    tops: list[str] = []

    def answer(request: httpx2.Request) -> httpx2.Response:
        tops.append(request.url.params["$top"])
        if len(tops) == 1:
            return httpx2.Response(200, content=b"x" * 1_100_000)
        return values(*(copy_of(n, LATER, JUNK_ID) for n in range(24)), copy_of(30, LATER, INBOX_ID))(request)

    server = message_server(detail_body(30), lookup=answer)
    assert by_ref(mailbox(server, clock=clock), MID9, clock).ref == GraphMessageRef("outlook", graph_id(30))
    assert tops == ["50", "25"]
    # 25 entries after halving fill the effective $top, but an acceptable hit was found: it is used.


def test_soft_stop_in_get_message_is_upstream_timeout() -> None:
    clock = FakeClock()

    def slow(body: Handler) -> Handler:
        def answer(request: httpx2.Request) -> httpx2.Response:
            clock.advance(17.0)  # the answer arrives after the 16 s soft stop
            return body(request)

        return answer

    lookup = message_server(detail_body(1), lookup=slow(values(item(1, LATER))))
    with pytest.raises(ProviderError) as info:
        by_ref(mailbox(lookup, clock=clock), "m1@example.test", clock)
    assert (info.value.code, info.value.cause) == ("upstream_timeout", "SoftStop")
    assert [r.url.path for r in lookup.requests] == [REF_PATH]

    by_id_server = Server()
    by_id_server.routes[message_path(graph_id(1))] = slow(js(detail_body(1)))
    with pytest.raises(ProviderError) as info:
        by_id(mailbox(by_id_server, clock=clock), graph_id(1), clock)
    assert (info.value.code, info.value.cause) == ("upstream_timeout", "SoftStop")
    assert folder_reads(by_id_server) == 0


def test_message_extended_meta() -> None:
    clock = FakeClock()
    raw = detail_body(
        1,
        SENT_ID,
        singleValueExtendedProperties=extended("102", "<p@example.test>", "<r@example.test> <p@example.test>"),
        flag={"flagStatus": "flagged"},
    )
    server = message_server(raw)
    detail = by_id(mailbox(server, clock=clock), graph_id(1), clock)
    assert detail.meta == MailMeta(
        folder="sent",
        folder_name=None,
        flags=MailFlags(answered=True, flagged=True),
        direction="outgoing",
        message_id="m1@example.test",
        in_reply_to="p@example.test",
        references=("r@example.test", "p@example.test"),
        thread=ThreadKey("outlook", "graph", "AAQkCONV0001="),
    )
    [message_get] = [r for r in server.requests if r.url.path == message_path(graph_id(1))]
    assert message_get.url.params["$expand"] == EXTENDED_EXPAND
    for name in ("isDraft", "internetMessageId", "conversationId", "parentFolderId", "flag"):
        assert name in MESSAGE_SELECT.split(",")
        assert name in FALLBACK_SELECT.split(",")


def test_foreign_or_malformed_stable_refs_not_found_before_any_request() -> None:
    clock = FakeClock()
    server = Server()
    box = mailbox(server, clock=clock)
    for target, cause in (
        (StableRef("uzh", MID9), "ForeignRef"),
        (StableRef("outlook", "it's@example.test"), "MalformedRef"),
        (StableRef("outlook", ""), "MalformedRef"),
    ):
        with pytest.raises(ProviderError) as info:
            box.get_message(target, CallDeadline.start(clock))
        assert (info.value.code, info.value.cause) == ("not_found", cause)
    assert server.requests == []


def test_get_message_logs_carry_no_message_id_or_filter() -> None:
    configure_logging("DEBUG")
    capture = Capture()
    logging.getLogger().addHandler(capture)
    try:
        clock = FakeClock()
        hits = [copy_of(1, LATER, INBOX_ID, id="AAMk/../BAD"), copy_of(2, LATER, INBOX_ID, receivedDateTime="x")]
        server = message_server(lookup=values(*hits))
        with pytest.raises(ProviderError):
            by_ref(mailbox(server, clock=clock, maps=primed(clock)), MID9, clock)
    finally:
        logging.getLogger().removeHandler(capture)
    events = [json.loads(line) for line in capture.lines]
    assert [e["event"] for e in events].count("item_degraded") == 2
    for event in events:
        assert set(event) <= allowed_fields(event["event"])
    joined = "\n".join(capture.lines + [m for _, _, m in capture.raw])
    for secret in (MID9, "internetMessageId", "example.test", "BAD", *FOLDER_IDS.values(), "AT-SENTINEL"):
        assert secret not in joined


# --- T1b facts (owner's Graph Explorer run) ----------------------------------------------------------------------


@pytest.mark.xfail(strict=True, reason="T1b facts pending")
def test_fixture_shapes_match_t1b_facts() -> None:
    assert T1B_FACTS is not None, "record the owner's Graph Explorer facts in tests/support/graph_fixtures.py"
    full = message(1, singleValueExtendedProperties=extended("102", "<p@example.test>", "<r@example.test>"))
    for name in T1B_FIELDS:
        assert (name in full) is T1B_FACTS[name]
    entries = full.get("singleValueExtendedProperties")
    present = {entry["id"] for entry in entries} if isinstance(entries, list) else set()
    for name in T1B_EXTENDED:
        assert (name in present) is T1B_FACTS[name]
    assert T1B_FACTS["request10_older_than_e"] is True  # the `lt` upper bound holds (S-f)
    replied = T1B_FACTS["replied_last_verb"]
    assert isinstance(replied, str)
    assert replied in ("102", "103")
    assert _answered(replied) is True  # the reported value is what `answered` reads
    assert T1B_FACTS["folder_ids_in_pattern"] is True  # requests 1-7: letters, digits, "=", "_" and "-" only
    assert all(FOLDER_ID.fullmatch(folder_id) for folder_id in FOLDER_IDS.values())
