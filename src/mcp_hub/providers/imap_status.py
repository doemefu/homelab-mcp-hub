"""IMAP `get_message_status` (spec 080 §5.2 "Location, IMAP", §5.4, §6.5; WP13b): the in-place check of `m` ids, the
location step and the mapping to `MessageStatus`, inside one `ImapSession` (one connection, one deadline, D76).

Read-only through the guard: EXAMINE, UID SEARCH and UID FETCH of FLAGS, INTERNALDATE, the Message-ID (and FROM)
header item and, on Gmail, X-GM-LABELS and X-GM-THRID. Nothing here is logged but `item_degraded`, through the
session and capped per call (SR6)."""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final

from mcp_hub.ids import GraphMessageRef, MessageRef, StableRef
from mcp_hub.providers.base import (
    FoundMessage,
    MailFlags,
    MailMeta,
    MessageStatus,
    ProviderError,
    StatusReason,
    StatusTarget,
    direction_of,
    status_unknown,
)
from mcp_hub.providers.deadline import SoftStop
from mcp_hub.providers.folders import MAX_LOCATE_FOLDERS, FolderEntry
from mcp_hub.providers.headers import CANDIDATE_HEADER_BYTES, MAX_ADDRESS_HEADER_BYTES, header_bytes
from mcp_hub.providers.imap_session import (
    FoundCopy,
    ImapSession,
    LocateResult,
    MissingInternalDate,
    _aware,
    _bound_entry,
    _candidate_headers,
    _detail_folder,
    _examine_bound,
    _field_complete,
    _flags,
    _in_sent,
    _is_draft,
    _labels,
    _replaces,
    _thread_id,
    locate_items,
)

# Gates from the T0 probe (Option 17), used by the reply and thread lookups (T9, T10).
# T0 P1 2026-10-22: References 0/3; In-Reply-To 3/3 but ~14 s per All Mail search
GMAIL_REPLY_HEADER_SEARCH: Final = False
OTHER_REPLY_IN_REPLY_TO: Final = True  # T0 P1: iCloud matches In-Reply-To; False → `replied` from the flag only
MAX_LOOKUP_HITS: Final = 500  # hits re-checked per reply/thread SEARCH batch and folder (C9)
LOOKUP_FETCH_UIDS: Final = 100  # UIDs per re-check FETCH: at most 6.4 MiB of 64 KiB header items per answer (C9)
MAX_REPLY_KEYS_PER_REF: Final = 5  # owner reply Message-IDs added to a ref's thread keys (E-mf1)
MAX_DEGRADED_LOGS: Final = 20  # item_degraded lines per provider call (SR6)
SOFT_CHECK_ROWS: Final = 10  # fetched rows parsed between two soft checks (SR2: parsing sends no command)
_SEEN: Final = b"\\seen"
_ANSWERED: Final = b"\\answered"
_FLAGGED: Final = b"\\flagged"
# The in-place read failures that are the entry's own answer, by (code, cause) (P6; E-im1, C4). Every other
# ProviderError fails the account, the hard deadline (`upstream_timeout`, `CallDeadline`) included.
_IN_PLACE_FAILURES: Final[Mapping[tuple[str, str | None], StatusReason]] = {
    ("not_found", "UnboundFolder"): "stale_id",  # refused (D75, B3) or unknown after the one rebuild: never opened
    ("upstream_timeout", "ExamineRefused"): "incomplete_search",
    ("upstream_timeout", "FetchRefused"): "incomplete_search",
}


@dataclass(frozen=True, slots=True)
class _InPlace:
    """What the in-place check of the `m` ids found, by target position."""

    decided: dict[int, MessageStatus] = field(default_factory=dict)  # stale_id, incomplete_search, id_mismatch
    copies: dict[int, tuple[FoundCopy, str | None]] = field(default_factory=dict)  # copy read, its valid Message-ID
    opened: list[str] = field(default_factory=list)  # distinct mailboxes opened, against the folder cap (SR5)


def soft_stopped(targets: Sequence[StatusTarget]) -> list[MessageStatus]:
    """A soft stop before anything was read (at LOGIN or the CAPABILITY after it): every entry incomplete_search."""
    return [status_unknown("incomplete_search") for _ in targets]


def message_status(
    session: ImapSession, targets: Sequence[StatusTarget], *, soft_check: Callable[[], None]
) -> list[MessageStatus]:
    """One status per target, in order (§5.2). `soft_check` raises SoftStop after the soft limit (P2).

    1. `m` ids are checked in place, grouped by mailbox in input order (D75 binding, UIDVALIDITY, UID, then the
       Message-ID header; E-im4, C4). A copy with a valid Message-ID seeds step 2; one without is final (A-td4).
    2. The location step for the refs and the seeded Message-IDs, each Message-ID once (`ImapSession.locate`).
    3. The mapping: found (also when uncertain, C3), gone after a complete search, else incomplete_search.

    A soft stop in step 1 keeps the copies read (found) and leaves every other entry incomplete_search, without a
    location (IR2, P7); one in step 2 is handled by `locate`. A BAD, an abort, a lost connection or the hard
    deadline propagate: the account fails (B7)."""
    session.cap_degraded_logs(MAX_DEGRADED_LOGS)
    in_place = _InPlace()
    try:
        _check_in_place(session, targets, in_place, soft_check)
    except SoftStop:
        return [_after_soft_stop(session, index, in_place) for index in range(len(targets))]
    result = _locate(session, targets, in_place, soft_check)
    return [_answer(session, index, target, in_place, result) for index, target in enumerate(targets)]


# --- step 1: `m` ids in place ------------------------------------------------------------------------------------


def _check_in_place(
    session: ImapSession, targets: Sequence[StatusTarget], state: _InPlace, soft_check: Callable[[], None]
) -> None:
    groups: dict[str, list[int]] = {}
    for index, target in enumerate(targets):
        if isinstance(target, GraphMessageRef):
            state.decided[index] = status_unknown("id_mismatch")  # defensive: the tool answers these itself
        elif isinstance(target, MessageRef):
            groups.setdefault(target.folder, []).append(index)
    for raw_name, indexes in groups.items():
        try:
            _check_mailbox(session, raw_name, indexes, targets, state, soft_check)
        except ProviderError as exc:
            reason = _IN_PLACE_FAILURES.get((exc.code, exc.cause))
            if reason is None:
                raise
            for index in indexes:
                if index not in state.decided and index not in state.copies:
                    state.decided[index] = status_unknown(reason)


def _check_mailbox(
    session: ImapSession,
    raw_name: str,
    indexes: list[int],
    targets: Sequence[StatusTarget],
    state: _InPlace,
    soft_check: Callable[[], None],
) -> None:
    """The ids of one mailbox: one EXAMINE and, for the ids of the current UIDVALIDITY, one FETCH (T7-4)."""
    entry = _bound_entry(session, raw_name)  # before any EXAMINE; at most one rebuild per call (Sec16, TS1)
    if entry.raw_name not in state.opened:
        if len(state.opened) >= MAX_LOCATE_FOLDERS:  # SR5
            state.decided.update((index, status_unknown("incomplete_search")) for index in indexes)
            return
        state.opened.append(entry.raw_name)
    validity = _examine_bound(session, entry)  # a missing UIDVALIDITY on the registry inbox fails the account
    if validity is None:  # IR5, P8
        state.decided.update((index, status_unknown("incomplete_search")) for index in indexes)
        return
    current: list[tuple[int, MessageRef]] = []
    for index in indexes:
        target = targets[index]
        if not isinstance(target, MessageRef):
            continue
        if target.uidvalidity != validity:
            state.decided[index] = status_unknown("stale_id")  # no FETCH
        else:
            current.append((index, target))
    if not current:
        return
    rows = session.fetch(
        [target.uid for _, target in current],
        locate_items(gmail=session.gmail, with_from=bool(session.own)),
        on_no="fail",
    )
    cap = MAX_ADDRESS_HEADER_BYTES if session.own else CANDIDATE_HEADER_BYTES
    for count, (index, target) in enumerate(current):
        if count and count % SOFT_CHECK_ROWS == 0:
            soft_check()
        read = _read_in_place(session, entry, target, rows.get(target.uid), cap)
        if isinstance(read, tuple):
            state.copies[index] = read
        else:
            state.decided[index] = status_unknown(read)


def _read_in_place(
    session: ImapSession, entry: FolderEntry, target: MessageRef, row: dict[bytes, object] | None, cap: int
) -> tuple[FoundCopy, str | None] | StatusReason:
    """The copy an id names, or why it has none: missing, a draft or without INTERNALDATE → stale_id; the header
    item missing or cut before its Message-ID ended → incomplete_search (C4, A-sec3, T7-1)."""
    if row is None or _is_draft(row):
        return "stale_id"
    received = _aware(row.get(b"INTERNALDATE"))
    if received is None:
        session.log_skipped(target, MissingInternalDate())
        return "stale_id"
    block = header_bytes(row)
    if not block:
        return "incomplete_search"
    message_id, sender = _candidate_headers(row)
    if len(block) >= cap:
        if message_id is None or not _field_complete(block, b"message-id"):
            return "incomplete_search"
        if not _field_complete(block, b"from"):
            sender = None  # a cut From may be a prefix of the real address: the folder rule decides
    copy = FoundCopy(
        entry,
        target.uidvalidity,
        target.uid,
        received,
        _detail_folder(entry, row, gmail=session.gmail),
        flags=_flags(row),
        labels=_labels(row),
        from_address=sender,
        thread_id=_thread_id(row),
    )
    return copy, message_id


def _after_soft_stop(session: ImapSession, index: int, state: _InPlace) -> MessageStatus:
    """IR2, P7: a copy read in place is found (its own copy only; no location ran); everything else unchecked."""
    if index in state.decided:
        return state.decided[index]
    if index in state.copies:
        copy, message_id = state.copies[index]
        return _found(session, copy, message_id, in_sent=False)
    return status_unknown("incomplete_search")


# --- step 2: location --------------------------------------------------------------------------------------------


def _locate(
    session: ImapSession, targets: Sequence[StatusTarget], state: _InPlace, soft_check: Callable[[], None]
) -> LocateResult:
    """`locate` over the refs and the seeded Message-IDs, each once, within the folders the in-place check left.

    An in-place mailbox counts once more only when the location would not open it anyway (a core folder): the cap
    counts distinct mailboxes (SR5). An in-place `other` mailbox that the location also reaches counts twice here,
    which can only leave a ref incomplete_search, never gone."""
    wanted: list[str] = []
    copies: list[tuple[str, FoundCopy]] = []
    for index, target in enumerate(targets):
        if isinstance(target, StableRef):
            wanted.append(target.message_id)
        elif index in state.copies:
            copy, message_id = state.copies[index]
            if message_id is not None:
                wanted.append(message_id)
                copies.append((message_id, copy))
    if not wanted:
        return LocateResult({}, True, frozenset(), frozenset(), frozenset())
    try:
        core, _ = session.folder_map().locate_order()
    except SoftStop:
        return _unsearched(wanted, dict(copies))
    rank = {entry.raw_name: position for position, entry in enumerate(core)}
    seeds: dict[str, FoundCopy] = {}
    for message_id, copy in copies:  # two ids of one Message-ID: the earlier copy seeds (§5.5 matching rule)
        if _replaces(copy, seeds.get(message_id), rank):
            seeds[message_id] = copy
    budget = MAX_LOCATE_FOLDERS - len(set(state.opened) - rank.keys())
    if budget < 1:
        return _unsearched(wanted, seeds)
    return session.locate(wanted, seeds=seeds, soft_check=soft_check, max_folders=budget)


def _unsearched(wanted: Sequence[str], seeds: Mapping[str, FoundCopy]) -> LocateResult:
    """No folder searched (a soft stop at the LIST, or no folder left under the cap): the seeds stand, uncertain."""
    missed = set(wanted)
    return LocateResult(
        found=dict(seeds),
        complete=False,
        incomplete=frozenset(missed - seeds.keys()),
        uncertain=frozenset(missed & seeds.keys()),
        in_sent=frozenset(message_id for message_id, copy in seeds.items() if _in_sent(copy)),
    )


# --- step 3: mapping ---------------------------------------------------------------------------------------------


def _answer(
    session: ImapSession, index: int, target: StatusTarget, state: _InPlace, result: LocateResult
) -> MessageStatus:
    if index in state.decided:
        return state.decided[index]
    if isinstance(target, StableRef):
        message_id = target.message_id
    elif index in state.copies:
        copy, seeded = state.copies[index]
        if seeded is None:  # no valid Message-ID: the copy read in place is final (A-td4)
            return _found(session, copy, None, in_sent=False)
        message_id = seeded
    else:
        return status_unknown("incomplete_search")  # defensive: every `m` id was decided or read in step 1
    found = result.found.get(message_id)
    if found is not None:  # also when uncertain: the copy reported may not be the earliest (C3)
        return _found(session, found, message_id, in_sent=message_id in result.in_sent)
    if message_id in result.incomplete:
        return status_unknown("incomplete_search")
    return MessageStatus("gone")


def _found(session: ImapSession, copy: FoundCopy, message_id: str | None, *, in_sent: bool) -> MessageStatus:
    """The copy's location, time, folder and flags; `direction` outgoing when any found copy is in sent (MF-R2).
    `thread_checked` stays false until the reply and thread lookups (T9, T10) have run."""
    sent = in_sent or _in_sent(copy)
    meta = MailMeta(
        folder=copy.folder,
        flags=MailFlags(answered=_ANSWERED in copy.flags, flagged=_FLAGGED in copy.flags),
        direction=direction_of(in_sent=sent, from_address=copy.from_address, own=session.own),
    )
    found = FoundMessage(
        location=MessageRef(session.account, copy.entry.raw_name, copy.uidvalidity, copy.uid),
        message_id=message_id,
        received_at=copy.received_at,
        unread=_SEEN not in copy.flags,
        meta=meta,
    )
    return MessageStatus("found", found=found, thread_checked=False)
