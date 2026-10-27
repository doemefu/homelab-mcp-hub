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
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from email.message import Message
from email.parser import BytesHeaderParser
from functools import partial
from typing import Final, Literal, cast

import imapclient
from imapclient.exceptions import LoginError

from mcp_hub import search as _search  # the module: `search` imports providers.base, so a name import would cycle
from mcp_hub.ids import AnyMessageRef, MessageRef, StableRef, ThreadKey
from mcp_hub.providers import imap_status, imap_thread
from mcp_hub.providers.base import (
    MAX_TEXT_PART_BYTES,
    PROVIDER_TIMEOUT_SECONDS,
    SNIPPET_FETCH_BYTES,
    AttachmentMeta,
    Direction,
    FolderPage,
    IncompleteReason,
    ListQuery,
    MailDetail,
    MailFlags,
    MailMeta,
    MailSummary,
    MessageStatus,
    ProviderError,
    SearchKey,
    StatusTarget,
    ThreadPage,
    ThreadQuery,
    direction_of,
    log_message_skipped,
    order_key_of,
    placeholder_summary,
)
from mcp_hub.providers.deadline import CallDeadline, SoftStop
from mcp_hub.providers.folders import (
    FOLDER_MAPS,
    FolderEntry,
    FolderMapCache,
    FolderPlan,
    ListFolder,
    MailFolder,
)
from mcp_hub.providers.headers import (
    SUMMARY_HEADER_ITEM,
    ParsedHeaders,
    address_pairs,
    candidate_header_item,
    header_bytes,
    imap_thread_key,
    parse_summary_headers,
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
    window_keys,
)
from mcp_hub.providers.imap_session import (
    _GMAIL_CANDIDATE_ITEM,
    _LABEL_INBOX,
    _LABEL_SENT,
    MAX_SEARCH_CANDIDATES,
    SOFT_CHECK_EVERY,
    ImapSession,
    MissingInternalDate,
    _answered_no,
    _aware,
    _bound_entry,
    _candidate_headers,
    _detail_folder,
    _examine_bound,
    _flags,
    _gmail_folder,
    _is_draft,
    _labels,
)
from mcp_hub.providers.imap_thread import BODY_RESERVE_SECONDS
from mcp_hub.providers.mime import Part, attachment_parts, choose_text_part, decode_part, walk
from mcp_hub.sanitize import html_to_text

LIST_SUMMARY_ITEMS: Final = ("FLAGS", "INTERNALDATE", "BODYSTRUCTURE", SUMMARY_HEADER_ITEM)  # §5.5 header sources
CANDIDATE_ITEMS: Final = ("INTERNALDATE", "FLAGS")  # FLAGS so that drafts drop before `limit`
_GMAIL_SUMMARY_ITEMS: Final = ("X-GM-LABELS", "X-GM-THRID")  # Gmail: per-message folder and thread (§5.5)
_SEEN: Final = b"\\seen"
_ANSWERED: Final = b"\\answered"
_FLAGGED: Final = b"\\flagged"
_MAX_THRID: Final = 2**64
_PARSER: Final = BytesHeaderParser(policy=email.policy.default)

ClientFactory = Callable[[str, int, ssl.SSLContext, float], ImapClientLike]


class HubImapClient(imapclient.IMAPClient):  # type: ignore[misc]  # IMAPClient ships no type information
    """IMAPClient that never announces `LITERAL+` to its own literal sender: the library asks `has_capability`
    to choose between `{N+}` and `{N}` + wait for `+`, and a search value is third-party text. With `{N}` the
    server must accept the command line before a single value byte is sent, so a server that rejects the line
    early cannot read the value as a new command (D82). Every other capability answers as the library does."""

    def has_capability(self, capability: str | bytes) -> bool:
        name = capability.decode("ascii", "replace") if isinstance(capability, bytes) else capability
        if name.upper() == "LITERAL+":
            return False
        return bool(super().has_capability(capability))


def default_client(host: str, port: int, context: ssl.SSLContext, timeout: float) -> ImapClientLike:
    return cast(ImapClientLike, HubImapClient(host, port=port, ssl=True, ssl_context=context, timeout=timeout))


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


def _check_soft(deadline: CallDeadline) -> None:
    """The soft limit of the call's own deadline (P9: new code never reads the stored `_deadline`)."""
    if deadline.soft_expired():
        raise SoftStop


def _addresses(message: Message, name: str) -> list[tuple[str, str]]:
    """(display name, address) pairs of the first `name` header: its raw text through the linear parser of
    `headers.address_pairs` (cut to 8 KiB). `Message.get` would run the policy parser, which is quadratic on a
    hostile 64 KiB header (security I1)."""
    wanted = name.lower()
    for key, value in message.raw_items():
        if key.lower() == wanted:
            return address_pairs(str(value))
    return []


def _unread(row: dict[bytes, object]) -> bool:
    return _SEEN not in _flags(row)


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


@dataclass(slots=True)
class _Progress:
    """What the list pipeline lost on the way. `incomplete` names why (the search tool lists the reasons); `searching`
    makes every loss also a truncation, which the list tools never had (their flags stay as they were)."""

    truncated: bool = False
    complete: bool = True
    searching: bool = False
    incomplete: set[IncompleteReason] = field(default_factory=set)
    refused: bool = False  # a folder could not be searched
    lost: bool = False  # incomplete for a reason other than a refusal

    def refuse(self) -> None:
        """The folder could not be searched (NO, no UIDVALIDITY, name too long): it counts as searched for the cursor
        (ruling 4)."""
        self.complete, self.refused = False, True
        self.incomplete.add("search_refused")
        self.truncated = self.truncated or self.searching

    def lose(self, reason: IncompleteReason, *, truncated: bool = False) -> None:
        self.complete, self.lost = False, True
        self.incomplete.add(reason)
        self.truncated = self.truncated or truncated or self.searching

    def cap(self) -> None:
        self.truncated = True
        self.incomplete.add("result_cap")

    def page(self, items: list[MailSummary], more: bool, frontier: tuple[int, str] | None) -> FolderPage:
        return FolderPage(
            items=items,
            more=more,
            truncated=self.truncated,
            complete=self.complete,
            frontier=frontier,
            incomplete=frozenset(self.incomplete),
            refused_only=self.refused and not self.lost and not self.complete,
        )


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
    session: ImapSession,
    rows: dict[int, dict[bytes, object]],
    length: int,
    refs: dict[int, MessageRef],
    soft_check: Callable[[], None],
) -> dict[int, str]:
    """Partial fetch of each message's chosen text part, one FETCH per section number. Snippets are optional: a
    FETCH that fails without losing the connection or answering BAD leaves those snippets empty. The decode of
    each member checks the soft limit first (it costs CPU without a command)."""
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
            soft_check()
            try:  # per UID: a broken part only empties that message's snippet
                raw = data.get(uid, {}).get(key)
                text = decode_part(raw if isinstance(raw, bytes) else b"", part)
                texts[uid] = html_to_text(text) if part.mime_type == "text/html" else text
            except PASS_THROUGH:
                raise
            except Exception:
                texts[uid] = ""
    return texts


def _soft_stopped_page(progress: _Progress | None = None) -> FolderPage:
    """Nothing read past the stop; the reasons collected before it stay (ruling 3)."""
    reasons: set[IncompleteReason] = {"time_limit"} if progress is None else {*progress.incomplete, "time_limit"}
    return FolderPage(items=[], more=False, truncated=True, complete=False, incomplete=frozenset(reasons))


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

    def message_status(self, targets: Sequence[StatusTarget], deadline: CallDeadline) -> list[MessageStatus]:
        """One status per target, in order (spec 080 §5.2). `imap_status` keeps what it read on a soft stop; a stop
        that reaches this level (at LOGIN or the CAPABILITY after it) makes every entry incomplete_search (IR2)."""
        wanted = list(targets)
        return self._run(
            lambda session: imap_status.message_status(session, wanted, soft_check=self._soft_check),
            deadline,
            on_soft_stop=lambda: imap_status.soft_stopped(wanted),
        )

    def get_message(self, target: AnyMessageRef | StableRef, deadline: CallDeadline) -> MailDetail:
        """By `id` (D75 binding) or by `ref` (the location step); a Graph id is foreign here."""
        if not isinstance(target, (MessageRef, StableRef)):
            raise ProviderError("not_found", "ForeignRef")
        own_target: MessageRef | StableRef = target
        return self._run(lambda session: self._get_message(session, own_target), deadline)

    def get_thread(self, query: ThreadQuery, deadline: CallDeadline) -> ThreadPage:
        """Spec 080 §5.2 `get_thread` (`imap_thread`); a Graph id is foreign here. No `on_soft_stop`: a stop at LOGIN or
        before the seed was read is `upstream_timeout`; later stops are answered inside `imap_thread`."""
        if query.seed is not None and not isinstance(query.seed, (MessageRef, StableRef)):
            raise ProviderError("not_found", "ForeignRef")
        return self._run(
            lambda session: imap_thread.get_thread(
                session,
                query,
                soft_check=partial(_check_soft, deadline),
                reserve_spent=lambda: deadline.elapsed() > BODY_RESERVE_SECONDS,
            ),
            deadline,
        )

    # --- list_messages -----------------------------------------------------------------------------------------

    def _list_messages(self, session: ImapSession, query: ListQuery) -> FolderPage:
        """Pass 1 searches every folder of the plan and keeps the candidates; the page is chosen from all of them
        (R3); pass 2 fetches the summaries of the chosen items per folder. A soft stop returns what was built."""
        progress = _Progress(searching=query.search is not None)
        candidates: list[Candidate] = []
        try:
            plans = self._plans(session, query.folder)
            # None: Gmail All Mail, where X-GM-LABELS decide per message
            folder_of = {plan.entry.raw_name: plan.fixed_folder for plan in plans}
            merge = query.folder == "all" and len(plans) > 1
            for plan in plans:
                candidates.extend(self._candidates(session, plan, query, merge, progress))
        except SoftStop:
            return _soft_stopped_page(progress)
        chosen, more = self._choose(candidates, query, merge)
        built: dict[MessageRef, MailSummary] = {}
        try:
            for entry, group in _by_folder(plans, chosen):
                self._summaries(session, entry, folder_of[entry.raw_name], group, progress, built)
        except SoftStop:  # SA13: pass 2 incomplete, so the page gets no cursor; the items built so far are kept
            progress.lose("time_limit", truncated=True)
        returned = [c for c in chosen if c.ref in built]
        frontier: tuple[int, str] | None = None
        if more:  # the whole window was scanned: the account's items continue below its last returned item
            last = returned[-1] if returned else chosen[-1]
            frontier = last.key
        return progress.page([built[c.ref] for c in returned], more, frontier)

    @staticmethod
    def _plans(session: ImapSession, folder: ListFolder) -> list[FolderPlan]:
        if folder == "inbox":
            return [FolderPlan(session.inbox, None, "inbox")]
        return session.folder_map().list_plan(folder)

    def _candidates(
        self, session: ImapSession, plan: FolderPlan, query: ListQuery, merge: bool, progress: _Progress
    ) -> list[Candidate]:
        entry = plan.entry
        values = _search_values(query, gmail=session.gmail)
        if values is None:  # before any command: this account cannot search these words
            progress.refuse()
            return []
        if entry.raw_name != session.registry_inbox and not examine_name_ok(entry.raw_name):
            progress.refuse()  # over 1,000 characters: the guard would refuse the EXAMINE
            return []
        try:
            validity = session.examine(entry)
        except PASS_THROUGH:
            raise
        except imaplib.IMAP4.error as exc:
            if not _answered_no(exc):
                raise
            if entry.raw_name != session.registry_inbox and not session.listed and session.may_force_rebuild():
                session.folder_map(refresh=True)  # one rebuild for the next call; this folder stays incomplete
            progress.refuse()
            return []
        if validity is None:  # missing UIDVALIDITY on a folder other than the registry inbox (I11)
            progress.refuse()
            return []
        prefix = _criteria(plan, query, merge)
        try:
            found = session.search_values(prefix, values) if values else session.search(prefix)
        except PASS_THROUGH:
            raise
        except imaplib.IMAP4.error as exc:
            if not _answered_no(exc):
                raise
            progress.refuse()  # no cursor (S-p); for a search the cursor rule of ruling 4 applies
            return []
        newest = sorted(set(found), reverse=True)
        if len(newest) > MAX_SEARCH_CANDIDATES:
            progress.cap()
            newest = newest[:MAX_SEARCH_CANDIDATES]
        fields: list[Literal["MESSAGE-ID", "FROM"]] = []
        if merge:
            fields = ["MESSAGE-ID", "FROM"]  # the dedup key (S-e)
        elif query.direction is not None and session.own:
            fields = ["FROM"]  # I13
        base = [*CANDIDATE_ITEMS, _GMAIL_CANDIDATE_ITEM] if session.gmail else list(CANDIDATE_ITEMS)
        items = [*base, candidate_header_item(fields)] if fields else base
        rows = session.fetch(newest, items)
        # Gmail without `\All`: the `\Sent` mailbox also holds self-sent mail, which is `inbox` (§5.5); its labels
        # (fetched above) drop it here, as the label key `NOT X-GM-LABELS "\Inbox"` does in All Mail.
        sent_mailbox = session.gmail and plan.fixed_folder == "sent" and plan.gmail_labels is None
        out: list[Candidate] = []
        for count, uid in enumerate(newest):
            if count and count % SOFT_CHECK_EVERY == 0:
                self._soft_check()  # header parsing costs CPU but sends no command the guard could stop
            row = rows.get(uid)
            if row is None:  # FETCH NO or expunged meanwhile: a search hit would vanish with no signal (D85)
                if progress.searching:
                    progress.lose("changed_during_call")
                continue
            if _is_draft(row):  # S1
                continue
            if sent_mailbox and _LABEL_INBOX in _labels(row):
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
        # Over every candidate of the window (B4, S-e): the first copy in the order inbox, archive, sent is kept,
        # unless a copy is in sent: then the first sent copy is shown (its folder, its id; outgoing, M3), so a
        # planted copy with the owner's From and the same Message-ID can neither hide nor replace it (security L1).
        if merge:
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
                elif in_sent and not merged[first][1]:
                    merged[first] = (candidate, True)
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
        out: dict[MessageRef, MailSummary],
    ) -> None:
        """Pass 2 for one folder, into `out`: EXAMINE only when another mailbox is open. A chosen item that cannot
        be read now (changed UIDVALIDITY, FETCH NO, expunged) is dropped and the page becomes incomplete: a cursor
        after the items returned would skip it for good. `folder` None (Gmail All Mail): each item's folder follows
        its X-GM-LABELS; on any other server that fails closed. Under a `direction: incoming` filter, a Gmail item
        whose `\\Sent` label appeared after pass 1 is dropped like a new draft: it no longer matches the filter.
        Each item checks the soft limit first (its header parse costs CPU without a command); a SoftStop leaves the
        items already in `out`."""
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
                progress.lose("changed_during_call")
                return
            if validity != group[0].candidate.uidvalidity:
                progress.lose("changed_during_call", truncated=True)
                return
        uids = [chosen.candidate.uid for chosen in group]
        gmail = session.gmail
        items = [*LIST_SUMMARY_ITEMS, *_GMAIL_SUMMARY_ITEMS] if gmail else list(LIST_SUMMARY_ITEMS)
        flag_items = ["FLAGS", _GMAIL_CANDIDATE_ITEM] if gmail else ["FLAGS"]
        rows, unparsable = _fetch_summaries(session, uids, items, flag_items)
        refs = {c.candidate.uid: c.ref for c in group}
        texts = _fetch_texts(session, rows, SNIPPET_FETCH_BYTES, refs, self._soft_check)
        for chosen in group:
            self._soft_check()
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
                progress.lose("changed_during_call")
                continue
            if _is_draft(row) or _sent_since_pass_one(chosen, row):  # excluded now, like any draft or other direction
                continue
            item_folder = folder or _gmail_folder(_labels(row))
            out[chosen.ref] = self._summary(entry, item_folder, chosen, row, texts.get(uid, ""), gmail=gmail)

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
        entry = _bound_entry(session, target.folder)
        if _examine_bound(session, entry) != target.uidvalidity:
            raise ProviderError("not_found", "UidValidityChanged")
        return self._read(session, entry, target, gone=ProviderError("not_found", "UnknownUid"))

    def _get_by_ref(self, session: ImapSession, message_id: str) -> MailDetail:
        """A `ref` opens the copy `locate` finds (spec §5.2, §5.5). Not found after a complete search → `not_found`;
        a search or read that did not complete → `upstream_timeout`, never `not_found` (B6); a soft stop anywhere
        → `upstream_timeout` (R2b)."""
        result = session.locate([message_id], soft_check=self._soft_check)
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


def _search_values(query: ListQuery, *, gmail: bool) -> list[tuple[SearchKey, str]] | None:
    """The value keys of a `search_messages` query, after the window and label keys (none for the list tools).
    None: a Gmail TEXT word the allowlist leaves nothing of; the folders are refused."""
    return [] if query.search is None else _search.imap_value_keys(query.search, gmail=gmail)


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
