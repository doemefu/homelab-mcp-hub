"""IMAP `get_thread` (spec 080 §5.2 `get_thread`, §5.5, §6.5; WP13c): the seed, the members, the merge and the body
pass, inside one `ImapSession` (one connection, one deadline, D76). Gmail finds the members by `X-GM-THRID` (Option
4a); other servers by the header rounds (Option 3a) and the subject fallback (3) (Option 8a).

Read-only through the guard: EXAMINE, UID SEARCH and UID FETCH of FLAGS, INTERNALDATE, the summary header item, the
8 KiB subject-scan item, X-GM-LABELS and X-GM-THRID (Gmail), BODYSTRUCTURE in chunks of at most 10 UIDs (A-sec1) and
the chosen text part as `BODY.PEEK[<section>]<0.65536>`. Subjects are compared locally, never sent (D74). Nothing here
is logged but `item_degraded`, through the session and capped per call.
"""

from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from typing import Final, cast

from mcp_hub.ids import AnyMessageRef, MessageRef, StableRef, ThreadKey
from mcp_hub.providers.base import (
    MAX_HEADER_BYTES,
    OMITTED_BODY,
    BodySource,
    LinkedBy,
    MailFlags,
    MailMeta,
    MailSummary,
    ProviderError,
    ThreadBody,
    ThreadMember,
    ThreadPage,
    ThreadQuery,
    direction_of,
    order_key,
)
from mcp_hub.providers.deadline import SoftStop
from mcp_hub.providers.folders import FolderEntry, FolderRole
from mcp_hub.providers.headers import (
    SUMMARY_HEADER_ITEM,
    ParsedHeaders,
    header_bytes,
    imap_thread_key,
    parse_descendant,
    parse_summary_headers,
    subject_digest,
    trim_cut,
    valid_message_id,
)
from mcp_hub.providers.imap_guard import PASS_THROUGH
from mcp_hub.providers.imap_search import (
    THREAD_ROUND_KEYS,
    THREAD_SEED_IDS,
    MixedKey,
    criteria,
    header_or,
    mixed_batches,
    mixed_or,
    or_batches,
    search_value_ok,
    thrid_or,
    window_keys,
)
from mcp_hub.providers.imap_session import (
    _LABEL_SENT,
    ImapSession,
    MissingInternalDate,
    _aware,
    _bound_entry,
    _detail_folder,
    _examine_bound,
    _field_complete,
    _flags,
    _is_draft,
    _labels,
    _open,
    _thread_id,
    _uid_rows,
)
from mcp_hub.providers.mime import Part, attachment_parts, choose_text_part, decode_part, walk
from mcp_hub.providers.threads import (
    ThreadCopy,
    choose_members,
    merge_copies,
    overview,
    participants_of,
    prepare_body,
)
from mcp_hub.sanitize import html_to_text

BODY_RESERVE_SECONDS: Final = 12.0  # no new member search after this: the body pass's share (G2, Q-b; soft stop - 4 s)
THREAD_BODY_BYTES: Final = 65_536  # partial fetch of a member's text part (§5.2 "Bodies")
STRUCTURE_CHUNK_UIDS: Final = 10  # BODYSTRUCTURE lines per UID FETCH (A-sec1)
MAX_DEGRADED_LOGS: Final = 20  # item_degraded lines per call (sec-G8)
MAX_ROUND_ITERATIONS: Final = 3  # round (2) iterations after round (1) (§6.5, S-m)
_THREAD_ROLES: Final[tuple[FolderRole, ...]] = ("inbox", "sent", "archive", "trash")  # trash with include_trash
SUBJECT_WINDOW: Final = timedelta(days=30)  # subject members within 30 days of the seed (§6.5 (3))
MAX_SUBJECT_CANDIDATES: Final = 500  # the highest UIDs read per folder and call (Q4); beyond: not exact
SUBJECT_SCAN_BYTES: Final = 8192
SUBJECT_SCAN_ITEM: Final = f"BODY.PEEK[HEADER.FIELDS (FROM TO CC SUBJECT MESSAGE-ID)]<0.{SUBJECT_SCAN_BYTES}>"
_SCAN_SINCE: Final = timedelta(days=31)  # SEARCH dates are the server's days: one day of margin (IR4)
_SCAN_BEFORE: Final = timedelta(days=32)  # BEFORE is exclusive: one day of margin after the 30 days
_SCAN_ITEMS: Final = ("FLAGS", "INTERNALDATE", SUBJECT_SCAN_ITEM)
_ROUND_ONE_FIELDS: Final = ("Message-ID", "References")
_MEMBER_ITEMS: Final = ("FLAGS", "INTERNALDATE", SUMMARY_HEADER_ITEM)
_GMAIL_MEMBER_ITEMS: Final = ("FLAGS", "INTERNALDATE", "X-GM-LABELS", "X-GM-THRID", SUMMARY_HEADER_ITEM)
_SEEN: Final = b"\\seen"
_ANSWERED: Final = b"\\answered"
_FLAGGED: Final = b"\\flagged"

Location = tuple[str, int, int]  # (raw mailbox name, UIDVALIDITY, UID)


@dataclass(frozen=True, slots=True)
class _Seed:
    """Where the seed lies; `row` when it was read in place (an `id`)."""

    entry: FolderEntry
    uidvalidity: int
    uid: int
    message_id: str | None  # a `ref` seed keeps the ref's Message-ID, also from a cut header block (IR2)
    received_at: datetime | None  # of the located copy (`ref`)
    thread_id: int | None  # Gmail X-GM-THRID of the located copy or of the row read in place
    row: dict[bytes, object] | None = None

    @property
    def location(self) -> Location:
        return self.entry.raw_name, self.uidvalidity, self.uid


@dataclass(slots=True)
class _Rounds:
    """The ids the header rounds searched (Option 3a): as `Message-ID` keys (round (1) and upward keys) and as
    `In-Reply-To` keys (downward keys). A member's id is never searched again."""

    up: set[str] = field(default_factory=set)
    down: set[str] = field(default_factory=set)


@dataclass(frozen=True, slots=True)
class _Candidate:
    """A subject-scan row whose subject digest matched, reduced at once (A-sec1): where it lies and the copy it
    becomes when it joins. The copy holds the scan item's fields (received_at, participants, Message-ID, sent
    state); the body pass completes the rest (Option 6a)."""

    entry: FolderEntry
    location: Location
    copy: ThreadCopy


@dataclass(slots=True)
class _Members:
    """The copies found, where they lie, and what the member passes left incomplete (P11)."""

    copies: list[ThreadCopy] = field(default_factory=list)
    seen: set[Location] = field(default_factory=set)  # every location is read once (P7)
    entries: dict[str, FolderEntry] = field(default_factory=dict)  # raw name → entry, for the body pass
    exact: bool = True  # every search of every pass completed (`message_count_exact`)
    truncated: bool = False  # members may be missing: SEARCH or EXAMINE NO, the reserve, the soft stop
    seed_headers: ParsedHeaders | None = None  # the seed's headers, trimmed when cut: round (1) ids (IR2, T7-3)
    seed_cut: bool = False  # the seed's block reached its cap: References beyond the cut were not seen
    cut_copies: set[Location] = field(default_factory=set)  # copies whose summary block reached its cap
    provisional: set[Location] = field(default_factory=set)  # copies from the scan item, completed in the body pass

    def partial(self) -> None:
        self.exact, self.truncated = False, True

    def inexact(self) -> None:
        self.exact = False

    def has_seed(self) -> bool:
        return any(copy.seed for copy in self.copies)


def get_thread(
    session: ImapSession,
    query: ThreadQuery,
    *,
    soft_check: Callable[[], None],
    reserve_spent: Callable[[], bool],
) -> ThreadPage:
    """§5.2 `get_thread` on one IMAP account. `soft_check` raises SoftStop after the call's soft limit;
    `reserve_spent` is true once no new member search may start (12 s, G2). A key whose source does not fit the
    account is `source_mismatch`, decided after LOGIN/CAPABILITY and before any LIST or EXAMINE. A soft stop before
    the seed is read is `upstream_timeout`; during the member passes the members so far are returned (truncated, not
    exact); during the body pass the rest of the bodies are omitted (truncated)."""
    session.cap_degraded_logs(MAX_DEGRADED_LOGS)
    state = _Members()
    if query.key is not None:
        if not _fits(query.key, gmail=session.gmail):
            return ThreadPage("source_mismatch")
        thread: ThreadKey | None = query.key
        _key_members(session, query.key, query, state, soft_check, reserve_spent)
    else:
        thread = _seed_members(session, query, state, soft_check, reserve_spent)
    merged = merge_copies(state.copies, include_trash=query.include_trash)
    counted, kept, capped = choose_members(merged, include_trash=query.include_trash)
    members = _body_pass(
        session, kept, state.entries, provisional=state.provisional, max_chars=query.max_chars, soft_check=soft_check
    )
    return ThreadPage(
        "ok",
        thread=thread,
        members=tuple(members),
        overview=overview(counted, exact=state.exact),
        truncated=capped or state.truncated or any(member.body.omitted for member in members),
    )


def _fits(key: ThreadKey, *, gmail: bool) -> bool:
    """`gmail` keys need `X-GM-EXT-1`; `headers` and `subject` keys belong to other servers; `graph` never fits."""
    if key.source == "gmail":
        return gmail
    return key.source in ("headers", "subject") and not gmail


def _linked_by(session: ImapSession) -> LinkedBy:
    """The thread's source, which is also the seed's `linked_by` (E-mf6, MF-P1)."""
    return "server" if session.gmail else "headers"


def _items(session: ImapSession) -> tuple[str, ...]:
    return _GMAIL_MEMBER_ITEMS if session.gmail else _MEMBER_ITEMS


def _utc_now() -> datetime:
    """The wall-clock time the scan of a `subject` key counts its days from (tests pin it)."""
    return datetime.now(UTC)


# --- seed ------------------------------------------------------------------------------------------------------


def _key_members(
    session: ImapSession,
    key: ThreadKey,
    query: ThreadQuery,
    state: _Members,
    soft_check: Callable[[], None],
    reserve_spent: Callable[[], bool],
) -> None:
    """The members from the key alone. A `headers` key runs the rounds from its root, then (3) from the root member
    (Q5); a `subject` key runs (3) only (Q-d). Nothing found is `not_found` after a complete search and
    `upstream_timeout` otherwise; members found by an incomplete search are a partial answer (B6, Q6 as refined by
    IR3)."""
    value = key.value
    try:
        if key.source == "gmail" and isinstance(value, int):
            _gmail_members(session, value, query, state, None, soft_check, reserve_spent)
        elif key.source == "headers" and isinstance(value, str):
            _header_members(session, [value], query, state, None, soft_check, reserve_spent)  # round (1): the root
            anchor = _anchor(state, root=value)
            if anchor is not None:
                _subject_members(session, query, state, *anchor, soft_check, reserve_spent)
        elif key.source == "subject" and isinstance(value, str):
            _subject_key_members(session, value, query, state, soft_check, reserve_spent)
        else:
            raise ProviderError("upstream_error", "NotImplemented")  # a key value of the wrong type (ids.py)
    except SoftStop:
        state.partial()
    if not state.copies:
        if state.exact:
            raise ProviderError("not_found", "NotFound")
        raise ProviderError("upstream_timeout", "IncompleteSearch")


def _seed_members(
    session: ImapSession,
    query: ThreadQuery,
    state: _Members,
    soft_check: Callable[[], None],
    reserve_spent: Callable[[], bool],
) -> ThreadKey | None:
    """The seed, then its thread's members; returns the thread key. On Gmail the seed's row comes from the
    X-GM-THRID fetch of its mailbox (Q-b); only a seed that no member search returned is read alone. A seed without
    a thread id is returned alone, not exact, with no key (SG4). Other servers read the seed first, then run the
    header rounds from its ids; a cut seed block still gives the ids before the cut, and the count is not exact
    (IR2, P12). When the rounds found no other member, (3) follows (`_anchor`); a seed with no id to search by and
    an empty normalised subject is returned alone, not exact (P12, SG4)."""
    seed = _seed(session, query.seed, soft_check)
    if seed.row is not None:
        _add(session, seed.entry, seed.uidvalidity, seed.uid, seed.row, state, linked_by=_linked_by(session), seed=seed)
    if session.gmail and seed.thread_id is not None:
        try:
            _gmail_members(session, seed.thread_id, query, state, seed, soft_check, reserve_spent)
        except SoftStop:
            state.partial()
            if not state.has_seed():
                raise  # the seed was not read yet: upstream_timeout
        if not state.has_seed():
            _read_seed(session, seed, state)
        return ThreadKey(session.account, "gmail", seed.thread_id)
    if not state.has_seed():
        _read_seed(session, seed, state)
    if session.gmail:
        state.inexact()  # the seed alone (SG4)
        return None
    ids = seed_round_ids(state.seed_headers) if state.seed_headers is not None else []
    if state.seed_cut:
        state.inexact()  # the References beyond the cut were not seen (IR2)
    try:
        if ids:
            _header_members(session, ids, query, state, seed, soft_check, reserve_spent)
        anchor = _anchor(state)
        if anchor is not None:
            _subject_members(session, query, state, *anchor, soft_check, reserve_spent)
        elif not ids:
            state.inexact()  # no id and no subject to search by: the seed alone (P12, SG4)
    except SoftStop:
        state.partial()
    return next(copy for copy in state.copies if copy.seed).summary.meta.thread


def _seed(session: ImapSession, target: AnyMessageRef | StableRef | None, soft_check: Callable[[], None]) -> _Seed:
    """A `ref` is located as in `get_message` (not found after a complete search → `not_found`; incomplete or
    uncertain → `upstream_timeout`, B6). An `id` is read in place (D75 binding): another UIDVALIDITY, a missing row
    or a draft → `not_found`, as in `get_message`."""
    if isinstance(target, StableRef):
        message_id = target.message_id
        result = session.locate([message_id], soft_check=soft_check)
        soft_check()
        copy = result.found.get(message_id)
        if copy is None or message_id in result.uncertain:  # uncertain: an earlier copy may be unread (S-e)
            if copy is None and message_id not in result.incomplete:
                raise ProviderError("not_found", "NotFound")
            raise ProviderError("upstream_timeout", "IncompleteSearch")
        return _Seed(copy.entry, copy.uidvalidity, copy.uid, message_id, copy.received_at, copy.thread_id)
    if not isinstance(target, MessageRef):
        raise ProviderError("not_found", "ForeignRef")
    entry = _bound_entry(session, target.folder)
    if _examine_bound(session, entry) != target.uidvalidity:
        raise ProviderError("not_found", "UidValidityChanged")
    row = session.fetch([target.uid], _items(session), on_no="fail").get(target.uid)
    if row is None:
        raise ProviderError("not_found", "UnknownUid")
    if _is_draft(row):
        raise ProviderError("not_found", "Draft")
    return _Seed(entry, target.uidvalidity, target.uid, None, None, _thread_id(row), row)


def _read_seed(session: ImapSession, seed: _Seed, state: _Members) -> None:
    """The located seed's row, read alone in its mailbox (EXAMINE unless selected). Not opened, renumbered or gone
    → `upstream_timeout`, as in `get_message` by ref (B6); a draft by now → `not_found`."""
    if not _open(session, seed.entry) or session.selected_uidvalidity != seed.uidvalidity:
        raise ProviderError("upstream_timeout", "IncompleteSearch")
    row = session.fetch([seed.uid], _items(session), on_no="fail").get(seed.uid)
    if row is None:
        raise ProviderError("upstream_timeout", "IncompleteSearch")
    if _is_draft(row):
        raise ProviderError("not_found", "Draft")
    _add(session, seed.entry, seed.uidvalidity, seed.uid, row, state, linked_by=_linked_by(session), seed=seed)


# --- members ---------------------------------------------------------------------------------------------------


def _gmail_members(
    session: ImapSession,
    thread_id: int,
    query: ThreadQuery,
    state: _Members,
    seed: _Seed | None,
    soft_check: Callable[[], None],
    reserve_spent: Callable[[], bool],
) -> None:
    """Option 4a: `UID SEARCH NOT DRAFT X-GM-THRID <n>` in All Mail (without `\\All`: INBOX and `\\Sent`, G3) and,
    with `include_trash`, in Trash, which is not searched once the reserve is spent (Q-b); the selected mailbox
    first. Each hit is re-checked on its fetched X-GM-THRID and drafts are dropped (S1). A mailbox that cannot be
    searched (EXAMINE or SEARCH NO, no UIDVALIDITY, the reserve) or a lost hit leaves the list partial (G4, P1,
    P11). A SoftStop propagates with the copies read so far kept in `state`."""
    folder_map = session.folder_map()
    roles: tuple[FolderRole, ...] = ("all",) if folder_map.role("all") is not None else ("inbox", "sent")
    if query.include_trash:
        roles += ("trash",)
    folders = sorted(
        (entry for role in roles if (entry := folder_map.role(role)) is not None),
        key=lambda entry: entry.raw_name != session.selected,  # stable: one EXAMINE fewer (Design A)
    )
    search = criteria(b"NOT DRAFT", thrid_or([thread_id]))
    for entry in folders:
        if entry.role == "trash" and reserve_spent():
            state.partial()
            continue
        if not _open(session, entry) or session.selected_uidvalidity is None:
            state.partial()
            continue
        validity = session.selected_uidvalidity
        rows = _uid_rows(session, search, _GMAIL_MEMBER_ITEMS, newest=True, miss=state.partial, soft_check=soft_check)
        for uid, row in rows:
            if _is_draft(row) or _thread_id(row) != thread_id:
                continue
            _add(session, entry, validity, uid, row, state, linked_by="server", seed=seed)


def _add(
    session: ImapSession,
    entry: FolderEntry,
    uidvalidity: int,
    uid: int,
    row: dict[bytes, object],
    state: _Members,
    *,
    linked_by: LinkedBy,
    seed: _Seed | None,
) -> None:
    """One copy, unless its location was read already (P7). A row without INTERNALDATE is dropped with one
    `item_degraded` line and the count is not exact (Q9). The seed is never dropped: a located seed keeps the time
    `locate` read; an `id` seed is kept at the call's time, as `get_message` opens it, with one `item_degraded` line,
    and the count is not exact, since its place in the order is a guess (T7-1)."""
    location: Location = (entry.raw_name, uidvalidity, uid)
    if location in state.seen:
        return
    state.seen.add(location)
    is_seed = seed is not None and location == seed.location
    ref = MessageRef(session.account, entry.raw_name, uidvalidity, uid)
    received = _aware(row.get(b"INTERNALDATE"))
    if received is None and is_seed and seed is not None:
        received = seed.received_at
        if received is None:
            session.log_skipped(ref, MissingInternalDate())
            state.inexact()
            received = datetime.now(UTC)
    if received is None:
        session.log_skipped(ref, MissingInternalDate())
        state.inexact()
        return
    state.entries[entry.raw_name] = entry
    known_id = seed.message_id if is_seed and seed is not None else None
    state.copies.append(
        _copy(session, entry, ref, received, row, state, linked_by=linked_by, seed=is_seed, known_id=known_id)
    )


def _copy(
    session: ImapSession,
    entry: FolderEntry,
    ref: MessageRef,
    received: datetime,
    row: dict[bytes, object],
    state: _Members,
    *,
    linked_by: LinkedBy,
    seed: bool,
    known_id: str | None,
) -> ThreadCopy:
    """The copy of a member row from its summary header item (`_headers`). The seed's headers are kept for round
    (1); a block at its cap is noted, since References beyond the cut may hide (no subject fallback from it)."""
    parsed = _headers(session, ref, row, state, known_id=known_id)
    cut = len(header_bytes(row)) >= MAX_HEADER_BYTES
    if cut:
        state.cut_copies.add((ref.folder, ref.uidvalidity, ref.uid))
    if seed:
        state.seed_headers, state.seed_cut = parsed, cut
    return _parsed_copy(session, entry, ref, received, row, parsed, linked_by=linked_by, seed=seed)


def _parsed_copy(
    session: ImapSession,
    entry: FolderEntry,
    ref: MessageRef,
    received: datetime,
    row: dict[bytes, object],
    parsed: ParsedHeaders,
    *,
    linked_by: LinkedBy,
    seed: bool,
) -> ThreadCopy:
    """The copy's summary (snippet empty: the body replaces it), participants and sent state. `has_attachments`
    stays false until the body pass reads its structure (Q-c)."""
    flags, labels = _flags(row), _labels(row)
    folder = _detail_folder(entry, row, gmail=session.gmail)
    in_sent = folder == "sent" or _LABEL_SENT in labels
    direction = direction_of(in_sent=in_sent, from_address=parsed.from_address, own=session.own)
    if session.gmail:
        thread_id = _thread_id(row)
        thread = None if thread_id is None else ThreadKey(session.account, "gmail", thread_id)
    else:
        thread = imap_thread_key(session.account, parsed)
    summary = MailSummary(
        ref=ref,
        received_at=received,
        unread=_SEEN not in flags,
        has_attachments=False,
        from_address=parsed.from_address,
        from_name=parsed.from_name,
        subject=parsed.subject,
        snippet_text="",
        to_addresses=parsed.to,
        cc_addresses=parsed.cc,
        meta=MailMeta(
            folder=folder,
            folder_name=entry.display_name,
            flags=MailFlags(answered=_ANSWERED in flags, flagged=_FLAGGED in flags),
            direction=direction,
            message_id=parsed.message_id,
            in_reply_to=parsed.in_reply_to,
            references=parsed.references,
            thread=thread,
        ),
    )
    participants = participants_of(direction, parsed.from_address, parsed.to, parsed.cc, session.own)
    return ThreadCopy(summary, linked_by, participants, in_sent=in_sent, seed=seed)


def _headers(
    session: ImapSession, ref: MessageRef, row: dict[bytes, object], state: _Members, *, known_id: str | None
) -> ParsedHeaders:
    """The summary headers of a copy. A block cut at 64 KiB (IR2, MF-P2, A-sec3) is parsed without its last, possibly
    shortened token (T7-3); a Message-ID that did not end before the cut is dropped (a `ref` seed keeps the ref's)
    and the count is not exact, since the copy cannot merge; a cut From gives the direction by folder. A block that
    does not parse degrades only this copy (SC1)."""
    block = header_bytes(row)
    try:
        parsed = parse_summary_headers(trim_cut(block) if len(block) >= MAX_HEADER_BYTES else block)
    except PASS_THROUGH:
        raise
    except Exception as exc:
        session.log_skipped(ref, exc)
        parsed = parse_summary_headers(None)
    if len(block) < MAX_HEADER_BYTES:
        return parsed
    if not _field_complete(block, b"from"):
        parsed = replace(parsed, from_address=None, from_name=None)
    if parsed.message_id is None or not _field_complete(block, b"message-id"):
        own_root = parsed.root is None or parsed.root == parsed.message_id
        if known_id is None:
            state.inexact()
        parsed = replace(parsed, message_id=known_id, root=known_id if own_root else parsed.root)
    return parsed


# --- header rounds (other servers) -----------------------------------------------------------------------------


def seed_round_ids(parsed: ParsedHeaders) -> list[str]:
    """Round-(1) ids (A-mf1, ruling G1): the root (the first valid `References` entry), then `In-Reply-To`, then the
    newest valid `References` entries, until THREAD_SEED_IDS, without duplicates. Without either header the root is
    the message's own Message-ID, so that id alone is searched."""
    candidates = (parsed.root, parsed.in_reply_to, *reversed(parsed.references))
    ids = [valid for value in candidates if (valid := valid_message_id(value)) is not None]
    return list(dict.fromkeys(ids))[:THREAD_SEED_IDS]


def _header_members(
    session: ImapSession,
    ids: Sequence[str],
    query: ThreadQuery,
    state: _Members,
    seed: _Seed | None,
    soft_check: Callable[[], None],
    reserve_spent: Callable[[], bool],
) -> None:
    """Option 3a: round (1) from `ids` (the seed's, or a `headers` key's root), then round (2) with the upward
    closure. No folder is opened and no SEARCH sent once the reserve is spent (G2, T8-1): the rounds stop there, the
    list is partial, and the body pass keeps its time. A SoftStop propagates with the copies read so far kept in
    `state`."""
    rounds = _Rounds()
    if _round_one(session, ids, query, state, rounds, seed, soft_check, reserve_spent):
        _round_two(session, query, state, rounds, seed, soft_check, reserve_spent)


def _round_one(
    session: ImapSession,
    ids: Sequence[str],
    query: ThreadQuery,
    state: _Members,
    rounds: _Rounds,
    seed: _Seed | None,
    soft_check: Callable[[], None],
    reserve_spent: Callable[[], bool],
) -> bool:
    """Round (1): per folder one `UID SEARCH NOT DRAFT` with OR-ed `HEADER Message-ID` and `HEADER References` keys
    for the ids (two commands only when the 4,000-byte cap splits them). A hit is a member when its Message-ID is one
    of the ids or its In-Reply-To or References name one. False when the reserve stopped it."""
    rounds.up.update(ids)
    values = _searchable(ids, state)
    if not values:
        return True
    searches = [
        criteria(b"NOT DRAFT", header_or(_ROUND_ONE_FIELDS, batch))
        for batch in or_batches(values, _ROUND_ONE_FIELDS, max_values=THREAD_SEED_IDS)
    ]
    wanted = frozenset(ids)
    return _round_pass(
        session, searches, query, state, seed, soft_check, reserve_spent, ancestors=wanted, linked_to=wanted
    )


def _round_two(
    session: ImapSession,
    query: ThreadQuery,
    state: _Members,
    rounds: _Rounds,
    seed: _Seed | None,
    soft_check: Callable[[], None],
    reserve_spent: Callable[[], bool],
) -> None:
    """Round (2), at most MAX_ROUND_ITERATIONS iterations: downward `HEADER In-Reply-To` keys for the Message-IDs of
    the members new in the last pass (iteration 1: every member so far, the seed included; A-mf2), then upward
    `HEADER Message-ID` keys for each new member's In-Reply-To and References root not yet a member or searched
    (E-mf4, A-mf4) and, in iteration 1, the seed's own id, so that its copies are found and merged (MF-T8-1),
    batched by `mixed_batches` (downward first; overflow goes to further SEARCHes, A-mf4a). A hit is a member when
    its Message-ID is an upward key or its In-Reply-To or References name a downward key. Ends when an iteration adds
    no member; keys left after the last iteration are never searched (not exact); the reserve stops the rounds
    (`_round_pass`)."""
    new = list(state.copies)
    for iteration in range(MAX_ROUND_ITERATIONS + 1):
        down, up = _round_keys(new, state, rounds)
        if iteration == 0:
            own = [mid for copy in new if copy.seed and (mid := valid_message_id(copy.summary.meta.message_id))]
            up = [*(mid for mid in own if mid not in rounds.up and mid not in up), *up]
        if not down and not up:
            return
        if iteration == MAX_ROUND_ITERATIONS:
            state.inexact()
            return
        rounds.down.update(down)
        rounds.up.update(up)
        keys: list[MixedKey] = [("In-Reply-To", value) for value in _searchable(down, state)]
        keys += [("Message-ID", value) for value in _searchable(up, state)]
        if not keys:
            return
        searches = [
            criteria(b"NOT DRAFT", mixed_or(batch)) for batch in mixed_batches(keys, max_keys=THREAD_ROUND_KEYS)
        ]
        before = len(state.copies)
        if not _round_pass(
            session,
            searches,
            query,
            state,
            seed,
            soft_check,
            reserve_spent,
            ancestors=frozenset(up),
            linked_to=frozenset(down),
        ):
            return
        new = state.copies[before:]
        if not new:
            return


def _round_keys(new: Sequence[ThreadCopy], state: _Members, rounds: _Rounds) -> tuple[list[str], list[str]]:
    """(downward, upward) ids of an iteration: the new members' Message-IDs not searched as In-Reply-To yet, and per
    new member at most two upward ids, its In-Reply-To and its References root, that are neither a member's id nor
    searched as a Message-ID yet."""
    members = {mid for copy in state.copies if (mid := valid_message_id(copy.summary.meta.message_id)) is not None}
    down: dict[str, None] = {}
    up: dict[str, None] = {}
    for copy in new:
        meta = copy.summary.meta
        own = valid_message_id(meta.message_id)
        if own is not None and own not in rounds.down:
            down[own] = None
        root = meta.thread.value if meta.thread is not None and meta.thread.source == "headers" else None
        for value in (meta.in_reply_to, root):
            parent = valid_message_id(value) if isinstance(value, str) else None
            if parent is not None and parent not in members and parent not in rounds.up:
                up[parent] = None
    return list(down), list(up)


def _searchable(ids: Sequence[str], state: _Members) -> list[str]:
    """The ids a search may carry (D74, T4-1), filtered before batching so that one refused id does not cost the
    others; a refused id is never searched, so the count is not exact."""
    values = [value for value in ids if search_value_ok(value) and "<" not in value and ">" not in value]
    if len(values) < len(ids):
        state.inexact()
    return values


def _round_folders(session: ImapSession, query: ThreadQuery) -> list[FolderEntry]:
    """Inbox, sent, archive and, with `include_trash`, trash; never `other`, junk or drafts. The selected mailbox
    goes first (one EXAMINE fewer per pass, im-M7). The folder map is never rebuilt here (K7, F6)."""
    folder_map = session.folder_map()
    roles = _THREAD_ROLES if query.include_trash else _THREAD_ROLES[:-1]
    entries = [entry for role in roles if (entry := folder_map.role(role)) is not None]
    return sorted(entries, key=lambda entry: entry.raw_name != session.selected)  # stable


def _round_pass(
    session: ImapSession,
    searches: Sequence[bytes],
    query: ThreadQuery,
    state: _Members,
    seed: _Seed | None,
    soft_check: Callable[[], None],
    reserve_spent: Callable[[], bool],
    *,
    ancestors: frozenset[str],
    linked_to: frozenset[str],
) -> bool:
    """The searches in every round folder. A folder that cannot be opened (EXAMINE NO, no UIDVALIDITY) or a SEARCH
    answered NO leaves the list partial (G4, P1); the hit cap leaves it inexact (P11). Locations read already are not
    fetched again (P7); each other hit is re-checked (`_linked`) and drafts are dropped (S1). Before every folder and
    every SEARCH the reserve is checked: once spent, the pass stops, the list is partial, and False is returned
    (G2, T8-1), since one iteration can hold dozens of SEARCHes per folder."""
    for entry in _round_folders(session, query):
        if reserve_spent():
            state.partial()
            return False
        if not _open(session, entry) or session.selected_uidvalidity is None:
            state.partial()
            continue
        validity = session.selected_uidvalidity
        for search in searches:
            if reserve_spent():
                state.partial()
                return False
            read = {uid for name, value, uid in state.seen if name == entry.raw_name and value == validity}
            rows = _uid_rows(
                session,
                search,
                _MEMBER_ITEMS,
                newest=True,
                miss=state.partial,
                soft_check=soft_check,
                capped=state.inexact,
                skip=read,
            )
            for uid, row in rows:
                if _is_draft(row):
                    continue
                ref = MessageRef(session.account, entry.raw_name, validity, uid)
                if _linked(session, ref, row, state, ancestors=ancestors, linked_to=linked_to):
                    _add(session, entry, validity, uid, row, state, linked_by="headers", seed=seed)
    return True


def _linked(
    session: ImapSession,
    ref: MessageRef,
    row: dict[bytes, object],
    state: _Members,
    *,
    ancestors: frozenset[str],
    linked_to: frozenset[str],
) -> bool:
    """Whether a round hit is a member: its Message-ID is one of `ancestors`, or its In-Reply-To or References name
    one of `linked_to`, as whole msg-id tokens compared exactly (`parse_descendant`, which trims a cut block), so
    substring, case, comment and quoted-string hits are not members. A cut block whose Message-ID did not end before
    the cut is never a member, and a cut block that shows no wanted id may hide one: both make the count inexact
    (A-sec3). A block that does not parse degrades only this hit (SC1)."""
    block = header_bytes(row)
    try:
        hit = parse_descendant(block, linked_to)
    except PASS_THROUGH:
        raise
    except Exception as exc:
        session.log_skipped(ref, exc)
        state.inexact()
        return False
    if hit.cut and (hit.message_id is None or not _field_complete(block, b"message-id")):
        state.inexact()
        return False
    if hit.referenced or (hit.message_id is not None and hit.message_id in ancestors):
        return True
    if hit.cut:
        state.inexact()
    return False


# --- subject fallback (other servers) --------------------------------------------------------------------------


def _anchor(state: _Members, *, root: str | None = None) -> tuple[ThreadCopy, str] | None:
    """The message (3) starts from and its subject digest, when the precondition of §6.5 (3) holds: every copy found
    is a copy of one message, the seed or, with a `headers` key, the member whose Message-ID is the root (Q5); it
    names no valid In-Reply-To or References id; no copy's block was cut, so no hidden References; and its
    normalised subject is not empty. The merged copy: its participants are a sent copy's when there is one."""
    merged = merge_copies(state.copies, include_trash=True)
    if len(merged) != 1 or state.cut_copies:
        return None
    anchor = merged[0]
    meta = anchor.summary.meta
    if not (anchor.seed if root is None else valid_message_id(meta.message_id) == root):
        return None
    if valid_message_id(meta.in_reply_to) is not None or meta.references:
        return None
    digest = subject_digest(anchor.summary.subject)
    return None if digest is None else (anchor, digest)


def _subject_members(
    session: ImapSession,
    query: ThreadQuery,
    state: _Members,
    anchor: ThreadCopy,
    digest: str,
    soft_check: Callable[[], None],
    reserve_spent: Callable[[], bool],
) -> None:
    """(3) from `anchor`: the round folders scanned for the days of its window, `SINCE <day - 31> BEFORE <day + 32>`
    (the exact ± 30 days are checked on INTERNALDATE), then the candidates joined (`_join`)."""
    day = anchor.summary.received_at.astimezone(UTC).date()
    folders = _round_folders(session, query)
    found = _subject_scan(
        session,
        folders,
        day - _SCAN_SINCE,
        day + _SCAN_BEFORE,
        digest=digest,
        state=state,
        scanned={},
        soft_check=soft_check,
        reserve_spent=reserve_spent,
    )
    _join(state, anchor, found)


def _subject_key_members(
    session: ImapSession,
    digest: str,
    query: ThreadQuery,
    state: _Members,
    soft_check: Callable[[], None],
    reserve_spent: Callable[[], bool],
) -> None:
    """A `subject` key runs (3) only (Q-d). The first scan sends `SINCE <today - 31>` without BEFORE (IR4); the newest
    match received within SUBJECT_WINDOW before the call is the seed (`linked_by: "subject"`, MF-P1). Then only the
    part of the seed's window the first scan did not cover, `[seed - 31, today - 31)`, is scanned, without the UIDs
    read already (im-M11), and the candidates of both scans are joined to the seed."""
    now = _utc_now()
    edge = now.date() - _SCAN_SINCE
    scanned: dict[tuple[str, int], set[int]] = {}
    found = _subject_scan(
        session,
        _round_folders(session, query),
        edge,
        None,
        digest=digest,
        state=state,
        scanned=scanned,
        soft_check=soft_check,
        reserve_spent=reserve_spent,
    )
    recent = [candidate for candidate in found if now - candidate.copy.summary.received_at <= SUBJECT_WINDOW]
    if not recent:
        return
    newest = max(recent, key=lambda candidate: order_key(candidate.copy.summary))
    seed = replace(newest.copy, seed=True)
    _take(state, newest, seed)
    since = seed.summary.received_at.astimezone(UTC).date() - _SCAN_SINCE
    if since < edge:
        found += _subject_scan(
            session,
            _round_folders(session, query),
            since,
            edge,
            digest=digest,
            state=state,
            scanned=scanned,
            soft_check=soft_check,
            reserve_spent=reserve_spent,
        )
    seed_id = valid_message_id(seed.summary.meta.message_id)
    copies = [
        seed,
        *(
            candidate.copy
            for candidate in found
            if candidate is not newest
            and seed_id is not None
            and valid_message_id(candidate.copy.summary.meta.message_id) == seed_id
        ),
    ]
    _join(state, merge_copies(copies, include_trash=True)[0], found)


def _subject_scan(
    session: ImapSession,
    folders: Sequence[FolderEntry],
    since: date,
    before: date | None,
    *,
    digest: str,
    state: _Members,
    scanned: dict[tuple[str, int], set[int]],
    soft_check: Callable[[], None],
    reserve_spent: Callable[[], bool],
) -> list[_Candidate]:
    """One `UID SEARCH NOT DRAFT SINCE … [BEFORE …]` per folder (`window_keys`; a subject is never a search value,
    D74), then the 8 KiB scan item of the highest UIDs, at most MAX_SUBJECT_CANDIDATES per folder over every scan of
    the call (Q4; more hits: not exact), in chunks of 50, each row reduced at once (`_candidate`, A-sec1). UIDs an
    earlier scan read (`scanned`, IR4) or that are members already (P7) are skipped. The reserve is checked before
    every folder and every SEARCH (G2, T8-1); EXAMINE or SEARCH NO, a folder without UIDVALIDITY, a lost row, the
    reserve and a soft stop leave the list partial (G4, P11). A SoftStop ends the scan with the candidates so far."""
    search = criteria(window_keys(since_day=since, before_day=before, unread_only=False))
    found: list[_Candidate] = []
    try:
        for entry in folders:
            if reserve_spent():
                state.partial()
                break
            if not _open(session, entry) or session.selected_uidvalidity is None:
                state.partial()
                continue
            validity = session.selected_uidvalidity
            done = scanned.setdefault((entry.raw_name, validity), set())
            left = MAX_SUBJECT_CANDIDATES - len(done)
            if left <= 0:
                continue  # an earlier scan read the folder's 500: it was capped, so not exact already
            if reserve_spent():
                state.partial()
                break
            read = done | {uid for name, value, uid in state.seen if name == entry.raw_name and value == validity}
            rows = _uid_rows(
                session,
                search,
                _SCAN_ITEMS,
                newest=True,
                miss=state.partial,
                soft_check=soft_check,
                capped=state.inexact,
                skip=read,
                limit=left,
            )
            for uid, row in rows:
                done.add(uid)
                candidate = _candidate(session, entry, validity, uid, row, state, digest=digest)
                if candidate is not None:
                    found.append(candidate)
    except SoftStop:
        state.partial()
    return found


def _candidate(
    session: ImapSession,
    entry: FolderEntry,
    uidvalidity: int,
    uid: int,
    row: dict[bytes, object],
    state: _Members,
    *,
    digest: str,
) -> _Candidate | None:
    """One scan row, unless it is a draft (S1) or its normalised subject digest differs. A block cut at 8 KiB is
    parsed without its last, possibly shortened token (T7-3): one whose Subject did not end before the cut, or that
    shows none, may hide a member, so it is no candidate and the count is not exact (fail safe); so is a matching
    row whose Message-ID did not end before the cut, or that shows none; a cut From gives the direction by folder.
    A matching row without INTERNALDATE is dropped with one `item_degraded` line and the count is not exact (Q9). A
    block that does not parse degrades only this row (SC1). The copy's thread key stays unknown until the body pass
    reads its References."""
    if _is_draft(row):
        return None
    ref = MessageRef(session.account, entry.raw_name, uidvalidity, uid)
    block = header_bytes(row)
    cut = len(block) >= SUBJECT_SCAN_BYTES
    try:
        parsed = parse_summary_headers(trim_cut(block) if cut else block)
    except PASS_THROUGH:
        raise
    except Exception as exc:
        session.log_skipped(ref, exc)
        state.inexact()
        return None
    if cut and (parsed.subject is None or not _field_complete(block, b"subject")):
        state.inexact()
        return None
    if subject_digest(parsed.subject) != digest:
        return None
    if cut and (parsed.message_id is None or not _field_complete(block, b"message-id")):
        state.inexact()
        return None
    received = _aware(row.get(b"INTERNALDATE"))
    if received is None:
        session.log_skipped(ref, MissingInternalDate())
        state.inexact()
        return None
    if cut and not _field_complete(block, b"from"):  # a From wholly past the cut is None already (MF-T9-2)
        parsed = replace(parsed, from_address=None, from_name=None)
    copy = _parsed_copy(session, entry, ref, received, row, parsed, linked_by="subject", seed=False)
    unkeyed = replace(copy.summary, meta=replace(copy.summary.meta, thread=None))
    return _Candidate(entry, (entry.raw_name, uidvalidity, uid), replace(copy, summary=unkeyed))


def _join(state: _Members, anchor: ThreadCopy, candidates: Sequence[_Candidate]) -> None:
    """(3)'s rule: a candidate received within SUBJECT_WINDOW of the anchor whose participants share an address with
    the anchor's, compared casefolded (F-1), joins with `linked_by: "subject"`. Only the anchor's participants
    count, never those of another subject member (Q3). A copy of the anchor (its valid Message-ID) joins as a copy,
    with the anchor's `linked_by`, so that the merge keeps one member."""
    anchor_id = valid_message_id(anchor.summary.meta.message_id)
    shared = {address.casefold() for address in anchor.participants}
    for candidate in candidates:
        copy = candidate.copy
        if candidate.location in state.seen:
            continue
        if anchor_id is not None and valid_message_id(copy.summary.meta.message_id) == anchor_id:
            copy = replace(copy, linked_by=anchor.linked_by)
        elif abs(copy.summary.received_at - anchor.summary.received_at) > SUBJECT_WINDOW or shared.isdisjoint(
            address.casefold() for address in copy.participants
        ):
            continue
        _take(state, candidate, copy)


def _take(state: _Members, candidate: _Candidate, copy: ThreadCopy) -> None:
    """A candidate becomes a copy of the thread; its summary is completed in the body pass (Option 6a)."""
    state.seen.add(candidate.location)
    state.provisional.add(candidate.location)
    state.entries[candidate.entry.raw_name] = candidate.entry
    state.copies.append(copy)


# --- body pass -------------------------------------------------------------------------------------------------


def _body_pass(
    session: ImapSession,
    kept: Sequence[ThreadCopy],
    entries: Mapping[str, FolderEntry],
    *,
    provisional: Collection[Location] = (),
    max_chars: int,
    soft_check: Callable[[], None],
) -> list[ThreadMember]:
    """Option 6a: the bodies of the kept members, one group per (mailbox, UIDVALIDITY), the group with the newest
    member first and the selected mailbox first on a tie (A-td3), so that a soft stop leaves the newest bodies read.
    Per group: EXAMINE unless selected, then its UIDVALIDITY is compared with `session.selected_uidvalidity`, also
    when the EXAMINE was skipped (IR5); another one or a failed EXAMINE leaves the group's bodies unread (im-M8).
    A body not read keeps `body_omitted` and, until its structure was read, `body_source: "none"` and
    `has_attachments: false` (A-td3a, Q-c). The `provisional` copies (from the subject scan) get their summary
    completed with their structure. A SoftStop leaves the rest unread; other errors propagate."""
    bodies: dict[AnyMessageRef, ThreadBody] = {}
    attachments: dict[AnyMessageRef, bool] = {}
    summaries: dict[AnyMessageRef, MailSummary] = {}
    groups: dict[tuple[str, int], list[tuple[MessageRef, ThreadCopy]]] = {}
    for copy in kept:
        if isinstance(copy.summary.ref, MessageRef):
            groups.setdefault((copy.summary.ref.folder, copy.summary.ref.uidvalidity), []).append(
                (copy.summary.ref, copy)
            )
    selected = session.selected
    ordered = sorted(
        groups.items(),
        key=lambda group: (
            -max(order_key(copy.summary)[0] for _, copy in group[1]),
            group[0][0] != selected,
        ),
    )
    try:
        for (raw_name, uidvalidity), group in ordered:
            entry = entries.get(raw_name)
            if entry is None or not _open(session, entry) or session.selected_uidvalidity != uidvalidity:
                continue
            group.sort(key=lambda member: order_key(member[1].summary), reverse=True)  # newest first
            _group_bodies(
                session,
                group,
                bodies,
                attachments,
                summaries,
                provisional=provisional,
                max_chars=max_chars,
                soft_check=soft_check,
            )
    except SoftStop:
        pass  # the bodies not read stay omitted (§5.4)
    return [
        ThreadMember(
            replace(
                summaries.get(copy.summary.ref, copy.summary), has_attachments=attachments.get(copy.summary.ref, False)
            ),
            copy.linked_by,
            bodies.get(copy.summary.ref, OMITTED_BODY),
        )
        for copy in kept
    ]


def _group_bodies(
    session: ImapSession,
    group: Sequence[tuple[MessageRef, ThreadCopy]],
    bodies: dict[AnyMessageRef, ThreadBody],
    attachments: dict[AnyMessageRef, bool],
    summaries: dict[AnyMessageRef, MailSummary],
    *,
    provisional: Collection[Location],
    max_chars: int,
    soft_check: Callable[[], None],
) -> None:
    """BODYSTRUCTURE per chunk of 10 UIDs, each reduced at once to the chosen text part and `has_attachments`; a chunk
    with a `provisional` copy also fetches the summary header item, which completes that copy's summary (Option 6a).
    Then one `UID FETCH BODY.PEEK[<section>]<0.65536>` per section; per member a soft check, then the body (`_body`).
    A member the FETCH did not return (NO, expunged) keeps its body omitted."""
    chosen: dict[str, list[tuple[MessageRef, ThreadCopy, Part]]] = {}
    for start in range(0, len(group), STRUCTURE_CHUNK_UIDS):
        if start:
            soft_check()  # reducing a chunk costs CPU without a command (sec-G5)
        chunk = group[start : start + STRUCTURE_CHUNK_UIDS]
        complete = {ref for ref, _ in chunk if (ref.folder, ref.uidvalidity, ref.uid) in provisional}
        items = ("BODYSTRUCTURE", SUMMARY_HEADER_ITEM) if complete else ("BODYSTRUCTURE",)
        rows = _structures(session, [ref for ref, _ in chunk], items)
        for ref, copy in chunk:
            row = rows.get(ref.uid)
            if row is None:  # FETCH NO, expunged, or a structure that cannot be parsed alone (P10)
                continue
            if ref in complete:
                summaries[ref] = _completed(session, ref, copy.summary, row)
            try:  # per member: an odd structure leaves only this body unread
                parts = walk(cast(Sequence[object], row.get(b"BODYSTRUCTURE", ())))
                part = choose_text_part(parts)
                has_attachments = bool(attachment_parts(parts, part))
            except PASS_THROUGH:
                raise
            except Exception as exc:
                session.log_skipped(ref, exc)
                continue
            attachments[ref] = has_attachments
            if part is None:  # no text part: nothing to read
                bodies[ref] = ThreadBody("", "none", "none", 0, False, False)
                continue
            bodies[ref] = _unread(part)
            chosen.setdefault(part.section, []).append((ref, copy, part))
    for section, members in chosen.items():
        data = session.fetch([ref.uid for ref, _, _ in members], [f"BODY.PEEK[{section}]<0.{THREAD_BODY_BYTES}>"])
        key = f"BODY[{section}]<0>".encode()
        for ref, copy, part in members:
            soft_check()  # decoding, conversion and quote removal cost CPU without a command (sec-G5)
            row = data.get(ref.uid)
            if row is None:
                continue
            raw = row.get(key)
            bodies[ref] = _body(
                session,
                ref,
                raw if isinstance(raw, bytes) else b"",
                part,
                subject=copy.summary.subject,
                max_chars=max_chars,
            )


def _structures(
    session: ImapSession, refs: Sequence[MessageRef], items: Sequence[str]
) -> dict[int, dict[bytes, object]]:
    """The `items` of one chunk: BODYSTRUCTURE, with the summary header item when the chunk holds a provisional copy.
    IMAPClient's recursive parser raises RecursionError on a structure nested about 1000 deep, failing the whole
    chunk; the chunk is then fetched one UID at a time, and only a UID that fails alone is missing (P10), with one
    `item_degraded` line."""
    try:
        return session.fetch([ref.uid for ref in refs], items)
    except RecursionError:
        rows: dict[int, dict[bytes, object]] = {}
        for ref in refs:
            try:
                rows.update(session.fetch([ref.uid], items))
            except RecursionError as exc:
                session.log_skipped(ref, exc)
        return rows


def _completed(session: ImapSession, ref: MessageRef, summary: MailSummary, row: dict[bytes, object]) -> MailSummary:
    """A subject member's In-Reply-To, References and thread key from its summary header item, which the 8 KiB scan
    item lacks (Option 6a). The other fields stay the scan item's, so the copy keeps the Message-ID and the
    participants it joined with. A block cut at 64 KiB is parsed without its last token (T7-3); a missing block, or
    one that does not parse (SC1), leaves the summary as it was."""
    block = header_bytes(row)
    if not block:
        return summary
    try:
        parsed = parse_summary_headers(trim_cut(block) if len(block) >= MAX_HEADER_BYTES else block)
    except PASS_THROUGH:
        raise
    except Exception as exc:
        session.log_skipped(ref, exc)
        return summary
    own = valid_message_id(summary.meta.message_id)
    root = parsed.root if parsed.root is not None and parsed.root != parsed.message_id else own
    keyed = replace(parsed, message_id=summary.meta.message_id, subject=summary.subject, root=root)
    meta = replace(
        summary.meta,
        in_reply_to=parsed.in_reply_to,
        references=parsed.references,
        thread=imap_thread_key(session.account, keyed),
    )
    return replace(summary, meta=meta)


def _source(part: Part) -> BodySource:
    return "text/html-converted" if part.mime_type == "text/html" else "text/plain"


def _unread(part: Part) -> ThreadBody:
    """A body whose structure was read but whose text was not: omitted, with its known source (Q-c)."""
    return ThreadBody("", _source(part), "none", None, False, True)


def _body(
    session: ImapSession, ref: MessageRef, raw: bytes, part: Part, *, subject: str | None, max_chars: int
) -> ThreadBody:
    """Decode, convert HTML, then `prepare_body` (§5.2 body order). `cut` when the part is larger than the fetch, or
    has no size and the fetch came back full. Any exception for one member omits only its body (SC1)."""
    try:
        text = decode_part(raw, part)
        if part.mime_type == "text/html":
            text = html_to_text(text)
        fetch_cut = part.size > THREAD_BODY_BYTES or (part.size <= 0 and len(raw) >= THREAD_BODY_BYTES)
        return prepare_body(text, _source(part), subject=subject, max_chars=max_chars, fetch_cut=fetch_cut)
    except PASS_THROUGH:
        raise
    except Exception as exc:
        session.log_skipped(ref, exc)
        return _unread(part)
