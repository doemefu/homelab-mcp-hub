"""Read-only Microsoft Graph mail adapter (spec 080 §6.3, rev. 4.6 S1 to S5; §5.5 and §6.5, rev. 4.9).

Lists cover the well-known folders by id (inbox, sentitems, archive, deleteditems), with the mail-history fields of
§5.5 and keyset paging; GET only, fixed host, no redirects, no nextLink, capped streamed reads against one call
deadline with the 16 s soft stop. `get_message` reads one message by its immutable id, bound to its parent folder
(D75), or first locates a `ref` by `internetMessageId` over all folders. Blocking; runs through
providers.base.run_blocking. Returns decoded, unsanitised text; the tool layer sanitises every string (§5.3)."""

import logging
import re
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final, Literal, Protocol
from urllib.parse import quote, urlsplit

import httpx2

from mcp_hub.ids import (
    GRAPH_ID,
    MESSAGE_ID_PATTERN,
    AnyMessageRef,
    GraphMessageRef,
    StableRef,
    ThreadKey,
    location_k,
)
from mcp_hub.logging import log_event
from mcp_hub.providers.base import (
    MAX_HEADER_BYTES,
    MAX_TEXT_PART_BYTES,
    PROVIDER_TIMEOUT_SECONDS,
    AttachmentMeta,
    FolderPage,
    ListQuery,
    MailDetail,
    MailFlags,
    MailMeta,
    MailSummary,
    ProviderError,
    UnreadPage,
    direction_of,
    load_json_object,
    log_message_skipped,
    order_key,
    placeholder_summary,
    read_capped,
)
from mcp_hub.providers.deadline import CallDeadline, SoftStop
from mcp_hub.providers.folders import (
    FOLDER_MAPS,
    GRAPH_WELL_KNOWN,
    FolderMapCache,
    GraphFolderIds,
    GraphWellKnown,
    ListFolder,
    MailFolder,
)
from mcp_hub.providers.headers import MAX_REFERENCES, bare_message_id, msg_id_tokens, valid_message_id
from mcp_hub.sanitize import html_to_text

GRAPH_HOST: Final = "graph.microsoft.com"
API: Final = f"https://{GRAPH_HOST}/v1.0"
MAX_LIST_RESPONSE_BYTES: Final = 1_048_576  # L1
MAX_MESSAGE_RESPONSE_BYTES: Final = 2_097_152  # L2
RETRY_AFTER_DEFAULT: Final = 30  # L16
RETRY_AFTER_MAX: Final = 300
PREFER: Final = 'IdType="ImmutableId", outlook.body-content-type="text"'
SUMMARY_SELECT: Final = (
    "id,receivedDateTime,isRead,hasAttachments,from,subject,bodyPreview,isDraft,internetMessageId,"
    "conversationId,parentFolderId,toRecipients,ccRecipients,flag,sentDateTime"
)
EXTENDED_EXPAND: Final = (
    "singleValueExtendedProperties($filter=id eq 'Integer 0x1081' or id eq 'String 0x1042' or id eq 'String 0x1039')"
)
GRAPH_LIST_TOP: Final = 101  # folder lists (spec 080 §5.4)
ANSWERED_VERBS: Final = frozenset({102, 103})  # PR_LAST_VERB_EXECUTED: reply to sender, reply to all (D77)
FOLDER_ID: Final = re.compile(r"[A-Za-z0-9=_-]{1,512}")  # fullmatch (Sec8): a folder id never changes a path
WELL_KNOWN_FOR: Final[dict[ListFolder, tuple[GraphWellKnown, ...]]] = {
    "inbox": ("inbox",),
    "sent": ("sentitems",),
    "archive": ("archive",),
    "trash": ("deleteditems",),
    "all": ("inbox", "archive", "sentitems"),  # never trash (§5.2); the folder order of S-e
}
_LAST_VERB: Final = "integer 0x1081"  # extended-property ids, compared lower-cased (G7)
_IN_REPLY_TO: Final = "string 0x1042"
_REFERENCES: Final = "string 0x1039"
_VERB: Final = re.compile(r"[0-9]{1,10}")  # ASCII digits only; anything else is "not answered"
_FRONTIER_K: Final = "f" * 16  # above every location key: a frontier covers its whole second
_HEADER_FIELDS: Final = "id,receivedDateTime,isRead,hasAttachments,from,toRecipients,ccRecipients,subject"
_HISTORY_FIELDS: Final = "parentFolderId,isDraft,internetMessageId,conversationId,flag"  # §5.5, with the $expand
MESSAGE_SELECT: Final = f"{_HEADER_FIELDS},body,{_HISTORY_FIELDS}"
FALLBACK_SELECT: Final = f"{_HEADER_FIELDS},bodyPreview,{_HISTORY_FIELDS}"
ATTACHMENT_SELECT: Final = "name,contentType,size,isInline"
REF_LOOKUP_TOP: Final = 50  # one Message-ID over all folders (spec 080 §5.2 "Location, Graph")
# S-e: among copies with the same Message-ID and received_at, the first of this order wins.
_FOLDER_RANK: Final[dict[MailFolder, int]] = {"inbox": 0, "archive": 1, "sent": 2, "trash": 3, "other": 4}
# Lookup failures that keep their code; every other lookup failure is an incomplete search, never not_found (B6).
_KEPT_CAUSES: Final = frozenset({"Throttled", "Forbidden"})
_RETRY_AFTER: Final = re.compile(r"[0-9]+")  # ASCII delay-seconds only; anything else is "not a number"
_log = logging.getLogger("mcp_hub.providers.graph")


class TokenSourceLike(Protocol):
    def access_token(self, deadline: float) -> str: ...
    def invalidate(self) -> None: ...


class _UnauthorizedError(Exception):
    """Graph answered 401: refresh once and retry (L15)."""


class MalformedItemError(Exception):
    """A list or message item with an unusable field; the class name is what gets logged."""


class Backoff:
    """Per-account 'not before' after 429/503 (L16); process memory, bounded by the registry."""

    def __init__(self) -> None:
        self._until: dict[str, float] = {}
        self._lock = threading.Lock()

    def check(self, account_id: str, now: float) -> None:
        with self._lock:
            if now < self._until.get(account_id, 0.0):
                raise ProviderError("upstream_error", "Throttled")

    def set(self, account_id: str, now: float, seconds: int) -> None:
        with self._lock:
            self._until[account_id] = now + seconds


BACKOFF: Final = Backoff()


def retry_after_seconds(value: str | None) -> int:
    text = (value or "").strip()
    if not _RETRY_AFTER.fullmatch(text):
        return RETRY_AFTER_DEFAULT  # missing, negative, HTTP-date or garbage (spec 080 §6.3)
    digits = text.lstrip("0") or "0"
    if len(digits) > 9:
        return RETRY_AFTER_MAX  # far above the ceiling; never int() an absurdly long string
    return min(max(int(digits), 1), RETRY_AFTER_MAX)


def default_client(timeout: float) -> httpx2.Client:
    return httpx2.Client(timeout=timeout, trust_env=False, follow_redirects=False)


def _provider_error(exc: Exception) -> ProviderError:
    if isinstance(exc, ProviderError):
        return exc
    cause = type(exc).__name__
    if isinstance(exc, SoftStop):  # a path without a partial result (R2b)
        return ProviderError("upstream_timeout", cause)
    if isinstance(exc, TimeoutError | httpx2.TimeoutException):
        return ProviderError("upstream_timeout", cause)
    if isinstance(exc, OSError | httpx2.TransportError):
        return ProviderError("unreachable", cause)
    return ProviderError("upstream_error", cause)


def _str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _address(value: object) -> tuple[str | None, str | None]:
    inner = value.get("emailAddress") if isinstance(value, dict) else None
    if not isinstance(inner, dict):
        return None, None
    return _str(inner.get("name")), _str(inner.get("address"))


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _utc_second(value: datetime) -> str:
    return f"{value.astimezone(UTC).replace(microsecond=0):%Y-%m-%dT%H:%M:%SZ}"


def list_filter(since: datetime, bound: datetime | None, unread_only: bool) -> str:
    """Exactly `receivedDateTime ge A[ and receivedDateTime lt B][ and isRead eq false]` (G2).

    `receivedDateTime` comes first, as Graph requires for `$orderby`; B is the cursor's second plus one second, `lt`
    rather than `le` because Exchange's sub-second precision is undocumented (S-f). The caller drops items at or above
    the `(received_at, k)` position locally. Sent only as an httpx query parameter, never concatenated into a URL."""
    terms = [f"receivedDateTime ge {_utc_second(since)}"]
    if bound is not None:
        terms.append(f"receivedDateTime lt {_utc_second(bound + timedelta(seconds=1))}")
    if unread_only:
        terms.append("isRead eq false")
    return " and ".join(terms)


def _folder_id(body: Mapping[str, object]) -> str | None:
    value = body.get("id")
    return value if isinstance(value, str) and FOLDER_ID.fullmatch(value) else None


def _parent(raw: Mapping[str, object]) -> str | None:
    """`parentFolderId` when it is a folder id (Sec8); anything else counts as no parent, which is excluded (D75)."""
    value = raw.get("parentFolderId")
    return value if isinstance(value, str) and FOLDER_ID.fullmatch(value) else None


def _kept(exc: ProviderError) -> bool:
    """Lookup failures that keep their code (G5): the deadline, credentials, the connection, throttling, 403."""
    return exc.code in ("upstream_timeout", "auth_expired", "unreachable") or (
        exc.code == "upstream_error" and exc.cause in _KEPT_CAUSES
    )


def _extended(raw: Mapping[str, object]) -> dict[str, str]:
    """Expanded extended properties by lower-cased id (G7), string values only, each cut to 64 KiB (as the IMAP header
    cap). A missing or malformed `singleValueExtendedProperties` gives {}: unknown, never an account error (S-n)."""
    entries = raw.get("singleValueExtendedProperties")
    found: dict[str, str] = {}
    if not isinstance(entries, list):
        return found
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        key, value = entry.get("id"), entry.get("value")
        if isinstance(key, str) and isinstance(value, str):
            found.setdefault(key.lower(), value[:MAX_HEADER_BYTES])
    return found


def _answered(value: str | None) -> bool:
    return value is not None and _VERB.fullmatch(value) is not None and int(value) in ANSWERED_VERBS


def graph_meta(raw: Mapping[str, object], ids: GraphFolderIds, own: frozenset[str], account: str) -> MailMeta:
    """Mail-history fields of a Graph message (spec 080 §5.5); third-party strings decoded, not yet sanitised.

    The folder comes from `parentFolderId`; callers drop items in excluded folders first, and an excluded or missing
    parent here is `not_found` (D75, fail closed). A `conversationId` outside the Graph id pattern only loses the
    thread key."""
    folder = ids.role_of(_parent(raw))
    if folder == "excluded":
        raise ProviderError("not_found", "ExcludedFolder")
    props = _extended(raw)
    message_id = bare_message_id((_str(raw.get("internetMessageId")) or "")[:MAX_HEADER_BYTES] or None)
    references_raw = props.get(_REFERENCES)
    references = (
        [t for t in msg_id_tokens(references_raw) if t != message_id and valid_message_id(t) is not None]
        if references_raw
        else []
    )
    flag = raw.get("flag")
    conversation = raw.get("conversationId")
    return MailMeta(
        folder=folder,
        folder_name=None,  # the hub does not read Graph folder display names (§5.5)
        flags=MailFlags(
            answered=_answered(props.get(_LAST_VERB)),
            flagged=isinstance(flag, dict) and flag.get("flagStatus") == "flagged",
        ),
        direction=direction_of(in_sent=folder == "sent", from_address=_address(raw.get("from"))[1], own=own),
        message_id=message_id,
        in_reply_to=bare_message_id(props.get(_IN_REPLY_TO)),
        references=tuple(references[-MAX_REFERENCES:]),
        thread=(
            ThreadKey(account, "graph", conversation)
            if isinstance(conversation, str) and GRAPH_ID.fullmatch(conversation)
            else None
        ),
    )


def _soft_stopped_page() -> FolderPage:
    return FolderPage(items=[], more=False, truncated=True, complete=False)


@dataclass(frozen=True, slots=True)
class _FolderAnswer:
    items: list[MailSummary]  # after the draft, window, unread, folder and position filters
    frontier: int | None  # a full answer: the second of its last item; that second and older not yet scanned
    stuck: bool  # a full answer whose frontier cannot move the cursor on (S-f residual)


class GraphMailbox:
    def __init__(
        self,
        account_id: str,
        tokens: TokenSourceLike,
        *,
        client_factory: Callable[[float], httpx2.Client] = default_client,
        clock: Callable[[], float] = time.monotonic,
        timeout: float = PROVIDER_TIMEOUT_SECONDS,
        backoff: Backoff = BACKOFF,
        own_addresses: frozenset[str] = frozenset(),
        folder_maps: FolderMapCache = FOLDER_MAPS,
    ) -> None:
        self._account, self._tokens, self._factory = account_id, tokens, client_factory
        self._clock, self._timeout, self._backoff = clock, timeout, backoff
        self._own_addresses, self._folder_maps = own_addresses, folder_maps

    # --- plumbing -------------------------------------------------------------------------------------------------
    def _run[T](
        self,
        work: Callable[[httpx2.Client, str, CallDeadline], T],
        call: CallDeadline,
        *,
        on_soft_stop: Callable[[], T] | None = None,
    ) -> T:
        # One deadline for the whole call, incl. a token refresh (L18), owned by the tool layer. Re-based onto this
        # adapter's clock (same elapsed time, same limits), so every comparison of the call (soft stop, request,
        # chunks, token refresh) uses one clock.
        now = self._clock()
        deadline = CallDeadline(now - (call.clock() - call.started), call.soft_seconds, call.hard_seconds, self._clock)
        try:
            self._backoff.check(self._account, now)  # no refresh, no request during a back-off (L16)
            with self._factory(self._timeout) as client:
                deadline.before_command()  # no token refresh starts after the soft stop (S-o)
                token = self._tokens.access_token(deadline.hard_at)  # a running refresh ends by the hard deadline
                try:
                    return work(client, token, deadline)
                except _UnauthorizedError:
                    self._tokens.invalidate()  # drop the rejected access token; one refresh and one retry (§6.3)
                deadline.before_command()
                token = self._tokens.access_token(deadline.hard_at)
                try:
                    return work(client, token, deadline)
                except _UnauthorizedError:
                    self._tokens.invalidate()  # Graph rejected the refreshed token too: the next call refreshes first
                    raise ProviderError("auth_expired", "GraphUnauthorized") from None
        except SoftStop:
            if on_soft_stop is None:
                raise ProviderError("upstream_timeout", "SoftStop") from None  # no partial result here (R2b)
            return on_soft_stop()
        except Exception as exc:
            raise _provider_error(exc) from None

    def _get(
        self, client: httpx2.Client, token: str, deadline: CallDeadline, path: str, params: dict[str, str], cap: int
    ) -> dict[str, object]:
        deadline.before_command()  # hard limit: upstream_timeout; soft stop: SoftStop (§5.4)
        now = self._clock()
        self._backoff.check(self._account, now)
        url = API + path
        parts = urlsplit(url)
        if parts.scheme != "https" or parts.hostname != GRAPH_HOST or parts.port not in (None, 443):
            raise ProviderError("upstream_error", "ForeignHost")
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "Prefer": PREFER,
        }
        # follow_redirects per request, not only in default_client: a following client would re-send the bearer
        # header to the Location host before the 3xx check below could refuse it (PR #26 review F2).
        with client.stream(
            "GET",
            url,
            params=params,
            headers=headers,
            timeout=min(deadline.remaining(), PROVIDER_TIMEOUT_SECONDS),
            follow_redirects=False,
        ) as response:
            status = response.status_code
            if status == 200:
                if response.headers.get("content-encoding", "identity").strip().lower() not in ("", "identity"):
                    raise ProviderError("upstream_error", "ContentEncoding")
                return load_json_object(
                    read_capped(response.iter_bytes(), cap, deadline=deadline.hard_at, clock=self._clock)
                )
            if 300 <= status < 400:
                raise ProviderError("upstream_error", "Redirect")
            if status == 401:
                raise _UnauthorizedError
            if status == 403:
                self._tokens.invalidate()  # D63: permission or licence, not authentication; next call re-fetches
                raise ProviderError("upstream_error", "Forbidden")
            if status == 404:
                raise ProviderError("not_found", "GraphNotFound")
            if status in (429, 503):
                self._backoff.set(
                    self._account, self._clock(), retry_after_seconds(response.headers.get("retry-after"))
                )
                raise ProviderError("upstream_error", "Throttled")
            raise ProviderError("upstream_error", "UnexpectedStatus")

    def _folder_ids(
        self, client: httpx2.Client, token: str, deadline: CallDeadline, *, refresh: bool = False
    ) -> GraphFolderIds:
        """The ids of the seven well-known folders (spec 080 §5.5), cached per account for at most 3,600 s.

        A 404 means the account has no such folder; any other failure, and an id outside `FOLDER_ID` (Sec8), fails
        the call. Ids are never logged."""
        if not refresh:
            cached = self._folder_maps.get(self._account)
            if isinstance(cached, GraphFolderIds):
                return cached
        found: dict[GraphWellKnown, str | None] = {}
        for name in GRAPH_WELL_KNOWN:
            try:
                body = self._get(
                    client, token, deadline, f"/me/mailFolders/{name}", {"$select": "id"}, MAX_LIST_RESPONSE_BYTES
                )
            except ProviderError as exc:
                if exc.code != "not_found":
                    raise
                found[name] = None
                continue
            folder_id = _folder_id(body)
            if folder_id is None:
                raise ProviderError("upstream_error", "MalformedFolderId")
            found[name] = folder_id
        ids = GraphFolderIds(self._account, found)
        self._folder_maps.put(ids)
        return ids

    def _bound_ids(
        self,
        client: httpx2.Client,
        token: str,
        deadline: CallDeadline,
        parents: Iterable[str | None],
        *,
        before_refusal: bool,
    ) -> GraphFolderIds:
        """The folder ids to bind `parents` with (D75).

        A map from an earlier call is read once more when it does not know a parent (`other`) or, with
        `before_refusal`, when it would exclude one; a map read in this call is used as it is. The forced re-read
        happens at most once per 60 s per account (Sec16: forged ids cannot multiply the folder GETs)."""
        cached = isinstance(self._folder_maps.get(self._account), GraphFolderIds)
        ids = self._folder_ids(client, token, deadline)
        refresh = ("other", "excluded") if before_refusal else ("other",)
        if (
            cached
            and any(parent is not None and ids.role_of(parent) in refresh for parent in parents)
            and self._folder_maps.may_force_rebuild(self._account)
        ):
            ids = self._folder_ids(client, token, deadline, refresh=True)
        return ids

    def _check_inbox(self, client: httpx2.Client, token: str, deadline: CallDeadline) -> None:
        body = self._get(client, token, deadline, "/me/mailFolders/inbox", {"$select": "id"}, MAX_LIST_RESPONSE_BYTES)
        if _folder_id(body) is None:
            raise ProviderError("upstream_error", "NoInboxId")

    # --- Mailbox protocol -----------------------------------------------------------------------------------------
    def check(self, deadline: CallDeadline) -> None:
        """Status check (spec 080 §7.4, rev. 4.6 S10): token (refresh only near expiry) + one inbox folder-id GET."""
        self._run(self._check_inbox, deadline)

    def list_unread(self, since: datetime, limit: int, deadline: CallDeadline) -> UnreadPage:
        """The unread inbox, through list_messages (kept until the tool layer calls list_messages itself)."""
        page = self.list_messages(ListQuery("inbox", since, True, None, limit), deadline)
        return UnreadPage(items=page.items, more=page.more or page.truncated)

    def list_messages(self, query: ListQuery, deadline: CallDeadline) -> FolderPage:
        return self._run(
            lambda client, token, call: self._list_messages(client, token, call, query),
            deadline,
            on_soft_stop=_soft_stopped_page,  # stopped before any folder answered: nothing read, no cursor
        )

    def get_message(self, target: AnyMessageRef | StableRef, deadline: CallDeadline) -> MailDetail:
        """One message by its Graph id (`id`, D75 binding) or by its Message-ID (`ref`, located first; §5.2)."""
        if isinstance(target, StableRef):
            if target.account != self._account:
                raise ProviderError("not_found", "ForeignRef")
            message_id = target.message_id
            if not MESSAGE_ID_PATTERN.fullmatch(message_id):  # the pattern keeps `'` out of the $filter literal
                raise ProviderError("not_found", "MalformedRef")
            return self._run(
                lambda client, token, call: self._message_by_ref(client, token, call, message_id), deadline
            )
        if not isinstance(target, GraphMessageRef) or target.account != self._account:
            raise ProviderError("not_found", "ForeignRef")
        ref = target
        return self._run(lambda client, token, call: self._message(client, token, call, ref), deadline)

    # --- list -----------------------------------------------------------------------------------------------------
    def _list_messages(self, client: httpx2.Client, token: str, deadline: CallDeadline, query: ListQuery) -> FolderPage:
        """One request per well-known folder of the query (spec 080 §5.5 "Cursors", §6.5), merged by order_key.

        Filter order as on IMAP (R3): drafts, window and unread per item, then the position, then the direction,
        then `limit`. A full answer (the effective `$top`, or a nextLink, which is never followed) sets `more` and a
        frontier at the second of its last item: only items received after that second are returned, and the page
        reports `(second, "ffff…")` so the tool's merge cuts there too and the next page reads the whole second again
        through `lt <second + 1 s>` (no gap where `$top` cuts inside one second)."""
        try:
            ids = self._folder_ids(client, token, deadline)
        except SoftStop:
            return _soft_stopped_page()
        bound = None if query.position is None else datetime.fromtimestamp(query.position.received_at, UTC)
        params = {
            "$filter": list_filter(query.since, bound, query.unread_only),
            "$orderby": "receivedDateTime desc",
            "$top": str(GRAPH_LIST_TOP),
            "$select": SUMMARY_SELECT,
            "$expand": EXTENDED_EXPAND,
        }
        found: list[MailSummary] = []
        seen: set[AnyMessageRef] = set()
        floor: int | None = None
        more = truncated = False
        complete = True
        for name in WELL_KNOWN_FOR[query.folder]:
            folder_id = ids.ids.get(name)
            if folder_id is None:
                continue  # the account has no such folder (404 when the ids were read)
            try:
                answer = self._folder_answer(client, token, deadline, folder_id, params, query, ids)
            except SoftStop:
                truncated, complete = True, False  # what was read is returned, without a cursor (§5.4)
                break
            if answer.stuck:
                # More than 100 messages in one second (S-f residual): the account stops, without a cursor.
                truncated, complete = True, False
                break
            if answer.frontier is not None:
                more = True
                floor = answer.frontier if floor is None else max(floor, answer.frontier)
            for summary in answer.items:
                if summary.ref not in seen:  # a repeated Graph id keeps its first entry
                    seen.add(summary.ref)
                    found.append(summary)
        if floor is not None:
            found = [summary for summary in found if order_key(summary)[0] > floor]
        if query.direction is not None:
            found = [summary for summary in found if summary.meta.direction == query.direction]
        found.sort(key=order_key, reverse=True)
        if len(found) > query.limit:
            more = True
        return FolderPage(
            items=found[: query.limit],
            more=more,
            truncated=truncated,
            complete=complete,
            frontier=None if floor is None else (floor, _FRONTIER_K),
        )

    def _folder_answer(
        self,
        client: httpx2.Client,
        token: str,
        deadline: CallDeadline,
        folder_id: str,
        params: dict[str, str],
        query: ListQuery,
        ids: GraphFolderIds,
    ) -> _FolderAnswer:
        path = f"/me/mailFolders/{quote(folder_id, safe='')}/messages"
        top = GRAPH_LIST_TOP
        try:
            body = self._get(client, token, deadline, path, params, MAX_LIST_RESPONSE_BYTES)
        except ProviderError as exc:
            if exc.code != "too_large":
                raise
            top = GRAPH_LIST_TOP // 2  # once more with $top halved; a second too_large fails the account (§5.4)
            body = self._get(client, token, deadline, path, params | {"$top": str(top)}, MAX_LIST_RESPONSE_BYTES)
        values = body.get("value")
        if not isinstance(values, list):
            raise ProviderError("upstream_error", "MalformedList")
        full = len(values) >= top or "@odata.nextLink" in body  # the effective $top decides (G6)
        items: list[MailSummary] = []
        after: list[tuple[int, str]] = []  # keys of the items read strictly after the position
        for raw in values[:top]:  # entries beyond the requested $top are never inspected (L7)
            key, summary = self._list_item(raw, ids, query)
            if key is None or (query.position is not None and not query.position.precedes(*key)):
                continue  # the local (received_at, k) drop of S-f
            after.append(key)
            if summary is not None:
                items.append(summary)
        if not full:
            return _FolderAnswer(items, None, False)
        if not after:
            return _FolderAnswer(items, None, True)
        second = min(key[0] for key in after)
        # Stuck only when the frontier cannot move the cursor on: on a later page a frontier second below the
        # position's second always progresses (the next page reads `lt <second + 1 s>`); on the first page there
        # must be an item above the frontier second. Otherwise one second holds at least $top messages.
        if query.position is not None:
            stuck = second >= query.position.received_at
        else:
            stuck = not any(key[0] > second for key in after)
        return _FolderAnswer(items, second, stuck)

    def _degraded(self, exc: Exception) -> None:
        # No valid id: item_degraded without the `item` field (spec 080 rev. 4.6 S12).
        log_event(
            _log,
            logging.WARNING,
            "item_degraded",
            account=self._account,
            capability="mail",
            exception=type(exc).__name__,
        )

    def _list_item(
        self, raw: object, ids: GraphFolderIds, query: ListQuery
    ) -> tuple[tuple[int, str] | None, MailSummary | None]:
        """(order key, summary): the key when the item has a valid id and timestamp, the summary when it is kept."""
        if not isinstance(raw, dict):
            self._degraded(MalformedItemError())
            return None, None
        graph_id = raw.get("id")
        if not isinstance(graph_id, str) or not GRAPH_ID.fullmatch(graph_id):
            self._degraded(MalformedItemError())
            return None, None
        ref = GraphMessageRef(self._account, graph_id)
        received = _timestamp(raw.get("receivedDateTime"))
        if received is None:
            log_message_skipped(ref, "mail", MalformedItemError())
            return None, None
        key = (int(received.timestamp()), location_k(ref))
        unread = raw.get("isRead") is not True
        # Drafts per message (S1); the window and unread_only re-checked on the server's answer.
        if raw.get("isDraft") is True or received < query.since or (query.unread_only and not unread):
            return key, None
        folder = ids.role_of(_parent(raw))
        if folder == "excluded":
            return key, None  # junk, drafts, outbox or no parent: never listed (D75)
        try:
            name, address = _address(raw.get("from"))
            return key, MailSummary(
                ref=ref,
                received_at=received,
                unread=unread,
                has_attachments=raw.get("hasAttachments") is True,
                from_address=address,
                from_name=name,
                subject=_str(raw.get("subject")),
                snippet_text=_str(raw.get("bodyPreview")) or "",
                to_addresses=tuple(self._recipients(raw.get("toRecipients"))),
                cc_addresses=tuple(self._recipients(raw.get("ccRecipients"))),
                meta=graph_meta(raw, ids, self._own_addresses, self._account),
            )
        except Exception as exc:
            log_message_skipped(ref, "mail", exc)
            listed = _listed_folder(folder)
            meta = MailMeta(folder=listed, direction="outgoing" if listed == "sent" else "incoming")
            return key, placeholder_summary(ref, received, unread, meta)

    # --- message --------------------------------------------------------------------------------------------------
    def _message_by_ref(self, client: httpx2.Client, token: str, deadline: CallDeadline, message_id: str) -> MailDetail:
        found = self._lookup_ref(client, token, deadline, message_id)
        if found is None:
            raise ProviderError("not_found", "RefNotFound")  # a complete search over all folders (B6)
        # The lookup already had the map's one re-read for this parent; the message read binds with that map.
        return self._message(client, token, deadline, found, may_rebuild=False)

    def _lookup_ref(
        self, client: httpx2.Client, token: str, deadline: CallDeadline, message_id: str
    ) -> GraphMessageRef | None:
        """The copy of `message_id` with the earliest `receivedDateTime`, ties by folder order (S-e), or None.

        One `GET /me/messages` over all folders, the value only inside the `$filter` string literal of an httpx
        query parameter (G8; the caller checked the §5.5 pattern, which excludes `'`). Hits are kept only with a
        Graph id (Sec8), the exact Message-ID (case-sensitive), no draft and a parent outside the excluded folders.
        A failed request, an answer that does not parse, or a full answer (`$top` or a nextLink, never followed)
        without an acceptable hit is an incomplete search: upstream_timeout, never None (G5, B6)."""
        params = {
            "$filter": f"internetMessageId eq '<{message_id}>'",
            "$select": SUMMARY_SELECT,
            "$top": str(REF_LOOKUP_TOP),
        }
        top = REF_LOOKUP_TOP
        try:
            try:
                body = self._get(client, token, deadline, "/me/messages", params, MAX_LIST_RESPONSE_BYTES)
            except ProviderError as exc:
                if exc.code != "too_large":
                    raise
                top = REF_LOOKUP_TOP // 2  # once more with $top halved, as for the folder lists (§5.4)
                body = self._get(
                    client, token, deadline, "/me/messages", params | {"$top": str(top)}, MAX_LIST_RESPONSE_BYTES
                )
            values = body.get("value")
            if not isinstance(values, list):
                raise ProviderError("upstream_error", "MalformedList")
        except ProviderError as exc:
            if _kept(exc):
                raise
            raise ProviderError("upstream_timeout", "IncompleteSearch") from None
        full = len(values) >= top or "@odata.nextLink" in body
        candidates: list[tuple[datetime, str, str | None]] = []
        for raw in values[:top]:  # entries beyond the requested $top are never inspected (L7)
            hit = self._lookup_hit(raw, message_id)
            if hit is not None:
                candidates.append(hit)
        best: tuple[datetime, int, str] | None = None
        if candidates:
            ids = self._bound_ids(client, token, deadline, [parent for *_, parent in candidates], before_refusal=False)
            for received, graph_id, parent in candidates:
                folder = ids.role_of(parent)
                if folder == "excluded":
                    continue  # junk, drafts, outbox: never located (D75)
                key = (received, _FOLDER_RANK[folder], graph_id)
                if best is None or key[:2] < best[:2]:  # equal keys: the first hit in Graph's order stays
                    best = key
        if best is None:
            if full:
                raise ProviderError("upstream_timeout", "IncompleteSearch")
            return None
        return GraphMessageRef(self._account, best[2])

    def _lookup_hit(self, raw: object, message_id: str) -> tuple[datetime, str, str | None] | None:
        """(received, Graph id, parent) of an exact, non-draft hit with a valid id and time; None otherwise."""
        if not isinstance(raw, dict):
            self._degraded(MalformedItemError())
            return None
        graph_id = raw.get("id")
        if not isinstance(graph_id, str) or not GRAPH_ID.fullmatch(graph_id):
            self._degraded(MalformedItemError())  # never used in a path (Sec8)
            return None
        found = bare_message_id((_str(raw.get("internetMessageId")) or "")[:MAX_HEADER_BYTES] or None)
        if found != message_id or raw.get("isDraft") is True:
            return None  # exact, case-sensitive equality (§5.5 matching); drafts never (S1)
        received = _timestamp(raw.get("receivedDateTime"))
        if received is None:
            log_message_skipped(GraphMessageRef(self._account, graph_id), "mail", MalformedItemError())
            return None
        return received, graph_id, _parent(raw)

    def _message(
        self,
        client: httpx2.Client,
        token: str,
        deadline: CallDeadline,
        ref: GraphMessageRef,
        *,
        may_rebuild: bool = True,
    ) -> MailDetail:
        """The immutable id follows moves (O37); 404, a draft or an excluded parent folder is not_found (D75, S1)."""
        path = f"/me/messages/{quote(ref.graph_id, safe='')}"
        preview_only = False
        try:
            body = self._get(
                client,
                token,
                deadline,
                path,
                {"$select": MESSAGE_SELECT, "$expand": EXTENDED_EXPAND},
                MAX_MESSAGE_RESPONSE_BYTES,
            )
        except ProviderError as exc:
            if exc.code != "too_large":
                raise
            preview_only = True
            body = self._get(
                client,
                token,
                deadline,
                path,
                {"$select": FALLBACK_SELECT, "$expand": EXTENDED_EXPAND},
                MAX_LIST_RESPONSE_BYTES,
            )
        if body.get("isDraft") is True:
            raise ProviderError("not_found", "Draft")
        parent = _parent(body)
        # A12: comparable ids are assumed (checked at the first live call, V3); the map is read once more before a
        # refusal or for an unknown parent, within the Sec16 limit, and an excluded parent then fails closed (SL6).
        ids = (
            self._bound_ids(client, token, deadline, [parent], before_refusal=True)
            if may_rebuild
            else self._folder_ids(client, token, deadline)
        )
        meta = graph_meta(body, ids, self._own_addresses, self._account)  # an excluded parent: not_found
        text, source, cut = self._body(body, preview_only)
        name, address = _address(body.get("from"))
        has_attachments = body.get("hasAttachments") is True
        return MailDetail(
            ref=ref,
            # Same fallback as the IMAP adapter (imap.py _get_message): a single read has no list timestamp.
            received_at=_timestamp(body.get("receivedDateTime")) or datetime.now(UTC),
            unread=body.get("isRead") is not True,
            attachments=self._attachments(client, token, deadline, path, ref) if has_attachments else [],
            from_address=address,
            from_name=name,
            to_addresses=self._recipients(body.get("toRecipients")),
            cc_addresses=self._recipients(body.get("ccRecipients")),
            subject=_str(body.get("subject")),
            body_text=text,
            body_source=source,
            body_cut=cut,
            meta=meta,
        )

    @staticmethod
    def _recipients(value: object) -> list[str]:
        if not isinstance(value, list):
            return []
        return [a for item in value if (a := _address(item)[1])]

    @staticmethod
    def _body(
        message: dict[str, object], preview_only: bool
    ) -> tuple[str, Literal["text/plain", "text/html-converted", "none"], bool]:
        if preview_only:
            return _str(message.get("bodyPreview")) or "", "text/plain", True
        body = message.get("body")
        content = body.get("content") if isinstance(body, dict) else None
        if not isinstance(content, str):
            return "", "none", False
        encoded = content.encode("utf-8", "surrogatepass")
        cut = len(encoded) > MAX_TEXT_PART_BYTES  # L10
        text = encoded[:MAX_TEXT_PART_BYTES].decode("utf-8", "ignore") if cut else content
        if isinstance(body, dict) and body.get("contentType") == "html":
            return html_to_text(text), "text/html-converted", cut
        return text, "text/plain", cut

    def _attachments(
        self, client: httpx2.Client, token: str, deadline: CallDeadline, path: str, ref: GraphMessageRef
    ) -> list[AttachmentMeta]:
        try:
            body = self._get(
                client, token, deadline, f"{path}/attachments", {"$select": ATTACHMENT_SELECT}, MAX_LIST_RESPONSE_BYTES
            )
            values = body.get("value")
            if not isinstance(values, list):
                raise MalformedItemError
        except ProviderError as exc:
            # The call deadline fails the whole call. A 401 is _UnauthorizedError, not a ProviderError: it reaches _run.
            if exc.code == "upstream_timeout":
                raise
            log_message_skipped(ref, "mail", exc)
            return []
        except MalformedItemError as exc:
            log_message_skipped(ref, "mail", exc)
            return []
        found: list[AttachmentMeta] = []
        for item in values:
            if not isinstance(item, dict) or item.get("isInline") is True:
                continue
            size = item.get("size")
            size_bytes = size if isinstance(size, int) and not isinstance(size, bool) and size >= 0 else 0
            found.append(AttachmentMeta(size_bytes, _str(item.get("name")), _str(item.get("contentType"))))
        return found


def _listed_folder(folder: MailFolder | Literal["excluded"]) -> MailFolder:
    return "other" if folder == "excluded" else folder
