"""Read-only Microsoft Graph mail adapter (spec 080 §6.3, rev. 4.6 S1 to S5; §5.5 and §6.5, rev. 4.9).

Lists cover the well-known folders by id (inbox, sentitems, archive, deleteditems), with the mail-history fields of
§5.5 and keyset paging; GET only, fixed host, no redirects, no nextLink, capped streamed reads against one call
deadline with the 16 s soft stop. `get_message` reads one message by its immutable id, bound to its parent folder
(D75), or first locates a `ref` by `internetMessageId` over all folders. `message_status` locates refs in batched
`internetMessageId` lookups, reads `g` ids without their body and looks up replies and newer items by `conversationId`
(§5.2 `get_message_status`). `get_thread` reads one conversation by `conversationId` with the server-cut `uniqueBody`
(§5.2 `get_thread`, §6.5). Blocking; runs through providers.base.run_blocking. Returns decoded, unsanitised text; the
tool layer sanitises every string (§5.3)."""

import logging
import re
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Final, Literal, Protocol, TypeGuard
from urllib.parse import quote, urlsplit

import httpx2

from mcp_hub import search as _search  # the module: `search` imports providers.base, so names would cycle
from mcp_hub.ids import (
    GRAPH_ID,
    MESSAGE_ID_PATTERN,
    AnyMessageRef,
    GraphMessageRef,
    MessageRef,
    SearchHitRef,
    StableRef,
    ThreadKey,
)
from mcp_hub.logging import log_event
from mcp_hub.providers.base import (
    MAX_HEADER_BYTES,
    MAX_TEXT_PART_BYTES,
    OMITTED_BODY,
    PROVIDER_TIMEOUT_SECONDS,
    AttachmentMeta,
    FolderPage,
    FoundMessage,
    IncompleteReason,
    ListQuery,
    MailDetail,
    MailFlags,
    MailMeta,
    MailSummary,
    MessageStatus,
    ProviderError,
    SearchTerms,
    StatusTarget,
    ThreadBody,
    ThreadMember,
    ThreadPage,
    ThreadQuery,
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
from mcp_hub.providers.graph_thread import MAX_UNIQUE_BODY_READS, THREAD_LIST_TOP, body_text, thread_list_params
from mcp_hub.providers.headers import MAX_REFERENCES, bare_message_id, msg_id_tokens, valid_message_id
from mcp_hub.providers.threads import (
    FOLDER_RANK,
    ThreadCopy,
    choose_members,
    merge_copies,
    overview,
    participants_of,
    prepare_body,
    server_body,
)
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
SEARCH_TOP: Final = 100  # one mailbox-wide `$search` request per account (owner decision, 2026-10-27)
SEARCH_HALVED_TOP: Final = 50  # the one retry after a too_large answer
# `$expand` with `$search` answered 200 with `singleValueExtendedProperties` present (probe P8, SEARCH_FACTS).
SEARCH_EXPAND: Final = True
READ_ON_SECONDS: Final = 8.0  # no new read-on request after this much of the call (G5-3)
FILTER_READ_REQUESTS: Final = 5  # list requests per folder for one filter-only search_messages call (ruling 5, GR1)
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
_SEED_SELECT: Final = "id,conversationId,parentFolderId,isDraft,receivedDateTime"  # a `get_thread` id seed (D75)
# Lookup failures that keep their code; every other lookup failure is an incomplete search, never not_found (B6).
_KEPT_CAUSES: Final = frozenset({"Throttled", "Forbidden"})
_RETRY_AFTER: Final = re.compile(r"[0-9]+")  # ASCII delay-seconds only; anything else is "not a number"
_LEGACY_DN: Final = re.compile(r"/O=[^/@]+(?:/OU=[^/@]+)*/CN=RECIPIENTS/CN=[^/@]+", re.IGNORECASE)  # fullmatch (T17)
_PLAIN_ADDRESS: Final = re.compile(r"[^@\s]+@[^@\s]+")  # fullmatch: one "@", no whitespace
_DN_ADDRESS: Final = re.compile(r"\s*/O=", re.IGNORECASE)  # match: an Exchange DN, never a participant (gr-G4)
# Log causes of the `$search` request: a 400 (the syntax was refused), a 404, and any other unmapped status (5xx) as
# `_UNEXPECTED_STATUS`, so that an operator can tell a sanitiser gap from an outage (spec 080 §6.5, D88).
_SEARCH_REJECTED: Final = "SearchRejected"
_SEARCH_NOT_FOUND: Final = "SearchNotFound"
_EMPTY_SEARCH_TEXT: Final = "EmptySearchText"  # the tool layer refuses words with no searchable text first
_UNEXPECTED_STATUS: Final = "UnexpectedStatus"  # `_get`'s cause for a status it does not map (G-R7)
# The get_thread list falls back on these causes too: any status but 2xx, 401, 403, 404, 429 and 503 (SG1, Q7).
_FALLBACK_CAUSES: Final = frozenset({_UNEXPECTED_STATUS, "Redirect"})
MAX_ADDRESS_CHARS: Final = 254
MAX_DEGRADED_LOGS: Final = 20  # item_degraded lines per get_message_status or get_thread provider call (SR6)
MAX_FAILURE_LOGS: Final = 20  # provider_call_failed lines per status or thread call, one per class (SR6, S-F2, P15)
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


def _legacy_dn_name(name: str | None, address: str | None) -> str | None:
    """The stripped name when `address` is a well-formed legacy DN and the name a plain address; None otherwise.

    T17: an own sent item can carry an Exchange legacy DN ("/O=.../CN=RECIPIENTS/CN=...") as the address and the SMTP
    address as the name; only a well-formed DN (no "@") and a plain address of at most 254 characters count. One
    predicate for the direction and the participants (G-R2)."""
    if address is None or name is None or not _LEGACY_DN.fullmatch(address):
        return None
    stripped = name.strip()
    return stripped if len(stripped) <= MAX_ADDRESS_CHARS and _PLAIN_ADDRESS.fullmatch(stripped) else None


def _sender_for_direction(raw: Mapping[str, object]) -> str | None:
    """The `from` address that decides the direction; never an output field. A legacy DN counts as its name."""
    name, address = _address(raw.get("from"))
    return _legacy_dn_name(name, address) or address


def _participant(name: str | None, address: str | None) -> str | None:
    """A participant address (§5.5): a DN ("/O=...") or any other value that is not a plain address becomes the
    name of a strict legacy DN or is dropped (gr-G4), so a participant is always an address."""
    if address is not None and (_DN_ADDRESS.match(address) or not _PLAIN_ADDRESS.fullmatch(address.strip())):
        return _legacy_dn_name(name, address)
    return address


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
        direction=direction_of(in_sent=folder == "sent", from_address=_sender_for_direction(raw), own=own),
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


class _ThreadLogs:
    """The log caps of one `get_thread` call; built outside `work`, so they hold across the 401 restart (P15)."""

    def __init__(self) -> None:
        self.degraded = 0  # item_degraded lines (SR6)
        self.failed: set[tuple[str, str | None]] = set()  # (outcome, exception) classes of failed single reads


@dataclass(frozen=True, slots=True)
class _ThreadSeed:
    """The seed of a `get_thread` call, as its lookup or read found it."""

    graph_id: str
    message_id: str | None  # the Message-ID of a `ref` seed; an `id` seed is recognised by its Graph id (G-R3)
    conversation: str | None  # a valid conversationId, else None (SG4)


def _conversation_id(raw: Mapping[str, object]) -> str | None:
    value = raw.get("conversationId")
    return value if isinstance(value, str) and GRAPH_ID.fullmatch(value) else None


def _soft_stopped_page(query: ListQuery) -> FolderPage:
    """Nothing read before the soft stop; the reason is reported for `search_messages` only (list pages unchanged)."""
    incomplete: frozenset[IncompleteReason] = frozenset({"time_limit"}) if query.search is not None else frozenset()
    return FolderPage(items=[], more=False, truncated=True, complete=False, incomplete=incomplete)


def _folded_has(needle: str, text: str | None, cap: int) -> bool:
    return text is not None and needle in _search.match_fold(text[:cap])


def _participant_matches(needle: str, entry: object) -> bool:
    """A name or a non-DN address of one Graph recipient value contains the folded needle (MF-D2)."""
    name, address = _address(entry)
    if _folded_has(needle, name, _search.MATCH_NAME_CHARS):
        return True
    return (
        address is not None
        and not _DN_ADDRESS.match(address)
        and _folded_has(needle, address, _search.MATCH_NAME_CHARS)
    )


def graph_filters_match(raw: Mapping[str, object], terms: SearchTerms) -> bool:
    """The hub-side `sender`, `recipient` and `subject` filters of `search_messages` (spec 080 §5.2, MF-D2).

    Plain substring on `match_fold` of needle and candidate, never a pattern: `sender` = `from` name or non-DN
    address; `recipient` = any `toRecipients` entry (never Cc/Bcc); `subject` = its first 4,000 characters. The words
    are not checked here: Graph matched them. An empty folded needle matches nothing."""
    if terms.sender is not None:
        needle = _search.match_fold(terms.sender)
        if not needle or not _participant_matches(needle, raw.get("from")):
            return False
    if terms.recipient is not None:
        needle = _search.match_fold(terms.recipient)
        to = raw.get("toRecipients")
        if not needle or not isinstance(to, list) or not any(_participant_matches(needle, e) for e in to):
            return False
    if terms.subject is not None:
        needle = _search.match_fold(terms.subject)
        if not needle or not _folded_has(needle, _str(raw.get("subject")), _search.MATCH_SUBJECT_CHARS):
            return False
    return True


def _search_requests(ids: GraphFolderIds, query: ListQuery) -> tuple[str, dict[str, str], frozenset[str]]:
    """The one `$search` request of a call: (path, params, allowed parent folder ids). The only place that builds it.

    Mailbox-wide, never per folder: a per-folder `$search` lost hits (probe P1b). The value is the allowlist-sanitised
    word text inside two double quotes, sent as an httpx query parameter; `$filter`, `$orderby` and `ConsistencyLevel`
    do not combine with `$search`, so window, order and folders are applied per hit."""
    terms = query.search
    text = None if terms is None else _search.graph_search_text(terms.words)
    if text is None:  # the tool refuses this first (`invalid_argument`); reaching here is a hub defect
        raise ProviderError("upstream_error", _EMPTY_SEARCH_TEXT)
    params = {"$search": f'"{text}"', "$top": str(SEARCH_TOP), "$select": SUMMARY_SELECT}
    if SEARCH_EXPAND:
        params["$expand"] = EXTENDED_EXPAND
    allowed = frozenset(i for name in WELL_KNOWN_FOR[query.folder] if (i := ids.ids.get(name)) is not None)
    return "/me/messages", params, allowed


def _by_id(ref: object) -> TypeGuard[GraphMessageRef]:
    """A Graph reference a by-id read may use: never a `SearchHitRef` (its id is a regular id, D87: never a GET)."""
    return isinstance(ref, GraphMessageRef) and not isinstance(ref, SearchHitRef)


@dataclass(frozen=True, slots=True)
class _FolderAnswer:
    items: list[MailSummary]  # after the draft, window, unread, folder and position filters
    frontier: int | None  # a full answer: the second of its last item; that second and older not yet scanned
    stuck: bool  # a full answer whose frontier cannot move the cursor on (S-f residual)
    gets: int = 1  # GETs it took: 2 after a too_large answer retried with `$top` halved


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
        self,
        client: httpx2.Client,
        token: str,
        deadline: CallDeadline,
        path: str,
        params: dict[str, str],
        cap: int,
        *,
        bad_request_cause: str | None = None,
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
            if status == 400 and bad_request_cause is not None:
                raise ProviderError("upstream_error", bad_request_cause)
            raise ProviderError("upstream_error", _UNEXPECTED_STATUS)

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
        if query.search is not None and query.search.words:
            if (
                _search.graph_search_text(query.search.words) is None
            ):  # the tool refuses this first (`invalid_argument`)
                raise ProviderError("upstream_error", _EMPTY_SEARCH_TEXT)
            search = self._search_messages
        else:
            search = self._list_messages
        return self._run(
            lambda client, token, call: search(client, token, call, query),
            deadline,
            on_soft_stop=lambda: _soft_stopped_page(
                query
            ),  # stopped before any folder answered: nothing read, no cursor
        )

    def message_status(self, targets: Sequence[StatusTarget], deadline: CallDeadline) -> list[MessageStatus]:
        """One status per target, in order (spec 080 §5.2 `get_message_status`). Account failures raise; a soft stop
        before the token or the folder ids makes every entry incomplete_search."""
        logged: set[tuple[str, str | None]] = set()  # outlives a 401 restart: the log cap holds per call (S-F2)
        # Decided once: after a 401 restart, the folder ids read before it are still this call's (PR #51 review CR4).
        fresh = not isinstance(self._folder_maps.get(self._account), GraphFolderIds)
        return self._run(
            lambda client, token, call: self._message_status(client, token, call, targets, logged, fresh=fresh),
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
        if not _by_id(target) or target.account != self._account:
            raise ProviderError("not_found", "ForeignRef")
        ref = target
        return self._run(lambda client, token, call: self._message(client, token, call, ref), deadline)

    def get_thread(self, query: ThreadQuery, deadline: CallDeadline) -> ThreadPage:
        """One conversation, oldest first (spec 080 §5.2 `get_thread`, §6.5 Graph). A soft stop before the list
        answered is upstream_timeout (no partial result); during the body reads the rest is body_omitted."""
        conversation: str | None = None
        if query.key is not None:
            if query.key.source != "graph":
                return ThreadPage("source_mismatch")
            if query.key.account != self._account:
                raise ProviderError("not_found", "ForeignRef")
            if not isinstance(query.key.value, str) or not GRAPH_ID.fullmatch(query.key.value):
                raise ProviderError("not_found", "MalformedRef")
            conversation = query.key.value
        elif isinstance(query.seed, StableRef):
            if query.seed.account != self._account:
                raise ProviderError("not_found", "ForeignRef")
            if not MESSAGE_ID_PATTERN.fullmatch(query.seed.message_id):  # keeps `'` out of the $filter literal
                raise ProviderError("not_found", "MalformedRef")
        elif not _by_id(query.seed) or query.seed.account != self._account:
            raise ProviderError("not_found", "ForeignRef")
        logs = _ThreadLogs()
        return self._run(
            lambda client, token, call: self._thread(client, token, call, query, conversation, logs), deadline
        )

    # --- search ---------------------------------------------------------------------------------------------------
    def _search_messages(
        self, client: httpx2.Client, token: str, deadline: CallDeadline, query: ListQuery
    ) -> FolderPage:
        """`search_messages` with words: one mailbox-wide `$search` request, the hub applies the rest (spec 080 §6.5).

        Per hit: not a draft, parent in the requested folders, inside the window, after the position, the hub filters
        and the direction. Dropped hits still count toward the 100 (`result_cap`). A hit carries a `SearchHitRef`: the
        id `$search` returns is not the immutable id, so it is only an order key and the handle is the Message-ID."""
        try:
            ids = self._folder_ids(client, token, deadline)
        except SoftStop:
            return _soft_stopped_page(query)
        try:
            path, params, allowed = _search_requests(ids, query)
            values, full = self._search_answer(client, token, deadline, path, params)
        except SoftStop:
            return _soft_stopped_page(query)
        except ProviderError as exc:
            if exc.code == "not_found":
                raise ProviderError("upstream_error", _SEARCH_NOT_FOUND) from None
            raise
        found: list[MailSummary] = []
        seen: set[AnyMessageRef] = set()
        for raw in values:
            key, summary = self._list_item(raw, ids, query, searched=True)
            if key is None or summary is None or _parent(raw if isinstance(raw, dict) else {}) not in allowed:
                continue
            if query.position is not None and not query.position.precedes(*key):
                continue
            if query.direction is not None and summary.meta.direction != query.direction:
                continue
            if summary.ref not in seen:  # a repeated returned id keeps its first entry (never by Message-ID, MF-R4)
                seen.add(summary.ref)
                found.append(summary)
        found.sort(key=order_key, reverse=True)
        return FolderPage(
            items=found[: query.limit],
            more=len(found) > query.limit,
            truncated=full,
            complete=True,
            incomplete=frozenset({"result_cap"}) if full else frozenset(),
        )

    def _search_answer(
        self, client: httpx2.Client, token: str, deadline: CallDeadline, path: str, params: dict[str, str]
    ) -> tuple[list[object], bool]:
        """The raw hits and whether the answer was full: the effective `$top` reached or an `@odata.nextLink`, which is
        never followed. A too_large answer is repeated once with `$top` halved; a second one fails the account."""
        top = SEARCH_TOP
        try:
            body = self._get(
                client, token, deadline, path, params, MAX_LIST_RESPONSE_BYTES, bad_request_cause=_SEARCH_REJECTED
            )
        except ProviderError as exc:
            if exc.code != "too_large":
                raise
            top = SEARCH_HALVED_TOP
            body = self._get(
                client,
                token,
                deadline,
                path,
                params | {"$top": str(top)},
                MAX_LIST_RESPONSE_BYTES,
                bad_request_cause=_SEARCH_REJECTED,
            )
        values = body.get("value")
        if not isinstance(values, list):
            raise ProviderError("upstream_error", "MalformedList")
        return values[:top], len(values) >= top or "@odata.nextLink" in body

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
            return _soft_stopped_page(query)
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
        incomplete: set[IncompleteReason] = set()
        read_on = query.search is not None and not query.search.words  # filter-only search (D84)
        plan = [(name, folder_id) for name in WELL_KNOWN_FOR[query.folder] if (folder_id := ids.ids.get(name))]
        for position, (_, folder_id) in enumerate(plan):  # a folder the account lacks (404 on the ids) is not planned
            throttled = False
            mine: dict[AnyMessageRef, MailSummary] = {}  # the re-read frontier second is counted once
            current, base, previous, requests = params, params, None, 0
            answer: _FolderAnswer | None = None
            try:
                while True:
                    # A retry could pass the cap: then the read stops early with the last frontier (read-on only).
                    retry = not read_on or previous is None or requests + 2 <= FILTER_READ_REQUESTS
                    try:
                        answer = self._folder_answer(
                            client, token, deadline, folder_id, current, query, ids, previous, retry
                        )
                    except ProviderError as exc:
                        # Past the first answer a read-on request may fail without losing what was read: too_large
                        # (no retry allowed here) and a throttled answer (the back-off keeps the next call away).
                        throttled = exc.code == "upstream_error" and exc.cause == "Throttled"
                        if answer is None or not (exc.code == "too_large" or throttled):
                            raise
                        break  # the last frontier stands
                    requests += answer.gets
                    if answer.stuck:
                        break
                    for summary in answer.items:
                        mine.setdefault(summary.ref, summary)
                    if answer.frontier is None or not read_on or requests >= FILTER_READ_REQUESTS:
                        break
                    if deadline.elapsed() >= READ_ON_SECONDS:
                        break  # no new read-on request: the page gets its cursor from the frontier
                    if sum(1 for summary in mine.values() if order_key(summary)[0] > answer.frontier) >= query.limit:
                        break
                    previous = answer.frontier  # read on: the next request starts at the frontier second
                    bound_at = datetime.fromtimestamp(previous, UTC)
                    if answer.gets > 1:
                        base = base | {"$top": str(GRAPH_LIST_TOP // 2)}  # a folder that needed halving keeps it
                    current = base | {"$filter": list_filter(query.since, bound_at, query.unread_only)}
            except SoftStop:
                truncated, complete = True, False  # what was read is returned, without a cursor (§5.4)
                incomplete.add("time_limit")
                answer = None
            for summary in mine.values():
                if summary.ref not in seen:  # a repeated Graph id keeps its first entry
                    seen.add(summary.ref)
                    found.append(summary)
            if answer is None:
                break
            if answer.stuck:
                # More than 100 messages in one second (S-f residual): the account stops, without a cursor.
                truncated, complete = True, False
                incomplete.add("result_cap")
                break
            if answer.frontier is not None:
                more = True
                floor = answer.frontier if floor is None else max(floor, answer.frontier)
            if throttled and position + 1 < len(plan):
                # The later folders were never read: a cursor at this frontier would skip their newer items.
                truncated, complete = True, False
                incomplete.add("time_limit")
                break
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
            incomplete=frozenset(incomplete) if query.search is not None else frozenset(),
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
        previous: int | None = None,
        retry: bool = True,
    ) -> _FolderAnswer:
        path = f"/me/mailFolders/{quote(folder_id, safe='')}/messages"
        top, gets = int(params["$top"]), 1
        try:
            body = self._get(client, token, deadline, path, params, MAX_LIST_RESPONSE_BYTES)
        except ProviderError as exc:
            if exc.code != "too_large" or not retry or top != GRAPH_LIST_TOP:
                raise  # only 101 is halved, once: there is no third tier
            top, gets = top // 2, 2  # once more with $top halved; a second too_large fails the account (§5.4)
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
            return _FolderAnswer(items, None, False, gets)
        if not after:
            return _FolderAnswer(items, None, True, gets)
        second = min(key[0] for key in after)
        # Stuck only when the frontier cannot move the cursor on: on a later page a frontier second below the
        # position's second always progresses (the next page reads `lt <second + 1 s>`); on the first page there
        # must be an item above the frontier second. Otherwise one second holds at least $top messages.
        if previous is not None:
            stuck = second >= previous  # a read-on request must end below the frontier it started at
        elif query.position is not None:
            stuck = second >= query.position.received_at
        else:
            stuck = not any(key[0] > second for key in after)
        return _FolderAnswer(items, second, stuck, gets)

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
        self, raw: object, ids: GraphFolderIds, query: ListQuery, *, searched: bool = False
    ) -> tuple[tuple[int, str] | None, MailSummary | None]:
        """(order key, summary): the key when the item has a valid id and timestamp, the summary when it is kept."""
        if not isinstance(raw, dict):
            self._degraded(MalformedItemError())
            return None, None
        graph_id = raw.get("id")
        if not isinstance(graph_id, str) or not GRAPH_ID.fullmatch(graph_id):
            self._degraded(MalformedItemError())
            return None, None
        ref = (SearchHitRef if searched else GraphMessageRef)(self._account, graph_id)
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
            if query.search is not None and not graph_filters_match(raw, query.search):
                return key, None  # the key still feeds the frontier
        except Exception as exc:  # the filter could not be evaluated: a hit that may not match is not listed
            log_message_skipped(ref, "mail", exc)
            return key, None
        try:  # past the filters the hit is confirmed: a failing summary becomes a placeholder, never a dropped match
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
        """`_lookup_copy` without the conversation (get_message)."""
        found = self._lookup_copy(client, token, deadline, message_id)
        return None if found is None else found[0]

    def _lookup_copy(
        self, client: httpx2.Client, token: str, deadline: CallDeadline, message_id: str
    ) -> tuple[GraphMessageRef, str | None] | None:
        """The copy of `message_id` with the earliest `receivedDateTime`, ties by folder order (S-e), with its valid
        `conversationId` (gr-G1), or None.

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
        candidates: list[tuple[datetime, str, str | None, str | None]] = []
        for raw in values[:top]:  # entries beyond the requested $top are never inspected (L7)
            hit = self._lookup_hit(raw, message_id)
            if hit is not None and isinstance(raw, dict):
                candidates.append((*hit, _conversation_id(raw)))
        best: tuple[datetime, int, str, str | None] | None = None
        if candidates:
            ids = self._bound_ids(client, token, deadline, [c[2] for c in candidates], before_refusal=False)
            for received, graph_id, parent, conversation in candidates:
                folder = ids.role_of(parent)
                if folder == "excluded":
                    continue  # junk, drafts, outbox: never located (D75)
                key = (received, FOLDER_RANK[folder], graph_id, conversation)
                if best is None or key[:2] < best[:2]:  # equal keys: the first hit in Graph's order stays
                    best = key
        if best is None:
            if full:
                raise ProviderError("upstream_timeout", "IncompleteSearch")
            return None
        return GraphMessageRef(self._account, best[2]), best[3]

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
        *,
        fresh: bool,
    ) -> list[MessageStatus]:
        """Folder ids, then the `ref` location batches, then the `g` reads, then the conversations (A-sec2). `fresh`:
        the call found no cached folder ids, so the ids it uses were read in this call.

        A soft stop inside the loop keeps what was read; every entry not resolved by then is incomplete_search, and
        every found entry whose conversation was not checked by then has `thread_checked: false`."""
        ids = self._folder_ids(client, token, deadline)  # a soft stop here: on_soft_stop; a failure fails the account
        run = _StatusRun(client, token, deadline, ids if fresh else None, logged)
        message_ids = [
            t.message_id
            for t in targets
            if isinstance(t, StableRef) and t.account == self._account and MESSAGE_ID_PATTERN.fullmatch(t.message_id)
        ]
        graph_refs = [t for t in targets if _by_id(t) and t.account == self._account and GRAPH_ID.fullmatch(t.graph_id)]
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
                key = (hit.received, FOLDER_RANK[folder])
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
            from_address=_sender_for_direction(raw),  # a legacy-DN own item is not newer incoming (T17)
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

    # --- get_thread -----------------------------------------------------------------------------------------------
    def _thread(
        self,
        client: httpx2.Client,
        token: str,
        deadline: CallDeadline,
        query: ThreadQuery,
        conversation: str | None,
        logs: _ThreadLogs,
    ) -> ThreadPage:
        """Seed, list, members, bodies (Option 5a). Inside `work`: the 401 restart runs it again from the start.

        A seed without a valid conversationId (SG4), or a seed input whose filtered list holds no member (G-R1, K9),
        is returned alone and not exact. A key whose list holds no member is not_found after a complete list and
        upstream_timeout otherwise (Q6)."""
        seed = None if query.seed is None else self._thread_seed(client, token, deadline, query.seed)
        if seed is not None:
            conversation = seed.conversation
            if conversation is None:
                return self._seed_alone(client, token, deadline, query, seed, None, logs)
        if conversation is None:
            raise ProviderError("not_found", "MalformedRef")  # unreachable: get_thread checked the key
        # The map of this call binds the members as it is: no forced re-read for an unknown parent (as C13).
        ids = self._folder_ids(client, token, deadline)
        values, full, unique = self._thread_list(client, token, deadline, conversation, logs)
        exact = not full  # 51 values or a nextLink: more members may exist (gr-G2)
        copies: list[ThreadCopy] = []
        raws: dict[AnyMessageRef, Mapping[str, object]] = {}
        for raw in values:
            try:
                copy = self._thread_copy(raw, ids, seed)
            except Exception as exc:  # an unusable item: dropped, and the list is not exact (gr-G2)
                self._thread_degraded(logs, exc)
                exact = False
                continue
            if copy is None or not isinstance(raw, dict) or copy.summary.ref in raws:
                continue  # a draft, an excluded folder, or a repeated Graph id (the first entry stays)
            raws[copy.summary.ref] = raw
            copies.append(copy)
        merged = merge_copies(copies, include_trash=query.include_trash)  # the trash filter after the merge (G-R3)
        counted, kept, capped = choose_members(merged, include_trash=query.include_trash)
        thread = ThreadKey(self._account, "graph", conversation)
        if not counted:
            if seed is not None:
                return self._seed_alone(client, token, deadline, query, seed, thread, logs)
            if not exact:
                raise ProviderError("upstream_timeout", "IncompleteSearch")
            raise ProviderError("not_found", "NotFound")
        members = self._thread_bodies(client, token, deadline, kept, raws if unique else {}, query.max_chars, logs)
        return ThreadPage(
            "ok",
            thread=thread,
            members=members,
            overview=overview(counted, exact=exact),
            truncated=full or capped or any(member.body.omitted for member in members),
        )

    def _thread_seed(
        self, client: httpx2.Client, token: str, deadline: CallDeadline, target: AnyMessageRef | StableRef
    ) -> _ThreadSeed:
        """A `ref` through `_lookup_copy` (not found → not_found; incomplete → upstream_timeout, B6); an `id` by a
        small read bound to its parent (D75): 404, a draft or an excluded parent → not_found."""
        if isinstance(target, StableRef):
            found = self._lookup_copy(client, token, deadline, target.message_id)
            if found is None:
                raise ProviderError("not_found", "RefNotFound")  # a complete search over all folders (B6)
            ref, conversation = found
            return _ThreadSeed(ref.graph_id, target.message_id, conversation)
        if not _by_id(target):
            raise ProviderError("not_found", "ForeignRef")
        path = f"/me/messages/{quote(target.graph_id, safe='')}"
        body = self._get(client, token, deadline, path, {"$select": _SEED_SELECT}, MAX_LIST_RESPONSE_BYTES)
        if body.get("isDraft") is True:
            raise ProviderError("not_found", "Draft")
        parent = _parent(body)
        ids = self._bound_ids(client, token, deadline, [parent], before_refusal=True)
        if ids.role_of(parent) == "excluded":
            raise ProviderError("not_found", "ExcludedFolder")
        return _ThreadSeed(target.graph_id, None, _conversation_id(body))

    def _seed_alone(
        self,
        client: httpx2.Client,
        token: str,
        deadline: CallDeadline,
        query: ThreadQuery,
        seed: _ThreadSeed,
        thread: ThreadKey | None,
        logs: _ThreadLogs,
    ) -> ThreadPage:
        """The seed read once with its summary fields and bodies (P5, G-R1) and returned alone, not exact."""
        path = f"/me/messages/{quote(seed.graph_id, safe='')}"
        params = {"$select": f"{SUMMARY_SELECT},uniqueBody,body", "$expand": EXTENDED_EXPAND}
        raw = self._get(client, token, deadline, path, params, MAX_MESSAGE_RESPONSE_BYTES)
        if raw.get("isDraft") is True:
            raise ProviderError("not_found", "Draft")
        # The seed's lookup or read already bound its parent; the map is used as it is (as get_message by ref).
        ids = self._folder_ids(client, token, deadline)
        received = _timestamp(raw.get("receivedDateTime")) or datetime.now(UTC)  # as get_message
        copy = self._thread_copy_of(raw, GraphMessageRef(self._account, seed.graph_id), received, ids, seed=True)
        body = self._thread_body(raw, copy.summary.subject, query.max_chars, logs, listed=False) or OMITTED_BODY
        return ThreadPage(
            "ok",
            thread=thread,
            members=(ThreadMember(copy.summary, copy.linked_by, body),),
            overview=overview([copy], exact=False),
            truncated=body.omitted,
        )

    def _thread_list(
        self, client: httpx2.Client, token: str, deadline: CallDeadline, conversation: str, logs: _ThreadLogs
    ) -> tuple[list[object], bool, bool]:
        """(values within `$top` 51, full, with uniqueBody) of the conversation list (§6.5).

        `too_large`, or any status but 2xx, 401, 403, 404, 429 and 503, reads the list once more without
        `uniqueBody` (SG1, Q7, Q-a); the trigger is logged once per call through `_thread_failed`, so a refused
        `uniqueBody` list (O37) shows in the logs. A second failure is the tool error of its code. `$top` is never
        halved and a nextLink is never followed; values beyond 51 are never inspected."""
        try:
            body = self._thread_list_get(client, token, deadline, conversation, unique_body=True)
            unique = True
        except ProviderError as exc:
            if not (exc.code == "too_large" or (exc.code == "upstream_error" and exc.cause in _FALLBACK_CAUSES)):
                raise
            self._thread_failed(logs, exc)
            body = self._thread_list_get(client, token, deadline, conversation, unique_body=False)
            unique = False
        values = body.get("value")
        if not isinstance(values, list):
            raise ProviderError("upstream_error", "MalformedList")
        full = len(values) >= THREAD_LIST_TOP or "@odata.nextLink" in body
        return values[:THREAD_LIST_TOP], full, unique

    def _thread_list_get(
        self, client: httpx2.Client, token: str, deadline: CallDeadline, conversation: str, *, unique_body: bool
    ) -> dict[str, object]:
        params = thread_list_params(
            conversation, select=SUMMARY_SELECT, expand=EXTENDED_EXPAND, unique_body=unique_body
        )
        try:
            return self._get(client, token, deadline, "/me/messages", params, MAX_MESSAGE_RESPONSE_BYTES)
        except ProviderError as exc:
            if exc.code == "not_found":  # the collection, not a message: never the tool's not_found (G-R4)
                raise ProviderError("upstream_error", exc.cause) from None
            raise

    def _thread_copy(self, raw: object, ids: GraphFolderIds, seed: _ThreadSeed | None) -> ThreadCopy | None:
        """A list item as a copy; None for a draft or an excluded folder. An unusable item (not an object, an invalid
        id, an unparsable time, a missing or malformed parent) raises MalformedItemError."""
        if not isinstance(raw, dict):
            raise MalformedItemError
        graph_id = raw.get("id")
        if not isinstance(graph_id, str) or not GRAPH_ID.fullmatch(graph_id):
            raise MalformedItemError  # never used in a path (Sec8)
        if raw.get("isDraft") is True:
            return None  # drafts never (S1)
        received = _timestamp(raw.get("receivedDateTime"))
        parent = _parent(raw)
        if received is None or parent is None:
            raise MalformedItemError
        if ids.role_of(parent) == "excluded":
            return None  # junk, drafts, outbox (D75)
        ref = GraphMessageRef(self._account, graph_id)
        copy = self._thread_copy_of(raw, ref, received, ids, seed=False)
        if seed is not None and (
            graph_id == seed.graph_id
            or (seed.message_id is not None and copy.summary.meta.message_id == seed.message_id)
        ):
            copy = replace(copy, seed=True)  # by Graph id or by Message-ID (G-R3)
        return copy

    def _thread_copy_of(
        self, raw: Mapping[str, object], ref: GraphMessageRef, received: datetime, ids: GraphFolderIds, *, seed: bool
    ) -> ThreadCopy:
        """Summary (snippet empty, the body replaces it) and participants through the DN rule (gr-G4)."""
        meta = graph_meta(raw, ids, self._own_addresses, self._account)  # an excluded parent: not_found (D75)
        name, address = _address(raw.get("from"))
        summary = MailSummary(
            ref=ref,
            received_at=received,
            unread=raw.get("isRead") is not True,
            has_attachments=raw.get("hasAttachments") is True,
            from_address=address,
            from_name=name,
            subject=_str(raw.get("subject")),
            snippet_text="",
            to_addresses=tuple(self._recipients(raw.get("toRecipients"))),
            cc_addresses=tuple(self._recipients(raw.get("ccRecipients"))),
            meta=meta,
        )
        participants = participants_of(
            meta.direction,
            _participant(name, address),
            self._participants(raw.get("toRecipients")),
            self._participants(raw.get("ccRecipients")),
            self._own_addresses,
        )
        return ThreadCopy(summary, "server", participants, in_sent=meta.folder == "sent", seed=seed)

    @staticmethod
    def _participants(value: object) -> list[str]:
        if not isinstance(value, list):
            return []
        return [a for item in value if (a := _participant(*_address(item)))]

    def _thread_bodies(
        self,
        client: httpx2.Client,
        token: str,
        deadline: CallDeadline,
        kept: Sequence[ThreadCopy],
        listed: Mapping[AnyMessageRef, Mapping[str, object]],
        max_chars: int,
        logs: _ThreadLogs,
    ) -> tuple[ThreadMember, ...]:
        """Bodies newest first: the list item's `uniqueBody` (`listed`, empty after the fallback), else one single
        read, at most MAX_UNIQUE_BODY_READS per call; the rest, and every body after a soft stop, body_omitted."""
        bodies: dict[AnyMessageRef, ThreadBody] = {}
        reads = 0
        try:
            for item in reversed(kept):
                if deadline.soft_expired():
                    raise SoftStop  # a soft check per member body, also without a request (sec-G5)
                raw = listed.get(item.summary.ref)
                body = (
                    None if raw is None else self._thread_body(raw, item.summary.subject, max_chars, logs, listed=True)
                )
                if body is None:
                    if reads == MAX_UNIQUE_BODY_READS:
                        continue  # beyond 20 single reads: body_omitted
                    reads += 1
                    body = self._read_thread_body(client, token, deadline, item, max_chars, logs)
                bodies[item.summary.ref] = body
        except SoftStop:
            pass  # what was read is kept; the rest is body_omitted (§5.4)
        return tuple(
            ThreadMember(item.summary, item.linked_by, bodies.get(item.summary.ref, OMITTED_BODY)) for item in kept
        )

    def _read_thread_body(
        self,
        client: httpx2.Client,
        token: str,
        deadline: CallDeadline,
        item: ThreadCopy,
        max_chars: int,
        logs: _ThreadLogs,
    ) -> ThreadBody:
        """One single read of `uniqueBody,body` (SG2, 2 MiB), retried once with `uniqueBody` alone after too_large
        (G-R6). A failed read omits only this body (SC5); account failures, a 401 and the soft stop pass (P6)."""
        if not isinstance(item.summary.ref, GraphMessageRef):
            return OMITTED_BODY  # every Graph member has a Graph id
        path = f"/me/messages/{quote(item.summary.ref.graph_id, safe='')}"
        try:
            try:
                raw = self._get(
                    client, token, deadline, path, {"$select": "uniqueBody,body"}, MAX_MESSAGE_RESPONSE_BYTES
                )
            except ProviderError as exc:
                if exc.code != "too_large":
                    raise
                raw = self._get(client, token, deadline, path, {"$select": "uniqueBody"}, MAX_MESSAGE_RESPONSE_BYTES)
        except _STATUS_PASS_THROUGH:
            raise
        except ProviderError as exc:
            if _fails_account(exc):
                raise
            self._thread_failed(logs, exc)
            return OMITTED_BODY
        return self._thread_body(raw, item.summary.subject, max_chars, logs, listed=False) or OMITTED_BODY

    def _thread_body(
        self, raw: Mapping[str, object], subject: str | None, max_chars: int, logs: _ThreadLogs, *, listed: bool
    ) -> ThreadBody | None:
        """`uniqueBody` → "server"; else, on a read (not `listed`), `body` with the heuristic (SG2). None: a list item
        without a usable `uniqueBody` (it joins the single reads). Any failure omits only this body (SC1)."""
        try:
            unique = body_text(raw, "uniqueBody")
            if unique is not None:
                text, source, cut = unique
                return server_body(text, source, max_chars=max_chars, fetch_cut=cut)
            if listed:
                return None
            found = body_text(raw, "body")
            if found is None:
                raise MalformedItemError  # a read without either body
            text, source, cut = found
            return prepare_body(text, source, subject=subject, max_chars=max_chars, fetch_cut=cut)
        except Exception as exc:
            self._thread_degraded(logs, exc)
            return OMITTED_BODY

    def _thread_degraded(self, logs: _ThreadLogs, exc: Exception) -> None:
        if logs.degraded < MAX_DEGRADED_LOGS:  # one counter per call (SR6)
            logs.degraded += 1
            self._degraded(exc)

    def _thread_failed(self, logs: _ThreadLogs, exc: ProviderError) -> None:
        """One WARNING per (outcome, exception) class and call, at most MAX_FAILURE_LOGS (P15): after a `Throttled`
        answer the back-off fails every later read without a request, and that is logged once."""
        key = (exc.code, exc.cause)
        if key in logs.failed or len(logs.failed) >= MAX_FAILURE_LOGS:
            return
        logs.failed.add(key)
        log_provider_failure(self._account, "mail", exc)


def _conversation(status: MessageStatus) -> str | None:
    """The valid conversationId of a found copy, re-checked against the Graph id pattern before any batch (C7)."""
    thread = status.found.meta.thread if status.found is not None else None
    value = thread.value if thread is not None and thread.source == "graph" else None
    return value if isinstance(value, str) and GRAPH_ID.fullmatch(value) else None


def _listed_folder(folder: MailFolder | Literal["excluded"]) -> MailFolder:
    return "other" if folder == "excluded" else folder
