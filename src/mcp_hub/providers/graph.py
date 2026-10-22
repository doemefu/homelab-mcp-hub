"""Read-only Microsoft Graph mail adapter (spec 080 §6.3, rev. 4.6 S1 to S5; §5.5 and §6.5, rev. 4.9).

Lists cover the well-known folders by id (inbox, sentitems, archive, deleteditems), with the mail-history fields of
§5.5 and keyset paging; GET only, fixed host, no redirects, no nextLink, capped streamed reads against one call
deadline with the 16 s soft stop. `get_message` reads one message by its immutable id, bound to its parent folder
(D75), or first locates a `ref` by `internetMessageId` over all folders. `message_status` locates refs in batched
`internetMessageId` lookups, reads `g` ids without their body and looks up replies and newer items by `conversationId`
(§5.2 `get_message_status`). Blocking; runs through providers.base.run_blocking. Returns decoded, unsanitised text;
the tool layer sanitises every string (§5.3)."""

import logging
import re
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Final, Literal, Protocol
from urllib.parse import quote, urlsplit

import httpx2

from mcp_hub.ids import (
    GRAPH_ID,
    MESSAGE_ID_PATTERN,
    AnyMessageRef,
    GraphMessageRef,
    MessageRef,
    StableRef,
    ThreadKey,
)
from mcp_hub.logging import log_event
from mcp_hub.providers.base import (
    MAX_HEADER_BYTES,
    MAX_TEXT_PART_BYTES,
    PROVIDER_TIMEOUT_SECONDS,
    AttachmentMeta,
    FolderPage,
    FoundMessage,
    ListQuery,
    MailDetail,
    MailFlags,
    MailMeta,
    MailSummary,
    MessageStatus,
    ProviderError,
    StatusTarget,
    direction_of,
    load_json_object,
    log_message_skipped,
    log_provider_failure,
    order_key,
    order_key_of,
    placeholder_summary,
    read_capped,
    status_unknown,
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
from mcp_hub.providers.graph_status import (
    STATUS_LOOKUP_TOP,
    THREAD_LOOKUP_TOP,
    THREAD_SELECT,
    ThreadEntry,
    conversation_filter,
    filter_batches,
    message_id_filter,
    thread_facts,
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
MAX_DEGRADED_LOGS: Final = 20  # item_degraded lines per get_message_status provider call (SR6)
MAX_FAILURE_LOGS: Final = 20  # provider_call_failed lines per get_message_status call, one per class (SR6, S-F2)
MAX_THREAD_SPLITS: Final = 10  # one-conversation re-requests of full conversationId answers per call (C14)
_log = logging.getLogger("mcp_hub.providers.graph")


class TokenSourceLike(Protocol):
    def access_token(self, deadline: float) -> str: ...
    def invalidate(self) -> None: ...


class _UnauthorizedError(Exception):
    """Graph answered 401: refresh once and retry (L15)."""


class MalformedItemError(Exception):
    """A list or message item with an unusable field; the class name is what gets logged."""


# Re-raised first by every per-batch handler of get_message_status (P6): the 401 refresh path and the soft stop are
# never a batch failure.
_STATUS_PASS_THROUGH: Final = (_UnauthorizedError, SoftStop)


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


def _fails_account(exc: ProviderError | MalformedItemError) -> bool:
    """Status-lookup failures that fail the account, not the batch (GR1, A-gr2): the hard deadline, credentials, the
    connection and 403. `Throttled` stays a batch failure, so `_kept` is not reused."""
    return isinstance(exc, ProviderError) and (
        exc.code in ("upstream_timeout", "auth_expired", "unreachable")
        or (exc.code == "upstream_error" and exc.cause == "Forbidden")
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


class _StatusRun:
    """State of one `message_status` work run. It is built inside `work`, so the restart after a 401 starts clean
    (Graph G9)."""

    def __init__(
        self,
        client: httpx2.Client,
        token: str,
        deadline: CallDeadline,
        fresh: GraphFolderIds | None,
        logged: set[tuple[str, str | None]],
    ) -> None:
        self.client, self.token, self.deadline = client, token, deadline
        self.fresh = fresh  # the folder ids when this call read them; None when they came from an earlier call
        self.located: dict[str, MessageStatus] = {}  # by bare Message-ID
        self.read: dict[GraphMessageRef, MessageStatus] = {}
        self.degraded = 0
        self.entries: dict[str, dict[str, ThreadEntry]] = {}  # by conversationId, then Graph id (united by id, C14)
        self.checked: set[str] = set()  # conversations whose answer was complete (not failed, not full, all usable)
        self.no_sent = False  # the account has no sentitems folder: no reply can be found (C5)
        self.logged = logged  # the (outcome, exception) classes of the call's provider_call_failed lines (S-F2)


@dataclass(frozen=True, slots=True)
class _Hit:
    """An exact, non-draft location hit with a usable id, time and parent."""

    message_id: str
    received: datetime
    graph_id: str
    parent: str
    raw: Mapping[str, object]


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

    def list_messages(self, query: ListQuery, deadline: CallDeadline) -> FolderPage:
        return self._run(
            lambda client, token, call: self._list_messages(client, token, call, query),
            deadline,
            on_soft_stop=_soft_stopped_page,  # stopped before any folder answered: nothing read, no cursor
        )

    def message_status(self, targets: Sequence[StatusTarget], deadline: CallDeadline) -> list[MessageStatus]:
        """One status per target, in order (spec 080 §5.2 `get_message_status`). Account failures raise; a soft stop
        before the token or the folder ids makes every entry incomplete_search."""
        logged: set[tuple[str, str | None]] = set()  # outlives a 401 restart: the log cap holds per call (S-F2)
        return self._run(
            lambda client, token, call: self._message_status(client, token, call, targets, logged),
            deadline,
            on_soft_stop=lambda: [status_unknown("incomplete_search") for _ in targets],
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
        key = order_key_of(received, ref)
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
        without an acceptable hit is an incomplete search: upstream_timeout, never None (G5, B6). Accepted residual
        (R39): a full answer that holds an acceptable hit uses the best hit on it, so with more than 50 copies of one
        Message-ID an earlier copy can lie beyond the page."""
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

    # --- get_message_status ---------------------------------------------------------------------------------------
    def _message_status(
        self,
        client: httpx2.Client,
        token: str,
        deadline: CallDeadline,
        targets: Sequence[StatusTarget],
        logged: set[tuple[str, str | None]],
    ) -> list[MessageStatus]:
        """Folder ids, then the `ref` location batches, then the `g` reads, then the conversations (A-sec2).

        A soft stop inside the loop keeps what was read; every entry not resolved by then is incomplete_search, and
        every found entry whose conversation was not checked by then has `thread_checked: false`."""
        fresh = not isinstance(self._folder_maps.get(self._account), GraphFolderIds)
        ids = self._folder_ids(client, token, deadline)  # a soft stop here: on_soft_stop; a failure fails the account
        run = _StatusRun(client, token, deadline, ids if fresh else None, logged)
        message_ids = [
            t.message_id
            for t in targets
            if isinstance(t, StableRef) and t.account == self._account and MESSAGE_ID_PATTERN.fullmatch(t.message_id)
        ]
        graph_refs = [
            t
            for t in targets
            if isinstance(t, GraphMessageRef) and t.account == self._account and GRAPH_ID.fullmatch(t.graph_id)
        ]
        try:
            for batch in filter_batches(message_ids, message_id_filter):
                self._locate_batch(run, batch)
            for ref in dict.fromkeys(graph_refs):  # one after another, the soft check before each (A-sec2)
                run.read[ref] = self._read_status(run, ref)
            self._thread_lookups(run)
        except SoftStop:
            pass  # what was read is kept (§5.4)
        return [self._with_thread(run, self._status_of(run, target)) for target in targets]

    def _status_of(self, run: _StatusRun, target: StatusTarget) -> MessageStatus:
        if isinstance(target, MessageRef):
            return status_unknown("id_mismatch")  # the tool answers these before any provider call
        incomplete = status_unknown("incomplete_search")  # not searched: refused value, foreign account or soft stop
        if isinstance(target, GraphMessageRef):
            return run.read.get(target, incomplete) if target.account == self._account else incomplete
        return run.located.get(target.message_id, incomplete) if target.account == self._account else incomplete

    def _status_ids(self, run: _StatusRun, parents: Iterable[str | None], *, before_refusal: bool) -> GraphFolderIds:
        """A map read in this call binds as it is; one from an earlier call goes through `_bound_ids` (Sec16).
        A failure of the re-read fails the account (a folder-id failure, C13)."""
        if run.fresh is not None:
            return run.fresh
        return self._bound_ids(run.client, run.token, run.deadline, parents, before_refusal=before_refusal)

    def _status_degraded(self, run: _StatusRun) -> None:
        if run.degraded < MAX_DEGRADED_LOGS:  # SR6
            run.degraded += 1
            self._degraded(MalformedItemError())

    def _batch_failed(self, run: _StatusRun, exc: ProviderError | MalformedItemError) -> None:
        """A WARNING with the cause class only; the status store and account_errors stay (GR4).

        Only the first failed batch of each (outcome, exception) class is logged per call, at most MAX_FAILURE_LOGS
        lines (S-F2): 50 failing `g` reads write one line, and after a `Throttled` answer the back-off makes every
        later batch fail without a request (T12-1)."""
        error = exc if isinstance(exc, ProviderError) else ProviderError("upstream_error", type(exc).__name__)
        key = (error.code, error.cause)
        if key in run.logged or len(run.logged) >= MAX_FAILURE_LOGS:
            return
        run.logged.add(key)
        log_provider_failure(self._account, "mail", error)

    def _lookup_answer(self, run: _StatusRun, params: dict[str, str], top: int) -> tuple[list[object], bool]:
        """(values within the effective `$top`, full) of one `GET /me/messages` lookup.

        `too_large` is requested once more with `$top` halved; a second one fails the batch (C10). Full: the effective
        `$top` reached, or a nextLink, which is never followed."""
        try:
            body = self._get(run.client, run.token, run.deadline, "/me/messages", params, MAX_LIST_RESPONSE_BYTES)
        except ProviderError as exc:
            if exc.code != "too_large":
                raise
            top //= 2
            params = params | {"$top": str(top)}
            body = self._get(run.client, run.token, run.deadline, "/me/messages", params, MAX_LIST_RESPONSE_BYTES)
        values = body.get("value")
        if not isinstance(values, list):
            raise ProviderError("upstream_error", "MalformedList")
        # Entries beyond the requested $top are never inspected (L7).
        return values[:top], len(values) >= top or "@odata.nextLink" in body

    def _locate_batch(self, run: _StatusRun, batch: list[str]) -> None:
        """One `internetMessageId` batch (Graph G1-G5): each value found, gone or incomplete_search.

        `gone` only after a 200 that was not full, without an acceptable hit and without an unusable exact match
        (GR2). A full answer (the effective `$top`, or a nextLink, never followed) keeps its hits (R39)."""
        try:
            hits, unusable, full = self._location_page(run, batch)
        except _STATUS_PASS_THROUGH:
            raise
        except (ProviderError, MalformedItemError) as exc:
            if _fails_account(exc):
                raise
            self._batch_failed(run, exc)
            return  # the batch's refs stay incomplete_search
        found: dict[str, MessageStatus] = {}
        if hits:
            ids = self._status_ids(run, [hit.parent for hit in hits], before_refusal=False)
            best: dict[str, tuple[datetime, int, _Hit]] = {}
            in_sent: set[str] = set()
            for hit in hits:
                folder = ids.role_of(hit.parent)
                if folder == "excluded":
                    continue  # junk, drafts, outbox: never located (D75)
                if folder == "sent":
                    in_sent.add(hit.message_id)
                current = best.get(hit.message_id)
                key = (hit.received, _FOLDER_RANK[folder])
                if current is None or key < current[:2]:  # equal keys: the first hit in Graph's order stays (S-e)
                    best[hit.message_id] = (*key, hit)
            for message_id, (received, _, hit) in best.items():
                meta = graph_meta(hit.raw, ids, self._own_addresses, self._account)
                if message_id in in_sent:
                    meta = replace(meta, direction="outgoing")  # any copy in sent (MF-R2)
                copy = FoundMessage(
                    location=GraphMessageRef(self._account, hit.graph_id),
                    message_id=message_id,
                    received_at=received,
                    unread=hit.raw.get("isRead") is not True,
                    meta=meta,
                )
                found[message_id] = MessageStatus("found", found=copy, thread_checked=False)
        for message_id in batch:
            if message_id in found:
                run.located[message_id] = found[message_id]
            elif message_id in unusable or full:
                run.located[message_id] = status_unknown("incomplete_search")
            else:
                run.located[message_id] = MessageStatus("gone")

    def _location_page(self, run: _StatusRun, batch: list[str]) -> tuple[list[_Hit], set[str], bool]:
        """(acceptable hits, Message-IDs with an unusable exact match, full) of one batch request."""
        params = {
            "$filter": message_id_filter(batch),  # re-checks the §5.5 pattern before the literal (A-sec2)
            "$select": SUMMARY_SELECT,
            "$expand": EXTENDED_EXPAND,
            "$top": str(STATUS_LOOKUP_TOP),
        }
        values, full = self._lookup_answer(run, params, STATUS_LOOKUP_TOP)
        wanted = set(batch)
        hits: list[_Hit] = []
        unusable: set[str] = set()
        for raw in values:
            hit = self._status_hit(run, raw, wanted)
            if isinstance(hit, str):
                unusable.add(hit)
            elif hit is not None:
                hits.append(hit)
        return hits, unusable, full

    def _status_hit(self, run: _StatusRun, raw: object, wanted: set[str]) -> _Hit | str | None:
        """An acceptable hit, the Message-ID of an exact match that is unusable (GR2), or None (no exact match)."""
        if not isinstance(raw, dict):
            self._status_degraded(run)
            return None
        message_id = bare_message_id((_str(raw.get("internetMessageId")) or "")[:MAX_HEADER_BYTES] or None)
        if message_id is None or message_id not in wanted or raw.get("isDraft") is True:
            return None  # exact, case-sensitive equality (§5.5 matching); drafts never (S1)
        graph_id = raw.get("id")
        received = _timestamp(raw.get("receivedDateTime"))
        parent = _parent(raw)
        if not (isinstance(graph_id, str) and GRAPH_ID.fullmatch(graph_id)) or received is None or parent is None:
            self._status_degraded(run)  # no value logged (§9.7)
            return message_id
        return _Hit(message_id, received, graph_id, parent, raw)

    def _read_status(self, run: _StatusRun, ref: GraphMessageRef) -> MessageStatus:
        """A `g` entry (Graph G6): the immutable id follows moves; no body and no attachment read, 1 MiB cap."""
        path = f"/me/messages/{quote(ref.graph_id, safe='')}"
        params = {"$select": SUMMARY_SELECT, "$expand": EXTENDED_EXPAND}
        try:
            body = self._get(run.client, run.token, run.deadline, path, params, MAX_LIST_RESPONSE_BYTES)
        except _STATUS_PASS_THROUGH:
            raise
        except (ProviderError, MalformedItemError) as exc:
            if _fails_account(exc):
                raise
            if isinstance(exc, ProviderError) and exc.code == "not_found":
                return MessageStatus("gone")  # 404: the id follows moves, so the message is gone
            self._batch_failed(run, exc)
            return status_unknown("incomplete_search")  # 400, 5xx, too_large (GR9), throttled
        if body.get("isDraft") is True:
            return MessageStatus("gone")  # turned into a draft (S1)
        parent = _parent(body)
        received = _timestamp(body.get("receivedDateTime"))
        if parent is None or received is None:
            self._status_degraded(run)
            return status_unknown("incomplete_search")  # an unusable answer is never gone (GR2)
        ids = self._status_ids(run, [parent], before_refusal=True)
        if ids.role_of(parent) == "excluded":
            return MessageStatus("gone")  # junk, drafts, outbox (D75)
        meta = graph_meta(body, ids, self._own_addresses, self._account)
        found = FoundMessage(
            location=ref,
            message_id=valid_message_id(meta.message_id),
            received_at=received,
            unread=body.get("isRead") is not True,
            meta=meta,
        )
        return MessageStatus("found", found=found, thread_checked=False)

    def _thread_lookups(self, run: _StatusRun) -> None:
        """The conversations of the found copies (Graph G7), each value once, in batches of `filter_batches`; then a
        full answer to a batch of several values is requested again one conversation per request, at most
        MAX_THREAD_SPLITS per call in batch order (C14). A conversation is checked once one answer for it was
        complete; what was read from full answers is kept."""
        found = [*run.located.values(), *run.read.values()]
        conversations = [c for status in found if (c := _conversation(status)) is not None]
        if not conversations:
            return
        # The map of this call binds the parents as it is: no forced re-read for an unknown parent, which is common
        # in a conversation; a folder-id failure fails the account (C13).
        ids = self._folder_ids(run.client, run.token, run.deadline)
        run.no_sent = ids.ids.get("sentitems") is None
        split: list[str] = []
        for batch in filter_batches(conversations, conversation_filter):
            if self._thread_batch(run, batch, ids) and len(batch) > 1:
                split.extend(batch)
        for conversation in split[:MAX_THREAD_SPLITS]:  # a single-value answer still full stays unchecked
            self._thread_batch(run, [conversation], ids)

    def _thread_batch(self, run: _StatusRun, batch: list[str], ids: GraphFolderIds) -> bool:
        """One `conversationId` request; True when its answer was full. A failed request marks only its
        conversations unchecked (B7, GR4); account failures, a 401 and the soft stop pass (GR1, P6)."""
        params = {"$filter": conversation_filter(batch), "$select": THREAD_SELECT, "$top": str(THREAD_LOOKUP_TOP)}
        try:
            values, full = self._lookup_answer(run, params, THREAD_LOOKUP_TOP)
        except _STATUS_PASS_THROUGH:
            raise
        except (ProviderError, MalformedItemError) as exc:
            if _fails_account(exc):
                raise
            self._batch_failed(run, exc)
            return False
        wanted = set(batch)
        spoiled: set[str] = set()  # conversations with an unusable item: it may be the reply or the newer item
        for raw in values:
            entry = self._thread_entry(run, raw, wanted, ids, spoiled)
            if entry is None:
                continue
            known = run.entries.setdefault(entry.conversation_id, {})
            if entry.graph_id in known:
                continue  # entries are united by Graph id (C14)
            known[entry.graph_id] = entry
            if (entry.sent_at if entry.folder == "sent" else entry.received_at) is None:
                self._status_degraded(run)  # GR10: its conversation is not checked (ThreadFacts.complete)
        if not full:
            run.checked.update(c for c in batch if c not in spoiled)
        return full

    def _thread_entry(
        self, run: _StatusRun, raw: object, wanted: set[str], ids: GraphFolderIds, spoiled: set[str]
    ) -> ThreadEntry | None:
        """A conversation item with a valid id, not a draft, in an allowed folder (an unknown parent is `other`) and
        of a conversation of the batch; None otherwise.

        Fail closed (T13-1): an unusable item of a batch conversation (an invalid id, a missing or malformed parent)
        leaves that conversation unchecked, an item that cannot be attributed (not an object, no conversationId)
        every conversation of the batch. Drafts, a valid excluded parent and other conversations are dropped."""
        if not isinstance(raw, dict):
            self._status_degraded(run)
            spoiled.update(wanted)
            return None
        if raw.get("isDraft") is True:
            return None  # drafts never (S1)
        conversation = raw.get("conversationId")
        if not isinstance(conversation, str):
            self._status_degraded(run)
            spoiled.update(wanted)
            return None
        if conversation not in wanted:
            return None
        graph_id = raw.get("id")
        parent = _parent(raw)  # None: missing or malformed, told apart from a valid excluded parent
        if not (isinstance(graph_id, str) and GRAPH_ID.fullmatch(graph_id)) or parent is None:
            self._status_degraded(run)  # no value logged (§9.7)
            spoiled.add(conversation)
            return None
        folder = ids.role_of(parent)
        if folder == "excluded":
            return None  # junk, drafts, outbox (D75)
        return ThreadEntry(
            graph_id=graph_id,
            conversation_id=conversation,
            folder=folder,
            received_at=_timestamp(raw.get("receivedDateTime")),
            sent_at=_timestamp(raw.get("sentDateTime")),
            message_id=bare_message_id((_str(raw.get("internetMessageId")) or "")[:MAX_HEADER_BYTES] or None),
            from_address=_address(raw.get("from"))[1],
        )

    def _with_thread(self, run: _StatusRun, status: MessageStatus) -> MessageStatus:
        """The reply and thread fields of a found status (§5.2): `thread_checked` only when its conversation was
        checked, no entry of it had an unparsable time (GR10) and the account has a sentitems folder (C5). A copy
        without a valid conversationId keeps `thread_checked: false` and no reply fields; replied then comes from the
        flag alone (C7)."""
        found = status.found
        conversation = _conversation(status)
        if found is None or conversation is None or not isinstance(found.location, GraphMessageRef):
            return status
        facts = thread_facts(
            found.location.graph_id,
            found.meta.message_id,  # the bare Message-ID: its copies never count, even when it is not valid (C8)
            found.received_at,
            conversation,
            list(run.entries.get(conversation, {}).values()),
            self._own_addresses,
        )
        return replace(
            status,
            reply_at=facts.reply_at,
            reply_message_id=valid_message_id(facts.reply_message_id),
            newer_incoming_at=facts.newer_incoming_at,
            thread_checked=conversation in run.checked and facts.complete and not run.no_sent,
        )


def _conversation(status: MessageStatus) -> str | None:
    """The valid conversationId of a found copy, re-checked against the Graph id pattern before any batch (C7)."""
    thread = status.found.meta.thread if status.found is not None else None
    value = thread.value if thread is not None and thread.source == "graph" else None
    return value if isinstance(value, str) and GRAPH_ID.fullmatch(value) else None


def _listed_folder(folder: MailFolder | Literal["excluded"]) -> MailFolder:
    return "other" if folder == "excluded" else folder
