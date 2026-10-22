"""Pure helpers of `get_message_status` for Graph: `$filter` batching and thread evaluation (spec 080 §5.4)."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final

import httpx2

from mcp_hub.ids import GRAPH_ID, MESSAGE_ID_PATTERN
from mcp_hub.providers.base import direction_of
from mcp_hub.providers.folders import MailFolder

STATUS_LOOKUP_TOP: Final = 50  # internetMessageId lookups (§5.4)
THREAD_LOOKUP_TOP: Final = 100  # conversationId lookups
MAX_FILTER_VALUES: Final = 10
MAX_FILTER_CHARS: Final = 1800  # encoded $filter per request
THREAD_SELECT: Final = "id,conversationId,parentFolderId,receivedDateTime,sentDateTime,from,internetMessageId,isDraft"


def message_id_filter(values: Sequence[str]) -> str:
    """`internetMessageId eq '<a>' or …`; the pattern keeps `'` out of the string literal."""
    if not all(MESSAGE_ID_PATTERN.fullmatch(v) for v in values):
        raise ValueError("message id refused")
    return " or ".join(f"internetMessageId eq '<{v}>'" for v in values)


def conversation_filter(values: Sequence[str]) -> str:
    if not all(GRAPH_ID.fullmatch(v) for v in values):
        raise ValueError("conversation id refused")
    return " or ".join(f"conversationId eq '{v}'" for v in values)


def encoded_filter_length(text: str) -> int:
    """Length of the `$filter` value as httpx2 puts it on the wire."""
    encoded = str(httpx2.QueryParams({"$filter": text}))
    return len(encoded.split("=", 1)[1])


def filter_batches(
    values: Sequence[str],
    render: Callable[[Sequence[str]], str],
    *,
    max_values: int = MAX_FILTER_VALUES,
    max_chars: int = MAX_FILTER_CHARS,
) -> list[list[str]]:
    """Unique values in input order; a batch closes before a value would break a cap, one value always fits alone.

    Every value is rendered alone first, so `render` refuses an invalid one (`ValueError`) before any batch is
    built; callers validate per ref beforehand to mark only that ref."""
    unique = list(dict.fromkeys(values))
    for value in unique:
        render([value])
    batches: list[list[str]] = []
    current: list[str] = []
    for value in unique:
        if current and (len(current) >= max_values or encoded_filter_length(render([*current, value])) > max_chars):
            batches.append(current)
            current = []
        current.append(value)
    if current:
        batches.append(current)
    return batches


@dataclass(frozen=True, slots=True)
class ThreadEntry:
    """One parsed conversation item (valid id, not a draft, allowed parent folder); built by graph.py."""

    graph_id: str
    conversation_id: str
    folder: MailFolder
    received_at: datetime | None
    sent_at: datetime | None
    message_id: str | None
    from_address: str | None


@dataclass(frozen=True, slots=True)
class ThreadFacts:
    reply_at: datetime | None
    reply_message_id: str | None
    newer_incoming_at: datetime | None
    complete: bool  # false when an entry with an unparsable time was skipped (a reply may be hidden)


def thread_facts(
    copy_id: str,
    message_id: str | None,
    received_at: datetime,
    conversation_id: str,
    entries: Sequence[ThreadEntry],
    own: frozenset[str],
) -> ThreadFacts:
    """Earliest own reply and latest newer incoming item of one conversation, by strict instant comparison.

    The message and its copies never count; an inbox copy of an own sent item is not newer incoming."""
    thread = [
        e
        for e in entries
        if e.conversation_id == conversation_id
        and e.graph_id != copy_id
        and not (message_id is not None and e.message_id == message_id)
    ]
    sent_ids = {e.message_id for e in thread if e.folder == "sent" and e.message_id is not None}
    reply: ThreadEntry | None = None
    reply_at: datetime | None = None
    newer_at: datetime | None = None
    complete = True
    for e in thread:
        in_sent = e.folder == "sent"
        when = e.sent_at if in_sent else e.received_at
        if when is None:
            complete = False
            continue
        if in_sent:
            if when > received_at and (reply_at is None or when < reply_at):
                reply, reply_at = e, when
        elif (
            direction_of(in_sent=False, from_address=e.from_address, own=own) == "incoming"
            and e.message_id not in sent_ids
            and when > received_at
            and (newer_at is None or when > newer_at)
        ):
            newer_at = when
    return ThreadFacts(reply_at, reply.message_id if reply else None, newer_at, complete)
