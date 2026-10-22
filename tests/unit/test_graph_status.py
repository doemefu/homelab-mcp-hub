"""Graph `get_message_status` (spec 080 §5.2 "Location, Graph" and "Failures", §5.4): the `internetMessageId`
location batches, the `g` id reads, the failure mapping and the soft stop (T12)."""

import json
import logging
from collections.abc import Callable, Iterator, Sequence
from datetime import UTC, datetime, timedelta

import httpx2
import pytest

from mcp_hub.ids import GraphMessageRef, MessageRef, StableRef
from mcp_hub.logging import configure_logging
from mcp_hub.providers import graph
from mcp_hub.providers.base import MessageStatus, ProviderError, StatusTarget
from mcp_hub.providers.deadline import CallDeadline
from mcp_hub.providers.folders import FolderMapCache, GraphFolderIds
from mcp_hub.providers.graph import EXTENDED_EXPAND, SUMMARY_SELECT, Backoff, GraphMailbox
from mcp_hub.providers.graph_status import MAX_FILTER_CHARS, STATUS_LOOKUP_TOP
from tests.support.clock import FakeClock
from tests.support.graph_fixtures import (
    ARCHIVE_ID,
    DRAFTS_ID,
    FOLDER_IDS,
    INBOX_ID,
    JUNK_ID,
    OUTBOX_ID,
    SENT_ID,
    T1C_FACTS,
    TRASH_ID,
    FakeTokens,
    extended,
    message,
)
from tests.support.logcapture import Capture
from tests.support.logfields import allowed_fields

Handler = Callable[[httpx2.Request], httpx2.Response]
PATH = "/v1.0/me/messages"
LATER = datetime(2026, 10, 20, 6, 0, tzinfo=UTC)
OWN = frozenset({"owner@example.test"})
OTHER_ID = "AQMkOTHERFOLDER"
IDS = GraphFolderIds("outlook", FOLDER_IDS)  # type: ignore[arg-type]  # fixture keys are str, not GraphWellKnown
TOO_LARGE = b"x" * 1_100_000  # above the 1 MiB list cap


def mid(n: int) -> str:
    return f"m{n}@example.test"


def gid(n: int) -> str:
    return f"AAMkMSG{n:04d}="


def ref(n: int) -> StableRef:
    return StableRef("outlook", mid(n))


def gref(n: int) -> GraphMessageRef:
    return GraphMessageRef("outlook", gid(n))


def iso(at: datetime) -> str:
    return at.strftime("%Y-%m-%dT%H:%M:%SZ")


def copy(n: int, of: int, at: datetime = LATER, folder_id: str = INBOX_ID, **extra: object) -> dict[str, object]:
    """Graph item `n`, a copy of the message with Message-ID `mid(of)`."""
    return message(
        n,
        **({"internetMessageId": f"<{mid(of)}>", "receivedDateTime": iso(at), "parentFolderId": folder_id} | extra),
    )


def without(item: dict[str, object], key: str) -> dict[str, object]:
    return {k: v for k, v in item.items() if k != key}


def js(body: object, status: int = 200, headers: dict[str, str] | None = None) -> Handler:
    return lambda request: httpx2.Response(
        status, content=json.dumps(body).encode(), headers={"Content-Type": "application/json"} | (headers or {})
    )


def values(*items: object, **extra: object) -> Handler:
    return js({"value": list(items)} | extra)


def raw(content: bytes) -> Handler:
    return lambda request: httpx2.Response(200, content=content)


def store(*items: dict[str, object]) -> Handler:
    """Answers like Graph: the items whose `internetMessageId` the request's `$filter` names, up to `$top`."""

    def answer(request: httpx2.Request) -> httpx2.Response:
        text = request.url.params["$filter"]
        top = int(request.url.params["$top"])
        chosen = [i for i in items if f"internetMessageId eq '{i['internetMessageId']}'" in text]
        return values(*chosen[:top])(request)

    return answer


def sequence(*handlers: Handler) -> Handler:
    """The n-th request gets the n-th handler; the last one answers every later request."""
    calls: list[int] = []

    def answer(request: httpx2.Request) -> httpx2.Response:
        calls.append(1)
        return handlers[min(len(calls), len(handlers)) - 1](request)

    return answer


def advancing(clock: FakeClock, seconds: float, handler: Handler) -> Handler:
    def answer(request: httpx2.Request) -> httpx2.Response:
        clock.advance(seconds)
        return handler(request)

    return answer


class Server:
    """Graph by path: the folder GETs answer FOLDER_IDS, PATH the location handler, PATH/<id> the `g` reads."""

    def __init__(self, located: Handler | None = None, reads: dict[str, Handler] | None = None) -> None:
        self.requests: list[httpx2.Request] = []
        self.routes: dict[str, Handler] = {
            f"/v1.0/me/mailFolders/{name}": js({"id": folder_id}) for name, folder_id in FOLDER_IDS.items()
        }
        self.routes[PATH] = located or values()
        for graph_id, handler in (reads or {}).items():
            self.routes[f"{PATH}/{graph_id}"] = handler

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        handler = self.routes.get(request.url.path)
        return handler(request) if handler is not None else httpx2.Response(404)

    def client(self, timeout: float) -> httpx2.Client:
        return httpx2.Client(transport=httpx2.MockTransport(self.handle), trust_env=False, follow_redirects=False)

    def located(self) -> list[httpx2.Request]:
        return [r for r in self.requests if r.url.path == PATH]

    def reads(self) -> list[str]:
        return [r.url.path.rsplit("/", 1)[1] for r in self.requests if r.url.path.startswith(PATH + "/")]

    def folder_reads(self) -> int:
        return sum(1 for r in self.requests if r.url.path.startswith("/v1.0/me/mailFolders/"))


def primed(clock: FakeClock) -> FolderMapCache:
    """A folder-id map read by an earlier call (warm)."""
    maps = FolderMapCache(clock=clock)
    maps.put(IDS)
    return maps


def mailbox(
    server: Server,
    clock: FakeClock,
    *,
    tokens: FakeTokens | None = None,
    maps: FolderMapCache | None = None,
    backoff: Backoff | None = None,
) -> GraphMailbox:
    return GraphMailbox(
        "outlook",
        tokens or FakeTokens(),
        client_factory=server.client,
        clock=clock,
        backoff=backoff or Backoff(),
        own_addresses=OWN,
        folder_maps=maps or FolderMapCache(clock=clock),
    )


def status(box: GraphMailbox, targets: Sequence[StatusTarget], clock: FakeClock) -> list[MessageStatus]:
    return box.message_status(targets, CallDeadline.start(clock))


def states(result: list[MessageStatus]) -> list[str]:
    return [s.state if s.reason is None else s.reason for s in result]


def location_of(result: MessageStatus) -> GraphMessageRef | None:
    found = result.found
    return found.location if found is not None and isinstance(found.location, GraphMessageRef) else None


def raw_filter(request: httpx2.Request) -> bytes:
    """The `$filter` value exactly as recorded on the wire."""
    [part] = [p for p in request.url.query.split(b"&") if p.startswith(b"%24filter=")]
    return part[len(b"%24filter=") :]


class Logs:
    def __init__(self) -> None:
        self.capture = Capture()

    def __enter__(self) -> "Logs":
        configure_logging("DEBUG")
        logging.getLogger().addHandler(self.capture)
        return self

    def __exit__(self, *args: object) -> None:
        logging.getLogger().removeHandler(self.capture)

    def events(self) -> list[dict[str, object]]:
        return [json.loads(line) for line in self.capture.lines]

    def named(self, event: str) -> list[dict[str, object]]:
        return [e for e in self.events() if e["event"] == event]

    def text(self) -> str:
        return "\n".join(self.capture.lines + [m for _, _, m in self.capture.raw])


# --- location: request shapes ------------------------------------------------------------------------------------


def test_ref_batches_of_ten_with_filter_shape() -> None:
    clock = FakeClock()
    server = Server(store(*(copy(n, n) for n in range(11))))
    result = status(mailbox(server, clock, maps=primed(clock)), [ref(n) for n in range(11)], clock)
    assert states(result) == ["found"] * 11
    first, second = server.located()
    assert dict(first.url.params) == {
        "$filter": " or ".join(f"internetMessageId eq '<{mid(n)}>'" for n in range(10)),
        "$select": SUMMARY_SELECT,
        "$expand": EXTENDED_EXPAND,
        "$top": "50",
    }
    assert dict(second.url.params) == dict(first.url.params) | {"$filter": f"internetMessageId eq '<{mid(10)}>'"}
    for request in (first, second):
        assert request.method == "GET"
        assert request.headers["prefer"] == graph.PREFER
        assert "$orderby" not in request.url.params
    assert server.folder_reads() == 0
    assert STATUS_LOOKUP_TOP == 50


def test_filter_boundary_1800_and_1801_on_the_wire() -> None:
    # One plain value encodes to 33 characters plus its length ("internetMessageId+eq+%27%3C" … "%3E%27"), and
    # " or " to "+or+": five 300-character values and one of 82 give exactly 1,800 encoded characters.
    long_ids = [chr(ord("a") + i) * 300 for i in range(5)]
    for pad, requests in ((82, 1), (83, 2)):
        clock = FakeClock()
        server = Server()
        targets = [StableRef("outlook", v) for v in [*long_ids, "z" * pad]]
        assert states(status(mailbox(server, clock, maps=primed(clock)), targets, clock)) == ["gone"] * 6
        located = server.located()
        assert len(located) == requests
        lengths = [len(raw_filter(r)) for r in located]
        assert all(length <= MAX_FILTER_CHARS for length in lengths)
        if requests == 1:
            assert lengths == [1800]
        else:
            assert located[1].url.params["$filter"] == f"internetMessageId eq '<{'z' * pad}>'"


def test_message_id_with_specials_round_trips_encoded() -> None:
    clock = FakeClock()
    odd = "a+&#%=;/?@x"
    server = Server(store(message(1, internetMessageId=f"<{odd}>")))
    [result] = status(mailbox(server, clock, maps=primed(clock)), [StableRef("outlook", odd)], clock)
    assert result.state == "found"
    [request] = server.requests
    assert request.url.path == PATH  # nothing of the value reaches the path
    assert raw_filter(request) == b"internetMessageId+eq+%27%3Ca%2B%26%23%25%3D%3B%2F%3F%40x%3E%27"
    assert request.url.params["$filter"] == f"internetMessageId eq '<{odd}>'"


def test_duplicate_refs_one_value() -> None:
    clock = FakeClock()
    server = Server(store(copy(1, 1), copy(2, 2)))
    result = status(mailbox(server, clock, maps=primed(clock)), [ref(1), ref(2), ref(1)], clock)
    [request] = server.located()
    assert request.url.params["$filter"] == f"internetMessageId eq '<{mid(1)}>' or internetMessageId eq '<{mid(2)}>'"
    assert len(result) == 3
    assert result[0] == result[2]
    assert location_of(result[0]) == gref(1)


# --- location: hits ----------------------------------------------------------------------------------------------


def test_found_earliest_copy_and_tie_rank() -> None:
    clock = FakeClock()
    early = LATER - timedelta(hours=1)
    items = (
        copy(1, 1, LATER, INBOX_ID),
        copy(2, 1, early, TRASH_ID),
        copy(3, 1, early, ARCHIVE_ID),  # the tie at `early`: archive before trash (S-e)
        copy(4, 2, LATER, SENT_ID),
        copy(5, 2, LATER, INBOX_ID),  # the tie: inbox before sent, whatever Graph's order
    )
    server = Server(store(*items))
    first, second = status(mailbox(server, clock, maps=primed(clock)), [ref(1), ref(2)], clock)
    assert location_of(first) == gref(3)
    assert first.found is not None
    assert (first.found.meta.folder, first.found.received_at, first.found.message_id) == ("archive", early, mid(1))
    assert location_of(second) == gref(5)
    assert (first.thread_checked, first.reply_at, first.reply_message_id, first.newer_incoming_at) == (
        False,
        None,
        None,
        None,
    )


def test_junk_drafts_outbox_isdraft_only_is_gone() -> None:
    clock = FakeClock()
    items = (
        copy(1, 1, folder_id=JUNK_ID),
        copy(2, 2, folder_id=DRAFTS_ID),
        copy(3, 3, folder_id=OUTBOX_ID),
        copy(4, 4, isDraft=True),
        copy(5, 5, folder_id=JUNK_ID),
        copy(6, 5, folder_id=TRASH_ID),
    )
    server = Server(store(*items))
    result = status(mailbox(server, clock, maps=primed(clock)), [ref(n) for n in range(1, 6)], clock)
    assert states(result) == ["gone"] * 4 + ["found"]
    assert all(s.thread_checked is None and s.found is None for s in result[:4])
    assert location_of(result[4]) == gref(6)
    assert server.folder_reads() == 0  # an excluded parent never forces a re-read in the location step


def test_unknown_parent_is_other() -> None:
    clock = FakeClock()
    server = Server(store(copy(1, 1, folder_id=OTHER_ID)))
    [result] = status(mailbox(server, clock, maps=primed(clock)), [ref(1)], clock)
    assert result.found is not None
    assert result.found.meta.folder == "other"
    assert server.folder_reads() == 7  # a map from an earlier call is read once more for an unknown parent

    fresh = Server(store(copy(1, 1, folder_id=OTHER_ID)))
    [result] = status(mailbox(fresh, clock), [ref(1)], clock)
    assert result.found is not None
    assert result.found.meta.folder == "other"
    assert fresh.folder_reads() == 7  # a map read in this call is used as it is


@pytest.mark.parametrize(
    "extra", [{}, {"@odata.nextLink": f"https://{graph.GRAPH_HOST}/v1.0/next"}], ids=["full-top", "next-link"]
)
def test_full_answer_or_next_link_marks_missing_refs_incomplete(extra: dict[str, object]) -> None:
    clock = FakeClock()
    if extra:
        answer = values(copy(1, 1), **extra)
    else:
        answer = values(copy(1, 1), *(copy(n, 1, folder_id=JUNK_ID) for n in range(2, 51)))
    server = Server(answer)
    first, second = status(mailbox(server, clock, maps=primed(clock)), [ref(1), ref(2)], clock)
    assert location_of(first) == gref(1)  # a hit on a full page stays found (R39)
    assert second == MessageStatus("unknown", "incomplete_search")  # never gone (Graph G4)
    assert [r.url.path for r in server.requests] == [PATH]  # the nextLink is never requested


# --- failures ----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("code", [400, 404, 500])
def test_failed_batch_marks_only_its_refs(code: int) -> None:
    clock = FakeClock()
    server = Server(sequence(js({"error": {"code": "x"}}, code), store(copy(10, 10))))
    with Logs() as logs:
        result = status(mailbox(server, clock, maps=primed(clock)), [ref(n) for n in range(11)], clock)
    assert states(result) == ["incomplete_search"] * 10 + ["found"]
    assert len(server.located()) == 2  # the next batch is still sent
    [failed] = logs.named("provider_call_failed")
    assert failed["level"] == "WARNING"
    assert failed["exception"] == {404: "GraphNotFound"}.get(code, "UnexpectedStatus")


def test_throttle_stops_later_requests_and_keeps_found_refs() -> None:
    clock = FakeClock()
    throttled = js({}, 429, {"Retry-After": "30"})
    server = Server(
        sequence(store(*(copy(n, n) for n in range(21))), throttled, store(*(copy(n, n) for n in range(21)))),
        {gid(90): js(copy(90, 90))},
    )
    targets: list[StatusTarget] = [*(ref(n) for n in range(21)), gref(90)]
    with Logs() as logs:
        result = status(mailbox(server, clock, maps=primed(clock)), targets, clock)
    assert states(result) == ["found"] * 10 + ["incomplete_search"] * 12
    assert len(server.located()) == 2  # the third batch and the `g` read fail fast, without a request
    assert server.reads() == []
    assert [e["exception"] for e in logs.named("provider_call_failed")] == ["Throttled"] * 3  # one per failed batch


def test_short_throttle_later_batch_sent_after_backoff() -> None:
    clock = FakeClock()

    class Elapsing(Backoff):
        def set(self, account_id: str, now: float, seconds: int) -> None:
            super().set(account_id, now, seconds)
            clock.advance(2.0)  # time passes after the throttled answer

    hits = store(*(copy(n, n) for n in range(21)))
    server = Server(sequence(hits, js({}, 429, {"Retry-After": "1"}), hits))
    box = mailbox(server, clock, maps=primed(clock), backoff=Elapsing())
    result = status(box, [ref(n) for n in range(21)], clock)
    assert states(result) == ["found"] * 10 + ["incomplete_search"] * 10 + ["found"]
    assert len(server.located()) == 3


def test_too_large_retried_with_half_top_then_batch_failure() -> None:
    clock = FakeClock()
    tops: list[str] = []

    def answer(request: httpx2.Request) -> httpx2.Response:
        tops.append(request.url.params["$top"])
        return raw(TOO_LARGE)(request)

    server = Server(answer)
    with Logs() as logs:
        [result] = status(mailbox(server, clock, maps=primed(clock)), [ref(1)], clock)
    assert result == MessageStatus("unknown", "incomplete_search")
    assert tops == ["50", "25"]
    [failed] = logs.named("provider_call_failed")
    assert (failed["outcome"], failed["exception"]) == ("too_large", "ResponseTooLarge")

    clock = FakeClock()
    halved = values(*(copy(n, 1, folder_id=JUNK_ID) for n in range(24)), copy(30, 1))
    server = Server(sequence(raw(TOO_LARGE), halved))
    first, second = status(mailbox(server, clock, maps=primed(clock)), [ref(1), ref(2)], clock)
    assert location_of(first) == gref(30)
    assert second == MessageStatus("unknown", "incomplete_search")  # 25 entries fill the effective $top


def test_degraded_matching_hit_is_incomplete_not_gone() -> None:
    clock = FakeClock()
    items = (
        copy(1, 1, id="AAMk/../BAD"),
        copy(2, 2, receivedDateTime="garbage"),
        without(copy(3, 3), "parentFolderId"),
        copy(4, 4, parentFolderId="AQMk/../BAD"),
        copy(5, 5, id="AAMk/../BAD"),
        copy(6, 5),
    )
    server = Server(store(*items))
    with Logs() as logs:
        result = status(mailbox(server, clock, maps=primed(clock)), [ref(n) for n in range(1, 7)], clock)
    assert states(result) == ["incomplete_search"] * 4 + ["found", "gone"]
    assert location_of(result[4]) == gref(6)  # another acceptable copy wins over the unusable one
    degraded = logs.named("item_degraded")
    assert len(degraded) == 5
    assert all("item" not in e for e in degraded)


def test_item_degraded_logged_at_most_twenty_times() -> None:
    clock = FakeClock()
    server = Server(store(*(copy(n, n, receivedDateTime="garbage") for n in range(30))))
    with Logs() as logs:
        result = status(mailbox(server, clock, maps=primed(clock)), [ref(n) for n in range(30)], clock)
    assert states(result) == ["incomplete_search"] * 30
    assert len(logs.named("item_degraded")) == 20  # SR6


def test_any_copy_in_sentitems_makes_direction_outgoing() -> None:
    clock = FakeClock()
    items = (copy(1, 1, LATER - timedelta(hours=1)), copy(2, 1, LATER, SENT_ID), copy(3, 2))
    server = Server(store(*items))
    first, second = status(mailbox(server, clock, maps=primed(clock)), [ref(1), ref(2)], clock)
    assert first.found is not None
    assert second.found is not None
    assert location_of(first) == gref(1)  # the earliest copy (MF-R2)
    assert (first.found.meta.folder, first.found.meta.direction) == ("inbox", "outgoing")
    assert (second.found.meta.folder, second.found.meta.direction) == ("inbox", "incoming")


def test_hostile_subject_preview_from_never_reach_result_or_logs() -> None:
    clock = FakeClock()
    hostile: dict[str, object] = {
        "subject": "SUBJECT-SENTINEL",
        "bodyPreview": "PREVIEW-SENTINEL",
        "from": {"emailAddress": {"name": "FROM-NAME-SENTINEL", "address": "from-sentinel@example.org"}},
        "toRecipients": [{"emailAddress": {"name": "TO-SENTINEL", "address": "to-sentinel@example.org"}}],
    }
    server = Server(
        sequence(store(copy(1, 1, **hostile), copy(2, 2, receivedDateTime="garbage", **hostile)), js({}, 500)),
        {gid(3): js(copy(3, 3, **hostile))},
    )
    targets: list[StatusTarget] = [ref(1), ref(2), gref(3), *(ref(n) for n in range(10, 20))]
    with Logs() as logs:
        result = status(mailbox(server, clock, maps=primed(clock)), targets, clock)
    assert states(result)[:3] == ["found", "incomplete_search", "found"]
    shown = repr(result) + logs.text()
    for sentinel in ("SUBJECT-SENTINEL", "PREVIEW-SENTINEL", "FROM-NAME-SENTINEL", "from-sentinel", "TO-SENTINEL"):
        assert sentinel not in shown


def connect_error(request: httpx2.Request) -> httpx2.Response:
    raise httpx2.ConnectError("c")


def late_body(clock: FakeClock) -> Handler:
    def answer(request: httpx2.Request) -> httpx2.Response:
        def body() -> Iterator[bytes]:
            yield b'{"value": ['
            clock.advance(25.0)  # the 20 s call deadline passes between two chunks
            yield b"]}"

        return httpx2.Response(200, content=body())

    return answer


@pytest.mark.parametrize(
    ("case", "code", "cause"),
    [
        ("backoff-at-start", "upstream_error", "Throttled"),
        ("token-failure", "auth_expired", "NoToken"),
        ("401-twice", "auth_expired", "GraphUnauthorized"),
        ("403", "upstream_error", "Forbidden"),
        ("403-graph-id", "upstream_error", "Forbidden"),
        ("folder-ids-500", "upstream_error", "UnexpectedStatus"),
        ("transport", "unreachable", "ConnectError"),
        ("hard-deadline", "upstream_timeout", "CallDeadline"),
    ],
)
def test_account_failures(case: str, code: str, cause: str) -> None:
    clock = FakeClock()
    backoff = Backoff()
    maps: FolderMapCache | None = primed(clock)
    tokens = FakeTokens()
    located: Handler = store(copy(1, 1))
    reads = {gid(2): js(copy(2, 2))}
    if case == "backoff-at-start":
        backoff.set("outlook", clock(), 30)
    elif case == "token-failure":

        class Failing(FakeTokens):
            def access_token(self, deadline: float) -> str:
                raise ProviderError("auth_expired", "NoToken")

        tokens = Failing()
    elif case == "401-twice":
        located = js({}, 401)
    elif case == "403":
        located = js({}, 403)
    elif case == "403-graph-id":
        reads = {gid(2): js({}, 403)}
    elif case == "folder-ids-500":
        maps = None
    elif case == "transport":
        located = connect_error
    elif case == "hard-deadline":
        located = late_body(clock)
    server = Server(located, reads)
    if case == "folder-ids-500":
        server.routes["/v1.0/me/mailFolders/archive"] = js({}, 500)
    box = mailbox(server, clock, tokens=tokens, maps=maps, backoff=backoff)
    with pytest.raises(ProviderError) as info:
        status(box, [ref(1), gref(2)], clock)
    assert (info.value.code, info.value.cause) == (code, cause)


def test_401_once_refreshes_and_restarts_without_duplicate_state() -> None:
    clock = FakeClock()
    tokens = FakeTokens()
    hits = store(copy(1, 1), copy(2, 2))
    server = Server(sequence(js({}, 401), hits))
    result = status(mailbox(server, clock, tokens=tokens, maps=primed(clock)), [ref(1), ref(2)], clock)
    assert [location_of(s) for s in result] == [gref(1), gref(2)]
    assert (tokens.issued, tokens.invalidated) == (2, 1)
    assert len(server.located()) == 2
    clean = Server(hits)
    assert status(mailbox(clean, clock, maps=primed(clock)), [ref(1), ref(2)], clock) == result


def test_401_on_second_location_batch_refreshes_not_batch_failure() -> None:
    clock = FakeClock()
    tokens = FakeTokens()
    hits = store(*(copy(n, n) for n in range(11)))
    server = Server(sequence(hits, js({}, 401), hits))
    with Logs() as logs:
        result = status(mailbox(server, clock, tokens=tokens, maps=primed(clock)), [ref(n) for n in range(11)], clock)
    assert states(result) == ["found"] * 11
    assert len(server.located()) == 4  # the whole work ran again after the refresh
    assert (tokens.issued, tokens.invalidated) == (2, 1)
    assert logs.named("provider_call_failed") == []


def test_401_in_graph_id_read_refreshes() -> None:
    clock = FakeClock()
    tokens = FakeTokens()
    server = Server(store(copy(1, 1)), {gid(2): sequence(js({}, 401), js(copy(2, 2)))})
    result = status(mailbox(server, clock, tokens=tokens, maps=primed(clock)), [ref(1), gref(2)], clock)
    assert [location_of(s) for s in result] == [gref(1), gref(2)]
    assert (tokens.issued, tokens.invalidated) == (2, 1)
    assert server.reads() == [gid(2), gid(2)]
    assert len(server.located()) == 2


# --- `g` reads ---------------------------------------------------------------------------------------------------


def test_graph_id_read_shape_without_body() -> None:
    clock = FakeClock()
    server = Server(reads={gid(1): js(copy(1, 1))})
    [result] = status(mailbox(server, clock, maps=primed(clock)), [gref(1)], clock)
    assert location_of(result) == gref(1)
    [request] = server.requests
    assert (request.method, request.url.path) == ("GET", f"{PATH}/{gid(1)}")
    assert dict(request.url.params) == {"$select": SUMMARY_SELECT, "$expand": EXTENDED_EXPAND}
    assert "body" not in SUMMARY_SELECT.split(",")
    assert request.headers["prefer"] == graph.PREFER


def test_graph_id_get_follows_move() -> None:
    clock = FakeClock()
    server = Server(
        reads={gid(1): js(copy(1, 1, folder_id=ARCHIVE_ID)), gid(2): js(copy(2, 2, internetMessageId="<a b>"))}
    )
    first, second = status(mailbox(server, clock, maps=primed(clock)), [gref(1), gref(2)], clock)
    assert first.found is not None
    assert second.found is not None
    assert (first.found.location, first.found.meta.folder, first.found.message_id) == (gref(1), "archive", mid(1))
    assert (second.found.location, second.found.message_id) == (gref(2), None)  # ref null, still found (A-mf2)
    assert first.thread_checked is False
    assert server.located() == []  # an immutable id needs no location step


def test_graph_id_reads_deduplicated() -> None:
    clock = FakeClock()
    server = Server(reads={gid(1): js(copy(1, 1))})
    result = status(mailbox(server, clock, maps=primed(clock)), [gref(1), gref(1)], clock)
    assert result[0] == result[1]
    assert server.reads() == [gid(1)]


@pytest.mark.parametrize(
    "answer",
    [js({}, 404), js(copy(1, 1, isDraft=True)), js(copy(1, 1, folder_id=JUNK_ID)), js(copy(1, 1, folder_id=OUTBOX_ID))],
    ids=["404", "draft", "junk", "outbox"],
)
def test_graph_id_404_draft_or_excluded_parent_is_gone(answer: Handler) -> None:
    clock = FakeClock()
    server = Server(reads={gid(1): answer})
    with Logs() as logs:
        [result] = status(mailbox(server, clock, maps=primed(clock)), [gref(1)], clock)
    assert result == MessageStatus("gone")
    assert logs.named("provider_call_failed") == []


def test_graph_id_excluded_parent_rereads_the_map_once_before_gone() -> None:
    clock = FakeClock()
    server = Server(reads={gid(1): js(copy(1, 1, folder_id=JUNK_ID))})
    [result] = status(mailbox(server, clock, maps=primed(clock)), [gref(1)], clock)
    assert result == MessageStatus("gone")
    assert server.folder_reads() == 7  # before_refusal: a map from an earlier call is read once more (Sec16)


@pytest.mark.parametrize(
    "answer",
    [js({}, 400), js({}, 500), js({}, 502), raw(TOO_LARGE), raw(b"{not json")],
    ids=["400", "500", "502", "too-large", "malformed"],
)
def test_graph_id_400_5xx_or_too_large_is_incomplete(answer: Handler) -> None:
    clock = FakeClock()
    server = Server(reads={gid(1): answer, gid(2): js(copy(2, 2))})
    with Logs() as logs:
        first, second = status(mailbox(server, clock, maps=primed(clock)), [gref(1), gref(2)], clock)
    assert first == MessageStatus("unknown", "incomplete_search")
    assert location_of(second) == gref(2)  # the next read still runs
    assert len(logs.named("provider_call_failed")) == 1


@pytest.mark.parametrize(
    "body",
    [
        without(copy(1, 1), "parentFolderId"),
        copy(1, 1, parentFolderId="AQMk/../BAD"),
        copy(1, 1, receivedDateTime="garbage"),
    ],
    ids=["missing-parent", "malformed-parent", "bad-time"],
)
def test_graph_id_missing_parent_is_incomplete(body: dict[str, object]) -> None:
    clock = FakeClock()
    server = Server(reads={gid(1): js(body)})
    [result] = status(mailbox(server, clock, maps=primed(clock)), [gref(1)], clock)
    assert result == MessageStatus("unknown", "incomplete_search")  # GR2: never gone


def test_answered_from_last_verb_102_103_not_104() -> None:
    clock = FakeClock()
    items = (
        copy(1, 1, singleValueExtendedProperties=extended("102")),
        copy(2, 2, singleValueExtendedProperties=extended("103")),
        copy(3, 3, singleValueExtendedProperties=extended("104")),
        copy(4, 4),
    )
    server = Server(store(*items), {gid(5): js(copy(5, 5, singleValueExtendedProperties=extended("102")))})
    result = status(mailbox(server, clock, maps=primed(clock)), [*(ref(n) for n in range(1, 5)), gref(5)], clock)
    assert [s.found.meta.flags.answered for s in result if s.found is not None] == [True, True, False, False, True]


def test_unsearchable_targets_marked_alone_without_request() -> None:
    clock = FakeClock()
    server = Server(store(copy(1, 1)))
    targets: list[StatusTarget] = [
        MessageRef("outlook", "INBOX", 1, 1),
        StableRef("outlook", "a'b"),
        StableRef("icloud", mid(1)),
        GraphMessageRef("outlook", "AAMk/../BAD"),
        ref(1),
    ]
    result = status(mailbox(server, clock, maps=primed(clock)), targets, clock)
    assert states(result) == ["id_mismatch", "incomplete_search", "incomplete_search", "incomplete_search", "found"]
    [request] = server.requests
    assert request.url.params["$filter"] == f"internetMessageId eq '<{mid(1)}>'"


# --- soft stop ---------------------------------------------------------------------------------------------------


def test_soft_stop_between_batches_keeps_found_refs() -> None:
    clock = FakeClock()
    hits = store(*(copy(n, n) for n in range(11)))
    server = Server(sequence(advancing(clock, 17.0, hits), hits), {gid(90): js(copy(90, 90))})
    targets: list[StatusTarget] = [*(ref(n) for n in range(11)), gref(90)]
    with Logs() as logs:
        result = status(mailbox(server, clock, maps=primed(clock)), targets, clock)
    assert states(result) == ["found"] * 10 + ["incomplete_search"] * 2
    assert len(server.requests) == 1
    assert logs.named("provider_call_failed") == []


def test_soft_stop_raised_in_batch_is_not_a_batch_failure() -> None:
    clock = FakeClock()
    server = Server(sequence(advancing(clock, 17.0, raw(TOO_LARGE)), store(copy(1, 1))), {gid(90): js(copy(90, 90))})
    with Logs() as logs:
        result = status(mailbox(server, clock, maps=primed(clock)), [ref(1), gref(90)], clock)
    assert states(result) == ["incomplete_search"] * 2
    assert len(server.requests) == 1  # the halved retry, the `g` read: no later request is sent
    assert logs.named("provider_call_failed") == []


def test_soft_stop_before_token_refresh_all_incomplete() -> None:
    clock = FakeClock()
    tokens = FakeTokens()
    server = Server(store(copy(1, 1)))
    deadline = CallDeadline.start(clock)
    clock.advance(16.5)
    result = mailbox(server, clock, tokens=tokens).message_status([ref(1), gref(2), ref(1)], deadline)
    assert result == [MessageStatus("unknown", "incomplete_search")] * 3
    assert server.requests == []
    assert tokens.issued == 0


def test_soft_stop_while_reading_folder_ids_all_incomplete() -> None:
    clock = FakeClock()
    server = Server(store(copy(1, 1)))
    server.routes["/v1.0/me/mailFolders/inbox"] = advancing(clock, 17.0, js({"id": INBOX_ID}))
    result = status(mailbox(server, clock), [ref(1)], clock)
    assert result == [MessageStatus("unknown", "incomplete_search")]
    assert server.located() == []


def test_no_token_refresh_after_soft_stop() -> None:
    clock = FakeClock()
    tokens = FakeTokens()
    server = Server(advancing(clock, 17.0, js({}, 401)))
    result = status(mailbox(server, clock, tokens=tokens, maps=primed(clock)), [ref(1)], clock)
    assert result == [MessageStatus("unknown", "incomplete_search")]
    assert (tokens.issued, tokens.invalidated) == (1, 1)  # the refresh after the 401 is never started


# --- wire and log hygiene ----------------------------------------------------------------------------------------


def test_every_request_is_get_to_graph_host() -> None:
    clock = FakeClock()
    server = Server(store(copy(1, 1), copy(2, 1, folder_id=OTHER_ID)), {gid(3): js(copy(3, 3))})
    status(mailbox(server, clock), [ref(1), ref(4), gref(3)], clock)
    assert len(server.requests) == 7 + 1 + 1
    for request in server.requests:
        assert (request.method, request.url.scheme, request.url.host, request.url.port) == (
            "GET",
            "https",
            "graph.microsoft.com",
            None,
        )


def test_status_logs_carry_no_filter_values_or_ids() -> None:
    clock = FakeClock()
    server = Server(
        sequence(store(copy(1, 1), copy(2, 2, id="AAMk/../BAD")), js({}, 500)),
        {gid(3): js(copy(3, 3)), gid(4): js({}, 400)},
    )
    targets: list[StatusTarget] = [ref(1), ref(2), *(ref(n) for n in range(10, 20)), gref(3), gref(4)]
    with Logs() as logs:
        status(mailbox(server, clock), targets, clock)
    failed = logs.named("provider_call_failed")
    assert [(e["level"], e["exception"]) for e in failed] == [("WARNING", "UnexpectedStatus")] * 2
    assert len(logs.named("item_degraded")) == 1
    for event in logs.events():
        assert set(event) <= allowed_fields(str(event["event"]))
    text = logs.text()
    for secret in (
        "example.test",
        "internetMessageId",
        "AAMkMSG",
        "AAQkCONV",
        "BAD",
        *FOLDER_IDS.values(),
        "AT-SENTINEL",
        "graph.microsoft.com",
    ):
        assert secret not in text


def test_warm_request_count_for_fifty_refs() -> None:
    clock = FakeClock()
    tokens = FakeTokens()
    server = Server(store(*(copy(n, n) for n in range(50))))
    result = status(mailbox(server, clock, tokens=tokens, maps=primed(clock)), [ref(n) for n in range(50)], clock)
    assert states(result) == ["found"] * 50
    assert server.folder_reads() == 0
    assert len(server.located()) == 5
    conversation_gets = [r for r in server.located() if "conversationId" in r.url.params["$filter"]]
    assert len(conversation_gets) <= 5
    assert len(server.requests) == 5  # no split GETs, no reads
    assert (tokens.issued, tokens.invalidated) == (1, 0)


# --- T17 facts (owner's Graph Explorer run, requests 14, 14b, 15, 16) ----------------------------------------------


@pytest.mark.xfail(strict=True, reason="T17: the owner's Graph Explorer facts are not recorded yet (merge gate)")
def test_fixture_shapes_match_t1c_facts() -> None:
    assert T1C_FACTS is not None, "record the owner's T17 facts in tests/support/graph_fixtures.py"
    for request in ("14", "14b", "15", "16"):
        assert T1C_FACTS[f"status_{request}"] == 200
    assert T1C_FACTS["batch_10_at_cap_ok"] is True  # otherwise lower MAX_FILTER_CHARS first (GR3)
    assert T1C_FACTS["next_link_seen"] is False  # a routine nextLink would make most lookups incomplete
    assert T1C_FACTS["count_14b"] == T1C_FACTS["count_14"]  # the padding values match nothing
    assert T1C_FACTS["request14_both_found"] is True
    assert T1C_FACTS["request14_replied_last_verb"] is True  # the $expand works on the batch; else the C6 fallback
    assert T1C_FACTS["request15_only_c1_c2"] is True
    assert T1C_FACTS["request15_sent_reply_after_receipt"] is True
    hit = copy(1, 1, singleValueExtendedProperties=extended("102"))
    assert {"internetMessageId", "parentFolderId", "isDraft", "receivedDateTime"} <= set(hit)
