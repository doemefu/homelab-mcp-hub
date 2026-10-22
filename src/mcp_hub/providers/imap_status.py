"""IMAP `get_message_status` (spec 080 §5.2 "Location, IMAP", "Replies and thread, IMAP", §5.4, §6.5; WP13b): the
in-place check of `m` ids, the location step, the mapping to `MessageStatus` and the reply and thread lookups (by
descendant headers, on Gmail by `X-GM-THRID`), inside one `ImapSession` (one connection, one deadline, D76).

Read-only through the guard: EXAMINE, UID SEARCH and UID FETCH of FLAGS, INTERNALDATE, the Message-ID (and FROM)
header item, the 64 KiB re-check item of the lookups and, on Gmail, X-GM-LABELS and X-GM-THRID. Nothing here is
logged but `item_degraded`, through the session and capped per call (SR6).

The Gmail reply search by In-Reply-To/References in All Mail is gated off by GMAIL_REPLY_HEADER_SEARCH (T0 P1: about
14 s per search). It is kept on purpose, tested, as the path to switch on if Gmail answers it faster."""

import imaplib
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from functools import partial
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
from mcp_hub.providers.folders import MAX_LOCATE_FOLDERS, FolderEntry, FolderMap, FolderRole
from mcp_hub.providers.headers import (
    CANDIDATE_HEADER_BYTES,
    DESCENDANT_HEADER_BYTES,
    DESCENDANT_HEADER_ITEM,
    MAX_ADDRESS_HEADER_BYTES,
    header_bytes,
    parse_descendant,
)
from mcp_hub.providers.imap_guard import PASS_THROUGH, examine_name_ok
from mcp_hub.providers.imap_search import (
    GMAIL_SENT_LABEL_KEY,
    LOOKUP_BATCH_VALUES,
    HeaderField,
    criteria,
    header_or,
    or_batches,
    search_value_ok,
    thrid_batches,
    thrid_or,
)
from mcp_hub.providers.imap_session import (
    _LABEL_SENT,
    FoundCopy,
    ImapSession,
    LocateResult,
    MissingInternalDate,
    _answered_no,
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
# T0 P1: iCloud not measurable (no aged sample); default kept. False → `replied` from the flag only
OTHER_REPLY_IN_REPLY_TO: Final = True
MAX_LOOKUP_HITS: Final = 500  # hits re-checked per reply/thread SEARCH batch and folder (C9)
LOOKUP_FETCH_UIDS: Final = 50  # UIDs per re-check FETCH: at most 3.2 MiB of 64 KiB header items per answer (C9)
MAX_REPLY_KEYS_PER_REF: Final = 5  # owner reply Message-IDs added to a ref's thread keys (E-mf1)
MAX_DEGRADED_LOGS: Final = 20  # item_degraded lines per provider call (SR6)
SOFT_CHECK_ROWS: Final = 10  # fetched rows parsed between two soft checks (SR2: parsing sends no command)
_LOOKUP_FIELDS: Final[tuple[HeaderField, ...]] = ("In-Reply-To", "References")
_LOOKUP_ITEMS: Final = ("FLAGS", "INTERNALDATE", DESCENDANT_HEADER_ITEM)
_THREAD_ROLES: Final[tuple[FolderRole, ...]] = ("inbox", "archive", "trash")
_GMAIL_REPLY_ITEMS: Final = ("FLAGS", "INTERNALDATE", "X-GM-LABELS", DESCENDANT_HEADER_ITEM)
_GMAIL_MEMBER_ITEMS: Final = ("FLAGS", "INTERNALDATE", "X-GM-LABELS", "X-GM-THRID", DESCENDANT_HEADER_ITEM)
_GMAIL_NO_ALL_ROLES: Final[tuple[FolderRole, ...]] = ("inbox", "trash", "sent")  # Gmail without `\All` (E-im3a)
_TOKEN_ENDS: Final = (b" ", b"\t", b"\r", b"\n", b",", b">")  # where a msg-id token can end
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
    4. The reply and thread lookups of the found copies: by descendant headers (`_lookups`), on Gmail by thread id
       (`_gmail_lookups`).

    A soft stop in step 1 keeps the copies read (found) and leaves every other entry incomplete_search, without a
    location (IR2, P7); one in step 2 is handled by `locate`, one in step 4 by the lookups. A BAD, an abort, a lost
    connection or the hard deadline propagate: the account fails (B7)."""
    session.cap_degraded_logs(MAX_DEGRADED_LOGS)
    in_place = _InPlace()
    try:
        _check_in_place(session, targets, in_place, soft_check)
    except SoftStop:
        return [_after_soft_stop(session, index, in_place) for index in range(len(targets))]
    result = _locate(session, targets, in_place, soft_check)
    answers = [_answer(session, index, target, in_place, result) for index, target in enumerate(targets)]
    if session.gmail:
        return _gmail_lookups(session, answers, soft_check)
    return _lookups(session, [status for status, _ in answers], soft_check)


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
        return _unsearched(wanted, _seeds(copies, {}))  # no map: no folder rank, the received time decides
    rank = {entry.raw_name: position for position, entry in enumerate(core)}
    seeds = _seeds(copies, rank)
    budget = MAX_LOCATE_FOLDERS - len(set(state.opened) - rank.keys())
    if budget < 1:
        return _unsearched(wanted, seeds)
    return session.locate(wanted, seeds=seeds, soft_check=soft_check, max_folders=budget)


def _seeds(copies: Sequence[tuple[str, FoundCopy]], rank: Mapping[str, int]) -> dict[str, FoundCopy]:
    """Two ids of one Message-ID: the earlier copy seeds (§5.5 matching rule)."""
    seeds: dict[str, FoundCopy] = {}
    for message_id, copy in copies:
        if _replaces(copy, seeds.get(message_id), rank):
            seeds[message_id] = copy
    return seeds


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
) -> tuple[MessageStatus, FoundCopy | None]:
    """The status of one target and the copy it reports, if found (the Gmail lookups need its thread id)."""
    if index in state.decided:
        return state.decided[index], None
    if isinstance(target, StableRef):
        message_id = target.message_id
    elif index in state.copies:
        copy, seeded = state.copies[index]
        if seeded is None:  # no valid Message-ID: the copy read in place is final (A-td4)
            return _found(session, copy, None, in_sent=False), copy
        message_id = seeded
    else:
        return status_unknown("incomplete_search"), None  # defensive: every `m` id was decided or read in step 1
    found = result.found.get(message_id)
    if found is not None:  # also when uncertain: the copy reported may not be the earliest (C3)
        return _found(session, found, message_id, in_sent=message_id in result.in_sent), found
    if message_id in result.incomplete:
        return status_unknown("incomplete_search"), None
    return MessageStatus("gone"), None


def _found(session: ImapSession, copy: FoundCopy, message_id: str | None, *, in_sent: bool) -> MessageStatus:
    """The copy's location, time, folder and flags; `direction` outgoing when any found copy is in sent (MF-R2).
    `thread_checked` stays false until the reply and thread lookups (step 4) have run."""
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


# --- step 4: reply and thread lookups (servers other than Gmail) -------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Hit:
    """A reply or thread hit that passed the re-check."""

    message_id: str | None  # valid bare Message-ID, else None
    from_address: str | None  # None when absent or cut
    received_at: datetime
    referenced: frozenset[str]  # the batch's ids among its whole In-Reply-To and References tokens


@dataclass(frozen=True, slots=True)
class _Lookups:
    """What the lookups learned, by participant Message-ID X."""

    unchecked: set[str] = field(default_factory=set)  # a lookup covering X did not complete or could not run
    done: set[str] = field(default_factory=set)  # every thread batch covering X was answered (or failed)
    first_reply: dict[str, tuple[datetime, str | None]] = field(default_factory=dict)  # earliest reply, its id
    replies: dict[str, dict[str, datetime]] = field(default_factory=dict)  # reply Message-ID → earliest copy
    sent_ids: set[str] = field(default_factory=set)  # Message-IDs of every re-checked sent hit (MF4)
    newer: dict[str, dict[str, datetime]] = field(default_factory=dict)  # counted Message-ID → earliest copy (MF6)
    newer_without_id: dict[str, datetime] = field(default_factory=dict)  # newest counted hit without a Message-ID


def _lookups(
    session: ImapSession, statuses: list[MessageStatus], soft_check: Callable[[], None]
) -> list[MessageStatus]:
    """§5.2 "Replies and thread, IMAP" for the found copies with a valid Message-ID, each Message-ID once (M7): the
    reply lookup in sent, where the location ended (E-im5 Design A), then the thread lookup in inbox, archive and
    trash. A copy without a valid Message-ID takes no part (E-mf3: `replied` from `\\Answered` alone).

    Without a sent mailbox (C5, E-im3b) or with `OTHER_REPLY_IN_REPLY_TO` off (Option 17, P13) there is no reply
    lookup: every ref is unchecked, and the thread lookup still runs with X as its only key. A NO marks the refs of
    its batch (or, for an EXAMINE, of the folder) unchecked; the folder map is never rebuilt here (§6.5, TS1). A
    soft stop keeps what was read and leaves every ref whose thread batches did not all finish unchecked (IR2)."""
    participants = list(
        dict.fromkeys(
            status.found.message_id
            for status in statuses
            if status.found is not None and status.found.message_id is not None
        )
    )
    if not participants:
        return statuses
    state = _Lookups()
    # A refused value (D80) is never searched, so never `done`: only its own ref is unchecked.
    searchable = [x for x in participants if search_value_ok(x)]
    try:
        folder_map = session.folder_map()  # the location's map: never rebuilt here (Sec16, TS1)
        sent = folder_map.role("sent")
        if sent is None or not OTHER_REPLY_IN_REPLY_TO:
            state.unchecked.update(searchable)  # replies cannot be searched: `replied` from the flag alone
        else:
            _reply_step(session, sent, searchable, state, soft_check)
        _thread_step(session, folder_map, searchable, state, soft_check)
    except SoftStop:
        pass  # positives read stay; refs not in `done` are unchecked
    return [_with_facts(status, state) for status in statuses]


def _reply_step(
    session: ImapSession, sent: FolderEntry, searchable: list[str], state: _Lookups, soft_check: Callable[[], None]
) -> None:
    """A sent hit is a reply to every batch id it names, except a hit whose Message-ID is that id (MF3, C8). There
    is no time condition (C8); the earliest reply by INTERNALDATE gives `reply_at` (E-mf1: its id the first key)."""
    if not _open(session, sent):
        state.unchecked.update(searchable)
        return
    for batch in or_batches(searchable, _LOOKUP_FIELDS, max_values=LOOKUP_BATCH_VALUES):
        for hit in _hits(
            session, batch, newest=False, thread=False, miss=state.unchecked.update, soft_check=soft_check
        ):
            if hit.message_id is not None:
                state.sent_ids.add(hit.message_id)
            for x in hit.referenced:
                if hit.message_id != x:
                    _add_reply(state, x, hit)
    for x in searchable:
        if len(state.replies.get(x, ())) > MAX_REPLY_KEYS_PER_REF:
            state.unchecked.add(x)  # E-mf1: a sixth reply id is no thread key


def _add_reply(state: _Lookups, x: str, hit: _Hit) -> None:
    first = state.first_reply.get(x)
    if first is None or hit.received_at < first[0]:
        state.first_reply[x] = (hit.received_at, hit.message_id)
    if hit.message_id is not None:
        copies = state.replies.setdefault(x, {})
        seen = copies.get(hit.message_id)
        if seen is None or hit.received_at < seen:
            copies[hit.message_id] = hit.received_at


def _reply_keys(state: _Lookups, x: str) -> list[str]:
    """The owner's reply Message-IDs of X, earliest first, at most MAX_REPLY_KEYS_PER_REF (E-mf1)."""
    ordered = sorted(state.replies.get(x, {}).items(), key=lambda item: (item[1], item[0]))
    keys = [message_id for message_id, _ in ordered[:MAX_REPLY_KEYS_PER_REF]]
    if not all(search_value_ok(key) for key in keys):
        state.unchecked.add(x)  # a refused reply id cannot be searched (D80)
    return [key for key in keys if search_value_ok(key)]


def _thread_step(
    session: ImapSession,
    folder_map: FolderMap,
    searchable: list[str],
    state: _Lookups,
    soft_check: Callable[[], None],
) -> None:
    """The keys X and the owner's replies to X, in inbox, archive and trash, each opened once. Every incoming
    descendant counts as newer, without an INTERNALDATE comparison with X, because a move resets it (E-mf2)."""
    key_refs: dict[str, set[str]] = {}
    for x in searchable:
        for key in (x, *_reply_keys(state, x)):
            key_refs.setdefault(key, set()).add(x)
    batches = or_batches(list(key_refs), _LOOKUP_FIELDS, max_values=LOOKUP_BATCH_VALUES)
    covered = [{x for key in batch for x in key_refs[key]} for batch in batches]
    folders = [entry for role in _THREAD_ROLES if (entry := folder_map.role(role)) is not None]
    left: dict[str, int] = {}
    for refs in covered:
        for x in refs:
            left[x] = left.get(x, 0) + len(folders)

    def miss(keys: Iterable[str]) -> None:
        state.unchecked.update(x for key in keys for x in key_refs[key])

    for entry in folders:
        opened = _open(session, entry)
        for batch, refs in zip(batches, covered, strict=True):
            if opened:
                for hit in _hits(session, batch, newest=True, thread=True, miss=miss, soft_check=soft_check):
                    _count_newer(session, state, hit, key_refs)
            else:
                state.unchecked.update(refs)
            for x in refs:
                left[x] -= 1
                if not left[x]:
                    state.done.add(x)


def _count_newer(session: ImapSession, state: _Lookups, hit: _Hit, key_refs: Mapping[str, set[str]]) -> None:
    """Never newer incoming: an outgoing message (own From, MF-R1), a copy of a sent hit, which includes the owner's
    replies (MF4, E-mf1), and a copy of X (MF3). Residual MF-R3, without an own-addresses file: own messages outside
    Sent count, including a delivered-back copy of an own reply that does not reference X itself (for example a
    second reply naming only the first): it is no sent hit of these batches, so `sent_ids` cannot exclude it."""
    if direction_of(in_sent=False, from_address=hit.from_address, own=session.own) == "outgoing":
        return
    if hit.message_id is not None and hit.message_id in state.sent_ids:
        return
    for x in {x for key in hit.referenced for x in key_refs[key]}:
        if hit.message_id == x:
            continue
        if hit.message_id is None:
            newest = state.newer_without_id.get(x)
            if newest is None or hit.received_at > newest:
                state.newer_without_id[x] = hit.received_at
            continue
        copies = state.newer.setdefault(x, {})
        seen = copies.get(hit.message_id)
        if seen is None or hit.received_at < seen:
            copies[hit.message_id] = hit.received_at


def _open(session: ImapSession, entry: FolderEntry) -> bool:
    """EXAMINE `entry` unless it is selected already (Design A). A NO is the caller's to mark, without a map rebuild
    (§6.5); a missing UIDVALIDITY is no failure here, since no id is built from it (M6)."""
    if session.selected == entry.raw_name:
        return True
    if entry.raw_name != session.registry_inbox and not examine_name_ok(entry.raw_name):
        return False  # over 1,000 characters: the guard would refuse the EXAMINE
    try:
        session.examine(entry)
    except PASS_THROUGH:
        raise
    except imaplib.IMAP4.error as exc:
        if not _answered_no(exc):
            raise
        return False
    return True


def _rows(
    session: ImapSession,
    search: bytes,
    items: Sequence[str],
    *,
    newest: bool,
    miss: Callable[[], None],
    soft_check: Callable[[], None],
) -> Iterator[dict[bytes, object]]:
    """One lookup `UID SEARCH` in the selected mailbox, then the rows of at most MAX_LOOKUP_HITS hits (the lowest
    UIDs, or the highest when `newest`; E-im2, C9), fetched in chunks of LOOKUP_FETCH_UIDS, each consumed before the
    next is fetched. `miss` marks the search's refs incomplete: a SEARCH NO (B7), the hit cap or a lost row."""
    try:
        found = session.search(search)
    except PASS_THROUGH:
        raise
    except imaplib.IMAP4.error as exc:
        if not _answered_no(exc):
            raise
        miss()
        return
    uids = sorted(set(found))
    if len(uids) > MAX_LOOKUP_HITS:
        uids = uids[-MAX_LOOKUP_HITS:] if newest else uids[:MAX_LOOKUP_HITS]
        miss()
    chunks = session.fetch_chunks(uids, items, chunk=LOOKUP_FETCH_UIDS)
    for start, rows in zip(range(0, len(uids), LOOKUP_FETCH_UIDS), chunks, strict=True):
        part = uids[start : start + LOOKUP_FETCH_UIDS]
        if len(rows.keys() & set(part)) < len(part):  # FETCH NO or expunged meanwhile: a hit is unchecked
            miss()
        for count, uid in enumerate(part):
            if count and count % SOFT_CHECK_ROWS == 0:
                soft_check()  # SR2: at most 10 blocks of 64 KiB parsed between two checks; parsing sends no command
            row = rows.get(uid)
            if row is not None:
                yield row
        if start + LOOKUP_FETCH_UIDS < len(uids):
            soft_check()  # per chunk; the guard checks again before the next FETCH


def _hits(
    session: ImapSession,
    batch: list[str],
    *,
    newest: bool,
    thread: bool,
    miss: Callable[[Iterable[str]], None],
    soft_check: Callable[[], None],
) -> Iterator[_Hit]:
    """The `_rows` of one `UID SEARCH NOT DRAFT` with OR-ed `HEADER In-Reply-To` and `HEADER References` keys for
    `batch`, each re-checked (`_recheck`). `miss` marks the batch ids whose answer is incomplete: a SEARCH NO (B7),
    the hit cap, a lost row or a failed re-check."""
    wanted = frozenset(batch)
    search = criteria(b"NOT DRAFT", header_or(_LOOKUP_FIELDS, batch))
    for row in _rows(session, search, _LOOKUP_ITEMS, newest=newest, miss=partial(miss, batch), soft_check=soft_check):
        if (hit := _recheck(row, wanted, thread=thread, miss=miss)) is not None:
            yield hit


def _recheck(
    row: dict[bytes, object], wanted: frozenset[str], *, thread: bool, miss: Callable[[Iterable[str]], None]
) -> _Hit | None:
    """The hit as its fetched headers show it: drafts and rows without INTERNALDATE are dropped (S1, §6.5); a
    missing header item cannot be re-checked. A block cut at 64 KiB (A-sec3, C9) is matched without its last token,
    which the cut may have shortened (T4-3), and marks every wanted id it does not show: the cut may hide it. That
    rule is deliberately conservative (IR8): an id shown by another hit is still unchecked. A cut hit that shows a
    wanted id is no positive without a Message-ID that ended before the cut (it may be a copy of X or of a reply,
    A-sec3), nor in the thread lookup when its From did not end before it (MF-R4: a cut From may hide the owner)."""
    if _is_draft(row):
        return None
    received = _aware(row.get(b"INTERNALDATE"))
    if received is None:
        return None
    block = header_bytes(row)
    if not block:
        miss(wanted)
        return None
    if len(block) < DESCENDANT_HEADER_BYTES:
        parsed = parse_descendant(block, wanted)
        return _Hit(parsed.message_id, parsed.from_address, received, parsed.referenced)
    parsed = parse_descendant(block[: max(block.rfind(end) for end in _TOKEN_ENDS) + 1], wanted)
    miss(wanted - parsed.referenced)
    if not parsed.referenced:
        return None
    sender = parsed.from_address if _field_complete(block, b"from") else None
    complete_id = parsed.message_id is not None and _field_complete(block, b"message-id")
    if not complete_id or (thread and sender is None):
        miss(parsed.referenced)
        return None
    return _Hit(parsed.message_id, sender, received, parsed.referenced)


def _with_facts(status: MessageStatus, state: _Lookups) -> MessageStatus:
    """The lookup fields of a found X; `thread_checked` only when every batch covering it completed."""
    found = status.found
    if found is None or found.message_id is None:
        return status
    x = found.message_id
    first = state.first_reply.get(x)
    newer = [*state.newer.get(x, {}).values()]
    if x in state.newer_without_id:
        newer.append(state.newer_without_id[x])
    return replace(
        status,
        reply_at=None if first is None else first[0],
        reply_message_id=None if first is None else first[1],
        newer_incoming_at=max(newer, default=None),
        thread_checked=x in state.done and x not in state.unchecked,
    )


# --- step 4, Gmail: reply and thread lookups by X-GM-THRID -------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _GmailRef:
    """A found Gmail copy taking part in the lookups."""

    message_id: str | None  # valid Message-ID; without one the reply part is unchecked (E-mf3)
    thread_id: int | None  # X-GM-THRID of the copy; without one the thread part is unchecked (T0)
    received_at: datetime


@dataclass(frozen=True, slots=True)
class _Member:
    """A thread member that passed the re-check."""

    thread_id: int
    sent: bool  # labelled `\\Sent`, or read in the `\\Sent` mailbox (Gmail without `\\All`)
    message_id: str | None  # valid bare Message-ID, else None
    from_address: str | None  # None when absent or cut
    received_at: datetime
    referenced: frozenset[str]  # its thread's ref Message-IDs among its In-Reply-To and References tokens
    unseen: frozenset[str] = frozenset()  # ref Message-IDs a cut `\\Sent` member may hide (C9)


@dataclass(frozen=True, slots=True)
class _GmailLookups:
    """What the Gmail lookups learned, by target position."""

    unchecked: set[int] = field(default_factory=set)  # a lookup covering the ref did not complete or could not run
    done: set[int] = field(default_factory=set)  # every thread batch covering the ref was answered (or failed)
    first_reply: dict[int, tuple[datetime, str | None]] = field(default_factory=dict)  # earliest reply, its id
    sent_ids: set[str] = field(default_factory=set)  # Message-IDs of every `\\Sent` hit and member (MF4)
    newer: dict[int, dict[str, datetime]] = field(default_factory=dict)  # incoming member id → earliest copy (MF6)
    newer_without_id: dict[int, datetime] = field(default_factory=dict)  # newest later member without a Message-ID


def _gmail_lookups(
    session: ImapSession,
    answers: Sequence[tuple[MessageStatus, FoundCopy | None]],
    soft_check: Callable[[], None],
) -> list[MessageStatus]:
    """§5.2 "Replies and thread, IMAP" on Gmail (T0 rulings, A-mf5): the thread lookup by the copies' X-GM-THRID in
    All Mail, inside the EXAMINE the location left (E-im5 Design A); without `\\All` in `\\Sent`, INBOX and
    `\\Trash`, where `thread_checked` stays true although archived mail is not seen (E-im3a). A reply is
    `\\Answered`, or a `\\Sent` member of the thread whose re-checked In-Reply-To/References name the copy's
    Message-ID; the All Mail header search runs only while GMAIL_REPLY_HEADER_SEARCH is on (T0 P1: off), and E-mf1
    reply keys are not used. Residuals (R35): a reply or follow-up whose subject was changed lands in a new Gmail
    thread (the owner's reply is then seen only through `\\Answered`, a follow-up not at all); Gmail may group
    unrelated same-subject mail without References into one thread, which can then count as newer incoming.

    A copy without a valid Message-ID keeps the thread lookup (E-mf3); one without a valid X-GM-THRID gets none.
    Both stay unchecked. A NO marks the refs of its batch (or, for an EXAMINE, of the mailbox) unchecked; the folder
    map is never rebuilt here (§6.5, TS1). A soft stop keeps what was read and leaves every ref whose thread batches
    did not all finish unchecked (IR2)."""
    statuses = [status for status, _ in answers]
    refs = {
        index: _GmailRef(status.found.message_id, copy.thread_id, copy.received_at)
        for index, (status, copy) in enumerate(answers)
        if status.found is not None and copy is not None
    }
    if not refs:
        return statuses
    state = _GmailLookups()
    try:
        folder_map = session.folder_map()  # the location's map: never rebuilt here (Sec16, TS1)
        all_mail = folder_map.role("all")
        reply_box: FolderEntry | None = all_mail
        if all_mail is not None:
            folders = [all_mail]
        else:
            reply_box = folder_map.role("sent")
            folders = sorted(  # the mailbox still selected first: one EXAMINE fewer (Design A)
                (entry for role in _GMAIL_NO_ALL_ROLES if (entry := folder_map.role(role)) is not None),
                key=lambda entry: entry.raw_name != session.selected,
            )
        if reply_box is None:
            state.unchecked.update(refs)  # no `\\Sent` mailbox: replies cannot be read (C5)
        elif GMAIL_REPLY_HEADER_SEARCH:
            _gmail_reply_step(session, reply_box, refs, state, soft_check)
        _gmail_thread_step(session, folders, refs, state, soft_check)
    except SoftStop:
        pass  # positives read stay; refs not in `done` are unchecked
    return [_with_gmail_facts(status, refs.get(index), index, state) for index, status in enumerate(statuses)]


def _gmail_reply_step(
    session: ImapSession,
    entry: FolderEntry,
    refs: Mapping[int, _GmailRef],
    state: _GmailLookups,
    soft_check: Callable[[], None],
) -> None:
    """Option 17 (a), only while GMAIL_REPLY_HEADER_SEARCH is on: OR-ed `HEADER In-Reply-To` and `HEADER References`
    keys, in All Mail with the label key `\\Sent` (never the `sent` list filter, IMAP M2), in the `\\Sent` mailbox
    without it. A `\\Sent` hit is a reply to every batch id it names, except a hit whose Message-ID is that id (C8)."""
    by_id: dict[str, list[int]] = {}
    for index, ref in refs.items():
        if ref.message_id is not None:
            if search_value_ok(ref.message_id):
                by_id.setdefault(ref.message_id, []).append(index)
            else:
                state.unchecked.add(index)  # a refused value is never searched (D80)

    def miss(ids: Iterable[str]) -> None:
        state.unchecked.update(index for value in ids for index in by_id[value])

    if not by_id:
        return
    if not _open(session, entry):
        miss(by_id)
        return
    label = (GMAIL_SENT_LABEL_KEY,) if entry.role == "all" else ()
    for batch in or_batches(list(by_id), _LOOKUP_FIELDS, max_values=LOOKUP_BATCH_VALUES):
        wanted = frozenset(batch)
        search = criteria(b"NOT DRAFT", *label, header_or(_LOOKUP_FIELDS, batch))
        for row in _rows(
            session, search, _GMAIL_REPLY_ITEMS, newest=False, miss=partial(miss, batch), soft_check=soft_check
        ):
            if not _gmail_sent(entry, row) or (hit := _recheck(row, wanted, thread=False, miss=miss)) is None:
                continue
            if hit.message_id is not None:
                state.sent_ids.add(hit.message_id)
            for x in hit.referenced:
                if hit.message_id != x:
                    for index in by_id[x]:
                        _gmail_reply(state, index, hit.received_at, hit.message_id)


def _gmail_thread_step(
    session: ImapSession,
    folders: Sequence[FolderEntry],
    refs: Mapping[int, _GmailRef],
    state: _GmailLookups,
    soft_check: Callable[[], None],
) -> None:
    """Per `thrid_batches` batch (at most 20 keys, P5) one `UID SEARCH NOT DRAFT` with OR-ed `X-GM-THRID` keys in
    every lookup mailbox, without SINCE (Option 13), each mailbox opened once. Refs that share a thread id share its
    key; `_count_member` decides per ref."""
    by_thread: dict[int, list[int]] = {}
    for index, ref in refs.items():
        if ref.thread_id is not None:
            by_thread.setdefault(ref.thread_id, []).append(index)
    wanted = {
        thread_id: frozenset(x for index in indexes if (x := refs[index].message_id) is not None)
        for thread_id, indexes in by_thread.items()
    }
    batches = thrid_batches(list(by_thread))
    if not batches:
        return  # no copy with a thread id: no mailbox is opened
    left = {index: len(folders) for indexes in by_thread.values() for index in indexes}

    def miss(thread_ids: Iterable[int]) -> None:
        state.unchecked.update(index for thread_id in thread_ids for index in by_thread[thread_id])

    for entry in folders:
        opened = _open(session, entry)
        for batch in batches:
            if opened:
                search = criteria(b"NOT DRAFT", thrid_or(batch))
                for row in _rows(
                    session, search, _GMAIL_MEMBER_ITEMS, newest=True, miss=partial(miss, batch), soft_check=soft_check
                ):
                    member = _gmail_member(entry, row, frozenset(batch), wanted, miss)
                    if member is None:
                        continue
                    indexes = by_thread[member.thread_id]
                    state.unchecked.update(index for index in indexes if refs[index].message_id in member.unseen)
                    _count_member(session, state, member, indexes, refs)
            else:
                miss(batch)
            for thread_id in batch:
                for index in by_thread[thread_id]:
                    left[index] -= 1
                    if not left[index]:
                        state.done.add(index)


def _gmail_member(
    entry: FolderEntry,
    row: dict[bytes, object],
    batch: frozenset[int],
    wanted: Mapping[int, frozenset[str]],
    miss: Callable[[Iterable[int]], None],
) -> _Member | None:
    """The member as its fetched row shows it. Drafts (flag or label, any case; S1) and rows without INTERNALDATE are
    dropped; a row without a valid X-GM-THRID cannot be attributed (its batch is unchecked), and one of a thread
    outside the batch is no member. A missing header item leaves its thread unchecked. A block cut at 64 KiB (A-sec3,
    C9) is matched without its last token (T4-3); the member is not used, and its thread unchecked, when its
    Message-ID did not end before the cut (it may be a copy of X or of a reply) or, for an incoming member, its From
    (MF-R4). A cut `\\Sent` member reports the ref Message-IDs it does not show as `unseen`."""
    if _is_draft(row):
        return None
    received = _aware(row.get(b"INTERNALDATE"))
    if received is None:
        return None
    thread_id = _thread_id(row)
    if thread_id is None:
        miss(batch)
        return None
    if thread_id not in batch:
        return None
    block = header_bytes(row)
    if not block:
        miss((thread_id,))
        return None
    sent = _gmail_sent(entry, row)
    ids = wanted[thread_id]
    if len(block) < DESCENDANT_HEADER_BYTES:
        parsed = parse_descendant(block, ids)
        return _Member(thread_id, sent, parsed.message_id, parsed.from_address, received, parsed.referenced)
    parsed = parse_descendant(block[: max(block.rfind(end) for end in _TOKEN_ENDS) + 1], ids)
    sender = parsed.from_address if _field_complete(block, b"from") else None
    complete_id = parsed.message_id is not None and _field_complete(block, b"message-id")
    if not complete_id or (not sent and sender is None):
        miss((thread_id,))
        return None
    unseen = ids - parsed.referenced if sent else frozenset()
    return _Member(thread_id, sent, parsed.message_id, sender, received, parsed.referenced, unseen)


def _gmail_sent(entry: FolderEntry, row: dict[bytes, object]) -> bool:
    return entry.role == "sent" or _LABEL_SENT in _labels(row)


def _count_member(
    session: ImapSession,
    state: _GmailLookups,
    member: _Member,
    indexes: Sequence[int],
    refs: Mapping[int, _GmailRef],
) -> None:
    """A `\\Sent` member is a reply to every ref of its thread whose Message-ID it names, except a copy of that ref
    (C8, A-mf5 (b)); it is never newer incoming, nor is a copy of its Message-ID (MF4: applied at the end, since a
    `\\Sent` copy may come later in the answer). An incoming member counts for a ref unless it is a copy of the ref
    (MF3) or its From is own (MF-R1); §5.2 keeps the strict time comparison on Gmail (E-mf2), applied to the
    earliest copy of each Message-ID (MF6)."""
    if member.sent:
        if member.message_id is not None:
            state.sent_ids.add(member.message_id)
        for index in indexes:
            x = refs[index].message_id
            if x is not None and x in member.referenced and member.message_id != x:
                _gmail_reply(state, index, member.received_at, member.message_id)
        return
    if direction_of(in_sent=False, from_address=member.from_address, own=session.own) == "outgoing":
        return
    for index in indexes:
        ref = refs[index]
        if member.message_id is None:
            newest = state.newer_without_id.get(index)
            if member.received_at > ref.received_at and (newest is None or member.received_at > newest):
                state.newer_without_id[index] = member.received_at
        elif member.message_id != ref.message_id:
            copies = state.newer.setdefault(index, {})
            seen = copies.get(member.message_id)
            if seen is None or member.received_at < seen:
                copies[member.message_id] = member.received_at


def _gmail_reply(state: _GmailLookups, index: int, received_at: datetime, message_id: str | None) -> None:
    first = state.first_reply.get(index)
    if first is None or received_at < first[0]:
        state.first_reply[index] = (received_at, message_id)


def _with_gmail_facts(status: MessageStatus, ref: _GmailRef | None, index: int, state: _GmailLookups) -> MessageStatus:
    """The lookup fields of a found copy. A counted Message-ID is newer only when it is no `\\Sent` message (MF4) and
    its earliest copy came strictly after the ref's (MF6); `thread_checked` needs a Message-ID and every batch
    covering the ref completed (a ref without a thread id has no batch, so it is never done)."""
    if ref is None:
        return status
    first = state.first_reply.get(index)
    newer = [
        at
        for message_id, at in state.newer.get(index, {}).items()
        if message_id not in state.sent_ids and at > ref.received_at
    ]
    if index in state.newer_without_id:
        newer.append(state.newer_without_id[index])
    checked = ref.message_id is not None and index in state.done
    return replace(
        status,
        reply_at=None if first is None else first[0],
        reply_message_id=None if first is None else first[1],
        newer_incoming_at=max(newer, default=None),
        thread_checked=checked and index not in state.unchecked,
    )
