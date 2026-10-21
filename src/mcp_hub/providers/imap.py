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

from mcp_hub.ids import AnyMessageRef, MessageRef
from mcp_hub.providers.base import (
    MAX_HEADER_BYTES,
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
    UnreadPage,
    direction_of,
    log_message_skipped,
    order_key_of,
    placeholder_summary,
)
from mcp_hub.providers.deadline import CallDeadline, SoftStop
from mcp_hub.providers.folders import (
    FOLDER_MAPS,
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
from mcp_hub.providers.imap_search import criteria, gmail_label_keys, window_keys
from mcp_hub.providers.mime import Part, attachment_parts, choose_text_part, decode_part, walk
from mcp_hub.sanitize import html_to_text

HEADER_ITEM: Final = f"BODY.PEEK[HEADER.FIELDS (FROM TO CC SUBJECT)]<0.{MAX_HEADER_BYTES}>"
SUMMARY_ITEMS: Final = ("FLAGS", "INTERNALDATE", "BODYSTRUCTURE", HEADER_ITEM)  # get_message
LIST_SUMMARY_ITEMS: Final = ("FLAGS", "INTERNALDATE", "BODYSTRUCTURE", SUMMARY_HEADER_ITEM)  # §5.5 header sources
CANDIDATE_ITEMS: Final = ("INTERNALDATE", "FLAGS")  # FLAGS so that drafts drop before `limit`
MAX_SEARCH_CANDIDATES: Final = 5000  # newest SEARCH hits per folder and call (§5.4, B5)
FETCH_CHUNK_UIDS: Final = 500  # UIDs per UID FETCH (the guard's MAX_FETCH_UIDS bounds the command line)
SOFT_CHECK_EVERY: Final = 100  # candidates parsed between two soft-limit checks in pass 1 (CPU without commands)
_SEEN: Final = b"\\seen"
_ANSWERED: Final = b"\\answered"
_FLAGGED: Final = b"\\flagged"
_DRAFT: Final = frozenset({b"\\draft", b"\\drafts"})  # any case (I8)
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


def _unread(row: dict[bytes, object]) -> bool:
    return _SEEN not in _flags(row)


def _is_draft(row: dict[bytes, object]) -> bool:
    return bool(_flags(row) & _DRAFT)


def _structure(row: dict[bytes, object]) -> list[Part]:
    return walk(cast(Sequence[object], row.get(b"BODYSTRUCTURE", ())))


def _sender_and_subject(headers: Message) -> tuple[tuple[str, str], str | None]:
    """str() on an encoded word with an odd charset can raise, so callers guard this per message."""
    sender = (_addresses(headers, "From") or [("", "")])[0]
    return sender, str(headers.get("Subject", "")) or None


def _fixed_folder(plan: FolderPlan) -> MailFolder:
    if plan.fixed_folder is None:  # Gmail All Mail decides per message by X-GM-LABELS (not implemented yet)
        raise ProviderError("upstream_error", "NotImplemented")
    return plan.fixed_folder


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
        self._selected: str | None = None

    @property
    def inbox(self) -> FolderEntry:
        """The registry inbox; needs no LIST (§5.5)."""
        return self._map.inbox if self._map is not None else inbox_entry(self.registry_inbox)

    @property
    def selected(self) -> str | None:
        """Raw name of the mailbox the last successful EXAMINE opened."""
        return self._selected

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

    def fetch(self, uids: Sequence[int], items: Sequence[str]) -> dict[int, dict[bytes, object]]:
        """UID FETCH in ascending chunks of at most 500 UIDs. A chunk answered NO is dropped: the UIDs it did not
        return are missing from the result (S-p). Every other error propagates."""
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
        return rows

    def noop(self) -> None:
        self._client.noop()


Unparsable = dict[int, tuple[Exception, dict[bytes, object]]]


def _fetch_summaries(
    session: ImapSession, uids: list[int], items: Sequence[str]
) -> tuple[dict[int, dict[bytes, object]], Unparsable]:
    """Summary rows of `uids`. IMAPClient's recursive response parser raises RecursionError on a BODYSTRUCTURE
    nested about 1000 deep; the batch is then fetched per UID, and a UID that fails alone is returned as unparsable,
    with its FLAGS fetched alone, so only that message degrades (to a placeholder with the right `unread`). A UID
    whose FLAGS do not come back either is missing from both results."""
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
                flags = session.fetch([uid], ["FLAGS"]).get(uid)
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

    def _run[T](self, work: Callable[[ImapSession], T], deadline: CallDeadline) -> T:
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

    def list_unread(self, since: datetime, limit: int, deadline: CallDeadline) -> UnreadPage:
        """`list_messages(folder="inbox", unread_only=True)` until the tool layer calls it directly (T18)."""
        page = self.list_messages(ListQuery("inbox", since, True, None, limit), deadline)
        return UnreadPage(items=page.items, more=page.more or page.truncated or not page.complete)

    def list_messages(self, query: ListQuery, deadline: CallDeadline) -> FolderPage:
        return self._run(lambda session: self._list_messages(session, query), deadline)

    def get_message(self, ref: AnyMessageRef, deadline: CallDeadline) -> MailDetail:
        if not isinstance(ref, MessageRef):
            raise ProviderError("not_found", "ForeignRef")
        return self._run(lambda session: self._get_message(session, ref), deadline)

    # --- list_messages -----------------------------------------------------------------------------------------

    def _list_messages(self, session: ImapSession, query: ListQuery) -> FolderPage:
        """Pass 1 searches every folder of the plan and keeps the candidates; the page is chosen from all of them
        (R3); pass 2 fetches the summaries of the chosen items per folder. A soft stop returns what was built."""
        progress = _Progress()
        candidates: list[Candidate] = []
        try:
            plans = self._plans(session, query.folder)
            folder_of = {plan.entry.raw_name: _fixed_folder(plan) for plan in plans}  # before any EXAMINE
            merge = query.folder == "all" and len(plans) > 1
            for plan in plans:
                candidates.extend(self._candidates(session, plan, query, merge, progress))
        except SoftStop:
            return FolderPage(items=[], more=False, truncated=True, complete=False)
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
        items = [*CANDIDATE_ITEMS, candidate_header_item(fields)] if fields else list(CANDIDATE_ITEMS)
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
            out.append(Candidate(plan, validity, uid, received, message_id, sender, False))
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
        self, session: ImapSession, entry: FolderEntry, folder: MailFolder, group: list[_Chosen], progress: _Progress
    ) -> dict[MessageRef, MailSummary]:
        """Pass 2 for one folder: EXAMINE only when another mailbox is open. A chosen item that cannot be read now
        (changed UIDVALIDITY, FETCH NO, expunged) is dropped and the page becomes incomplete: a cursor after the
        items returned would skip it for good."""
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
        rows, unparsable = _fetch_summaries(session, uids, LIST_SUMMARY_ITEMS)
        texts = _fetch_texts(session, rows, SNIPPET_FETCH_BYTES, {c.candidate.uid: c.ref for c in group})
        out: dict[MessageRef, MailSummary] = {}
        for chosen in group:
            uid = chosen.candidate.uid
            if uid in unparsable:
                failure, flag_row = unparsable[uid]
                if _is_draft(flag_row):
                    continue
                log_message_skipped(chosen.ref, "mail", failure)
                flags = _flags(flag_row)
                meta = self._meta(entry, folder, chosen, flags, None)
                out[chosen.ref] = placeholder_summary(
                    chosen.ref, chosen.candidate.received_at, _SEEN not in flags, meta
                )
                continue
            row = rows.get(uid)
            if row is None:  # FETCH NO (S-p) or expunged meanwhile
                progress.complete = False
                continue
            if _is_draft(row):  # a draft by now: excluded, like any draft
                continue
            out[chosen.ref] = self._summary(entry, folder, chosen, row, texts.get(uid, ""))
        return out

    def _meta(
        self, entry: FolderEntry, folder: MailFolder, chosen: _Chosen, flags: frozenset[bytes], from_address: str | None
    ) -> MailMeta:
        direction = chosen.direction or direction_of(
            in_sent=chosen.in_sent, from_address=from_address, own=self._own_addresses
        )
        return MailMeta(
            folder=folder,
            folder_name=entry.display_name,
            flags=MailFlags(answered=_ANSWERED in flags, flagged=_FLAGGED in flags),
            direction=direction,
        )

    def _summary(
        self, entry: FolderEntry, folder: MailFolder, chosen: _Chosen, row: dict[bytes, object], snippet: str
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
                    thread=imap_thread_key(self._account, parsed),
                ),
            )
        except PASS_THROUGH:
            raise
        except Exception as exc:
            log_message_skipped(chosen.ref, "mail", exc)
            meta = self._meta(entry, folder, chosen, flags, None)
            return placeholder_summary(chosen.ref, received, _SEEN not in flags, meta)

    # --- get_message -------------------------------------------------------------------------------------------

    def _get_message(self, session: ImapSession, ref: MessageRef) -> MailDetail:
        if ref.folder != session.registry_inbox:  # the tool layer binds ids to the registry inbox (D56)
            raise ProviderError("not_found", "UnboundFolder")
        if session.examine(session.inbox) != ref.uidvalidity:
            raise ProviderError("not_found", "UidValidityChanged")
        structure_known = True
        try:
            row = session.fetch([ref.uid], SUMMARY_ITEMS).get(ref.uid)
        except RecursionError as exc:  # see _fetch_summaries: open the message without its structure
            log_message_skipped(ref, "mail", exc)
            structure_known = False
            row = session.fetch([ref.uid], [i for i in SUMMARY_ITEMS if i != "BODYSTRUCTURE"]).get(ref.uid)
        if row is None:
            raise ProviderError("not_found", "UnknownUid")
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
            data = session.fetch([ref.uid], [f"BODY.PEEK[{text_part.section}]<0.{MAX_TEXT_PART_BYTES}>"])
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
        try:
            headers = _PARSER.parsebytes(header_bytes(row))
            sender, subject = _sender_and_subject(headers)
            to = [a for _, a in _addresses(headers, "To") if a]
            cc = [a for _, a in _addresses(headers, "Cc") if a]
        except PASS_THROUGH:
            raise
        except Exception as exc:
            log_message_skipped(ref, "mail", exc)
            sender, subject, to, cc = ("", ""), None, [], []
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
