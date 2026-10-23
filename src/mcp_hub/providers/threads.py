"""Provider-neutral steps of `get_thread` (spec 080 §5.2 `get_thread`, §5.5 participants; WP13c A-mf3, MF-P3, SG3, Q1).

The adapters find the copies of a thread; these helpers merge them into members, apply the trash filter and the
50-message cap, build the thread overview and prepare the bodies. Pure: nothing is logged.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Final

from mcp_hub.ids import location_k
from mcp_hub.providers.base import MAX_PARTICIPANTS as MAX_PARTICIPANTS
from mcp_hub.providers.base import MAX_THREAD_MESSAGES as MAX_THREAD_MESSAGES
from mcp_hub.providers.base import (
    OMITTED_BODY,
    BodySource,
    Direction,
    LinkedBy,
    MailSummary,
    QuoteRemoval,
    ThreadBody,
    ThreadOverview,
    order_key,
)
from mcp_hub.providers.folders import MailFolder
from mcp_hub.providers.headers import is_own, valid_message_id
from mcp_hub.providers.quotes import remove_quotes

BODY_KEEP_FACTOR: Final = 4  # the adapter keeps at most this many times max_chars of a body; the tool cuts to max_chars
FOLDER_RANK: Final[dict[MailFolder, int]] = {"inbox": 0, "archive": 1, "sent": 2, "trash": 3, "other": 4}  # S-e
_SPACES: Final = str.maketrans({"\u00a0": " ", "\u202f": " "})


@dataclass(frozen=True, slots=True)
class ThreadCopy:
    """One copy of a thread message as an adapter found it; `participants` from `participants_of`."""

    summary: MailSummary
    linked_by: LinkedBy
    participants: tuple[str, ...]
    in_sent: bool  # this copy is in the sent folder (Gmail: label \\Sent)
    seed: bool = False
    body: ThreadBody = OMITTED_BODY


def participants_of(
    direction: Direction, from_address: str | None, to: Sequence[str], cc: Sequence[str], own: frozenset[str]
) -> tuple[str, ...]:
    """Incoming: From and Cc; outgoing: To and Cc (§5.5), own removed, in header order. Deduplicated by casefold, the
    first spelling kept; so compare casefolded values (the shared-participant check of the subject round, Q3)."""
    addresses = (from_address, *cc) if direction == "incoming" else (*to, *cc)
    seen: dict[str, str] = {}
    for address in addresses:
        if address and not is_own(address, own):
            seen.setdefault(address.casefold(), address)
    return tuple(seen.values())


def _rank(item: ThreadCopy) -> tuple[datetime, int, str]:
    """S-e: the earliest received_at, then the folder rank; the location key only makes the choice total."""
    return item.summary.received_at, FOLDER_RANK[item.summary.meta.folder], location_k(item.summary.ref)


def _merged(group: Sequence[ThreadCopy]) -> ThreadCopy:
    winner = min(group, key=_rank)
    seed = next((item for item in group if item.seed), None)
    sent = next((item for item in group if item.in_sent), None)
    summary, participants = winner.summary, winner.participants
    if sent is not None:  # outgoing if any copy is in sent (MF-R2); its participants are To + Cc
        summary = replace(summary, meta=replace(summary.meta, direction="outgoing"))
        participants = sent.participants
    return replace(
        winner,
        summary=summary,
        participants=participants,
        in_sent=sent is not None,
        seed=seed is not None,
        linked_by=winner.linked_by if seed is None else seed.linked_by,  # MF-P3
    )


def _in_trash(item: ThreadCopy) -> bool:
    return item.summary.meta.folder == "trash"


def merge_copies(copies: Iterable[ThreadCopy], *, include_trash: bool) -> list[ThreadCopy]:
    """One entry per valid Message-ID, in first-seen order; copies without one are never merged (A-mf3).

    Without `include_trash`, the trash copies of every message but the seed are dropped first, so a later copy
    outside trash stays a member; a group holding the seed keeps all its copies (SG3, G-R3). The merged entry is the
    S-e copy; it is the seed, with the seed's `linked_by`, when any copy is the seed (MF-P3), and `in_sent` when any
    copy is."""
    entries: list[str | ThreadCopy] = []
    groups: dict[str, list[ThreadCopy]] = {}
    for item in copies:
        message_id = valid_message_id(item.summary.meta.message_id)
        if message_id is None:
            if include_trash or item.seed or not _in_trash(item):
                entries.append(item)
            continue
        group = groups.get(message_id)
        if group is None:
            group = groups[message_id] = []
            entries.append(message_id)
        group.append(item)
    merged: list[ThreadCopy] = []
    for entry in entries:
        if not isinstance(entry, str):
            merged.append(entry)
            continue
        group = groups[entry]
        if not include_trash and not any(item.seed for item in group):
            group = [item for item in group if not _in_trash(item)]
        if group:
            merged.append(_merged(group))
    return merged


def choose_members(
    merged: Sequence[ThreadCopy], *, include_trash: bool
) -> tuple[list[ThreadCopy], list[ThreadCopy], bool]:
    """(counted, kept, capped): every member after the trash filter, the newest 50 of them, and whether the cap cut.

    Both lists are oldest first by (received_at, k). The seed is exempt from the trash filter only; the cap applies
    to it like any member (SG3, Q1, P6). `merge_copies` already dropped the trash copies; the filter here only
    guards input that did not pass through it."""
    counted = sorted(
        (item for item in merged if include_trash or item.seed or not _in_trash(item)),
        key=lambda item: order_key(item.summary),
    )
    return counted, counted[-MAX_THREAD_MESSAGES:], len(counted) > MAX_THREAD_MESSAGES


def overview(counted: Sequence[ThreadCopy], *, exact: bool) -> ThreadOverview:
    """`summary` over every member found, including those beyond the cap (§5.2)."""
    newest_first = sorted(counted, key=lambda item: order_key(item.summary), reverse=True)
    if not newest_first:
        raise ValueError("message_count is at least 1")
    incoming = next((item for item in newest_first if item.summary.meta.direction == "incoming"), None)
    outgoing = next((item for item in newest_first if item.summary.meta.direction == "outgoing"), None)
    participants: dict[str, str] = {}
    for address in (address for item in newest_first for address in item.participants):
        if len(participants) == MAX_PARTICIPANTS:
            break
        participants.setdefault(address.casefold(), address)
    return ThreadOverview(
        message_count=len(newest_first),
        message_count_exact=exact,
        last_direction=newest_first[0].summary.meta.direction,
        last_incoming_at=None if incoming is None else incoming.summary.received_at,
        last_outgoing_at=None if outgoing is None else outgoing.summary.received_at,
        participants=tuple(participants.values()),
    )


def _normalised(text: str) -> str:
    return text.replace("\r\n", "\n").translate(_SPACES)


def _kept(
    text: str, source: BodySource, mode: QuoteRemoval, original: int | None, *, max_chars: int, fetch_cut: bool
) -> ThreadBody:
    keep = BODY_KEEP_FACTOR * max_chars
    return ThreadBody(text[:keep], source, mode, original, fetch_cut or len(text) > keep, False)


def prepare_body(text: str, source: BodySource, *, subject: str | None, max_chars: int, fetch_cut: bool) -> ThreadBody:
    """A decoded (and HTML-converted) body: CRLF → LF and NBSP/U+202F → space, counted, quotes removed, kept.

    `subject` is the raw subject (a forward keeps its header blocks); `fetch_cut` when the 64 KiB fetch cut it."""
    normalised = _normalised(text)
    removed, mode = remove_quotes(normalised, subject=subject)
    return _kept(removed, source, mode, len(normalised), max_chars=max_chars, fetch_cut=fetch_cut)


def server_body(text: str, source: BodySource, *, max_chars: int, fetch_cut: bool) -> ThreadBody:
    """A body whose history the server already removed (Graph `uniqueBody`): not counted, not scanned for quotes."""
    return _kept(_normalised(text), source, "server", None, max_chars=max_chars, fetch_cut=fetch_cut)
