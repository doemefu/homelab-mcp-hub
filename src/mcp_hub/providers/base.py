"""Provider-call plumbing shared by every adapter (spec 080 §5.1 timeouts, §5.4 inbound limits, §6 common rules)."""

import hashlib
import json
import logging
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final, Literal

import anyio
import anyio.to_thread

from mcp_hub.cursors import Position
from mcp_hub.errors import ErrorCode
from mcp_hub.ids import AnyMessageRef, GraphMessageRef, MessageRef, StableRef, ThreadKey, location_id, location_k
from mcp_hub.logging import log_event
from mcp_hub.providers.folders import ListFolder, MailFolder
from mcp_hub.providers.headers import is_own
from mcp_hub.registry import Capability

PROVIDER_TIMEOUT_SECONDS: Final = 20.0
TOOL_TIMEOUT_SECONDS: Final = 60.0
MAX_CONNECTIONS_PER_ACCOUNT: Final = 2
MAX_PROVIDER_THREADS: Final = 16  # live worker threads incl. abandoned ones, all accounts together
MAX_TEXT_PART_BYTES: Final = 262_144
SNIPPET_FETCH_BYTES: Final = 4096
MAX_HEADER_BYTES: Final = 65_536
MAX_HTTP_RESPONSE_BYTES: Final = 5 * 1024 * 1024
# Fixed texts for account_errors and tool errors; never provider text (spec 080 §5.1, §5.3 rule 8).
ACCOUNT_ERROR_MESSAGES: Final[dict[ErrorCode, str]] = {
    "auth_expired": "Credential rejected by provider; re-login required",
    "unreachable": "Provider not reachable",
    "upstream_timeout": "Provider did not answer in time",
    "upstream_error": "Provider returned an error",
    "too_large": "Provider response exceeded the inbound size limit",
    "not_found": "Item not found",
}
UNDECODABLE_NOTE: Final = "[the hub could not decode this message]"  # hub text, shown inside untrusted.snippet
_log = logging.getLogger("mcp_hub.providers")


class ProviderError(Exception):
    """A provider call failed. `cause` is an exception class name for the log, never exception text."""

    def __init__(self, code: ErrorCode, cause: str | None = None) -> None:
        super().__init__(code)
        self.code: ErrorCode = code
        self.cause = cause


Direction = Literal["incoming", "outgoing"]


@dataclass(frozen=True, slots=True)
class MailFlags:
    answered: bool = False
    flagged: bool = False  # "seen" is the inverse of `unread` on the summary


@dataclass(frozen=True, slots=True)
class MailMeta:
    """Mail-history fields of a message (spec 080 §5.1); third-party strings are decoded, not yet sanitised."""

    folder: MailFolder = "inbox"
    folder_name: str | None = None
    flags: MailFlags = MailFlags()
    direction: Direction = "incoming"
    message_id: str | None = None
    in_reply_to: str | None = None
    references: tuple[str, ...] = ()
    thread: ThreadKey | None = None


NO_META: Final = MailMeta()


@dataclass(frozen=True, slots=True)
class MailSummary:
    """Decoded but not yet sanitised; the tool layer sanitises every string (spec 080 §5.3)."""

    ref: AnyMessageRef
    received_at: datetime  # timezone-aware INTERNALDATE
    unread: bool
    has_attachments: bool
    from_address: str | None
    from_name: str | None
    subject: str | None
    snippet_text: str
    to_addresses: tuple[str, ...] = ()
    cc_addresses: tuple[str, ...] = ()
    meta: MailMeta = MailMeta()


@dataclass(frozen=True, slots=True)
class AttachmentMeta:
    size_bytes: int
    filename: str | None
    content_type: str | None


@dataclass(frozen=True, slots=True)
class MailDetail:
    ref: AnyMessageRef
    received_at: datetime
    unread: bool
    attachments: list[AttachmentMeta]  # all of them; the tool returns at most 20
    from_address: str | None
    from_name: str | None
    to_addresses: list[str]
    cc_addresses: list[str]
    subject: str | None
    body_text: str
    body_source: Literal["text/plain", "text/html-converted", "none"]
    body_cut: bool  # the text part exceeded the 256 KiB inbound limit
    meta: MailMeta = MailMeta()


SearchKey = Literal["TEXT", "FROM", "TO", "SUBJECT"]
IncompleteReason = Literal["result_cap", "search_refused", "time_limit", "changed_during_call"]
MAX_SEARCH_WORDS: Final = 8


@dataclass(frozen=True, slots=True)
class SearchTerms:
    """Normalised wire form of a search (spec 080 §5.2 "Query handling"); built by `search.parse_search_terms`."""

    words: tuple[str, ...]
    sender: str | None
    recipient: str | None
    subject: str | None

    def __post_init__(self) -> None:
        if not (self.words or self.sender or self.recipient or self.subject):
            raise ValueError("at least one search term is required")
        if len(self.words) > MAX_SEARCH_WORDS:
            raise ValueError("at most 8 words")
        values = (*self.words, *(v for v in (self.sender, self.recipient, self.subject) if v is not None))
        if any(not value or any(ch.isspace() for ch in value) for value in self.words) or "" in values:
            raise ValueError("words and filters must be non-empty, words without whitespace")
        if len({word.casefold() for word in self.words}) != len(self.words):
            raise ValueError("words must be distinct")


@dataclass(frozen=True, slots=True)
class ListQuery:
    folder: ListFolder
    since: datetime
    unread_only: bool
    direction: Direction | None
    limit: int
    position: Position | None = None  # last item of the previous page; None on the first page
    search: SearchTerms | None = None  # search_messages only


@dataclass(frozen=True, slots=True)
class FolderPage:
    items: list[MailSummary]
    more: bool  # more matching messages exist than returned
    truncated: bool  # the adapter stopped early (soft stop or candidate cap)
    complete: bool  # every folder of the plan was searched
    # With `more`: the order_key of the oldest position the adapter scanned. A page can hold fewer than `limit`
    # items (filters applied after the scan) yet have scanned everything above it; nothing below it was looked at.
    # Graph gives the frontier second as (second, "f" * 16), so that whole second is read again on the next page.
    # None with `more`: a full page, whose last item is the frontier.
    frontier: tuple[int, str] | None = None
    incomplete: frozenset[IncompleteReason] = frozenset()  # why the page may miss matches (search_messages)
    # Every incomplete folder of the page was incomplete only because it was refused (search_messages cursor rule).
    refused_only: bool = False


StatusState = Literal["found", "gone", "unknown"]
StatusReason = Literal["account_error", "account_unavailable", "id_mismatch", "stale_id", "incomplete_search"]
StatusTarget = StableRef | MessageRef | GraphMessageRef  # one distinct entry of one account


@dataclass(frozen=True, slots=True)
class FoundMessage:
    """The earliest found copy of a message; third-party strings are decoded, not yet sanitised.

    `location`, `received_at` and `meta.folder` are those of the earliest copy, but `meta.direction` is `outgoing`
    when any found copy of the Message-ID is in sent (ruling MF-R2); an `uncertain` copy still counts as found (C3)."""

    location: AnyMessageRef  # the copy found (-> `id`)
    message_id: str | None  # valid bare Message-ID of that copy (-> `ref`), else None
    received_at: datetime  # timezone-aware
    unread: bool
    meta: MailMeta  # folder, flags (answered, flagged), direction


@dataclass(frozen=True, slots=True)
class MessageStatus:
    state: StatusState
    reason: StatusReason | None = None
    found: FoundMessage | None = None
    reply_at: datetime | None = None  # earliest reply found (IMAP INTERNALDATE / Graph sentDateTime)
    reply_message_id: str | None = None  # its valid bare Message-ID (-> `reply_ref`)
    newer_incoming_at: datetime | None = None  # newest incoming thread message (rules per provider)
    # Every reply/thread batch covering this ref completed: a bool when found, None otherwise (ruling E-td1).
    thread_checked: bool | None = None

    def __post_init__(self) -> None:
        if (self.found is not None) != (self.state == "found"):
            raise ValueError("found is set exactly when state is found")
        if (self.reason is None) != (self.state != "unknown"):
            raise ValueError("reason is set exactly when state is unknown")
        if (self.found is not None) != (self.thread_checked is not None):
            raise ValueError("thread_checked is a bool exactly when state is found")
        if self.reply_message_id is not None and self.reply_at is None:
            raise ValueError("reply_message_id needs reply_at")
        if self.found is None and (
            self.reply_at is not None
            or self.reply_message_id is not None
            or self.newer_incoming_at is not None
            or self.thread_checked is not None
        ):
            raise ValueError("reply and thread fields need a found message")


def status_unknown(reason: StatusReason) -> MessageStatus:
    return MessageStatus("unknown", reason)


LinkedBy = Literal["server", "headers", "subject"]
BodySource = Literal["text/plain", "text/html-converted", "none"]
QuoteRemoval = Literal["server", "heuristic", "none"]
MIN_CHARS_PER_MESSAGE: Final = 200
MAX_CHARS_PER_MESSAGE: Final = 5000
MAX_THREAD_MESSAGES: Final = 50
MAX_PARTICIPANTS: Final = 20


@dataclass(frozen=True, slots=True)
class ThreadQuery:
    """One `get_thread` call (spec 080 §5.2): a seed (`ref` or `id`) or a thread key, never both."""

    seed: AnyMessageRef | StableRef | None
    key: ThreadKey | None
    include_trash: bool
    max_chars: int  # max_chars_per_message

    def __post_init__(self) -> None:
        if (self.seed is None) == (self.key is None):
            raise ValueError("exactly one of seed and key is set")
        if not MIN_CHARS_PER_MESSAGE <= self.max_chars <= MAX_CHARS_PER_MESSAGE:
            raise ValueError("max_chars is out of range")


@dataclass(frozen=True, slots=True)
class ThreadBody:
    """A member body after quote removal, decoded but not yet sanitised; the tool cuts it to `max_chars`."""

    text: str  # at most BODY_KEEP_FACTOR * max_chars characters
    source: BodySource
    quote_removal: QuoteRemoval
    original_chars: int | None  # None for a server cut ("server") and for an omitted body
    cut: bool  # the 64 KiB fetch or the uniqueBody cap, or the BODY_KEEP_FACTOR * max_chars keep, cut the text
    omitted: bool  # not read

    def __post_init__(self) -> None:
        if self.omitted and (self.text or self.quote_removal != "none"):
            raise ValueError("an omitted body has no text and no quote removal")
        if (self.original_chars is None) != (self.omitted or self.quote_removal == "server"):
            raise ValueError("original_chars is None exactly for an omitted body or a server cut")
        if self.original_chars is not None and self.original_chars < 0:
            raise ValueError("original_chars is negative")


OMITTED_BODY: Final = ThreadBody("", "none", "none", None, False, True)


@dataclass(frozen=True, slots=True)
class ThreadMember:
    summary: MailSummary  # snippet_text ""; meta carries folder, flags, direction, ids and its own thread key
    linked_by: LinkedBy
    body: ThreadBody


@dataclass(frozen=True, slots=True)
class ThreadOverview:
    """`summary` of a thread over every member found, before the 50-message cap (spec 080 §5.2)."""

    message_count: int
    message_count_exact: bool
    last_direction: Direction
    last_incoming_at: datetime | None
    last_outgoing_at: datetime | None
    participants: tuple[str, ...]  # at most 20, newest member first, own addresses removed, not yet sanitised

    def __post_init__(self) -> None:
        if self.message_count < 1:
            raise ValueError("message_count is at least 1")
        if len(self.participants) > MAX_PARTICIPANTS:
            raise ValueError("too many participants")


@dataclass(frozen=True, slots=True)
class ThreadPage:
    """`source_mismatch`: the key's source does not fit the account (the tool answers invalid_argument).

    `truncated`: members or bodies are missing (the 50-message cap, a soft stop, a body not read)."""

    outcome: Literal["ok", "source_mismatch"]
    thread: ThreadKey | None = None  # None for a seed without a derivable key (SG4)
    members: tuple[ThreadMember, ...] = ()  # oldest first by (received_at, k)
    overview: ThreadOverview | None = None
    truncated: bool = False

    def __post_init__(self) -> None:
        if (self.overview is not None) != (self.outcome == "ok"):
            raise ValueError("overview is set exactly when outcome is ok")
        if self.overview is None:
            if self.members:
                raise ValueError("members need outcome ok")
            return
        if not 1 <= len(self.members) <= MAX_THREAD_MESSAGES:
            raise ValueError("an ok page holds 1 to 50 members")
        if self.overview.message_count < len(self.members):
            raise ValueError("message_count is below the number of members")
        if self.overview.message_count > len(self.members) and not self.truncated:
            raise ValueError("message_count above the members needs truncated")  # the cap (P11)
        keys = [order_key(member.summary) for member in self.members]
        if keys != sorted(keys):
            raise ValueError("members are not oldest first")


def read_credential(secrets_dir: Path, ref: str) -> str:
    """Read at connection time, so a rotated Secret file is picked up without a restart (spec 080 §7.1)."""
    try:
        value = (secrets_dir / ref).read_text(encoding="utf-8").rstrip("\r\n")
    except (OSError, UnicodeDecodeError) as exc:
        raise ProviderError("upstream_error", type(exc).__name__) from None
    if not value:
        raise ProviderError("upstream_error", "EmptyCredential")
    return value


@dataclass(frozen=True, slots=True)
class AccountSlot:
    """Connection slots of one account plus the process-wide thread budget.

    Both are threading semaphores held by the worker thread itself: anyio returns its own limiter token when an
    abandon_on_cancel call is cancelled although the thread keeps running, so an anyio limiter cannot enforce
    "at most 2 connections per account" (spec 080 §6) after a timeout."""

    connections: threading.BoundedSemaphore
    threads: threading.BoundedSemaphore


class AccountLimiters:
    def __init__(
        self, per_account: int = MAX_CONNECTIONS_PER_ACCOUNT, total_threads: int = MAX_PROVIDER_THREADS
    ) -> None:
        self._per_account = per_account
        self._threads = threading.BoundedSemaphore(total_threads)
        self._connections: dict[str, threading.BoundedSemaphore] = {}
        self._lock = threading.Lock()

    def get(self, account_id: str) -> AccountSlot:
        with self._lock:
            connections = self._connections.get(account_id)
            if connections is None:
                connections = self._connections[account_id] = threading.BoundedSemaphore(self._per_account)
        return AccountSlot(connections=connections, threads=self._threads)


async def run_blocking[T](
    func: Callable[[], T],
    *,
    slot: AccountSlot,
    timeout: float | None = None,  # noqa: ASYNC109 - applied here with fail_after around the worker thread
) -> T:
    """Run a blocking provider call in a worker thread with a deadline.

    The thread budget is taken before the thread starts and the account's connection slot inside the thread; both
    are released only when the thread ends, so an abandoned (timed-out) call keeps its slot until its socket gives
    up. A call that finds no free slot within its deadline fails with upstream_timeout instead of opening a third
    connection."""
    timeout = PROVIDER_TIMEOUT_SECONDS if timeout is None else timeout  # read at call time (tests patch it)
    deadline = time.monotonic() + timeout
    if not slot.threads.acquire(blocking=False):
        raise ProviderError("upstream_timeout", "ThreadLimit")
    # Hand-over: exactly one side releases the thread token. If the caller is cancelled before the worker starts,
    # the worker never touches the semaphores and the caller releases the token itself.
    handover = threading.Lock()
    state = {"started": False, "abandoned": False}

    def guarded() -> T:
        with handover:
            if state["abandoned"]:
                raise ProviderError("upstream_timeout", "Abandoned")
            state["started"] = True
        try:
            # Wait for a connection slot only as long as the caller still waits.
            if not slot.connections.acquire(timeout=max(deadline - time.monotonic() - 1.0, 0.0)):
                raise ProviderError("upstream_timeout", "ConnectionLimit")
            try:
                return func()
            finally:
                slot.connections.release()
        finally:
            slot.threads.release()

    try:
        with anyio.fail_after(timeout):
            # Own limiter per call: the real bounds are the two semaphores above.
            return await anyio.to_thread.run_sync(guarded, limiter=anyio.CapacityLimiter(1), abandon_on_cancel=True)
    except TimeoutError:
        raise ProviderError("upstream_timeout", "TimeoutError") from None
    finally:
        with handover:
            if not state["started"]:
                state["abandoned"] = True
                slot.threads.release()


def read_capped(
    chunks: Iterable[bytes],
    limit: int = MAX_HTTP_RESPONSE_BYTES,
    *,
    deadline: float | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> bytes:
    """Read a streamed body; abort as soon as it exceeds `limit` (spec 080 §5.4) or, with a deadline, as soon as a
    chunk arrives after it (one deadline per provider call, rev. 4.6 S4)."""
    buffer = bytearray()
    for chunk in chunks:
        if deadline is not None and clock() > deadline:
            raise ProviderError("upstream_timeout", "CallDeadline")
        buffer += chunk
        if len(buffer) > limit:
            raise ProviderError("too_large", "ResponseTooLarge")
    return bytes(buffer)


def load_json_object(raw: bytes) -> dict[str, object]:
    """Third-party JSON: any parse problem, deep nesting or a non-object is upstream_error (never a crash)."""
    try:
        value = json.loads(raw)
    except (ValueError, RecursionError):
        raise ProviderError("upstream_error", "MalformedJson") from None
    if not isinstance(value, dict):
        raise ProviderError("upstream_error", "MalformedJson")
    return value


def item_hash(opaque_id: str) -> str:
    """12 hex characters of SHA-256 over an opaque id: correlates log lines without exposing the id (§9.7)."""
    return hashlib.sha256(opaque_id.encode()).hexdigest()[:12]


def order_key_of(received_at: datetime, ref: AnyMessageRef) -> tuple[int, str]:
    """Total list order (received_at desc, k desc): epoch seconds and the 16-hex location key."""
    return int(received_at.timestamp()), location_k(ref)


def order_key(item: MailSummary) -> tuple[int, str]:
    return order_key_of(item.received_at, item.ref)


def direction_of(*, in_sent: bool, from_address: str | None, own: frozenset[str]) -> Direction:
    return "outgoing" if in_sent or is_own(from_address, own) else "incoming"


def log_message_skipped(ref: AnyMessageRef, capability: Capability, exc: Exception) -> None:
    """`item` is logged only when the message has a MessageId (a refused folder name or an overlong id has none)."""
    fields: dict[str, str] = {}
    encoded = location_id(ref)
    if encoded is not None:
        fields["item"] = item_hash(encoded)
    log_event(
        _log,
        logging.WARNING,
        "item_degraded",
        account=ref.account,
        capability=capability,
        exception=type(exc).__name__,
        **fields,
    )


def placeholder_summary(
    ref: AnyMessageRef, received_at: datetime, unread: bool, meta: MailMeta = NO_META
) -> MailSummary:
    """A message the hub could not decode: listed with empty third-party fields and a fixed note (spec 080 §10.2)."""
    return MailSummary(
        ref=ref,
        received_at=received_at,
        unread=unread,
        has_attachments=False,
        from_address=None,
        from_name=None,
        subject=None,
        snippet_text=UNDECODABLE_NOTE,
        meta=meta,
    )


def log_provider_failure(account: str, capability: Capability, error: ProviderError) -> None:
    log_event(
        _log,
        logging.WARNING,
        "provider_call_failed",
        account=account,
        capability=capability,
        outcome=error.code,
        exception=error.cause,
    )
