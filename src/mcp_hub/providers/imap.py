"""Read-only IMAP adapter (spec 080 §5.1, §5.2, §5.5, §6.1, §6.5): EXAMINE and BODY.PEEK only, so \\Seen is never set.

Blocking; the tool layer runs it through providers.base.run_blocking with the CallDeadline it started. Every command
goes through `imap_guard.GuardedImapClient`; one connection per account and call covers the folders one after another
(D76). Returns decoded, unsanitised text.
"""

import contextlib
import email.policy
import imaplib
import ssl
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from email.headerregistry import AddressHeader
from email.message import Message
from email.parser import BytesHeaderParser
from email.utils import getaddresses
from typing import Final, Literal, cast

import imapclient
from imapclient.exceptions import LoginError

from mcp_hub.ids import AnyMessageRef, MessageRef, StableRef, ThreadKey
from mcp_hub.providers.base import (
    MAX_TEXT_PART_BYTES,
    PROVIDER_TIMEOUT_SECONDS,
    SNIPPET_FETCH_BYTES,
    AttachmentMeta,
    Direction,
    FolderPage,
    ListQuery,
    MailDetail,
    MailFlags,
    MailMeta,
    MailSummary,
    ProviderError,
    direction_of,
    log_message_skipped,
    order_key_of,
    placeholder_summary,
)
from mcp_hub.providers.deadline import CallDeadline, SoftStop
from mcp_hub.providers.folders import (
    FOLDER_MAPS,
    MAX_LOCATE_FOLDERS,
    FolderEntry,
    FolderMap,
    FolderMapCache,
    FolderPlan,
    ListFolder,
    MailFolder,
    build_imap_folder_map,
    inbox_entry,
    is_gmail,
)
from mcp_hub.providers.headers import (
    SUMMARY_HEADER_ITEM,
    ParsedHeaders,
    candidate_header_item,
    header_bytes,
    imap_thread_key,
    parse_summary_headers,
    valid_message_id,
)
from mcp_hub.providers.imap_guard import (
    PASS_THROUGH,
    GuardedImapClient,
    ImapClientLike,
    examine_name_ok,
    imap_error_kind,
)
from mcp_hub.providers.imap_search import (
    criteria,
    gmail_label_keys,
    header_or,
    or_batches,
    search_value_ok,
    window_keys,
)
from mcp_hub.providers.mime import Part, attachment_parts, choose_text_part, decode_part, walk
from mcp_hub.sanitize import html_to_text

LIST_SUMMARY_ITEMS: Final = ("FLAGS", "INTERNALDATE", "BODYSTRUCTURE", SUMMARY_HEADER_ITEM)  # §5.5 header sources
CANDIDATE_ITEMS: Final = ("INTERNALDATE", "FLAGS")  # FLAGS so that drafts drop before `limit`
LOCATE_ITEMS: Final = ("FLAGS", "INTERNALDATE", candidate_header_item(["MESSAGE-ID"]))  # the re-check of a hit
LOCATE_BATCH_VALUES: Final = 20  # Message-IDs per UID SEARCH in the location step (F6; or_batches bounds the bytes)
_GMAIL_CANDIDATE_ITEM: Final = "X-GM-LABELS"  # Gmail: label drafts and `sent_label` before `limit` (I8, C4)
_GMAIL_SUMMARY_ITEMS: Final = ("X-GM-LABELS", "X-GM-THRID")  # Gmail: per-message folder and thread (§5.5)
MAX_SEARCH_CANDIDATES: Final = 5000  # newest SEARCH hits per folder and call (§5.4, B5)
FETCH_CHUNK_UIDS: Final = 500  # UIDs per UID FETCH (the guard's MAX_FETCH_UIDS bounds the command line)
SOFT_CHECK_EVERY: Final = 100  # candidates parsed between two soft-limit checks in pass 1 (CPU without commands)
_SEEN: Final = b"\\seen"
_ANSWERED: Final = b"\\answered"
_FLAGGED: Final = b"\\flagged"
_DRAFT: Final = frozenset({b"\\draft", b"\\drafts"})  # any case (I8); as a flag and as a Gmail label (S-r)
_LABEL_INBOX: Final = b"\\inbox"
_LABEL_SENT: Final = b"\\sent"
_MAX_THRID: Final = 2**64
_PARSER: Final = BytesHeaderParser(policy=email.policy.default)

ClientFactory = Callable[[str, int, ssl.SSLContext, float], ImapClientLike]


def default_client(host: str, port: int, context: ssl.SSLContext, timeout: float) -> ImapClientLike:
    return cast(ImapClientLike, imapclient.IMAPClient(host, port=port, ssl=True, ssl_context=context, timeout=timeout))


class MissingInternalDate(Exception):  # noqa: N818
    """A SEARCH hit without a usable INTERNALDATE: dropped and logged by this class name only (I11)."""


def _provider_error(exc: Exception) -> ProviderError:
    if isinstance(exc, ProviderError):
        return exc
    cause = type(exc).__name__
    if isinstance(exc, SoftStop):  # a path without a partial result (R2b)
        return ProviderError("upstream_timeout", cause)
    if isinstance(exc, LoginError):
        # The server's response code decides; the text itself is never logged or returned.
        text = str(exc).upper()
        if "[UNAVAILABLE]" in text:
            return ProviderError("unreachable", cause)
        if "[" in text and not any(code in text for code in ("[AUTHENTICATIONFAILED]", "[AUTHORIZATIONFAILED]")):
            return ProviderError("upstream_error", cause)
        return ProviderError("auth_expired", cause)  # AUTHENTICATIONFAILED, AUTHORIZATIONFAILED or no code
    if isinstance(exc, TimeoutError):
        return ProviderError("upstream_timeout", cause)
    if isinstance(exc, OSError):  # DNS, connect, TLS (ssl.SSLError is an OSError)
        return ProviderError("unreachable", cause)
    return ProviderError("upstream_error", cause)  # IMAPClientError, a BAD answer, a refused command, anything else


def _answered_no(exc: imaplib.IMAP4.error) -> bool:
    """True for a tagged NO. Every other IMAP error (BAD, a parse error) is the caller's to re-raise (S-p, I9)."""
    return imap_error_kind(exc) == "no"


def _aware(value: object) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _addresses(message: Message, name: str) -> list[tuple[str, str]]:
    """(display name, address) pairs; falls back to the lenient stdlib parser for malformed headers."""
    header = message.get(name)
    if header is None:
        return []
    try:  # policy.default yields an AddressHeader; typeshed types Message.get as str
        return [(a.display_name, a.addr_spec) for a in cast(AddressHeader, header).addresses]
    except PASS_THROUGH:
        raise
    except Exception:  # malformed header: parse the raw text leniently
        return [(n, a) for n, a in getaddresses([str(header)]) if a or n]


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


def _unread(row: dict[bytes, object]) -> bool:
    return _SEEN not in _flags(row)


def _is_draft(row: dict[bytes, object]) -> bool:
    """`\\Draft` or `\\Drafts` in any case, as a flag or (Gmail) as a label (S1, S-r)."""
    return bool((_flags(row) | _labels(row)) & _DRAFT)


def _gmail_folder(labels: frozenset[bytes]) -> MailFolder:
    """All Mail decides per message (§5.5): `\\Inbox` → inbox, `\\Sent` → sent, else archive (drafts are dropped
    before this)."""
    if _LABEL_INBOX in labels:
        return "inbox"
    return "sent" if _LABEL_SENT in labels else "archive"


def _gmail_thread(account: str, row: dict[bytes, object]) -> ThreadKey | None:
    """The `gmail` thread key from `X-GM-THRID` (an unsigned 64-bit integer); no key without a valid one (D79)."""
    value = row.get(b"X-GM-THRID")
    if type(value) is int and 0 <= value < _MAX_THRID:
        return ThreadKey(account, "gmail", value)
    return None


def _structure(row: dict[bytes, object]) -> list[Part]:
    return walk(cast(Sequence[object], row.get(b"BODYSTRUCTURE", ())))


def _sender_and_subject(headers: Message) -> tuple[tuple[str, str], str | None]:
    """str() on an encoded word with an odd charset can raise, so callers guard this per message."""
    sender = (_addresses(headers, "From") or [("", "")])[0]
    return sender, str(headers.get("Subject", "")) or None


@dataclass(frozen=True, slots=True)
class Candidate:
    """One SEARCH hit kept for the choice of a page (Sec5: nothing else is kept per candidate)."""

    plan: FolderPlan
    uidvalidity: int
    uid: int
    received_at: datetime
    message_id: str | None
    from_address: str | None
    sent_label: bool  # Gmail only (conflict C4)


@dataclass(frozen=True, slots=True)
class _Chosen:
    """A candidate that passed every filter: its location, list-order key and what decides its direction."""

    candidate: Candidate
    ref: MessageRef
    key: tuple[int, str]  # base.order_key of the item: (received_at seconds, k)
    in_sent: bool  # in sent, or merged with a copy in sent (M3, S-j)
    direction: Direction | None  # decided from the candidate headers when `direction` filtered; else in pass 2


def _sent_since_pass_one(chosen: _Chosen, row: dict[bytes, object]) -> bool:
    """A Gmail item chosen as `incoming` (no `\\Sent` label in pass 1) that carries the label in pass 2: listing it
    would pair `folder: sent` with `direction: incoming`. Empty labels (other servers) never match."""
    return chosen.direction == "incoming" and _LABEL_SENT in _labels(row)


@dataclass(frozen=True, slots=True)
class FoundCopy:
    """One located copy of a Message-ID: where it is and when it arrived."""

    entry: FolderEntry
    uidvalidity: int
    uid: int
    received_at: datetime


@dataclass(frozen=True, slots=True)
class LocateResult:
    # Message-ID → copy with the earliest received_at; ties by folder order (spec §5.5 matching rule, C3)
    found: dict[str, FoundCopy]
    complete: bool  # not incomplete and not uncertain
    incomplete: frozenset[str]  # not found, and a step that covered it did not complete (WP13b: incomplete_search)
    uncertain: frozenset[str]  # found, but a failed step may hide an earlier copy (S-e): not a reliable answer


@dataclass(slots=True)
class _Progress:
    truncated: bool = False
    complete: bool = True


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

    @property
    def inbox(self) -> FolderEntry:
        """The registry inbox; needs no LIST (§5.5)."""
        return self._map.inbox if self._map is not None else inbox_entry(self.registry_inbox)

    @property
    def selected(self) -> str | None:
        """Raw name of the mailbox the last successful EXAMINE opened."""
        return self._selected

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
        info = self._client.examine(entry.raw_name)
        self._selected = entry.raw_name
        value = info.get(b"UIDVALIDITY")
        if type(value) is int and 0 < value < 2**32:
            return value
        if entry.raw_name == self.registry_inbox:
            raise ProviderError("upstream_error", "MissingUidValidity")
        return None

    def search(self, criteria: bytes) -> list[int]:
        return self._client.search(criteria)

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
                if on_no == "fail":
                    raise ProviderError("upstream_timeout", "FetchRefused") from None
        return rows

    def locate(
        self,
        message_ids: Sequence[str],
        *,
        max_folders: int = MAX_LOCATE_FOLDERS,
        batch_values: int = LOCATE_BATCH_VALUES,
    ) -> LocateResult:
        """The location step of spec §5.5 (matching rule, C3) and §5.2 (`get_message_status` lookups).

        Every core folder of `FolderMap.locate_order` is searched for every ref, so the copies there are compared;
        the `other` mailboxes follow in LIST order, only for refs still unresolved, until none is left. At most
        `max_folders` mailboxes in all (core first). Per mailbox: EXAMINE, then per batch of at most `batch_values`
        Message-IDs (and 4,000 bytes of keys) one `UID SEARCH NOT DRAFT` with OR-ed `HEADER Message-ID` keys, then
        `UID FETCH` of FLAGS, INTERNALDATE and the Message-ID header of the hits (Gmail: also X-GM-LABELS); a hit
        counts only on exact equality and without a draft flag or label. The earliest `received_at` wins; on a tie
        the first in folder order. A SoftStop ends the search early; other errors propagate.

        A ref is "missed" when a step that covered it did not complete: a refused value, the folder cap, the
        folder map's mailbox cap, an EXAMINE or SEARCH answered NO, a FETCH that lost hits or came back without
        the Message-ID header, or the soft stop. A missed ref is `incomplete` when it was not found and
        `uncertain` when it was (B6, B7). A copy without INTERNALDATE is dropped like a non-match (S-p)."""
        if type(max_folders) is not int or not 1 <= max_folders <= MAX_LOCATE_FOLDERS:
            raise ValueError("max_folders out of range")
        if type(batch_values) is not int or not 1 <= batch_values <= LOCATE_BATCH_VALUES:
            raise ValueError("batch_values out of range")
        wanted = list(dict.fromkeys(message_ids))
        # A pattern-valid value that search_value_ok refuses (one containing `X-GM-RAW`) can never be searched: it
        # stays incomplete, so get_message answers upstream_timeout for it every time (accepted, never not_found).
        searchable = [v for v in wanted if valid_message_id(v) is not None and search_value_ok(v)]
        missed = set(wanted) - set(searchable)
        found: dict[str, FoundCopy] = {}
        in_core = True
        if searchable:
            try:
                folder_map = self.folder_map()
                core, other = folder_map.locate_order()
                ordered = [(entry, True) for entry in core] + [(entry, False) for entry in other]
                for index, (entry, is_core) in enumerate(ordered):
                    in_core = is_core
                    refs = searchable if is_core else [v for v in searchable if v not in found]
                    if not refs:
                        break  # every ref resolved: no further `other` mailbox
                    if index >= max_folders:
                        missed.update(refs)
                        break
                    self._locate_in(entry, refs, batch_values, found, missed)
                else:
                    if folder_map.capped:  # mailboxes the map dropped were never searched for the open refs
                        missed.update(v for v in searchable if v not in found)
            except SoftStop:  # in the core folders every ref was still open; afterwards only the unresolved ones
                missed.update(searchable if in_core else [v for v in searchable if v not in found])
        incomplete = frozenset(missed - found.keys())
        uncertain = frozenset(missed & found.keys())
        return LocateResult(
            found=found, complete=not incomplete and not uncertain, incomplete=incomplete, uncertain=uncertain
        )

    def _locate_in(
        self, entry: FolderEntry, refs: list[str], batch_values: int, found: dict[str, FoundCopy], missed: set[str]
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
            if entry.raw_name != self.registry_inbox and not self.listed and self.may_force_rebuild():
                self.folder_map(refresh=True)  # one rebuild for the next call; this mailbox stays unsearched (S-p)
            missed.update(refs)
            return
        if validity is None:  # missing UIDVALIDITY on a folder other than the registry inbox (I11)
            missed.update(refs)
            return
        items = [*LOCATE_ITEMS, _GMAIL_CANDIDATE_ITEM] if self.gmail else list(LOCATE_ITEMS)
        for batch in or_batches(refs, ("Message-ID",), max_values=batch_values):
            try:
                hits = self.search(criteria(b"NOT DRAFT", header_or(("Message-ID",), batch)))
            except PASS_THROUGH:
                raise
            except imaplib.IMAP4.error as exc:
                if not _answered_no(exc):
                    raise
                missed.update(batch)  # only the refs of this batch (B7)
                continue
            uids = sorted(set(hits))
            if len(uids) > MAX_SEARCH_CANDIDATES:  # substring hits of short values: the newest are re-checked
                uids = uids[-MAX_SEARCH_CANDIDATES:]
                missed.update(batch)
            rows = self.fetch(uids, items) if uids else {}
            if len(rows.keys() & set(uids)) < len(uids):  # FETCH NO or expunged meanwhile: a hit is unchecked
                missed.update(batch)
            wanted = set(batch)
            for uid in uids:
                row = rows.get(uid)
                if row is None:
                    continue
                if not header_bytes(row):  # the Message-ID item did not come back: the hit cannot be re-checked
                    missed.update(batch)
                    continue
                message_id, _ = _candidate_headers(row)
                if message_id is None or message_id not in wanted or _is_draft(row):
                    continue
                received = _aware(row.get(b"INTERNALDATE"))
                if received is None:  # S-p: dropped like a non-match, never a lasting upstream_timeout
                    log_message_skipped(
                        MessageRef(self.account, entry.raw_name, validity, uid), "mail", MissingInternalDate()
                    )
                    continue
                current = found.get(message_id)
                if current is None or received < current.received_at:  # a tie keeps the earlier folder (S-e)
                    found[message_id] = FoundCopy(entry, validity, uid, received)

    def noop(self) -> None:
        self._client.noop()


Unparsable = dict[int, tuple[Exception, dict[bytes, object]]]


def _fetch_summaries(
    session: ImapSession, uids: list[int], items: Sequence[str], flag_items: Sequence[str]
) -> tuple[dict[int, dict[bytes, object]], Unparsable]:
    """Summary rows of `uids`. IMAPClient's recursive response parser raises RecursionError on a BODYSTRUCTURE
    nested about 1000 deep; the batch is then fetched per UID, and a UID that fails alone is returned as unparsable,
    with its `flag_items` (FLAGS; on Gmail also X-GM-LABELS) fetched alone, so only that message degrades (to a
    placeholder with the right `unread` and folder). A UID whose FLAGS do not come back either is missing from both
    results."""
    if not uids:
        return {}, {}
    try:
        return session.fetch(uids, items), {}
    except RecursionError:
        rows: dict[int, dict[bytes, object]] = {}
        unparsable: Unparsable = {}
        for uid in uids:
            try:
                rows.update(session.fetch([uid], items))
            except RecursionError as exc:
                flags = session.fetch([uid], flag_items).get(uid)
                if flags is not None:
                    unparsable[uid] = (exc, flags)
        return rows, unparsable


def _fetch_texts(
    session: ImapSession, rows: dict[int, dict[bytes, object]], length: int, refs: dict[int, MessageRef]
) -> dict[int, str]:
    """Partial fetch of each message's chosen text part, one FETCH per section number. Snippets are optional: a
    FETCH that fails without losing the connection or answering BAD leaves those snippets empty."""
    by_section: dict[str, list[tuple[int, Part]]] = {}
    for uid, row in rows.items():
        try:  # per UID: an empty or odd BODYSTRUCTURE leaves this snippet empty
            part = choose_text_part(_structure(row))
        except PASS_THROUGH:
            raise
        except Exception:  # noqa: S112 - the message is listed without a snippet
            continue
        if part is not None:
            by_section.setdefault(part.section, []).append((uid, part))
    texts: dict[int, str] = {}
    for section, members in by_section.items():
        try:
            data = session.fetch([uid for uid, _ in members], [f"BODY.PEEK[{section}]<0.{length}>"])
        except PASS_THROUGH:
            raise
        except Exception as exc:  # a protocol or parse error: no snippet for these messages; BAD fails the account
            if isinstance(exc, imaplib.IMAP4.error) and imap_error_kind(exc) == "bad":
                raise
            for uid, _ in members:
                log_message_skipped(refs[uid], "mail", exc)
            continue
        key = f"BODY[{section}]<0>".encode()
        for uid, part in members:
            try:  # per UID: a broken part only empties that message's snippet
                raw = data.get(uid, {}).get(key)
                text = decode_part(raw if isinstance(raw, bytes) else b"", part)
                texts[uid] = html_to_text(text) if part.mime_type == "text/html" else text
            except PASS_THROUGH:
                raise
            except Exception:
                texts[uid] = ""
    return texts


def _candidate_headers(row: dict[bytes, object]) -> tuple[str | None, str | None]:
    """(valid Message-ID, From address) of the candidate header block (at most 2 KiB, Sec5)."""
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


def _soft_stopped_page() -> FolderPage:
    return FolderPage(items=[], more=False, truncated=True, complete=False)


class ImapMailbox:
    def __init__(
        self,
        account_id: str,
        *,
        host: str,
        port: int,
        folder: str,
        username: str,
        password: str,
        ssl_context: ssl.SSLContext | None = None,
        timeout: float = PROVIDER_TIMEOUT_SECONDS,
        client_factory: ClientFactory = default_client,
        own_addresses: frozenset[str] = frozenset(),
        folder_maps: FolderMapCache = FOLDER_MAPS,
    ) -> None:
        self._account = account_id
        self._host, self._port, self._folder = host, port, folder
        self._username, self._password = username, password
        self._context = ssl_context or ssl.create_default_context()
        self._timeout = timeout
        self._factory = client_factory
        self._own_addresses, self._folder_maps = own_addresses, folder_maps
        self._deadline: CallDeadline | None = None  # stored per call; the guard applies it before every command

    def _connect(self, deadline: CallDeadline) -> GuardedImapClient:
        """The raw client is wrapped at once and never kept (Sec13); the same raw registry inbox goes to the guard
        and to the folder map."""
        raw = self._factory(self._host, self._port, self._context, self._timeout)
        try:
            raw.normalise_times = False  # keep the server's offset on INTERNALDATE
            return GuardedImapClient(raw, deadline, registry_inbox_raw=self._folder)
        except BaseException:  # cleanup and re-raise: nothing is swallowed here (Sec3)
            with contextlib.suppress(Exception):
                raw.logout()
            raise

    def _run[T](
        self, work: Callable[[ImapSession], T], deadline: CallDeadline, *, on_soft_stop: Callable[[], T] | None = None
    ) -> T:
        """`on_soft_stop` answers a soft stop that reaches this level (at LOGIN or the CAPABILITY after it, before
        any folder was read); without it the stop is `upstream_timeout` (R2b)."""
        self._deadline = deadline  # started by the tool layer (R2a); never started here
        client: GuardedImapClient | None = None
        try:
            client = self._connect(self._deadline)
            client.login(self._username, self._password)
            session = ImapSession(
                client,
                account=self._account,
                registry_inbox=self._folder,
                folder_maps=self._folder_maps,
                own=self._own_addresses,
            )
            return work(session)
        except SoftStop:
            if on_soft_stop is None:
                raise ProviderError("upstream_timeout", "SoftStop") from None
            return on_soft_stop()
        except Exception as exc:
            raise _provider_error(exc) from None
        finally:
            if client is not None:
                with contextlib.suppress(Exception):
                    client.logout()

    def _soft_check(self) -> None:
        if self._deadline is not None and self._deadline.soft_expired():
            raise SoftStop

    def check(self, deadline: CallDeadline) -> None:
        """Status check (spec 080 §7.4): login + NOOP."""
        self._run(lambda session: session.noop(), deadline)

    def list_messages(self, query: ListQuery, deadline: CallDeadline) -> FolderPage:
        return self._run(
            lambda session: self._list_messages(session, query),
            deadline,
            on_soft_stop=_soft_stopped_page,  # stopped before any folder answered: nothing read, no cursor (D76)
        )

    def get_message(self, target: AnyMessageRef | StableRef, deadline: CallDeadline) -> MailDetail:
        """By `id` (D75 binding) or by `ref` (the location step); a Graph id is foreign here."""
        if not isinstance(target, (MessageRef, StableRef)):
            raise ProviderError("not_found", "ForeignRef")
        own_target: MessageRef | StableRef = target
        return self._run(lambda session: self._get_message(session, own_target), deadline)

    # --- list_messages -----------------------------------------------------------------------------------------

    def _list_messages(self, session: ImapSession, query: ListQuery) -> FolderPage:
        """Pass 1 searches every folder of the plan and keeps the candidates; the page is chosen from all of them
        (R3); pass 2 fetches the summaries of the chosen items per folder. A soft stop returns what was built."""
        progress = _Progress()
        candidates: list[Candidate] = []
        try:
            plans = self._plans(session, query.folder)
            # None: Gmail All Mail, where X-GM-LABELS decide per message
            folder_of = {plan.entry.raw_name: plan.fixed_folder for plan in plans}
            merge = query.folder == "all" and len(plans) > 1
            for plan in plans:
                candidates.extend(self._candidates(session, plan, query, merge, progress))
        except SoftStop:
            return _soft_stopped_page()
        chosen, more = self._choose(candidates, query, merge)
        built: dict[MessageRef, MailSummary] = {}
        try:
            for entry, group in _by_folder(plans, chosen):
                built.update(self._summaries(session, entry, folder_of[entry.raw_name], group, progress))
        except SoftStop:  # SA13: pass 2 incomplete, so the page gets no cursor
            progress.truncated, progress.complete = True, False
        returned = [c for c in chosen if c.ref in built]
        frontier: tuple[int, str] | None = None
        if more:  # the whole window was scanned: the account's items continue below its last returned item
            last = returned[-1] if returned else chosen[-1]
            frontier = last.key
        return FolderPage(
            items=[built[c.ref] for c in returned],
            more=more,
            truncated=progress.truncated,
            complete=progress.complete,
            frontier=frontier,
        )

    @staticmethod
    def _plans(session: ImapSession, folder: ListFolder) -> list[FolderPlan]:
        if folder == "inbox":
            return [FolderPlan(session.inbox, None, "inbox")]
        return session.folder_map().list_plan(folder)

    def _candidates(
        self, session: ImapSession, plan: FolderPlan, query: ListQuery, merge: bool, progress: _Progress
    ) -> list[Candidate]:
        entry = plan.entry
        if entry.raw_name != session.registry_inbox and not examine_name_ok(entry.raw_name):
            progress.complete = False  # over 1,000 characters: the guard would refuse the EXAMINE
            return []
        try:
            validity = session.examine(entry)
        except PASS_THROUGH:
            raise
        except imaplib.IMAP4.error as exc:
            if not _answered_no(exc):
                raise
            if entry.raw_name != session.registry_inbox and session.may_force_rebuild():
                session.folder_map(refresh=True)  # one rebuild for the next call; this folder stays incomplete
            progress.complete = False
            return []
        if validity is None:  # missing UIDVALIDITY on a folder other than the registry inbox (I11)
            progress.complete = False
            return []
        try:
            found = session.search(_criteria(plan, query, merge))
        except PASS_THROUGH:
            raise
        except imaplib.IMAP4.error as exc:
            if not _answered_no(exc):
                raise
            progress.complete = False  # no cursor (S-p)
            return []
        newest = sorted(set(found), reverse=True)
        if len(newest) > MAX_SEARCH_CANDIDATES:
            progress.truncated = True
            newest = newest[:MAX_SEARCH_CANDIDATES]
        fields: list[Literal["MESSAGE-ID", "FROM"]] = []
        if merge:
            fields = ["MESSAGE-ID", "FROM"]  # the dedup key (S-e)
        elif query.direction is not None and session.own:
            fields = ["FROM"]  # I13
        base = [*CANDIDATE_ITEMS, _GMAIL_CANDIDATE_ITEM] if session.gmail else list(CANDIDATE_ITEMS)
        items = [*base, candidate_header_item(fields)] if fields else base
        rows = session.fetch(newest, items)
        out: list[Candidate] = []
        for count, uid in enumerate(newest):
            if count and count % SOFT_CHECK_EVERY == 0:
                self._soft_check()  # header parsing costs CPU but sends no command the guard could stop
            row = rows.get(uid)
            if row is None or _is_draft(row):  # FETCH NO, expunged meanwhile, or a draft (S1)
                continue
            received = _aware(row.get(b"INTERNALDATE"))
            if received is None:
                log_message_skipped(
                    MessageRef(self._account, entry.raw_name, validity, uid), "mail", MissingInternalDate()
                )
                continue
            if received < query.since:
                continue
            message_id, sender = _candidate_headers(row) if fields else (None, None)
            sent_label = session.gmail and _LABEL_SENT in _labels(row)  # Gmail direction before `limit` (C4)
            out.append(Candidate(plan, validity, uid, received, message_id, sender, sent_label))
        return out

    def _choose(self, candidates: list[Candidate], query: ListQuery, merge: bool) -> tuple[list[_Chosen], bool]:
        """R3 after the per-candidate drops of pass 1: dedup (`all`), position, direction, then `limit` by
        `order_key`. Returns the chosen items in list order and whether more matched."""
        merged: list[tuple[Candidate, bool]] = []
        if merge:  # over every candidate of the window, first copy in the order inbox, archive, sent (B4, S-e)
            index: dict[tuple[str, str | None], int] = {}
            for candidate in candidates:
                in_sent = candidate.plan.fixed_folder == "sent"
                if candidate.message_id is None:  # never merged
                    merged.append((candidate, in_sent))
                    continue
                sender = candidate.from_address.lower() if candidate.from_address else None
                merge_key = (candidate.message_id, sender)
                first = index.get(merge_key)
                if first is None:
                    index[merge_key] = len(merged)
                    merged.append((candidate, in_sent))
                elif in_sent:
                    merged[first] = (merged[first][0], True)  # M3: outgoing if any copy is in sent
        else:
            merged = [(c, c.plan.fixed_folder == "sent") for c in candidates]
        kept: list[_Chosen] = []
        for candidate, in_sent in merged:
            ref = MessageRef(self._account, candidate.plan.entry.raw_name, candidate.uidvalidity, candidate.uid)
            key = order_key_of(candidate.received_at, ref)
            if query.position is not None and not query.position.precedes(*key):
                continue
            sent = in_sent or candidate.sent_label
            direction: Direction | None = None
            if query.direction is not None:
                direction = direction_of(in_sent=sent, from_address=candidate.from_address, own=self._own_addresses)
                if direction != query.direction:
                    continue
            kept.append(_Chosen(candidate, ref, key, sent, direction))
        kept.sort(key=lambda chosen: chosen.key, reverse=True)
        return kept[: query.limit], len(kept) > query.limit

    def _summaries(
        self,
        session: ImapSession,
        entry: FolderEntry,
        folder: MailFolder | None,
        group: list[_Chosen],
        progress: _Progress,
    ) -> dict[MessageRef, MailSummary]:
        """Pass 2 for one folder: EXAMINE only when another mailbox is open. A chosen item that cannot be read now
        (changed UIDVALIDITY, FETCH NO, expunged) is dropped and the page becomes incomplete: a cursor after the
        items returned would skip it for good. `folder` None (Gmail All Mail): each item's folder follows its
        X-GM-LABELS; on any other server that fails closed. Under a `direction: incoming` filter, a Gmail item whose
        `\\Sent` label appeared after pass 1 is dropped like a new draft: it no longer matches the filter."""
        if folder is None and not session.gmail:  # only Gmail All Mail decides the folder per message
            raise ProviderError("upstream_error", "UnmappedFolder")
        if session.selected != entry.raw_name:
            try:
                validity = session.examine(entry)
            except PASS_THROUGH:
                raise
            except imaplib.IMAP4.error as exc:
                if not _answered_no(exc):
                    raise
                progress.complete = False
                return {}
            if validity != group[0].candidate.uidvalidity:
                progress.truncated, progress.complete = True, False
                return {}
        uids = [chosen.candidate.uid for chosen in group]
        gmail = session.gmail
        items = [*LIST_SUMMARY_ITEMS, *_GMAIL_SUMMARY_ITEMS] if gmail else list(LIST_SUMMARY_ITEMS)
        flag_items = ["FLAGS", _GMAIL_CANDIDATE_ITEM] if gmail else ["FLAGS"]
        rows, unparsable = _fetch_summaries(session, uids, items, flag_items)
        texts = _fetch_texts(session, rows, SNIPPET_FETCH_BYTES, {c.candidate.uid: c.ref for c in group})
        out: dict[MessageRef, MailSummary] = {}
        for chosen in group:
            uid = chosen.candidate.uid
            if uid in unparsable:
                failure, flag_row = unparsable[uid]
                if _is_draft(flag_row) or _sent_since_pass_one(chosen, flag_row):
                    continue
                log_message_skipped(chosen.ref, "mail", failure)
                flags = _flags(flag_row)
                meta = self._meta(entry, folder or _gmail_folder(_labels(flag_row)), chosen, flags, None)
                out[chosen.ref] = placeholder_summary(
                    chosen.ref, chosen.candidate.received_at, _SEEN not in flags, meta
                )
                continue
            row = rows.get(uid)
            if row is None:  # FETCH NO (S-p) or expunged meanwhile
                progress.complete = False
                continue
            if _is_draft(row) or _sent_since_pass_one(chosen, row):  # excluded now, like any draft or other direction
                continue
            item_folder = folder or _gmail_folder(_labels(row))
            out[chosen.ref] = self._summary(entry, item_folder, chosen, row, texts.get(uid, ""), gmail=gmail)
        return out

    def _meta(
        self, entry: FolderEntry, folder: MailFolder, chosen: _Chosen, flags: frozenset[bytes], from_address: str | None
    ) -> MailMeta:
        direction = chosen.direction or direction_of(
            in_sent=chosen.in_sent or folder == "sent", from_address=from_address, own=self._own_addresses
        )
        return MailMeta(
            folder=folder,
            folder_name=entry.display_name,
            flags=MailFlags(answered=_ANSWERED in flags, flagged=_FLAGGED in flags),
            direction=direction,
        )

    def _summary(
        self,
        entry: FolderEntry,
        folder: MailFolder,
        chosen: _Chosen,
        row: dict[bytes, object],
        snippet: str,
        *,
        gmail: bool,
    ) -> MailSummary:
        flags = _flags(row)
        received = chosen.candidate.received_at
        try:  # one hostile message degrades to a placeholder, never fails the account
            parts = _structure(row)
            text_part = choose_text_part(parts)
            parsed = parse_summary_headers(header_bytes(row))  # CPU-bounded (§5.5, Sec4); in the worker thread
            meta = self._meta(entry, folder, chosen, flags, parsed.from_address)
            return MailSummary(
                ref=chosen.ref,
                received_at=received,
                unread=_SEEN not in flags,
                has_attachments=bool(attachment_parts(parts, text_part)),
                from_address=parsed.from_address,
                from_name=parsed.from_name,
                subject=parsed.subject,
                snippet_text=snippet,
                to_addresses=parsed.to,
                cc_addresses=parsed.cc,
                meta=replace(
                    meta,
                    message_id=parsed.message_id,
                    in_reply_to=parsed.in_reply_to,
                    references=parsed.references,
                    thread=_gmail_thread(self._account, row) if gmail else imap_thread_key(self._account, parsed),
                ),
            )
        except PASS_THROUGH:
            raise
        except Exception as exc:
            log_message_skipped(chosen.ref, "mail", exc)
            meta = self._meta(entry, folder, chosen, flags, None)
            return placeholder_summary(chosen.ref, received, _SEEN not in flags, meta)

    # --- get_message -------------------------------------------------------------------------------------------

    def _get_message(self, session: ImapSession, target: MessageRef | StableRef) -> MailDetail:
        if isinstance(target, StableRef):
            return self._get_by_ref(session, target.message_id)
        entry = self._bound_entry(session, target.folder)
        if self._examine_bound(session, entry) != target.uidvalidity:
            raise ProviderError("not_found", "UidValidityChanged")
        return self._read(session, entry, target, gone=ProviderError("not_found", "UnknownUid"))

    @staticmethod
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

    @staticmethod
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
            raise ProviderError("not_found", "UnboundFolder")
        raise ProviderError("upstream_timeout", "ExamineRefused")

    def _get_by_ref(self, session: ImapSession, message_id: str) -> MailDetail:
        """A `ref` opens the copy `locate` finds (spec §5.2, §5.5). Not found after a complete search → `not_found`;
        a search or read that did not complete → `upstream_timeout`, never `not_found` (B6); a soft stop anywhere
        → `upstream_timeout` (R2b)."""
        result = session.locate([message_id])
        self._soft_check()
        copy = result.found.get(message_id)
        if copy is None or message_id in result.uncertain:  # uncertain: an earlier copy may be unread (S-e)
            if copy is None and message_id not in result.incomplete:
                raise ProviderError("not_found", "NotFound")
            raise ProviderError("upstream_timeout", "IncompleteSearch")
        if session.selected != copy.entry.raw_name:  # the search moved on to other mailboxes
            try:
                validity = session.examine(copy.entry)
            except PASS_THROUGH:
                raise
            except imaplib.IMAP4.error as exc:
                if not _answered_no(exc):
                    raise
                validity = None
            if validity != copy.uidvalidity:
                raise ProviderError("upstream_timeout", "IncompleteSearch")
        ref = MessageRef(self._account, copy.entry.raw_name, copy.uidvalidity, copy.uid)
        return self._read(session, copy.entry, ref, gone=ProviderError("upstream_timeout", "IncompleteSearch"))

    def _read(self, session: ImapSession, entry: FolderEntry, ref: MessageRef, *, gone: ProviderError) -> MailDetail:
        """One message of the examined `entry`: the summary items (Gmail: also labels and thread id), then only the
        chosen text part. A draft is `not_found` (S1); a FETCH answered NO is `upstream_timeout`; a message that
        does not come back raises `gone`."""
        gmail = session.gmail
        items = [*LIST_SUMMARY_ITEMS, *_GMAIL_SUMMARY_ITEMS] if gmail else list(LIST_SUMMARY_ITEMS)
        structure_known = True
        try:
            row = session.fetch([ref.uid], items, on_no="fail").get(ref.uid)
        except RecursionError as exc:  # see _fetch_summaries: open the message without its structure
            log_message_skipped(ref, "mail", exc)
            structure_known = False
            row = session.fetch([ref.uid], [i for i in items if i != "BODYSTRUCTURE"], on_no="fail").get(ref.uid)
        if row is None:
            raise gone
        if _is_draft(row):
            raise ProviderError("not_found", "Draft")
        folder = _detail_folder(entry, row, gmail=gmail)
        received = _aware(row.get(b"INTERNALDATE")) or datetime.now(UTC)
        body = ""
        source: Literal["text/plain", "text/html-converted", "none"] = "none"
        cut = False
        parts: list[Part] = []
        text_part: Part | None = None
        try:  # each step degrades on its own; a hostile message still opens
            if structure_known:
                parts = _structure(row)
                text_part = choose_text_part(parts)
        except PASS_THROUGH:
            raise
        except Exception as exc:
            log_message_skipped(ref, "mail", exc)
        if text_part is not None:
            data = session.fetch([ref.uid], [f"BODY.PEEK[{text_part.section}]<0.{MAX_TEXT_PART_BYTES}>"], on_no="fail")
            raw = data.get(ref.uid, {}).get(f"BODY[{text_part.section}]<0>".encode())
            raw_bytes = raw if isinstance(raw, bytes) else b""
            # The BODYSTRUCTURE size decides; only without one does a full-length fetch count as cut.
            cut = text_part.size > MAX_TEXT_PART_BYTES or (
                text_part.size <= 0 and len(raw_bytes) >= MAX_TEXT_PART_BYTES
            )
            try:
                body = decode_part(raw_bytes, text_part)
                if text_part.mime_type == "text/html":
                    body, source = html_to_text(body), "text/html-converted"
                else:
                    source = "text/plain"
            except PASS_THROUGH:
                raise
            except Exception as exc:
                log_message_skipped(ref, "mail", exc)
                body, source = "", "none"
        raw_headers = header_bytes(row)
        try:
            headers = _PARSER.parsebytes(raw_headers)
            sender, subject = _sender_and_subject(headers)
            to = [a for _, a in _addresses(headers, "To") if a]
            cc = [a for _, a in _addresses(headers, "Cc") if a]
        except PASS_THROUGH:
            raise
        except Exception as exc:
            log_message_skipped(ref, "mail", exc)
            sender, subject, to, cc = ("", ""), None, [], []
        parsed: ParsedHeaders | None = None
        try:  # the history fields degrade on their own: sender, subject, To and Cc stay
            parsed = parse_summary_headers(raw_headers)  # Message-ID, In-Reply-To, References (§5.5)
        except PASS_THROUGH:
            raise
        except Exception as exc:
            log_message_skipped(ref, "mail", exc)
        flags = _flags(row)
        if gmail:
            thread = _gmail_thread(self._account, row)
        else:
            thread = imap_thread_key(self._account, parsed) if parsed is not None else None
        meta = MailMeta(
            folder=folder,
            folder_name=entry.display_name,
            flags=MailFlags(answered=_ANSWERED in flags, flagged=_FLAGGED in flags),
            direction=direction_of(
                in_sent=folder == "sent" or (gmail and _LABEL_SENT in _labels(row)),
                from_address=sender[1] or None,
                own=self._own_addresses,
            ),
            message_id=parsed.message_id if parsed is not None else None,
            in_reply_to=parsed.in_reply_to if parsed is not None else None,
            references=parsed.references if parsed is not None else (),
            thread=thread,
        )
        return MailDetail(
            ref=ref,
            received_at=received,
            unread=_unread(row),
            attachments=[AttachmentMeta(p.size, p.filename, p.mime_type) for p in attachment_parts(parts, text_part)],
            from_address=sender[1] or None,
            from_name=sender[0] or None,
            to_addresses=to,
            cc_addresses=cc,
            subject=subject,
            body_text=body,
            body_source=source,
            body_cut=cut,
            meta=meta,
        )


def _criteria(plan: FolderPlan, query: ListQuery, merge: bool) -> bytes:
    """`NOT DRAFT [UNSEEN] SINCE <window start - 1 day> [BEFORE <position day + 2>]` (§5.5 "Cursors"): no BEFORE
    for a merged `all`, whose later pages search the whole window again (S-a)."""
    since_day = query.since.astimezone(UTC).date() - timedelta(days=1)
    before_day: date | None = None
    if query.position is not None and not merge:
        before_day = datetime.fromtimestamp(query.position.received_at, UTC).date() + timedelta(days=2)
    keys = [window_keys(since_day=since_day, before_day=before_day, unread_only=query.unread_only)]
    if plan.gmail_labels is not None:
        keys.append(gmail_label_keys(plan.gmail_labels))
    return criteria(*keys)


def _by_folder(plans: list[FolderPlan], chosen: list[_Chosen]) -> list[tuple[FolderEntry, list[_Chosen]]]:
    """The chosen items grouped by mailbox, in plan order; each group keeps list order."""
    groups: dict[str, tuple[FolderEntry, list[_Chosen]]] = {plan.entry.raw_name: (plan.entry, []) for plan in plans}
    for item in chosen:
        groups[item.candidate.plan.entry.raw_name][1].append(item)
    return [(entry, items) for entry, items in groups.values() if items]
