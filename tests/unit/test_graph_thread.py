"""Graph `get_thread` (spec 080 §5.2 `get_thread`, §5.4, §6.5 Graph; WP13c Option 5a): the seed, the conversation
list with `uniqueBody`, the fallback list and the single body reads, the member filters, the DN participants and the
logs (T11)."""

import json
import logging
from collections.abc import Callable
from typing import Any

import httpx2
import pytest

from mcp_hub.ids import GraphMessageRef, MessageRef, StableRef, ThreadKey
from mcp_hub.logging import configure_logging
from mcp_hub.providers import graph
from mcp_hub.providers.base import ProviderError, ThreadPage, ThreadQuery
from mcp_hub.providers.deadline import CallDeadline
from mcp_hub.providers.folders import FolderMapCache, GraphFolderIds
from mcp_hub.providers.graph import (
    EXTENDED_EXPAND,
    MAX_DEGRADED_LOGS,
    SUMMARY_SELECT,
    Backoff,
    GraphMailbox,
    _legacy_dn_name,
    _participant,
    _sender_for_direction,
)
from mcp_hub.providers.graph_thread import MAX_UNIQUE_BODY_READS, UNIQUE_BODY_BYTES, body_text
from tests.support.clock import FakeClock
from tests.support.graph_fixtures import (
    CONVERSATION,
    DRAFTS_ID,
    FOLDER_IDS,
    INBOX_ID,
    JUNK_ID,
    LEGACY_DN,
    OUTBOX_ID,
    SENT_ID,
    TRASH_ID,
    FakeTokens,
    body_object,
    sender,
    thread_item,
)
from tests.support.logcapture import Capture
from tests.support.logfields import allowed_fields

Handler = Callable[[httpx2.Request], httpx2.Response]
PATH = "/v1.0/me/messages"
OWN = frozenset({"owner@example.test"})
IDS = GraphFolderIds("outlook", FOLDER_IDS)  # type: ignore[arg-type]  # fixture keys are str, not GraphWellKnown
KEY = ThreadKey("outlook", "graph", CONVERSATION)
TWO_MIB = 2_097_152
OVER_TWO_MIB = b"x" * (TWO_MIB + 1)
LIST_FILTER = f"receivedDateTime ge 1900-01-01T00:00:00Z and conversationId eq '{CONVERSATION}'"


def gid(n: int) -> str:
    return f"AAMkMSG{n:04d}="


def mid(n: int) -> str:
    return f"t{n}@example.test"


def without(item: dict[str, object], *keys: str) -> dict[str, object]:
    return {k: v for k, v in item.items() if k not in keys}


def js(body: object, status: int = 200, headers: dict[str, str] | None = None) -> Handler:
    return lambda request: httpx2.Response(
        status, content=json.dumps(body).encode(), headers={"Content-Type": "application/json"} | (headers or {})
    )


def values(*items: object, **extra: object) -> Handler:
    return js({"value": list(items)} | extra)


def newest_first(*items: dict[str, object]) -> list[dict[str, object]]:
    """The items as Graph orders the list: `receivedDateTime desc`."""
    return sorted(items, key=lambda i: str(i["receivedDateTime"]), reverse=True)


def status(code: int, headers: dict[str, str] | None = None) -> Handler:
    return lambda request: httpx2.Response(code, headers=headers or {})


def raw(content: bytes) -> Handler:
    return lambda request: httpx2.Response(200, content=content)


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


def read_ok(request: httpx2.Request) -> httpx2.Response:
    """A single body read: `uniqueBody` "Read <graph id>"."""
    graph_id = request.url.path.rsplit("/", 1)[1]
    return js({"uniqueBody": body_object(f"Read {graph_id}")})(request)


def seed_read(item: dict[str, object]) -> Handler:
    """The seed read of the SG4 / empty-list path: the item with its bodies."""
    return js(item | {"body": body_object("Full body")})


class Server:
    """Graph by path: PATH with an `internetMessageId` filter → `located`, with the thread filter → `listed`;
    PATH/<id> → `reads` (default: `read_ok`); the folder GETs answer FOLDER_IDS."""

    def __init__(
        self,
        listed: Handler | None = None,
        *,
        located: Handler | None = None,
        reads: dict[str, Handler] | None = None,
    ) -> None:
        self.requests: list[httpx2.Request] = []
        self.listed = listed or values()
        self.located = located or values()
        self.reads = reads or {}
        self.routes: dict[str, Handler] = {
            f"/v1.0/me/mailFolders/{name}": js({"id": folder_id}) for name, folder_id in FOLDER_IDS.items()
        }

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        path = request.url.path
        if path == PATH:
            located = request.url.params.get("$filter", "").startswith("internetMessageId eq ")
            return (self.located if located else self.listed)(request)
        if path.startswith(PATH + "/"):
            return self.reads.get(path.rsplit("/", 1)[1], read_ok)(request)
        handler = self.routes.get(path)
        return handler(request) if handler is not None else httpx2.Response(404)

    def client(self, timeout: float) -> httpx2.Client:
        return httpx2.Client(transport=httpx2.MockTransport(self.handle), trust_env=False, follow_redirects=False)

    def lists(self) -> list[httpx2.Request]:
        return [r for r in self.requests if r.url.path == PATH and r.url.params.get("$filter", "") == LIST_FILTER]

    def lookups(self) -> list[httpx2.Request]:
        return [r for r in self.requests if r.url.path == PATH and r not in self.lists()]

    def read_ids(self) -> list[str]:
        return [r.url.path.rsplit("/", 1)[1] for r in self.requests if r.url.path.startswith(PATH + "/")]


def warm(clock: FakeClock) -> FolderMapCache:
    maps = FolderMapCache(clock=clock)
    maps.put(IDS)
    return maps


def mailbox(
    server: Server, clock: FakeClock, *, tokens: FakeTokens | None = None, backoff: Backoff | None = None
) -> GraphMailbox:
    return GraphMailbox(
        "outlook",
        tokens or FakeTokens(),
        client_factory=server.client,
        clock=clock,
        backoff=backoff or Backoff(),
        own_addresses=OWN,
        folder_maps=warm(clock),
    )


def thread(
    server: Server,
    *,
    seed: Any = None,
    key: ThreadKey | None = None,
    include_trash: bool = True,
    max_chars: int = 2000,
    clock: FakeClock | None = None,
    tokens: FakeTokens | None = None,
) -> ThreadPage:
    clock = clock or FakeClock()
    query = ThreadQuery(
        seed=seed,
        key=None if seed is not None else key or KEY,  # the conversation key unless a seed is given
        include_trash=include_trash,
        max_chars=max_chars,
    )
    return mailbox(server, clock, tokens=tokens).get_thread(query, CallDeadline.start(clock))


def refused(server: Server, **kwargs: Any) -> ProviderError:
    with pytest.raises(ProviderError) as info:
        thread(server, **kwargs)
    return info.value


def member_ids(page: ThreadPage) -> list[str]:
    return [m.summary.ref.graph_id for m in page.members if isinstance(m.summary.ref, GraphMessageRef)]


def omitted(page: ThreadPage) -> list[str]:
    return [
        m.summary.ref.graph_id for m in page.members if isinstance(m.summary.ref, GraphMessageRef) and m.body.omitted
    ]


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

    def failures(self) -> list[tuple[object, object]]:
        return [(e["outcome"], e["exception"]) for e in self.named("provider_call_failed")]

    def text(self) -> str:
        return "\n".join(self.capture.lines + [m for _, _, m in self.capture.raw])


# --- the list ----------------------------------------------------------------------------------------------------


def test_list_request_exact() -> None:
    server = Server(values(thread_item(1)))
    page = thread(server, key=KEY)
    [request] = server.requests  # warm token and folder map: one GET (2/2/1)
    assert request.method == "GET"
    assert request.url.host == graph.GRAPH_HOST
    assert request.url.path == PATH
    assert dict(request.url.params) == {
        "$filter": LIST_FILTER,  # the receivedDateTime term first (InefficientFilter, O37)
        "$orderby": "receivedDateTime desc",
        "$top": "51",
        "$select": f"{SUMMARY_SELECT},uniqueBody",
        "$expand": EXTENDED_EXPAND,
    }
    assert request.headers["prefer"] == graph.PREFER
    assert page.outcome == "ok"
    assert page.thread == KEY
    assert not page.truncated
    [only] = page.members
    assert (only.linked_by, only.summary.snippet_text) == ("server", "")
    assert (only.body.text, only.body.quote_removal, only.body.original_chars) == ("Unique 1", "server", None)
    assert page.overview is not None
    assert page.overview.message_count_exact


def test_fifty_one_or_next_link_not_exact() -> None:
    items = newest_first(*(thread_item(n) for n in range(1, 52)))
    with Logs() as logs:
        page = thread(Server(values(*items, "the 52nd value: never parsed")))
    assert page.overview is not None
    assert (page.overview.message_count, page.overview.message_count_exact, page.truncated) == (51, False, True)
    assert member_ids(page) == [gid(n) for n in range(2, 52)]  # the newest 50, oldest first
    assert logs.named("item_degraded") == []

    link = f"https://{graph.GRAPH_HOST}/v1.0/me/messages?$skip=3"
    server = Server(values(*newest_first(*(thread_item(n) for n in (1, 2, 3))), **{"@odata.nextLink": link}))
    page = thread(server)
    assert page.overview is not None
    assert (page.overview.message_count, page.overview.message_count_exact, page.truncated) == (3, False, True)
    assert len(server.requests) == 1  # the nextLink is never requested
    assert all("skip" not in str(r.url) for r in server.requests)


def test_newest_fifty_kept_oldest_first() -> None:
    items = newest_first(*(thread_item(n) for n in range(1, 51)))
    page = thread(Server(values(*items)))
    assert member_ids(page) == [gid(n) for n in range(1, 51)]
    assert page.overview is not None
    assert (page.overview.message_count, page.overview.message_count_exact, page.truncated) == (50, True, False)
    assert page.overview.last_incoming_at == page.members[-1].summary.received_at


@pytest.mark.parametrize("include_trash", [True, False])
def test_drafts_junk_outbox_dropped_trash_only_with_include_trash_seed_kept(include_trash: bool) -> None:
    items = [
        thread_item(1),
        thread_item(2, isDraft=True),
        thread_item(3, JUNK_ID),
        thread_item(4, OUTBOX_ID),
        thread_item(5, DRAFTS_ID),
        thread_item(6, TRASH_ID),
        thread_item(7, SENT_ID),
        thread_item(8, TRASH_ID),  # the seed
        thread_item(9, "AQMkOTHERFOLDER"),
    ]
    server = Server(values(*newest_first(*items)), located=values(thread_item(8, TRASH_ID)))
    with Logs() as logs:
        page = thread(server, seed=StableRef("outlook", mid(8)), include_trash=include_trash)
    expected = [1, 6, 7, 8, 9] if include_trash else [1, 7, 8, 9]
    assert member_ids(page) == [gid(n) for n in expected]
    folders = {m.summary.ref: m.summary.meta.folder for m in page.members}
    assert folders[GraphMessageRef("outlook", gid(8))] == "trash"
    assert folders[GraphMessageRef("outlook", gid(9))] == "other"
    assert page.overview is not None
    assert page.overview.message_count_exact
    assert not page.truncated
    assert logs.named("item_degraded") == []


def test_unusable_item_degrades_and_marks_inexact() -> None:
    items = [
        thread_item(6),
        "not an object",
        thread_item(5, id="bad id'"),
        thread_item(4, receivedDateTime="yesterday"),
        without(thread_item(3), "parentFolderId"),
        thread_item(2, parentFolderId="bad/parent"),
    ]
    with Logs() as logs:
        page = thread(Server(values(*items)))
    assert member_ids(page) == [gid(6)]
    assert page.overview is not None
    assert not page.overview.message_count_exact
    assert not page.truncated  # an unusable item marks the list inexact only (P11)
    degraded = logs.named("item_degraded")
    assert [e["exception"] for e in degraded] == ["MalformedItemError"] * 5
    assert all("item" not in e for e in degraded)


def test_item_degraded_capped_per_call() -> None:
    items = [thread_item(1), *(["not an object"] * 30)]
    with Logs() as logs:
        thread(Server(values(*items)))
    assert len(logs.named("item_degraded")) == MAX_DEGRADED_LOGS


# --- bodies ------------------------------------------------------------------------------------------------------


def test_unique_body_text_and_html() -> None:
    items = [
        thread_item(2, uniqueBody=body_object("<p>Hi <b>you</b></p><div>next</div>", "html")),
        thread_item(1, uniqueBody=body_object("Hello\r\nthere\u00a0now")),
    ]
    page = thread(Server(values(*items)))
    first, second = (m.body for m in page.members)
    assert (first.text, first.source, first.quote_removal, first.original_chars) == (
        "Hello\nthere now",
        "text/plain",
        "server",
        None,
    )
    assert (second.source, second.quote_removal, second.cut) == ("text/html-converted", "server", False)
    assert "Hi you" in second.text
    assert "<" not in second.text
    assert not page.truncated


def test_unique_body_cut_64_kib() -> None:
    # 20,000 four-byte characters are 80,000 bytes: the 64 KiB cut leaves 16,384, below the 4 x 5,000 keep.
    item = thread_item(1, uniqueBody=body_object("\U0001f600" * 20_000))
    [member] = thread(Server(values(item)), max_chars=5000).members
    assert len(member.body.text) == UNIQUE_BODY_BYTES // 4
    assert member.body.cut
    assert member.body.quote_removal == "server"


def test_body_text_html_cut_above_and_below_cap() -> None:
    # T6-1: the cut applies to the converted text, not the raw HTML.
    above = body_text(
        {"uniqueBody": body_object("<p>" + "a" * (UNIQUE_BODY_BYTES + 10) + "</p>", "html")}, "uniqueBody"
    )
    assert above is not None
    assert (above[1], above[2], len(above[0].encode())) == ("text/html-converted", True, UNIQUE_BODY_BYTES)
    below = body_text({"uniqueBody": body_object("<span></span>" * 10_000 + "short", "html")}, "uniqueBody")
    assert below is not None
    assert (below[0].strip(), below[1], below[2]) == ("short", "text/html-converted", False)


@pytest.mark.parametrize(
    "first",
    [raw(OVER_TWO_MIB), status(400), status(500), status(302, {"Location": "https://example.test/"})],
    ids=["too_large", "400", "500", "302"],
)
def test_too_large_or_unexpected_status_falls_back(first: Handler) -> None:
    items = newest_first(*(without(thread_item(n), "uniqueBody") for n in range(1, 26)))
    pad = "p" * 1_500_000  # above the 1 MiB list cap, below the 2 MiB fallback cap (Q-a)
    server = Server(sequence(first, values(*items, pad=pad)))
    page = thread(server)
    first_list, second_list = server.lists()
    assert first_list.url.params["$select"] == f"{SUMMARY_SELECT},uniqueBody"
    assert dict(second_list.url.params) == dict(first_list.url.params) | {"$select": SUMMARY_SELECT}
    assert server.read_ids() == [gid(n) for n in range(25, 5, -1)]  # newest first, at most 20
    reads = [r for r in server.requests if r.url.path.startswith(PATH + "/")]
    assert all(dict(r.url.params) == {"$select": "uniqueBody,body"} for r in reads)
    assert omitted(page) == [gid(n) for n in range(1, 6)]
    assert page.members[-1].body.text == f"Read {gid(25)}"
    assert page.overview is not None
    assert page.overview.message_count_exact
    assert page.truncated  # bodies not read (P11)


def test_fallback_list_above_two_mib_is_too_large() -> None:
    server = Server(raw(OVER_TWO_MIB))
    assert refused(server).code == "too_large"
    assert len(server.lists()) == 2
    assert server.read_ids() == []


@pytest.mark.parametrize("handlers", [(status(404),), (status(400), status(404))], ids=["list", "re-read"])
def test_list_not_found_is_upstream_error(handlers: tuple[Handler, ...]) -> None:
    server = Server(sequence(*handlers))
    error = refused(server)
    assert (error.code, error.cause) == ("upstream_error", "GraphNotFound")
    assert len(server.lists()) == len(handlers)


def test_single_read_too_large_retries_unique_body_only() -> None:
    def big_body(request: httpx2.Request) -> httpx2.Response:
        if request.url.params["$select"] == "uniqueBody,body":
            return raw(OVER_TWO_MIB)(request)
        return js({"uniqueBody": body_object("Small")})(request)

    item = without(thread_item(1), "uniqueBody")
    server = Server(values(item), reads={gid(1): big_body})
    [member] = thread(server).members
    assert (member.body.text, member.body.quote_removal) == ("Small", "server")
    reads = [r.url.params["$select"] for r in server.requests if r.url.path.startswith(PATH + "/")]
    assert reads == ["uniqueBody,body", "uniqueBody"]

    server = Server(values(item), reads={gid(1): raw(OVER_TWO_MIB)})
    with Logs() as logs:
        page = thread(server)
    assert omitted(page) == [gid(1)]
    assert page.truncated
    assert logs.failures() == [("too_large", "ResponseTooLarge")]
    assert len(server.read_ids()) == 2  # one retry, in the same read slot


def test_missing_unique_body_on_list_item_uses_single_read() -> None:
    items = [thread_item(3, uniqueBody=None), without(thread_item(2), "uniqueBody"), thread_item(1)]
    server = Server(values(*items))
    page = thread(server)
    assert server.read_ids() == [gid(3), gid(2)]  # newest first; item 1 kept its list body
    assert [m.body.text for m in page.members] == ["Unique 1", f"Read {gid(2)}", f"Read {gid(3)}"]
    assert not page.truncated


def test_single_read_body_fallback_heuristic() -> None:
    quoted = "Thanks, see you then.\n\nOn Mon, 5 Oct 2026 at 10:00, Peer <peer@example.test> wrote:\n> Shall we meet?\n"
    html = "<p>Thanks</p><div>On Mon, 5 Oct 2026 at 10:00, Peer &lt;peer@example.test&gt; wrote:</div><p>&gt; old</p>"
    items = [without(thread_item(2), "uniqueBody"), without(thread_item(1), "uniqueBody")]
    reads = {
        gid(1): js({"body": body_object(quoted)}),
        gid(2): js({"uniqueBody": None, "body": body_object(html, "html")}),
    }
    first, second = (m.body for m in thread(Server(values(*items), reads=reads)).members)
    assert (first.text, first.source, first.quote_removal, first.original_chars) == (
        "Thanks, see you then.",
        "text/plain",
        "heuristic",
        len(quoted),
    )
    assert (second.text.strip(), second.source, second.quote_removal) == ("Thanks", "text/html-converted", "heuristic")
    assert second.original_chars is not None
    assert second.original_chars > len("Thanks")


def test_single_read_failures_omit_only_that_body() -> None:
    items = newest_first(*(without(thread_item(n), "uniqueBody") for n in range(1, 7)))
    reads = {
        gid(5): status(404),
        gid(4): status(500),
        gid(3): raw(OVER_TWO_MIB),
        gid(2): status(429, {"Retry-After": "30"}),
        gid(1): read_ok,  # never requested: the back-off answers Throttled without a request
    }
    server = Server(values(*items), reads=reads)
    with Logs() as logs:
        page = thread(server)
    assert server.read_ids() == [gid(6), gid(5), gid(4), gid(3), gid(3), gid(2)]
    assert omitted(page) == [gid(n) for n in range(1, 6)]
    assert page.members[-1].body.text == f"Read {gid(6)}"
    assert page.truncated
    assert logs.failures() == [
        ("not_found", "GraphNotFound"),
        ("upstream_error", "UnexpectedStatus"),
        ("too_large", "ResponseTooLarge"),
        ("upstream_error", "Throttled"),  # once, although two reads were throttled
    ]
    assert all(e["level"] == "WARNING" for e in logs.named("provider_call_failed"))


@pytest.mark.parametrize("path", ["list", "read"])
def test_one_hostile_body_degrades_only_itself(path: str, monkeypatch: pytest.MonkeyPatch) -> None:
    def hostile(name: str) -> Callable[..., Any]:
        real = getattr(graph, name)

        def build(text: str, *args: Any, **kwargs: Any) -> Any:
            if "2" in text:
                raise RecursionError
            return real(text, *args, **kwargs)

        return build

    if path == "list":
        monkeypatch.setattr(graph, "server_body", hostile("server_body"))
        items = [thread_item(3), thread_item(2), thread_item(1)]
        reads: dict[str, Handler] = {}
    else:
        monkeypatch.setattr(graph, "prepare_body", hostile("prepare_body"))
        items = [without(thread_item(n), "uniqueBody") for n in (3, 2, 1)]
        reads = {gid(n): js({"body": body_object(f"Body {n}")}) for n in (1, 2, 3)}
    with Logs() as logs:
        page = thread(Server(values(*items), reads=reads))
    assert omitted(page) == [gid(2)]
    assert [m.body.omitted for m in page.members] == [False, True, False]
    assert page.truncated
    assert [e["exception"] for e in logs.named("item_degraded")] == ["RecursionError"]


@pytest.mark.parametrize(
    ("first", "logged"),
    [(raw(OVER_TWO_MIB), ("too_large", "ResponseTooLarge")), (status(400), ("upstream_error", "UnexpectedStatus"))],
    ids=["too_large", "400"],
)
def test_list_fallback_logged_once_per_call(first: Handler, logged: tuple[str, str]) -> None:
    # M4-2: the SG1/Q7 fallback leaves one provider_call_failed line per call, not one per single read, and not a
    # second one when a 401 in a read restarts the call (the caps live outside `work`).
    items = newest_first(*(without(thread_item(n), "uniqueBody") for n in range(1, 4)))
    tokens = FakeTokens()
    server = Server(
        sequence(first, values(*items), first, values(*items)), reads={gid(3): sequence(status(401), read_ok)}
    )
    with Logs() as logs:
        page = thread(server, tokens=tokens)
    assert tokens.invalidated == 1
    assert len(server.lists()) == 4  # the list, its re-read, then both again after the restart
    assert server.read_ids() == [gid(3), gid(3), gid(2), gid(1)]
    assert not page.truncated
    assert logs.failures() == [logged]
    [line] = logs.named("provider_call_failed")
    assert set(line) <= allowed_fields("provider_call_failed")


def test_unauthorized_in_single_read_refreshes() -> None:
    tokens = FakeTokens()
    server = Server(values(without(thread_item(1), "uniqueBody")), reads={gid(1): sequence(status(401), read_ok)})
    with Logs() as logs:
        page = thread(server, tokens=tokens)
    assert (tokens.issued, tokens.invalidated) == (2, 1)
    assert [m.body.text for m in page.members] == [f"Read {gid(1)}"]
    assert not page.truncated
    assert len(server.lists()) == 2  # the restart runs the whole call again
    assert logs.failures() == []  # a 401 is not a body failure


def connect_error(request: httpx2.Request) -> httpx2.Response:
    raise httpx2.ConnectError("refused", request=request)


@pytest.mark.parametrize(
    ("make", "code", "cause"),
    [
        (lambda clock: status(403), "upstream_error", "Forbidden"),
        (lambda clock: connect_error, "unreachable", "ConnectError"),
        (lambda clock: advancing(clock, 21.0, read_ok), "upstream_timeout", "CallDeadline"),
    ],
    ids=["403", "transport", "hard-deadline"],
)
def test_account_failure_in_single_read_fails_the_call(
    make: Callable[[FakeClock], Handler], code: str, cause: str
) -> None:
    # T11-1 (P6, GR1): an account-class failure in a body read is a tool error, never body_omitted.
    clock = FakeClock()
    tokens = FakeTokens()
    items = newest_first(*(without(thread_item(n), "uniqueBody") for n in range(1, 4)))
    server = Server(values(*items), reads={gid(3): make(clock)})
    error = refused(server, clock=clock, tokens=tokens)
    assert (error.code, error.cause) == (code, cause)
    assert server.read_ids() == [gid(3)]  # no later read is sent
    assert tokens.invalidated == (1 if code == "upstream_error" else 0)  # a 403 drops the token (D63)


def test_forbidden_list_is_a_tool_error_without_fallback() -> None:
    server = Server(status(403))
    error = refused(server)
    assert (error.code, error.cause) == ("upstream_error", "Forbidden")
    assert len(server.lists()) == 1


def test_unauthorized_on_the_list_refreshes_once() -> None:
    # T11-3: a 401 on the list request refreshes the token once and runs the call again.
    tokens = FakeTokens()
    server = Server(sequence(status(401), values(thread_item(1))))
    page = thread(server, tokens=tokens)
    assert (tokens.issued, tokens.invalidated) == (2, 1)
    assert len(server.lists()) == 2
    assert [m.body.text for m in page.members] == ["Unique 1"]
    assert server.requests[1].headers["authorization"] == "Bearer AT-SENTINEL-2"


def test_get_only_no_next_link_and_at_most_twenty_reads() -> None:
    link = f"https://{graph.GRAPH_HOST}/v1.0/me/messages?$skiptoken=next"
    items = newest_first(*(without(thread_item(n), "uniqueBody") for n in range(1, 31)))
    server = Server(values(*items, **{"@odata.nextLink": link}))
    page = thread(server)
    assert len(server.read_ids()) == MAX_UNIQUE_BODY_READS == 20
    assert len(server.requests) == 21
    assert all(r.method == "GET" and r.url.host == graph.GRAPH_HOST for r in server.requests)
    assert all("skiptoken" not in str(r.url) for r in server.requests)
    assert omitted(page) == [gid(n) for n in range(1, 11)]
    assert page.overview is not None
    assert not page.overview.message_count_exact
    assert page.truncated


def test_soft_stop_before_list_upstream_timeout_during_reads_omitted() -> None:
    clock = FakeClock()
    server = Server(values(thread_item(1)), located=advancing(clock, 17.0, values(thread_item(1))))
    error = refused(server, seed=StableRef("outlook", mid(1)), clock=clock)
    assert (error.code, error.cause) == ("upstream_timeout", "SoftStop")
    assert server.lists() == []

    clock = FakeClock()
    items = newest_first(*(without(thread_item(n), "uniqueBody") for n in range(1, 5)))
    server = Server(values(*items), reads={gid(3): advancing(clock, 17.0, read_ok)})
    page = thread(server, clock=clock)
    assert server.read_ids() == [gid(4), gid(3)]
    assert omitted(page) == [gid(1), gid(2)]
    assert page.truncated
    assert page.overview is not None
    assert page.overview.message_count_exact  # bodies only (P11)


# --- the seed ----------------------------------------------------------------------------------------------------


def id_seed(item: dict[str, object]) -> Handler:
    """The small `id` seed read: the five fields of `_SEED_SELECT`."""
    fields = ("id", "conversationId", "parentFolderId", "isDraft", "receivedDateTime")
    return js({k: item[k] for k in fields if k in item})


def test_seed_by_ref_id_key() -> None:
    items = values(*newest_first(thread_item(1), thread_item(2), thread_item(3)))
    by_ref = Server(items, located=values(thread_item(2)))
    by_id = Server(items, reads={gid(2): id_seed(thread_item(2))})
    by_key = Server(items)
    pages = [
        thread(by_ref, seed=StableRef("outlook", mid(2))),
        thread(by_id, seed=GraphMessageRef("outlook", gid(2))),
        thread(by_key, key=KEY),
    ]
    assert [len(s.requests) for s in (by_ref, by_id, by_key)] == [2, 2, 1]  # warm
    [lookup] = by_ref.lookups()
    assert lookup.url.params["$filter"] == f"internetMessageId eq '<{mid(2)}>'"
    [seed_get] = [r for r in by_id.requests if r.url.path.startswith(PATH + "/")]
    assert dict(seed_get.url.params) == {"$select": "id,conversationId,parentFolderId,isDraft,receivedDateTime"}
    for page in pages:
        assert page.thread == KEY
        assert member_ids(page) == [gid(1), gid(2), gid(3)]
        assert {m.linked_by for m in page.members} == {"server"}  # the seed too (MF-P1)
        assert page.overview is not None
        assert page.overview.message_count_exact


@pytest.mark.parametrize("kind", ["ref", "id"])
def test_seed_input_with_empty_list_returns_seed_alone(kind: str) -> None:
    item = thread_item(2)
    reads = {gid(2): sequence(id_seed(item), seed_read(item)) if kind == "id" else seed_read(item)}
    server = Server(values(thread_item(5, isDraft=True)), located=values(item), reads=reads)
    seed = StableRef("outlook", mid(2)) if kind == "ref" else GraphMessageRef("outlook", gid(2))
    page = thread(server, seed=seed)
    assert len(server.requests) == 3
    assert len(server.lists()) == 1
    final = server.requests[-1]
    assert dict(final.url.params) == {"$select": f"{SUMMARY_SELECT},uniqueBody,body", "$expand": EXTENDED_EXPAND}
    assert page.thread == KEY  # a valid conversationId keeps its key (K9)
    assert page.overview is not None
    assert (page.overview.message_count, page.overview.message_count_exact, page.truncated) == (1, False, False)
    [member] = page.members
    assert (member.linked_by, member.body.text, member.body.quote_removal) == ("server", "Unique 2", "server")


def test_seed_without_conversation_alone_not_exact() -> None:
    quoted = "Sure.\n\nOn Mon, 5 Oct 2026 at 10:00, Peer <peer@example.test> wrote:\n> Lunch?\n"
    item = without(thread_item(2, conversationId="not valid'"), "uniqueBody")
    server = Server(located=values(item), reads={gid(2): js(item | {"body": body_object(quoted)})})
    page = thread(server, seed=StableRef("outlook", mid(2)))
    assert server.lists() == []
    assert len(server.requests) == 2  # lookup + the P5 read
    [read] = [r for r in server.requests if r.url.path.startswith(PATH + "/")]
    assert dict(read.url.params) == {"$select": f"{SUMMARY_SELECT},uniqueBody,body", "$expand": EXTENDED_EXPAND}
    assert page.thread is None
    assert page.overview is not None
    assert (page.overview.message_count, page.overview.message_count_exact, page.truncated) == (1, False, False)
    [member] = page.members
    assert member.linked_by == "server"
    assert member.summary.meta.thread is None
    assert (member.body.text, member.body.quote_removal, member.body.original_chars) == (
        "Sure.",
        "heuristic",
        len(quoted),
    )


def test_seed_marked_by_graph_id_or_message_id() -> None:
    # A ref seed whose located copy is in trash, plus a later inbox copy of the same Message-ID: one member, the
    # earliest copy (G-R3); another trash message is dropped without include_trash.
    copy = thread_item(6, internetMessageId=f"<{mid(5)}>")
    items = newest_first(thread_item(1), thread_item(5, TRASH_ID), copy, thread_item(7, TRASH_ID))
    page = thread(
        Server(values(*items), located=values(thread_item(5, TRASH_ID))),
        seed=StableRef("outlook", mid(5)),
        include_trash=False,
    )
    assert member_ids(page) == [gid(1), gid(5)]
    assert page.members[1].summary.meta.folder == "trash"

    # Recognised by the Message-ID alone: the located copy (inbox) is not in the list, its trash copy is.
    located = thread_item(9, internetMessageId=f"<{mid(5)}>")
    page = thread(
        Server(values(thread_item(5, TRASH_ID), thread_item(1)), located=values(located)),
        seed=StableRef("outlook", mid(5)),
        include_trash=False,
    )
    assert member_ids(page) == [gid(1), gid(5)]

    # An id seed, recognised by its Graph id although it has a different Message-ID copy elsewhere.
    page = thread(
        Server(values(thread_item(5, TRASH_ID), thread_item(1)), reads={gid(5): id_seed(thread_item(5, TRASH_ID))}),
        seed=GraphMessageRef("outlook", gid(5)),
        include_trash=False,
    )
    assert member_ids(page) == [gid(1), gid(5)]


@pytest.mark.parametrize(
    ("read", "cause"),
    [
        (status(404), "GraphNotFound"),
        (id_seed(thread_item(2, isDraft=True)), "Draft"),
        (id_seed(thread_item(2, JUNK_ID)), "ExcludedFolder"),
    ],
)
def test_id_seed_excluded_parent_or_draft_not_found(read: Handler, cause: str) -> None:
    server = Server(values(thread_item(2)), reads={gid(2): read})
    error = refused(server, seed=GraphMessageRef("outlook", gid(2)))
    assert (error.code, error.cause) == ("not_found", cause)
    assert server.lists() == []


def test_ref_incomplete_upstream_timeout() -> None:
    others = [thread_item(n, internetMessageId=f"<other{n}@example.test>") for n in range(50)]
    for located in (values(*others), status(500)):
        server = Server(values(thread_item(2)), located=located)
        error = refused(server, seed=StableRef("outlook", mid(2)))
        assert (error.code, error.cause) == ("upstream_timeout", "IncompleteSearch")
        assert server.lists() == []
    server = Server(values(thread_item(2)))  # a complete, empty lookup
    assert refused(server, seed=StableRef("outlook", mid(2))).code == "not_found"


def test_key_without_member_not_found() -> None:
    for listed in (values(), values(thread_item(1, isDraft=True), thread_item(2, JUNK_ID))):
        error = refused(Server(listed), key=KEY)
        assert (error.code, error.cause) == ("not_found", "NotFound")
    link = {"@odata.nextLink": f"https://{graph.GRAPH_HOST}/v1.0/me/messages?$skip=1"}
    for listed in (values("not an object"), values(thread_item(1, isDraft=True), **link)):
        error = refused(Server(listed), key=KEY)
        assert (error.code, error.cause) == ("upstream_timeout", "IncompleteSearch")  # not a complete search (Q6)


def test_foreign_key_seed_or_source_without_request() -> None:
    server = Server()
    clock = FakeClock()
    box = mailbox(server, clock)
    mismatch = ThreadQuery(seed=None, key=ThreadKey("outlook", "headers", "x"), include_trash=True, max_chars=2000)
    assert box.get_thread(mismatch, CallDeadline.start(clock)) == ThreadPage("source_mismatch")
    for query in (
        ThreadQuery(seed=None, key=ThreadKey("gmail", "graph", CONVERSATION), include_trash=True, max_chars=2000),
        ThreadQuery(seed=None, key=ThreadKey("outlook", "graph", "bad'id"), include_trash=True, max_chars=2000),
        ThreadQuery(seed=StableRef("outlook", "bad'id"), key=None, include_trash=True, max_chars=2000),
        ThreadQuery(seed=StableRef("gmail", mid(1)), key=None, include_trash=True, max_chars=2000),
        ThreadQuery(seed=MessageRef("outlook", "INBOX", 1, 1), key=None, include_trash=True, max_chars=2000),
    ):
        with pytest.raises(ProviderError) as info:
            box.get_thread(query, CallDeadline.start(clock))
        assert info.value.code == "not_found"
    assert server.requests == []


# --- participants and the legacy DN ------------------------------------------------------------------------------


def test_legacy_dn_participants() -> None:
    incoming = thread_item(
        1,
        ccRecipients=[
            sender(LEGACY_DN, "owner@example.test"),  # the owner's own DN: removed
            sender("/O=EXAMPLE/OU=TEST/CN=RECIPIENTS/CN=THIRD", "third@example.test"),
            sender("/O=EXAMPLE/OU=TEST/CN=RECIPIENTS/CN=NONAME", "No Name"),  # no plain address: dropped
        ],
    )
    own = thread_item(
        2,
        **{"from": sender(LEGACY_DN, "owner@example.test")},
        toRecipients=[sender("peer@example.test", "Peer")],
    )
    page = thread(Server(values(own, incoming)))
    first, second = page.members
    assert first.summary.meta.direction == "incoming"
    assert second.summary.meta.direction == "outgoing"  # the DN's name is an own address
    assert second.summary.from_address == LEGACY_DN  # the summary shows what Graph sent
    assert page.overview is not None
    assert page.overview.participants == ("peer@example.test", "third@example.test")
    assert not any(p.startswith("/") for p in page.overview.participants)


@pytest.mark.parametrize(
    ("name", "address", "direction", "participant"),
    [
        ("owner@example.test", LEGACY_DN, "owner@example.test", "owner@example.test"),
        (" owner@example.test ", LEGACY_DN, "owner@example.test", "owner@example.test"),
        ("owner@example.test", "/O=EXAMPLE/CN=OWNER", "/O=EXAMPLE/CN=OWNER", None),  # malformed DN
        ("owner@example.test", "/o=x/cn=Recipients/cn=y@z", "/o=x/cn=Recipients/cn=y@z", None),  # "@" in the DN
        ("Owner Name", LEGACY_DN, LEGACY_DN, None),
        ("a b@example.test", LEGACY_DN, LEGACY_DN, None),
        ("x" * 250 + "@example.test", LEGACY_DN, LEGACY_DN, None),  # above 254 characters
        (None, LEGACY_DN, LEGACY_DN, None),
        ("Peer", "peer@example.test", "peer@example.test", "peer@example.test"),
        ("owner@example.test", "peer@example.test", "peer@example.test", "peer@example.test"),
        ("Peer", None, None, None),
        ("Group", "/OU=X/CN=Y", "/OU=X/CN=Y", None),  # another X.500 form: never a participant (T11-2)
        ("Group", "Some Group", "Some Group", None),  # display text without "@"
        ("owner@example.test", "Some Group", "Some Group", None),  # only a strict DN maps to its name
    ],
)
def test_dn_predicate_shared_by_direction_and_participants(
    name: str | None, address: str | None, direction: str | None, participant: str | None
) -> None:
    assert _sender_for_direction({"from": {"emailAddress": {"name": name, "address": address}}}) == direction
    assert _participant(name, address) == participant
    assert _legacy_dn_name(name, address) == (participant if address == LEGACY_DN else None)


# --- get_message keeps its lookup ----------------------------------------------------------------------------------


def test_lookup_ref_unchanged_for_get_message() -> None:
    clock = FakeClock()
    item = thread_item(2) | {"body": body_object("Full body")}
    server = Server(located=values(item), reads={gid(2): js(item)})
    detail = mailbox(server, clock).get_message(StableRef("outlook", mid(2)), CallDeadline.start(clock))
    assert detail.ref == GraphMessageRef("outlook", gid(2))
    assert detail.body_text == "Full body"
    [lookup] = server.lookups()
    assert dict(lookup.url.params) == {
        "$filter": f"internetMessageId eq '<{mid(2)}>'",
        "$select": SUMMARY_SELECT,
        "$top": "50",
    }
    assert len(server.requests) == 2

    box = mailbox(Server(located=values(thread_item(2, conversationId="bad'"))), clock)
    with box._factory(20.0) as client:
        found = box._lookup_copy(client, "AT", CallDeadline.start(clock), mid(2))
    assert found == (GraphMessageRef("outlook", gid(2)), None)
    box = mailbox(Server(located=values(thread_item(2))), clock)
    with box._factory(20.0) as client:
        found = box._lookup_copy(client, "AT", CallDeadline.start(clock), mid(2))
    assert found == (GraphMessageRef("outlook", gid(2)), CONVERSATION)


# --- logs --------------------------------------------------------------------------------------------------------


def test_logs_have_no_thread_values() -> None:
    items = newest_first(
        *(without(thread_item(n), "uniqueBody") for n in range(1, 4)),
        thread_item(4, ccRecipients=[sender(LEGACY_DN, "third@example.test")]),
    )
    reads = {gid(3): status(500), gid(2): status(404)}
    server = Server(sequence(status(400), values(*items, "not an object")), located=values(thread_item(4)), reads=reads)
    with Logs() as logs:
        page = thread(server, seed=StableRef("outlook", mid(4)))
    assert page.truncated
    assert logs.failures()
    assert logs.named("item_degraded")
    text = logs.text()
    for value in (CONVERSATION, "example.test", "Unique", "Read", "Subject", "/O=", "AAMk", INBOX_ID, "t4@"):
        assert value not in text
    for event in logs.events():
        assert event["event"] in ("item_degraded", "provider_call_failed", "folder_map_refreshed")  # the warm map
        assert set(event) <= allowed_fields(str(event["event"]))
