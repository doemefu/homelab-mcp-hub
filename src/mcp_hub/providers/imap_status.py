"""IMAP `get_message_status` (spec 080 §5.2 "Location, IMAP", "Replies and thread, IMAP", §5.4, §6.5; WP13b): the
in-place check of `m` ids, the location step, the mapping to `MessageStatus` and, on servers other than Gmail, the
reply and thread lookups, inside one `ImapSession` (one connection, one deadline, D76).

Read-only through the guard: EXAMINE, UID SEARCH and UID FETCH of FLAGS, INTERNALDATE, the Message-ID (and FROM)
header item, the 64 KiB re-check item of the lookups and, on Gmail, X-GM-LABELS and X-GM-THRID. Nothing here is
logged but `item_degraded`, through the session and capped per call (SR6)."""

import imaplib
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
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
    LOOKUP_BATCH_VALUES,
    HeaderField,
    criteria,
    header_or,
    or_batches,
    search_value_ok,
)
from mcp_hub.providers.imap_session import (
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
LOOKUP_FETCH_UIDS: Final = 100  # UIDs per re-check FETCH: at most 6.4 MiB of 64 KiB header items per answer (C9)
MAX_REPLY_KEYS_PER_REF: Final = 5  # owner reply Message-IDs added to a ref's thread keys (E-mf1)
MAX_DEGRADED_LOGS: Final = 20  # item_degraded lines per provider call (SR6)
SOFT_CHECK_ROWS: Final = 10  # fetched rows parsed between two soft checks (SR2: parsing sends no command)
_LOOKUP_FIELDS: Final[tuple[HeaderField, ...]] = ("In-Reply-To", "References")
_LOOKUP_ITEMS: Final = ("FLAGS", "INTERNALDATE", DESCENDANT_HEADER_ITEM)
_THREAD_ROLES: Final[tuple[FolderRole, ...]] = ("inbox", "archive", "trash")
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
    4. On servers other than Gmail, the reply and thread lookups of the found Message-IDs (`_lookups`); the Gmail
       lookups are T10, so `thread_checked` stays false there for now.

    A soft stop in step 1 keeps the copies read (found) and leaves every other entry incomplete_search, without a
    location (IR2, P7); one in step 2 is handled by `locate`, one in step 4 by `_lookups`. A BAD, an abort, a lost
    connection or the hard deadline propagate: the account fails (B7)."""
    session.cap_degraded_logs(MAX_DEGRADED_LOGS)
    in_place = _InPlace()
    try:
        _check_in_place(session, targets, in_place, soft_check)
    except SoftStop:
        return [_after_soft_stop(session, index, in_place) for index in range(len(targets))]
    result = _locate(session, targets, in_place, soft_check)
    statuses = [_answer(session, index, target, in_place, result) for index, target in enumerate(targets)]
    return statuses if session.gmail else _lookups(session, statuses, soft_check)


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
    """Never newer incoming: an outgoing message (own From, MF-R1; without own addresses a residual, MF-R3), a copy
    of a sent hit, which includes the owner's replies (MF4, E-mf1), and a copy of X (MF3)."""
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


def _hits(
    session: ImapSession,
    batch: list[str],
    *,
    newest: bool,
    thread: bool,
    miss: Callable[[Iterable[str]], None],
    soft_check: Callable[[], None],
) -> Iterator[_Hit]:
    """One `UID SEARCH NOT DRAFT` with OR-ed `HEADER In-Reply-To` and `HEADER References` keys for `batch` in the
    selected mailbox, then the re-check of at most MAX_LOOKUP_HITS hits (the lowest UIDs, or the highest when
    `newest`; E-im2, C9), fetched in chunks of LOOKUP_FETCH_UIDS, each reduced before the next is fetched. `miss`
    marks the batch ids whose answer is incomplete: a SEARCH NO (B7), the hit cap, a lost row or a failed re-check."""
    try:
        found = session.search(criteria(b"NOT DRAFT", header_or(_LOOKUP_FIELDS, batch)))
    except PASS_THROUGH:
        raise
    except imaplib.IMAP4.error as exc:
        if not _answered_no(exc):
            raise
        miss(batch)
        return
    uids = sorted(set(found))
    if len(uids) > MAX_LOOKUP_HITS:
        uids = uids[-MAX_LOOKUP_HITS:] if newest else uids[:MAX_LOOKUP_HITS]
        miss(batch)
    wanted = frozenset(batch)
    chunks = session.fetch_chunks(uids, _LOOKUP_ITEMS, chunk=LOOKUP_FETCH_UIDS)
    for start, rows in zip(range(0, len(uids), LOOKUP_FETCH_UIDS), chunks, strict=True):
        part = uids[start : start + LOOKUP_FETCH_UIDS]
        if len(rows.keys() & set(part)) < len(part):  # FETCH NO or expunged meanwhile: a hit is unchecked
            miss(batch)
        for count, uid in enumerate(part):
            if count and count % SOFT_CHECK_ROWS == 0:
                soft_check()  # SR2: at most 10 blocks of 64 KiB parsed between two checks; parsing sends no command
            row = rows.get(uid)
            if row is not None and (hit := _recheck(row, wanted, thread=thread, miss=miss)) is not None:
                yield hit
        if start + LOOKUP_FETCH_UIDS < len(uids):
            soft_check()  # per chunk; the guard checks again before the next FETCH


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
