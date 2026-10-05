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

from mcp_hub.errors import ErrorCode
from mcp_hub.ids import MessageRef, encode_message_id
from mcp_hub.logging import log_event
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


@dataclass(frozen=True, slots=True)
class MailSummary:
    """Decoded but not yet sanitised; the tool layer sanitises every string (spec 080 §5.3)."""

    ref: MessageRef
    received_at: datetime  # timezone-aware INTERNALDATE
    unread: bool
    has_attachments: bool
    from_address: str | None
    from_name: str | None
    subject: str | None
    snippet_text: str


@dataclass(frozen=True, slots=True)
class UnreadPage:
    items: list[MailSummary]
    more: bool  # more unread messages exist than returned


@dataclass(frozen=True, slots=True)
class AttachmentMeta:
    size_bytes: int
    filename: str | None
    content_type: str | None


@dataclass(frozen=True, slots=True)
class MailDetail:
    ref: MessageRef
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


def log_message_skipped(ref: MessageRef, capability: Capability, exc: Exception) -> None:
    log_event(
        _log,
        logging.WARNING,
        "item_degraded",
        account=ref.account,
        capability=capability,
        item=item_hash(encode_message_id(ref)),
        exception=type(exc).__name__,
    )


def placeholder_summary(ref: MessageRef, received_at: datetime, unread: bool) -> MailSummary:
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
