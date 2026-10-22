"""IMAP `get_thread` (spec 080 §5.2 `get_thread`, §5.5, §6.5; WP13c): the seed, the members, the merge and the body
pass, inside one `ImapSession` (one connection, one deadline, D76). Gmail finds the members by `X-GM-THRID` (Option
4a); other servers by the header rounds (T8) and the subject fallback (T9).

Read-only through the guard: EXAMINE, UID SEARCH and UID FETCH of FLAGS, INTERNALDATE, the summary header item,
X-GM-LABELS and X-GM-THRID (Gmail), BODYSTRUCTURE in chunks of at most 10 UIDs (A-sec1) and the chosen text part as
`BODY.PEEK[<section>]<0.65536>`. Nothing here is logged but `item_degraded`, through the session and capped per call.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
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
    parse_summary_headers,
)
from mcp_hub.providers.imap_guard import PASS_THROUGH
from mcp_hub.providers.imap_search import criteria, thrid_or
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
class _Members:
    """The copies found, where they lie, and what the member passes left incomplete (P11)."""

    copies: list[ThreadCopy] = field(default_factory=list)
    seen: set[Location] = field(default_factory=set)  # every location is read once (P7)
    entries: dict[str, FolderEntry] = field(default_factory=dict)  # raw name → entry, for the body pass
    exact: bool = True  # every search of every pass completed (`message_count_exact`)
    truncated: bool = False  # members may be missing: SEARCH or EXAMINE NO, the reserve, the soft stop

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
    members = _body_pass(session, kept, state.entries, max_chars=query.max_chars, soft_check=soft_check)
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


# --- seed ------------------------------------------------------------------------------------------------------


def _key_members(
    session: ImapSession,
    key: ThreadKey,
    query: ThreadQuery,
    state: _Members,
    soft_check: Callable[[], None],
    reserve_spent: Callable[[], bool],
) -> None:
    """The members from the key alone. Nothing found is `not_found` after a complete search and `upstream_timeout`
    otherwise; members found by an incomplete search are a partial answer (B6, Q6 as refined by IR3)."""
    value = key.value
    if key.source != "gmail" or not isinstance(value, int):
        raise ProviderError("upstream_error", "NotImplemented")  # `headers` and `subject` keys: WP13c T8, T9
    try:
        _gmail_members(session, value, query, state, None, soft_check, reserve_spent)
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
    a thread id is returned alone, not exact, with no key (SG4)."""
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
    state.inexact()  # Gmail: the seed alone (SG4); other servers: the header rounds follow (T8)
    if session.gmail:
        return None
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
    """The copy's summary (snippet empty: the body replaces it), participants and sent state. `has_attachments`
    stays false until the body pass reads its structure (Q-c)."""
    flags, labels = _flags(row), _labels(row)
    folder = _detail_folder(entry, row, gmail=session.gmail)
    parsed = _headers(session, ref, row, state, known_id=known_id)
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
    """The summary headers of a copy. A block cut at 64 KiB (IR2, MF-P2, A-sec3): a Message-ID that did not end
    before the cut is dropped (a `ref` seed keeps the ref's) and the count is not exact, since the copy cannot merge;
    a cut From gives the direction by folder. A block that does not parse degrades only this copy (SC1)."""
    block = header_bytes(row)
    try:
        parsed = parse_summary_headers(block)
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


# --- body pass -------------------------------------------------------------------------------------------------


def _body_pass(
    session: ImapSession,
    kept: Sequence[ThreadCopy],
    entries: Mapping[str, FolderEntry],
    *,
    max_chars: int,
    soft_check: Callable[[], None],
) -> list[ThreadMember]:
    """Option 6a: the bodies of the kept members, one group per (mailbox, UIDVALIDITY), the group with the newest
    member first and the selected mailbox first on a tie (A-td3), so that a soft stop leaves the newest bodies read.
    Per group: EXAMINE unless selected, then its UIDVALIDITY is compared with `session.selected_uidvalidity`, also
    when the EXAMINE was skipped (IR5); another one or a failed EXAMINE leaves the group's bodies unread (im-M8).
    A body not read keeps `body_omitted` and, until its structure was read, `body_source: "none"` and
    `has_attachments: false` (A-td3a, Q-c). A SoftStop leaves the rest unread; other errors propagate."""
    bodies: dict[AnyMessageRef, ThreadBody] = {}
    attachments: dict[AnyMessageRef, bool] = {}
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
            _group_bodies(session, group, bodies, attachments, max_chars=max_chars, soft_check=soft_check)
    except SoftStop:
        pass  # the bodies not read stay omitted (§5.4)
    return [
        ThreadMember(
            replace(copy.summary, has_attachments=attachments.get(copy.summary.ref, False)),
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
    *,
    max_chars: int,
    soft_check: Callable[[], None],
) -> None:
    """BODYSTRUCTURE per chunk of 10 UIDs, each reduced at once to the chosen text part and `has_attachments`; then
    one `UID FETCH BODY.PEEK[<section>]<0.65536>` per section; per member a soft check, then the body (`_body`). A
    member the FETCH did not return (NO, expunged) keeps its body omitted."""
    chosen: dict[str, list[tuple[MessageRef, ThreadCopy, Part]]] = {}
    for start in range(0, len(group), STRUCTURE_CHUNK_UIDS):
        if start:
            soft_check()  # reducing a chunk costs CPU without a command (sec-G5)
        chunk = group[start : start + STRUCTURE_CHUNK_UIDS]
        rows = _structures(session, [ref for ref, _ in chunk])
        for ref, copy in chunk:
            row = rows.get(ref.uid)
            if row is None:  # FETCH NO, expunged, or a structure that cannot be parsed alone (P10)
                continue
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


def _structures(session: ImapSession, refs: Sequence[MessageRef]) -> dict[int, dict[bytes, object]]:
    """BODYSTRUCTURE of one chunk. IMAPClient's recursive parser raises RecursionError on a structure nested about
    1000 deep, failing the whole chunk; the chunk is then fetched one UID at a time, and only a UID that fails alone
    is missing (P10), with one `item_degraded` line."""
    try:
        return session.fetch([ref.uid for ref in refs], ["BODYSTRUCTURE"])
    except RecursionError:
        rows: dict[int, dict[bytes, object]] = {}
        for ref in refs:
            try:
                rows.update(session.fetch([ref.uid], ["BODYSTRUCTURE"]))
            except RecursionError as exc:
                session.log_skipped(ref, exc)
        return rows


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
