"""IMAP session of one account and call (spec 080 §5.5): the guarded connection, the folder map, EXAMINE, SEARCH,
FETCH and the location step of Message-IDs. Moved out of `imap.py` so that the status lookups can share it (WP13b)."""

import imaplib
from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Final, Literal

from mcp_hub.ids import MessageRef
from mcp_hub.providers.base import ProviderError, SearchKey, log_message_skipped
from mcp_hub.providers.deadline import SoftStop
from mcp_hub.providers.folders import (
    MAX_LOCATE_FOLDERS,
    FolderEntry,
    FolderMap,
    FolderMapCache,
    MailFolder,
    build_imap_folder_map,
    inbox_entry,
    is_gmail,
)
from mcp_hub.providers.headers import (
    CANDIDATE_HEADER_BYTES,
    MAX_ADDRESS_HEADER_BYTES,
    candidate_header_item,
    header_bytes,
    parse_summary_headers,
    valid_message_id,
)
from mcp_hub.providers.imap_guard import PASS_THROUGH, GuardedImapClient, examine_name_ok, imap_error_kind
from mcp_hub.providers.imap_search import LOCATE_BATCH_VALUES, criteria, header_or, or_batches, search_value_ok

# The re-check item of a location hit: the Message-ID, with FROM 8 KiB so that a long From rarely cuts it (A-sec3).
_LOCATE_FROM_ITEM: Final = f"BODY.PEEK[HEADER.FIELDS (MESSAGE-ID FROM)]<0.{MAX_ADDRESS_HEADER_BYTES}>"
_GMAIL_LOCATE_ITEMS: Final = ("X-GM-LABELS", "X-GM-THRID")  # Gmail: drafts, `\Sent` and the thread id of a copy
_GMAIL_CANDIDATE_ITEM: Final = "X-GM-LABELS"  # Gmail: label drafts and `sent_label` before `limit` (I8, C4)
MAX_SEARCH_CANDIDATES: Final = 5000  # newest SEARCH hits per folder and call (§5.4, B5)
FETCH_CHUNK_UIDS: Final = 500  # UIDs per UID FETCH (the guard's MAX_FETCH_UIDS bounds the command line)
MAX_LOOKUP_HITS: Final = 500  # hits re-checked per reply/thread SEARCH batch and folder (C9)
LOOKUP_FETCH_UIDS: Final = 50  # UIDs per re-check FETCH: at most 3.2 MiB of 64 KiB header items per answer (C9)
SOFT_CHECK_ROWS: Final = 10  # fetched rows parsed between two soft checks (SR2: parsing sends no command)
SOFT_CHECK_EVERY: Final = 100  # candidates parsed between two soft-limit checks in pass 1 (CPU without commands)
_DRAFT: Final = frozenset({b"\\draft", b"\\drafts"})  # any case (I8); as a flag and as a Gmail label (S-r)
_LABEL_INBOX: Final = b"\\inbox"
_LABEL_SENT: Final = b"\\sent"
_THRID_LIMIT: Final = 2**64  # X-GM-THRID is an unsigned 64-bit number
_FOLDED: Final = (b" ", b"\t")  # a line starting with either continues the field above it


class MissingInternalDate(Exception):  # noqa: N818
    """A SEARCH hit without a usable INTERNALDATE: dropped and logged by this class name only (I11)."""


def _answered_no(exc: imaplib.IMAP4.error) -> bool:
    """True for a tagged NO. Every other IMAP error (BAD, a parse error) is the caller's to re-raise (S-p, I9)."""
    return imap_error_kind(exc) == "no"


def _aware(value: object) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _flags(row: dict[bytes, object]) -> frozenset[bytes]:
    flags = row.get(b"FLAGS", ())
    if not isinstance(flags, (tuple, list)):
        return frozenset()
    return frozenset(flag.lower() for flag in flags if isinstance(flag, bytes))


def _labels(row: dict[bytes, object]) -> frozenset[bytes]:
    """Gmail `X-GM-LABELS`, lowercased; empty on other servers (never fetched there)."""
    labels = row.get(b"X-GM-LABELS", ())
    if not isinstance(labels, (tuple, list)):
        return frozenset()
    out: set[bytes] = set()
    for label in labels:
        if isinstance(label, str):
            label = label.encode("utf-8", "replace")
        if isinstance(label, bytes):
            out.add(label.lower())
    return frozenset(out)


def _is_draft(row: dict[bytes, object]) -> bool:
    """`\\Draft` or `\\Drafts` in any case, as a flag or (Gmail) as a label (S1, S-r)."""
    return bool((_flags(row) | _labels(row)) & _DRAFT)


def _thread_id(row: dict[bytes, object]) -> int | None:
    """Gmail `X-GM-THRID` as the server returned it: an `int` (not `bool`) in 0..2^64-1, else None."""
    value = row.get(b"X-GM-THRID")
    return value if type(value) is int and 0 <= value < _THRID_LIMIT else None


def _field_complete(block: bytes, name: bytes) -> bool:
    """Whether the first `name` field of a header block cut at its cap ended before the cut: its last line (folded
    lines included) is CRLF-terminated and followed by the start of another line that is not a continuation. A
    block that ends right after a CRLF may hide a folded line, so its last field is not complete. A field the block
    does not hold counts as complete. `name` is lowercase, without the colon."""
    lines = block.split(b"\r\n")  # the last element is the text after the last CRLF (the cut)
    for index, line in enumerate(lines):
        if line[:1] in _FOLDED or line.partition(b":")[0].strip().lower() != name:
            continue
        end = index + 1
        while end < len(lines) and lines[end][:1] in _FOLDED:
            end += 1
        return end < len(lines) - 1 or (end == len(lines) - 1 and lines[end] != b"")
    return True


def locate_items(*, gmail: bool, with_from: bool) -> list[str]:
    """FETCH items of the location re-check: FLAGS, INTERNALDATE and the Message-ID header (2 KiB), with FROM in
    8 KiB (A-sec3); Gmail also its labels and thread id."""
    header = _LOCATE_FROM_ITEM if with_from else candidate_header_item(["MESSAGE-ID"])
    return ["FLAGS", "INTERNALDATE", header, *(_GMAIL_LOCATE_ITEMS if gmail else ())]


def _gmail_folder(labels: frozenset[bytes]) -> MailFolder:
    """All Mail decides per message (§5.5): `\\Inbox` → inbox, `\\Sent` → sent, else archive (drafts are dropped
    before this)."""
    if _LABEL_INBOX in labels:
        return "inbox"
    return "sent" if _LABEL_SENT in labels else "archive"


@dataclass(frozen=True, slots=True)
class FoundCopy:
    """One located copy of a Message-ID: where it is, when it arrived, and what the status needs of it."""

    entry: FolderEntry
    uidvalidity: int
    uid: int
    received_at: datetime
    folder: MailFolder  # normalised; Gmail All Mail by the copy's labels (`_detail_folder`)
    flags: frozenset[bytes] = frozenset()  # lowercased
    labels: frozenset[bytes] = frozenset()  # Gmail X-GM-LABELS, lowercased
    from_address: str | None = None  # only when FROM was fetched (own addresses configured)
    thread_id: int | None = None  # Gmail X-GM-THRID


@dataclass(frozen=True, slots=True)
class LocateResult:
    # Message-ID → copy with the earliest received_at; ties by folder rank (spec §5.5 matching rule, C3)
    found: dict[str, FoundCopy]
    complete: bool  # not incomplete and not uncertain
    incomplete: frozenset[str]  # not found, and a step that covered it did not complete (WP13b: incomplete_search)
    uncertain: frozenset[str]  # found, but a failed step may hide an earlier copy (S-e): not a reliable answer
    in_sent: frozenset[str]  # any accepted copy in sent (Gmail: label `\Sent`), for `direction` (MF-R2)


_NO_SEEDS: Final[Mapping[str, FoundCopy]] = MappingProxyType({})


def _replaces(copy: FoundCopy, current: FoundCopy | None, rank: Mapping[str, int]) -> bool:
    """Whether `copy` beats `current` by `(received_at, folder rank)`. The rank is the index in the core list of
    `FolderMap.locate_order` (the tie order of §5.5); any other mailbox ranks after it (IR3). The same copy (folder,
    UIDVALIDITY and UID) never replaces itself, so a seed read in place stays as read."""
    if current is None:
        return True
    if (copy.entry.raw_name, copy.uidvalidity, copy.uid) == (current.entry.raw_name, current.uidvalidity, current.uid):
        return False
    after = len(rank)
    return (copy.received_at, rank.get(copy.entry.raw_name, after)) < (
        current.received_at,
        rank.get(current.entry.raw_name, after),
    )


class ImapSession:
    """One guarded connection of one account for one tool call (D76); WP13b to WP13d reuse it.

    The session is typed on the guard only: no raw client is reachable from here (Sec13)."""

    def __init__(
        self,
        client: GuardedImapClient,
        *,
        account: str,
        registry_inbox: str,
        folder_maps: FolderMapCache,
        own: frozenset[str],
    ) -> None:
        self._client = client
        self.account = account
        self.registry_inbox = registry_inbox
        self.own = own
        self._maps = folder_maps
        self.gmail: bool = is_gmail(client.capabilities())  # cached at LOGIN: no command
        self._map: FolderMap | None = None
        self._listed = False
        self._selected: str | None = None
        self._selected_uidvalidity: int | None = None
        self._degraded_left: int | None = None  # item_degraded lines this call may still log; None: no cap
        self._refused: dict[str, None] = {}  # causes of the commands the server answered NO, first seen first (#52)

    def cap_degraded_logs(self, limit: int) -> None:
        """At most `limit` item_degraded lines for the rest of this call (SR6, `get_message_status`)."""
        self._degraded_left = limit

    def log_skipped(self, ref: MessageRef, exc: Exception) -> None:
        """`log_message_skipped` for a mail message, within the cap of `cap_degraded_logs`."""
        if self._degraded_left is not None:
            if self._degraded_left <= 0:
                return
            self._degraded_left -= 1
        log_message_skipped(ref, "mail", exc)

    def note_refused(self, cause: str) -> None:
        """Remembers that the server answered an EXAMINE, UID SEARCH or UID FETCH with NO in this call (#52)."""
        self._refused.setdefault(cause, None)

    @property
    def refused(self) -> tuple[str, ...]:
        """The distinct causes (`ExamineRefused`, `SearchRefused`, `FetchRefused`) in the order first seen."""
        return tuple(self._refused)

    @property
    def inbox(self) -> FolderEntry:
        """The registry inbox; needs no LIST (§5.5)."""
        return self._map.inbox if self._map is not None else inbox_entry(self.registry_inbox)

    @property
    def selected(self) -> str | None:
        """Raw name of the mailbox the last successful EXAMINE opened."""
        return self._selected

    @property
    def selected_uidvalidity(self) -> int | None:
        """UIDVALIDITY the last EXAMINE reported for the selected mailbox; None when it had none or the EXAMINE failed
        (P1, IR5: compared also when an EXAMINE is skipped because the mailbox is still selected)."""
        return self._selected_uidvalidity

    @property
    def listed(self) -> bool:
        """True once this session has sent `LIST`: its map is current, so a forced rebuild would gain nothing."""
        return self._listed

    def folder_map(self, *, refresh: bool = False) -> FolderMap:
        """The account's folder map: the one built in this session, else the cached one, else one `LIST "" "*"`.
        `refresh=True` lists again; callers ask `FolderMapCache.may_force_rebuild` first (Sec16)."""
        if not refresh:
            if self._map is not None:
                return self._map
            cached = self._maps.get(self.account)
            if (
                isinstance(cached, FolderMap)
                and cached.gmail == self.gmail
                and cached.inbox.raw_name == self.registry_inbox
            ):
                self._map = cached
                return cached
        built = build_imap_folder_map(
            self.account, self.registry_inbox, gmail=self.gmail, listing=self._client.list_folders()
        )
        self._listed = True
        self._maps.put(built)
        self._map = built
        return built

    def may_force_rebuild(self) -> bool:
        return self._maps.may_force_rebuild(self.account)

    def examine(self, entry: FolderEntry) -> int | None:
        """EXAMINE; the UIDVALIDITY, or None when a folder other than the registry inbox has none (I11). A missing
        UIDVALIDITY on the registry inbox is `upstream_error`. IMAP errors propagate to the caller."""
        self._selected = None  # a failed EXAMINE leaves no mailbox selected (RFC 3501 6.3.1)
        self._selected_uidvalidity = None
        info = self._client.examine(entry.raw_name)
        self._selected = entry.raw_name
        value = info.get(b"UIDVALIDITY")
        if type(value) is int and 0 < value < 2**32:
            self._selected_uidvalidity = value
            return value
        if entry.raw_name == self.registry_inbox:
            raise ProviderError("upstream_error", "MissingUidValidity")
        return None

    def search(self, criteria: bytes) -> list[int]:
        return self._client.search(criteria)

    def search_values(self, prefix: bytes, values: Sequence[tuple[SearchKey, str]]) -> list[int]:
        return self._client.search_values(prefix, values)

    def fetch(
        self, uids: Sequence[int], items: Sequence[str], *, on_no: Literal["drop", "fail"] = "drop"
    ) -> dict[int, dict[bytes, object]]:
        """UID FETCH in ascending chunks of at most 500 UIDs. A chunk answered NO is dropped: the UIDs it did not
        return are missing from the result (S-p). With `on_no="fail"` a NO is `upstream_timeout` instead (a single
        message read, where a missing row would otherwise read as "not found", B6). Every other error propagates."""
        ordered = sorted(set(uids))
        rows: dict[int, dict[bytes, object]] = {}
        for start in range(0, len(ordered), FETCH_CHUNK_UIDS):
            try:
                rows.update(self._client.fetch(ordered[start : start + FETCH_CHUNK_UIDS], items))
            except PASS_THROUGH:
                raise
            except imaplib.IMAP4.error as exc:
                if not _answered_no(exc):
                    raise
                self.note_refused("FetchRefused")
                if on_no == "fail":
                    raise ProviderError("upstream_timeout", "FetchRefused") from None
        return rows

    def fetch_chunks(
        self, uids: Sequence[int], items: Sequence[str], *, chunk: int, soft_check: Callable[[], None] | None = None
    ) -> Iterator[dict[int, dict[bytes, object]]]:
        """UID FETCH in ascending chunks of at most `chunk` (≤ 500) UIDs, one answer per chunk, fetched only when the
        caller asks for it, so that rows are reduced per chunk and never accumulated. A chunk answered NO yields `{}`:
        the caller sees its UIDs as lost (S-p). `soft_check` runs before each chunk after the first (a SoftStop ends
        the iteration). The generator drops its reference to a chunk once the caller asks for the next one. Every
        other error propagates."""
        if type(chunk) is not int or not 1 <= chunk <= FETCH_CHUNK_UIDS:
            raise ValueError("chunk out of range")
        ordered = sorted(set(uids))
        for start in range(0, len(ordered), chunk):
            if start and soft_check is not None:
                soft_check()
            try:
                rows = self._client.fetch(ordered[start : start + chunk], items)
            except PASS_THROUGH:
                raise
            except imaplib.IMAP4.error as exc:
                if not _answered_no(exc):
                    raise
                self.note_refused("FetchRefused")
                rows = {}
            yield rows
            rows = {}  # the caller has reduced the chunk: do not hold it while the next one is fetched

    def locate(
        self,
        message_ids: Sequence[str],
        *,
        seeds: Mapping[str, FoundCopy] = _NO_SEEDS,
        soft_check: Callable[[], None],
        max_folders: int = MAX_LOCATE_FOLDERS,
        batch_values: int = LOCATE_BATCH_VALUES,
    ) -> LocateResult:
        """The location step of spec §5.5 (matching rule, C3) and §5.2 (`get_message_status` lookups).

        Every core folder of `FolderMap.locate_order` is searched for every ref, so the copies there are compared,
        with the reply mailbox last (sent; Gmail: All Mail), so that the reply lookup continues in it (E-im5); the
        `other` mailboxes follow in LIST order, only for refs still unresolved, until none is left. At most
        `max_folders` mailboxes in all (core first). Per mailbox: EXAMINE, then per batch of at most `batch_values`
        Message-IDs (and 4,000 bytes of keys) one `UID SEARCH NOT DRAFT` with OR-ed `HEADER Message-ID` keys, then
        `UID FETCH` of `locate_items` for the hits in chunks of 500, each reduced before the next; a hit counts only
        on exact equality and without a draft flag or label. `soft_check` (raises SoftStop) runs every
        `SOFT_CHECK_EVERY` rows parsed and before every further chunk, since parsing sends no command the guard could
        stop (SR2); a search whose last chunk is parsed is kept. Copies compare by `(received_at, folder rank)`, the
        rank being the core list's tie order, so the search order never decides a tie. A SoftStop ends the search
        early; other errors propagate.

        `seeds` are copies read in place (an `m` id; E-im4): each enters `found` first and competes like any copy,
        ranked after the core folders when its mailbox is not one of them (IR3); a seeded ref counts as resolved
        for the `other` mailboxes. Every seed must name one of `message_ids`.

        A ref is "missed" when a step that covered it did not complete: a refused value, the folder cap, the
        folder map's mailbox cap, an EXAMINE or SEARCH answered NO, a FETCH that lost hits, came back without
        the Message-ID header or with a header block at its cap whose Message-ID did not end before the cut
        (A-sec3), or the soft stop. A missed ref is `incomplete` when it was not found and `uncertain` when it was
        (B6, B7); a seed thus stands. A copy without INTERNALDATE is dropped like a non-match (S-p)."""
        if type(max_folders) is not int or not 1 <= max_folders <= MAX_LOCATE_FOLDERS:
            raise ValueError("max_folders out of range")
        if type(batch_values) is not int or not 1 <= batch_values <= LOCATE_BATCH_VALUES:
            raise ValueError("batch_values out of range")
        wanted = list(dict.fromkeys(message_ids))
        if not seeds.keys() <= set(wanted):
            raise ValueError("seed for a Message-ID that is not located")
        # A pattern-valid value that search_value_ok refuses (one containing `X-GM-RAW`) can never be searched: it
        # stays incomplete, so get_message answers upstream_timeout for it every time (accepted, never not_found).
        searchable = [v for v in wanted if valid_message_id(v) is not None and search_value_ok(v)]
        missed = set(wanted) - set(searchable)
        found: dict[str, FoundCopy] = dict(seeds)
        in_sent = {message_id for message_id, copy in seeds.items() if _in_sent(copy)}
        refs = searchable  # the refs of the mailbox being searched: a soft stop leaves them open
        if searchable:
            try:
                folder_map = self.folder_map()
                core, other = folder_map.locate_order()
                rank = {entry.raw_name: index for index, entry in enumerate(core)}
                reply = "all" if self.gmail and any(entry.role == "all" for entry in core) else "sent"
                search = sorted(core, key=lambda entry: entry.role == reply)  # stable: the reply mailbox last
                ordered = [(entry, True) for entry in search] + [(entry, False) for entry in other]
                for index, (entry, is_core) in enumerate(ordered):
                    refs = searchable if is_core else [v for v in searchable if v not in found]
                    if not refs:
                        break  # every ref resolved: no further `other` mailbox
                    if index >= max_folders:
                        missed.update(refs)
                        break
                    self._locate_in(entry, refs, batch_values, found, missed, in_sent, rank, soft_check)
                else:
                    if folder_map.capped:  # mailboxes the map dropped were never searched for the open refs
                        missed.update(v for v in searchable if v not in found)
            except SoftStop:  # core: every ref was open; `other`: the refs unresolved before this mailbox, as a copy
                missed.update(refs)  # found in a chunk of it may have an earlier one in a chunk not read

        incomplete = frozenset(missed - found.keys())
        uncertain = frozenset(missed & found.keys())
        return LocateResult(
            found=found,
            complete=not incomplete and not uncertain,
            incomplete=incomplete,
            uncertain=uncertain,
            in_sent=frozenset(in_sent),
        )

    def _locate_in(
        self,
        entry: FolderEntry,
        refs: list[str],
        batch_values: int,
        found: dict[str, FoundCopy],
        missed: set[str],
        in_sent: set[str],
        rank: Mapping[str, int],
        soft_check: Callable[[], None],
    ) -> None:
        if entry.raw_name != self.registry_inbox and not examine_name_ok(entry.raw_name):
            missed.update(refs)  # over 1,000 characters: the guard would refuse the EXAMINE
            return
        try:
            validity = self.examine(entry)
        except PASS_THROUGH:
            raise
        except imaplib.IMAP4.error as exc:
            if not _answered_no(exc):
                raise
            self.note_refused("ExamineRefused")
            if entry.raw_name != self.registry_inbox and not self.listed and self.may_force_rebuild():
                self.folder_map(refresh=True)  # one rebuild for the next call; this mailbox stays unsearched (S-p)
            missed.update(refs)
            return
        if validity is None:  # missing UIDVALIDITY on a folder other than the registry inbox (I11)
            missed.update(refs)
            return
        items = locate_items(gmail=self.gmail, with_from=bool(self.own))
        cap = MAX_ADDRESS_HEADER_BYTES if self.own else CANDIDATE_HEADER_BYTES
        for batch in or_batches(refs, ("Message-ID",), max_values=batch_values):
            try:
                hits = self.search(criteria(b"NOT DRAFT", header_or(("Message-ID",), batch)))
            except PASS_THROUGH:
                raise
            except imaplib.IMAP4.error as exc:
                if not _answered_no(exc):
                    raise
                self.note_refused("SearchRefused")
                missed.update(batch)  # only the refs of this batch (B7)
                continue
            uids = sorted(set(hits))
            if len(uids) > MAX_SEARCH_CANDIDATES:  # substring hits of short values: the newest are re-checked
                uids = uids[-MAX_SEARCH_CANDIDATES:]
                missed.update(batch)
            wanted = set(batch)
            chunks = self.fetch_chunks(uids, items, chunk=FETCH_CHUNK_UIDS)
            for start, rows in zip(range(0, len(uids), FETCH_CHUNK_UIDS), chunks, strict=True):
                part = uids[start : start + FETCH_CHUNK_UIDS]
                if len(rows.keys() & set(part)) < len(part):  # FETCH NO or expunged meanwhile: a hit is unchecked
                    missed.update(batch)
                for count, uid in enumerate(part):
                    if count and count % SOFT_CHECK_EVERY == 0:
                        soft_check()  # header parsing costs CPU but sends no command the guard could stop (SR2)
                    row = rows.get(uid)
                    if row is None:
                        continue
                    block = header_bytes(row)
                    if not block:  # the Message-ID item did not come back: the hit cannot be re-checked
                        missed.update(batch)
                        continue
                    message_id, sender = _candidate_headers(row)
                    cut = len(block) >= cap
                    if cut and (message_id is None or not _field_complete(block, b"message-id")):
                        missed.update(batch)  # the Message-ID did not end before the cut: unchecked (A-sec3)
                        continue
                    if cut and not _field_complete(block, b"from"):
                        sender = None  # a cut From may be a prefix of the real address: the folder rule decides
                    if message_id is None or message_id not in wanted or _is_draft(row):
                        continue
                    received = _aware(row.get(b"INTERNALDATE"))
                    if received is None:  # S-p: dropped like a non-match, never a lasting upstream_timeout
                        self.log_skipped(MessageRef(self.account, entry.raw_name, validity, uid), MissingInternalDate())
                        continue
                    copy = FoundCopy(
                        entry,
                        validity,
                        uid,
                        received,
                        _detail_folder(entry, row, gmail=self.gmail),
                        flags=_flags(row),
                        labels=_labels(row),
                        from_address=sender,
                        thread_id=_thread_id(row),
                    )
                    if _in_sent(copy):
                        in_sent.add(message_id)
                    if _replaces(copy, found.get(message_id), rank):
                        found[message_id] = copy
                if start + FETCH_CHUNK_UIDS < len(uids):
                    soft_check()  # per chunk; after the last one the guard checks before the next command

    def noop(self) -> None:
        self._client.noop()


def _in_sent(copy: FoundCopy) -> bool:
    return copy.folder == "sent" or _LABEL_SENT in copy.labels


def _candidate_headers(row: dict[bytes, object]) -> tuple[str | None, str | None]:
    """(valid Message-ID, From address) of the candidate header block (at most 2 KiB, Sec5; 8 KiB in `locate`)."""
    try:
        parsed = parse_summary_headers(header_bytes(row))
    except PASS_THROUGH:
        raise
    except Exception:  # an unparsable block: never merged, direction by folder only
        return None, None
    return valid_message_id(parsed.message_id), parsed.from_address


_ROLE_FOLDER: Final[dict[str, MailFolder]] = {
    "inbox": "inbox",
    "sent": "sent",
    "archive": "archive",
    "trash": "trash",
    "other": "other",
}


def _detail_folder(entry: FolderEntry, row: dict[bytes, object], *, gmail: bool) -> MailFolder:
    """The normalised folder of a message read in `entry`: Gmail All Mail decides per message by its labels (§5.5);
    any other role no id can name fails closed."""
    if entry.role == "all" and gmail:
        return _gmail_folder(_labels(row))
    folder = _ROLE_FOLDER.get(entry.role)
    if folder is None:
        raise ProviderError("upstream_error", "UnmappedFolder")
    return folder


def _bound_entry(session: ImapSession, raw_name: str) -> FolderEntry:
    """D75, before EXAMINE: the registry inbox passes without LIST; any other name must be `allowed` in the
    folder map. A cached map that does not know the name is rebuilt once, when Sec16 allows it; a map listed
    in this call is current already."""
    if raw_name == session.registry_inbox:
        return session.inbox
    folder_map = session.folder_map()
    binding = folder_map.binding(raw_name)
    if binding == "unknown" and not session.listed and session.may_force_rebuild():
        folder_map = session.folder_map(refresh=True)
        binding = folder_map.binding(raw_name)
    entry = folder_map.lookup(raw_name)
    if binding != "allowed" or entry is None:
        raise ProviderError("not_found", "UnboundFolder")
    return entry


def _examine_bound(session: ImapSession, entry: FolderEntry) -> int | None:
    """EXAMINE for an id. A NO rebuilds the map once (S-p): a mailbox the new map no longer allows is
    `not_found`; otherwise the read did not complete (`upstream_timeout`, B6). A map listed in this call is not
    listed again (Sec16: the rebuild check consumes the window, so it comes last)."""
    try:
        return session.examine(entry)
    except PASS_THROUGH:
        raise
    except imaplib.IMAP4.error as exc:
        if not _answered_no(exc):
            raise
    if (
        entry.raw_name != session.registry_inbox
        and not session.listed
        and session.may_force_rebuild()
        and session.folder_map(refresh=True).binding(entry.raw_name) != "allowed"
    ):
        raise ProviderError("not_found", "UnboundFolder")  # deleted or no longer allowed: no refusal to note
    session.note_refused("ExamineRefused")
    raise ProviderError("upstream_timeout", "ExamineRefused")


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
        session.note_refused("ExamineRefused")
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
    for _, row in _uid_rows(session, search, items, newest=newest, miss=miss, soft_check=soft_check):
        yield row


def _uid_rows(
    session: ImapSession,
    search: bytes,
    items: Sequence[str],
    *,
    newest: bool,
    miss: Callable[[], None],
    soft_check: Callable[[], None],
    capped: Callable[[], None] | None = None,
    skip: Collection[int] = (),
    limit: int = MAX_LOOKUP_HITS,
) -> Iterator[tuple[int, dict[bytes, object]]]:
    """`_rows` with each row's UID (a fetched row does not carry it), for callers that build ids (WP13c). `capped`
    marks the hit cap instead of `miss` when given (P11); hits in `skip` were read already: they are neither fetched
    nor counted against the cap (P7); `limit` (1 to MAX_LOOKUP_HITS) lowers the cap for a folder whose budget an
    earlier search used in part (the subject scan, Q4)."""
    if type(limit) is not int or not 1 <= limit <= MAX_LOOKUP_HITS:
        raise ValueError("limit out of range")
    try:
        found = session.search(search)
    except PASS_THROUGH:
        raise
    except imaplib.IMAP4.error as exc:
        if not _answered_no(exc):
            raise
        session.note_refused("SearchRefused")
        miss()
        return
    uids = sorted(set(found).difference(skip))
    if len(uids) > limit:
        uids = uids[-limit:] if newest else uids[:limit]
        (miss if capped is None else capped)()
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
                yield uid, row
        if start + LOOKUP_FETCH_UIDS < len(uids):
            soft_check()  # per chunk; the guard checks again before the next FETCH
