import gzip
import json
from collections.abc import Callable, Iterator
from datetime import UTC, datetime

import httpx2
import pytest

from mcp_hub.ids import GraphMessageRef, MessageRef
from mcp_hub.providers.base import FolderPage, ListQuery, MailDetail, ProviderError
from mcp_hub.providers.deadline import CallDeadline
from mcp_hub.providers.graph import (
    EXTENDED_EXPAND,
    FALLBACK_SELECT,
    MESSAGE_SELECT,
    SUMMARY_SELECT,
    Backoff,
    GraphMailbox,
    retry_after_seconds,
)
from tests.support.dav_transport import RecordingTransport
from tests.support.graph_fixtures import FOLDER_IDS, INBOX_ID, FakeTokens, message
from tests.support.ms_transport import ChunkStream, streamed

BASE = "https://graph.microsoft.com:443/v1.0"
LIST = ("GET", f"{BASE}/me/mailFolders/{INBOX_ID}/messages")  # lists address the well-known folder by its id
FOLDER = ("GET", f"{BASE}/me/mailFolders/inbox")
MID = "AAMkMSG0001="
SINCE = datetime(2026, 9, 29, 6, 0, 30, 500_000, tzinfo=UTC)
Handler = Callable[[httpx2.Request], httpx2.Response]


def js(body: object, status: int = 200, headers: dict[str, str] | None = None) -> Handler:
    return lambda request: httpx2.Response(
        status, content=json.dumps(body).encode(), headers={"Content-Type": "application/json"} | (headers or {})
    )


def folder_routes() -> dict[tuple[str, str], Handler]:
    """The seven well-known folder GETs of the folder-id map (spec 080 §5.5)."""
    return {("GET", f"{BASE}/me/mailFolders/{name}"): js({"id": folder_id}) for name, folder_id in FOLDER_IDS.items()}


def routes(answer: Handler) -> dict[tuple[str, str], Handler]:
    """The folder-id map plus `answer` for the inbox list."""
    return folder_routes() | {LIST: answer}


def list_requests(t: RecordingTransport) -> int:
    return sum(1 for *_, path, _ in t.seen if path.endswith("/messages"))


def box(
    t: RecordingTransport,
    tokens: FakeTokens | None = None,
    backoff: Backoff | None = None,
    now: list[float] | None = None,
) -> GraphMailbox:
    clock = now or [100.0]
    return GraphMailbox(
        "outlook",
        tokens or FakeTokens(),
        client_factory=lambda timeout: httpx2.Client(transport=t.transport(), trust_env=False, follow_redirects=False),
        clock=lambda: clock[0],
        backoff=backoff or Backoff(),
    )


def call_deadline(now: list[float] | None = None) -> CallDeadline:
    """The tool layer's deadline on the same fake clock as the mailbox built by `box`."""
    clock = now or [100.0]
    return CallDeadline.start(lambda: clock[0])


def list_unread(
    t: RecordingTransport,
    *,
    tokens: FakeTokens | None = None,
    backoff: Backoff | None = None,
    now: list[float] | None = None,
) -> FolderPage:
    return box(t, tokens, backoff, now).list_messages(ListQuery("inbox", SINCE, True, None, 20), call_deadline(now))


def test_list_query_shape_and_headers() -> None:
    seen: list[httpx2.Request] = []

    def answer(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return js({"value": [message(1)]})(request)

    page = box(RecordingTransport(routes(answer))).list_messages(
        ListQuery("inbox", SINCE, True, None, 20), call_deadline()
    )
    assert dict(seen[0].url.params) == {
        "$filter": "receivedDateTime ge 2026-09-29T06:00:30Z and isRead eq false",
        "$orderby": "receivedDateTime desc",
        "$select": SUMMARY_SELECT,
        "$expand": EXTENDED_EXPAND,
        "$top": "101",
    }
    h = seen[0].headers
    assert h["prefer"] == 'IdType="ImmutableId", outlook.body-content-type="text"'
    assert h["accept-encoding"] == "identity"
    assert h["authorization"] == "Bearer AT-SENTINEL-1"
    assert [i.ref for i in page.items] == [GraphMessageRef("outlook", MID)]
    assert not page.more


def test_more_when_limit_plus_one_or_next_link() -> None:
    assert (
        box(RecordingTransport(routes(js({"value": [message(n) for n in range(1, 4)]}))))
        .list_messages(ListQuery("inbox", SINCE, True, None, 2), call_deadline())
        .more
    )
    t = RecordingTransport(routes(js({"value": [message(1)], "@odata.nextLink": "https://evil.example.test/next"})))
    page = box(t).list_messages(ListQuery("inbox", SINCE, True, None, 20), call_deadline())
    assert page.truncated  # a nextLink is never followed: the page is incomplete
    assert {s[0] for s in t.seen} == {"graph.microsoft.com"}
    assert list_requests(t) == 1


def test_duplicate_graph_ids_keep_the_first_entry() -> None:
    first = message(1, subject="first copy")
    entries = [first, message(2), message(1, subject="second copy", receivedDateTime="2026-09-29T06:59:00Z")]
    page = list_unread(RecordingTransport(routes(js({"value": entries}))))
    assert [(i.ref.graph_id, i.subject) for i in page.items] == [("AAMkMSG0002=", "Subject 2"), (MID, "first copy")]


def test_next_link_never_requested() -> None:
    t = RecordingTransport(
        routes(js({"value": [], "@odata.nextLink": f"{BASE}/me/mailFolders/{INBOX_ID}/messages?$skip=10"}))
    )
    box(t).list_messages(ListQuery("inbox", SINCE, True, None, 20), call_deadline())
    assert list_requests(t) == 1


def test_only_graph_host_gets_bearer() -> None:
    t = RecordingTransport(routes(js({"value": [message(1)]})))
    box(t).list_messages(ListQuery("inbox", SINCE, True, None, 20), call_deadline())
    assert all(host == "graph.microsoft.com" and port == 443 for host, port, *_ in t.seen)


@pytest.mark.parametrize("status", [301, 302, 307, 308])
def test_redirect_not_followed(status: int) -> None:
    t = RecordingTransport(
        routes(lambda r: httpx2.Response(status, headers={"Location": "https://evil.example.test/"}))
    )
    with pytest.raises(ProviderError) as info:
        list_unread(t)
    assert (info.value.code, info.value.cause) == ("upstream_error", "Redirect")
    assert list_requests(t) == 1
    assert {s[0] for s in t.seen} == {"graph.microsoft.com"}


@pytest.mark.parametrize("status", [301, 307])
def test_redirect_not_followed_even_by_a_following_client(status: int) -> None:
    """follow_redirects=False is set per request, so a following client cannot send the bearer to Location (F2)."""
    t = RecordingTransport(
        routes(lambda r: httpx2.Response(status, headers={"Location": "https://evil.example.test/"}))
    )
    following = GraphMailbox(
        "outlook",
        FakeTokens(),
        client_factory=lambda timeout: httpx2.Client(transport=t.transport(), follow_redirects=True),
        backoff=Backoff(),
    )
    with pytest.raises(ProviderError) as info:
        following.list_messages(
            ListQuery("inbox", SINCE, True, None, 20), CallDeadline.start()
        )  # this client runs on the real clock
    assert info.value.cause == "Redirect"
    assert {s[0] for s in t.seen} == {"graph.microsoft.com"}
    assert list_requests(t) == 1


def test_response_over_cap_stops_after_first_chunk_beyond() -> None:
    served: list[ChunkStream] = []

    def over_cap(request: httpx2.Request) -> httpx2.Response:
        response, stream = streamed(200, [b'{"value": ['] + [b" " * 65_536] * 40)
        served.append(stream)
        return response

    t = RecordingTransport(routes(over_cap))
    with pytest.raises(ProviderError) as info:
        list_unread(t)
    assert info.value.code == "too_large"
    assert [s.served for s in served] == [17, 17]  # 1 + 16 x 64 KiB > 1 MiB; once more with $top halved (§5.4)


def test_deeply_nested_json_is_upstream_error() -> None:
    t = RecordingTransport(routes(lambda r: httpx2.Response(200, content=b'{"value": ' + b"[" * 200_000)))
    with pytest.raises(ProviderError) as info:
        list_unread(t)
    assert (info.value.code, info.value.cause) == ("upstream_error", "MalformedJson")


def test_gzip_refused_before_reading() -> None:
    response, stream = streamed(200, [gzip.compress(b'{"value": []}')], {"Content-Encoding": "gzip"})
    t = RecordingTransport(routes(lambda r: response))
    with pytest.raises(ProviderError) as info:
        list_unread(t)
    assert info.value.cause == "ContentEncoding"
    assert stream.served == 0


def test_deadline_between_chunks() -> None:
    now = [100.0]

    def chunks_advancing_clock(request: httpx2.Request) -> httpx2.Response:
        def body() -> Iterator[bytes]:
            for _ in range(30):
                now[0] += 1.0
                yield b" "

        return httpx2.Response(200, content=body())

    t = RecordingTransport(routes(chunks_advancing_clock))
    with pytest.raises(ProviderError) as info:
        list_unread(t, now=now)
    assert (info.value.code, info.value.cause) == ("upstream_timeout", "CallDeadline")


def test_no_request_after_the_deadline() -> None:
    now = [100.0]

    class SlowTokens(FakeTokens):
        def access_token(self, deadline: float) -> str:
            now[0] = deadline + 1.0  # the token refresh used up the whole call deadline
            return super().access_token(deadline)

    t = RecordingTransport(routes(js({"value": []})))
    with pytest.raises(ProviderError) as info:
        list_unread(t, tokens=SlowTokens(), now=now)
    assert (info.value.code, info.value.cause) == ("upstream_timeout", "CallDeadline")
    assert t.seen == []


def test_401_refreshes_once_then_auth_expired() -> None:
    tokens = FakeTokens()
    t = RecordingTransport(routes(js({"error": {"code": "InvalidAuthenticationToken"}}, 401)))
    with pytest.raises(ProviderError) as info:
        list_unread(t, tokens=tokens)
    assert (info.value.code, info.value.cause) == ("auth_expired", "GraphUnauthorized")
    assert tokens.invalidated == 2  # the refreshed token Graph also rejected is dropped too (review round 1 F7)
    assert tokens.issued == 2
    assert list_requests(t) == 2


def test_after_a_second_401_the_next_call_starts_with_a_refresh() -> None:
    class CachingTokens(FakeTokens):
        """Hands out the cached token until it is invalidated, like GraphTokenSource with AccessTokenCache."""

        def __init__(self) -> None:
            super().__init__()
            self.cached: str | None = None

        def access_token(self, deadline: float) -> str:
            if self.cached is None:
                self.cached = super().access_token(deadline)
            return self.cached

        def invalidate(self) -> None:
            super().invalidate()
            self.cached = None

    sent: list[str] = []

    def reject(request: httpx2.Request) -> httpx2.Response:
        sent.append(request.headers["authorization"])
        return httpx2.Response(401, content=b"{}")

    tokens, t = CachingTokens(), RecordingTransport(routes(reject))
    for _ in range(2):
        with pytest.raises(ProviderError):
            list_unread(t, tokens=tokens)
    assert sent == [f"Bearer AT-SENTINEL-{n}" for n in (1, 2, 3, 4)]  # AT-SENTINEL-2 is never sent twice


def test_401_then_success_uses_the_refreshed_token() -> None:
    answers = iter([js({}, 401), js({"value": [message(1)]})])
    seen: list[str] = []

    def answer(request: httpx2.Request) -> httpx2.Response:
        seen.append(request.headers["authorization"])
        return next(answers)(request)

    page = list_unread(RecordingTransport(routes(answer)))
    assert len(page.items) == 1
    assert seen == ["Bearer AT-SENTINEL-1", "Bearer AT-SENTINEL-2"]


def test_403_is_upstream_error_and_drops_the_token() -> None:
    tokens = FakeTokens()
    t = RecordingTransport(routes(js({"error": {"code": "ErrorAccessDenied", "message": "secret text"}}, 403)))
    with pytest.raises(ProviderError) as info:
        list_unread(t, tokens=tokens)
    assert (info.value.code, info.value.cause) == ("upstream_error", "Forbidden")
    assert tokens.invalidated == 1
    assert list_requests(t) == 1
    assert "secret" not in str(info.value)


@pytest.mark.parametrize(
    ("status", "code", "cause"),
    [
        (404, "not_found", "GraphNotFound"),
        (500, "upstream_error", "UnexpectedStatus"),
        (504, "upstream_error", "UnexpectedStatus"),
        (400, "upstream_error", "UnexpectedStatus"),
    ],
)
def test_status_mapping(status: int, code: str, cause: str) -> None:
    t = RecordingTransport(routes(js({"error": {"code": "x", "message": "secret text"}}, status)))
    with pytest.raises(ProviderError) as info:
        list_unread(t)
    assert (info.value.code, info.value.cause) == (code, cause)
    assert "secret" not in str(info.value)


@pytest.mark.parametrize("status", [429, 503])
def test_throttling_backoff_clamped_fails_fast_and_skips_token_refresh(status: int) -> None:
    now, backoff, tokens = [100.0], Backoff(), FakeTokens()
    t = RecordingTransport(routes(js({}, status, {"Retry-After": "99999"})))
    with pytest.raises(ProviderError) as first:
        list_unread(t, tokens=tokens, backoff=backoff, now=now)
    assert (first.value.code, first.value.cause) == ("upstream_error", "Throttled")
    issued = tokens.issued
    with pytest.raises(ProviderError) as info:
        list_unread(t, tokens=tokens, backoff=backoff, now=now)
    assert info.value.cause == "Throttled"
    sent = len(t.seen)
    assert list_requests(t) == 1
    assert tokens.issued == issued
    now[0] += 299
    with pytest.raises(ProviderError):
        list_unread(t, tokens=tokens, backoff=backoff, now=now)
    assert len(t.seen) == sent  # still inside the clamped 300 s: no request at all
    now[0] += 2
    with pytest.raises(ProviderError):
        list_unread(t, tokens=tokens, backoff=backoff, now=now)
    assert list_requests(t) == 2


def test_backoff_is_per_account() -> None:
    backoff = Backoff()
    backoff.set("outlook", 100.0, 30)
    backoff.check("uzh", 100.0)
    with pytest.raises(ProviderError):
        backoff.check("outlook", 129.0)
    backoff.check("outlook", 130.0)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, 30),
        ("", 30),
        ("abc", 30),
        ("²", 30),
        ("-5", 30),
        ("1.5", 30),
        ("0", 1),
        ("12", 12),
        (" 12 ", 12),
        ("9999999", 300),
        ("999999999", 300),
        ("9999999999", 300),  # review round 1 F6: any digit string above the ceiling clamps to 300
        pytest.param("9" * 5000, 300, id="5000-digits"),
        ("0000000000012", 12),
        ("1e9", 30),
        ("Wed, 21 Oct 2026 07:28:00 GMT", 30),
        ("12 garbage", 30),
    ],
)
def test_retry_after_parsing(value: str | None, expected: int) -> None:
    assert retry_after_seconds(value) == expected


def message_routes(body: dict[str, object], *, attachments: Handler | None = None) -> dict[tuple[str, str], Handler]:
    routes = {FOLDER: js({"id": INBOX_ID}), ("GET", f"{BASE}/me/messages/{MID}"): js(body)}
    if attachments is not None:
        routes[("GET", f"{BASE}/me/messages/{MID}/attachments")] = attachments
    return routes


def full(**extra: object) -> dict[str, object]:
    return (
        message(1)
        | {
            "parentFolderId": INBOX_ID,
            "toRecipients": [{"emailAddress": {"address": "to@example.test"}}],
            "ccRecipients": [],
            "body": {"contentType": "text", "content": "Hello"},
        }
        | extra
    )


def get(routes: dict[tuple[str, str], Handler]) -> MailDetail:
    return box(RecordingTransport(routes)).get_message(GraphMessageRef("outlook", MID), call_deadline())


def test_get_message_text_body_and_folder_binding() -> None:
    detail = get(message_routes(full()))
    assert (detail.body_text, detail.body_source, detail.body_cut) == ("Hello", "text/plain", False)
    assert detail.to_addresses == ["to@example.test"]
    assert detail.attachments == []
    assert detail.ref == GraphMessageRef("outlook", MID)
    assert detail.received_at == datetime(2026, 9, 29, 6, 1, tzinfo=UTC)


def test_get_message_query_shape() -> None:
    seen: list[httpx2.Request] = []

    def answer(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return js(full())(request)

    get({FOLDER: js({"id": INBOX_ID}), ("GET", f"{BASE}/me/messages/{MID}"): answer})
    assert dict(seen[0].url.params) == {
        "$select": "id,receivedDateTime,isRead,hasAttachments,from,toRecipients,ccRecipients,subject,"
        "body,parentFolderId,isDraft,internetMessageId,conversationId,flag",
        "$expand": EXTENDED_EXPAND,
    }
    assert seen[0].url.params["$select"] == MESSAGE_SELECT
    assert FALLBACK_SELECT == (
        "id,receivedDateTime,isRead,hasAttachments,from,toRecipients,ccRecipients,subject,"
        "bodyPreview,parentFolderId,isDraft,internetMessageId,conversationId,flag"
    )
    assert seen[0].headers["prefer"] == 'IdType="ImmutableId", outlook.body-content-type="text"'


def test_message_in_an_excluded_folder_is_not_found_without_a_second_read() -> None:
    # The folder ids are read in this call, so there is nothing to re-read before the refusal (D75).
    t = RecordingTransport(folder_routes() | message_routes(full(parentFolderId=FOLDER_IDS["junkemail"])))
    with pytest.raises(ProviderError) as info:
        box(t).get_message(GraphMessageRef("outlook", MID), call_deadline())
    assert (info.value.code, info.value.cause) == ("not_found", "ExcludedFolder")
    assert [p for *_, p, _ in t.seen].count("/v1.0/me/mailFolders/junkemail") == 1


@pytest.mark.parametrize("parent", [None, 5, "", "a/../b", "x" * 513])
def test_message_without_a_parent_folder_is_not_found(parent: object) -> None:
    with pytest.raises(ProviderError) as info:
        get(message_routes(full(parentFolderId=parent)))
    assert info.value.code == "not_found"


def test_html_body_converted_and_cut_at_256_kib() -> None:
    html = "<p>" + "ä" * 200_000 + "</p><script>x</script>"
    detail = get(message_routes(full(body={"contentType": "html", "content": html})))
    assert detail.body_source == "text/html-converted"
    assert detail.body_cut
    assert "script" not in detail.body_text
    assert len(detail.body_text.encode()) <= 262_144


def test_message_over_cap_falls_back_to_preview() -> None:
    calls: list[str] = []

    def answer(request: httpx2.Request) -> httpx2.Response:
        select = request.url.params["$select"]
        calls.append(select)
        assert request.url.params["$expand"] == EXTENDED_EXPAND
        if "body," in select + ",":
            return httpx2.Response(200, content=b'{"body": {"content": "' + b"x" * 2_200_000 + b'"}}')
        return js(full(bodyPreview="short preview") | {"body": None})(request)

    detail = get({FOLDER: js({"id": INBOX_ID}), ("GET", f"{BASE}/me/messages/{MID}"): answer})
    assert detail.body_text == "short preview"
    assert detail.body_cut
    assert calls == [MESSAGE_SELECT, FALLBACK_SELECT]
    assert detail.meta.message_id == "m1@example.test"


def test_attachments_listed_without_inline_and_degrade_alone() -> None:
    att = {
        "value": [
            {"name": "a.pdf", "contentType": "application/pdf", "size": 10, "isInline": False},
            {"name": "logo.png", "contentType": "image/png", "size": 5, "isInline": True},
            {"name": 7, "contentType": None, "size": True},
            "not an object",
        ]
    }
    detail = get(message_routes(full(hasAttachments=True), attachments=js(att)))
    assert [(a.filename, a.size_bytes) for a in detail.attachments] == [("a.pdf", 10), (None, 0)]
    broken = get(
        message_routes(full(hasAttachments=True), attachments=lambda r: httpx2.Response(200, content=b"[" * 50))
    )
    assert broken.attachments == []


def test_attachments_after_the_deadline_fail_the_whole_call() -> None:
    now = [100.0]

    def folder_using_up_the_deadline(request: httpx2.Request) -> httpx2.Response:
        def body() -> Iterator[bytes]:
            yield json.dumps({"id": INBOX_ID}).encode()
            now[0] += 25.0  # the inbox-id answer completes, then the 20 s call deadline is over

        return httpx2.Response(200, content=body())

    routes = message_routes(full(hasAttachments=True), attachments=js({"value": []}))
    routes[FOLDER] = folder_using_up_the_deadline
    t = RecordingTransport(routes)
    with pytest.raises(ProviderError) as info:
        box(t, now=now).get_message(GraphMessageRef("outlook", MID), call_deadline(now))
    assert (info.value.code, info.value.cause) == ("upstream_timeout", "CallDeadline")
    assert [r[3] for r in t.seen] == [f"/v1.0/me/messages/{MID}", "/v1.0/me/mailFolders/inbox"]  # no attachments GET


def test_attachments_401_refreshes_the_whole_call_once() -> None:
    # A 401 on the attachments request is not a ProviderError: it reaches _run, which refreshes once and re-runs the
    # whole call; a second 401 is auth_expired. So _attachments never sees an auth_expired ProviderError.
    tokens = FakeTokens()
    t = RecordingTransport(message_routes(full(hasAttachments=True), attachments=js({}, 401)))
    with pytest.raises(ProviderError) as info:
        box(t, tokens).get_message(GraphMessageRef("outlook", MID), call_deadline())
    assert (info.value.code, info.value.cause) == ("auth_expired", "GraphUnauthorized")
    assert tokens.issued == 2
    assert sum(r[3].endswith("/attachments") for r in t.seen) == 2


def test_foreign_refs_not_found_before_any_request() -> None:
    t = RecordingTransport({})
    for ref in (MessageRef("outlook", "INBOX", 1, 1), GraphMessageRef("uzh", MID)):
        with pytest.raises(ProviderError) as info:
            box(t).get_message(ref, call_deadline())
        assert (info.value.code, info.value.cause) == ("not_found", "ForeignRef")
    assert t.seen == []


def test_check_fetches_inbox_id() -> None:
    t = RecordingTransport({FOLDER: js({"id": INBOX_ID})})
    box(t).check(call_deadline())
    assert len(t.seen) == 1


@pytest.mark.parametrize("body", [{}, {"id": 5}, {"id": ""}, {"id": "x" * 513}])
def test_check_without_a_usable_inbox_id_fails(body: dict[str, object]) -> None:
    t = RecordingTransport({FOLDER: js(body)})
    with pytest.raises(ProviderError) as info:
        box(t).check(call_deadline())
    assert (info.value.code, info.value.cause) == ("upstream_error", "NoInboxId")


@pytest.mark.parametrize(
    ("exc", "code"),
    [
        (httpx2.ConnectTimeout("t"), "upstream_timeout"),
        (httpx2.ConnectError("c"), "unreachable"),
        (OSError("o"), "unreachable"),
    ],
)
def test_transport_failures_map_to_class_names(exc: Exception, code: str) -> None:
    def fail(request: httpx2.Request) -> httpx2.Response:
        raise exc

    with pytest.raises(ProviderError) as info:
        list_unread(RecordingTransport(routes(fail)))
    assert (info.value.code, info.value.cause) == (code, type(exc).__name__)
