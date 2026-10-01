"""Read-only IMAP adapter (spec 080 §5.1, §5.2, §6.1): EXAMINE and BODY.PEEK only, so \\Seen is never set.

Blocking; the tool layer runs it through providers.base.run_blocking. Returns decoded, unsanitised text.
"""

import contextlib
import email.policy
import ssl
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from email.headerregistry import AddressHeader
from email.message import Message
from email.parser import BytesHeaderParser
from email.utils import getaddresses
from typing import Final, Literal, Protocol, cast

import imapclient
from imapclient.exceptions import LoginError

from mcp_hub.ids import MessageRef
from mcp_hub.providers.base import (
    MAX_HEADER_BYTES,
    MAX_TEXT_PART_BYTES,
    PROVIDER_TIMEOUT_SECONDS,
    SNIPPET_FETCH_BYTES,
    AttachmentMeta,
    MailDetail,
    MailSummary,
    ProviderError,
    UnreadPage,
    log_message_skipped,
    placeholder_summary,
)
from mcp_hub.providers.mime import Part, attachment_parts, choose_text_part, decode_part, walk
from mcp_hub.sanitize import html_to_text

HEADER_ITEM: Final = f"BODY.PEEK[HEADER.FIELDS (FROM TO CC SUBJECT)]<0.{MAX_HEADER_BYTES}>"
SUMMARY_ITEMS: Final = ("FLAGS", "INTERNALDATE", "BODYSTRUCTURE", HEADER_ITEM)
MAX_SEARCH_CANDIDATES: Final = 500  # newest unread UIDs considered per call
_SEEN: Final = b"\\Seen"
_PARSER: Final = BytesHeaderParser(policy=email.policy.default)


class ImapClientLike(Protocol):
    normalise_times: bool

    def login(self, username: str, password: str) -> object: ...
    def select_folder(self, folder: str, readonly: bool = False) -> dict[bytes, object]: ...
    def search(self, criteria: list[object]) -> list[int]: ...
    def fetch(self, messages: Sequence[int], data: Sequence[str]) -> dict[int, dict[bytes, object]]: ...
    def noop(self) -> object: ...
    def logout(self) -> object: ...


ClientFactory = Callable[[str, int, ssl.SSLContext, float], ImapClientLike]


def default_client(host: str, port: int, context: ssl.SSLContext, timeout: float) -> ImapClientLike:
    return cast(ImapClientLike, imapclient.IMAPClient(host, port=port, ssl=True, ssl_context=context, timeout=timeout))


def _provider_error(exc: Exception) -> ProviderError:
    if isinstance(exc, ProviderError):
        return exc
    cause = type(exc).__name__
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
    return ProviderError("upstream_error", cause)  # IMAPClientError and anything unexpected


def _aware(value: object) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _uidvalidity(info: dict[bytes, object]) -> int:
    value = info.get(b"UIDVALIDITY")
    if type(value) is not int:
        raise ProviderError("upstream_error", "MissingUidValidity")
    return value


def _header_bytes(row: dict[bytes, object]) -> bytes:
    for key, value in row.items():
        if key.upper().startswith(b"BODY[HEADER.FIELDS") and isinstance(value, bytes):
            return value
    return b""


def _addresses(message: Message, name: str) -> list[tuple[str, str]]:
    """(display name, address) pairs; falls back to the lenient stdlib parser for malformed headers."""
    header = message.get(name)
    if header is None:
        return []
    try:  # policy.default yields an AddressHeader; typeshed types Message.get as str
        return [(a.display_name, a.addr_spec) for a in cast(AddressHeader, header).addresses]
    except Exception:  # malformed header: parse the raw text leniently
        return [(n, a) for n, a in getaddresses([str(header)]) if a or n]


def _unread(row: dict[bytes, object]) -> bool:
    flags = row.get(b"FLAGS", ())
    return not (isinstance(flags, tuple) and _SEEN in flags)


def _structure(row: dict[bytes, object]) -> list[Part]:
    return walk(cast(Sequence[object], row.get(b"BODYSTRUCTURE", ())))


def _sender_and_subject(headers: Message) -> tuple[tuple[str, str], str | None]:
    """str() on an encoded word with an odd charset can raise, so callers guard this per message."""
    sender = (_addresses(headers, "From") or [("", "")])[0]
    return sender, str(headers.get("Subject", "")) or None


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
    ) -> None:
        self._account = account_id
        self._host, self._port, self._folder = host, port, folder
        self._username, self._password = username, password
        self._context = ssl_context or ssl.create_default_context()
        self._timeout = timeout
        self._factory = client_factory

    def _run[T](self, work: Callable[[ImapClientLike], T]) -> T:
        client: ImapClientLike | None = None
        try:
            client = self._factory(self._host, self._port, self._context, self._timeout)
            client.normalise_times = False  # keep the server's offset on INTERNALDATE
            client.login(self._username, self._password)
            return work(client)
        except Exception as exc:
            raise _provider_error(exc) from None
        finally:
            if client is not None:
                with contextlib.suppress(Exception):
                    client.logout()

    def check(self) -> None:
        """Status check (spec 080 §7.4): login + NOOP."""
        self._run(lambda client: client.noop())

    def list_unread(self, since: datetime, limit: int) -> UnreadPage:
        return self._run(lambda client: self._list_unread(client, since, limit))

    def get_message(self, ref: MessageRef) -> MailDetail:
        return self._run(lambda client: self._get_message(client, ref))

    def _list_unread(self, client: ImapClientLike, since: datetime, limit: int) -> UnreadPage:
        validity = _uidvalidity(client.select_folder(self._folder, readonly=True))
        # SEARCH SINCE is day-granular in the server's zone: search one day earlier, filter exactly below.
        day = (since.astimezone(UTC) - timedelta(days=1)).date()
        found = sorted(client.search(["UNSEEN", "SINCE", day]), reverse=True)
        candidates = found[:MAX_SEARCH_CANDIDATES]
        dates = client.fetch(candidates, ["INTERNALDATE"]) if candidates else {}
        recent = sorted(
            (
                (received, uid)
                for uid, row in dates.items()
                if (received := _aware(row.get(b"INTERNALDATE"))) and received >= since
            ),
            reverse=True,
        )
        chosen = recent[:limit]
        more = len(recent) > limit or len(found) > MAX_SEARCH_CANDIDATES
        rows = client.fetch([uid for _, uid in chosen], list(SUMMARY_ITEMS)) if chosen else {}
        texts = self._fetch_texts(client, rows, SNIPPET_FETCH_BYTES)
        items: list[MailSummary] = []
        for received, uid in chosen:
            row = rows.get(uid)
            if row is None:  # expunged meanwhile
                continue
            ref = MessageRef(self._account, self._folder, validity, uid)
            try:  # one hostile message degrades to a placeholder, never fails the account
                parts = _structure(row)
                text_part = choose_text_part(parts)
                sender, subject = _sender_and_subject(_PARSER.parsebytes(_header_bytes(row)))
                items.append(
                    MailSummary(
                        ref=ref,
                        received_at=received,
                        unread=_unread(row),
                        has_attachments=bool(attachment_parts(parts, text_part)),
                        from_address=sender[1] or None,
                        from_name=sender[0] or None,
                        subject=subject,
                        snippet_text=texts.get(uid, ""),
                    )
                )
            except Exception as exc:
                log_message_skipped(ref, "mail", exc)
                items.append(placeholder_summary(ref, received, _unread(row)))
        return UnreadPage(items=items, more=more)

    def _fetch_texts(self, client: ImapClientLike, rows: dict[int, dict[bytes, object]], length: int) -> dict[int, str]:
        """Partial fetch of each message's chosen text part, one FETCH per section number."""
        by_section: dict[str, list[tuple[int, Part]]] = {}
        for uid, row in rows.items():
            try:  # per UID: an empty or odd BODYSTRUCTURE leaves this snippet empty
                part = choose_text_part(_structure(row))
            except Exception:  # noqa: S112 - the message is listed without a snippet
                continue
            if part is not None:
                by_section.setdefault(part.section, []).append((uid, part))
        texts: dict[int, str] = {}
        for section, members in by_section.items():
            data = client.fetch([uid for uid, _ in members], [f"BODY.PEEK[{section}]<0.{length}>"])
            key = f"BODY[{section}]<0>".encode()
            for uid, part in members:
                try:  # per UID: a broken part only empties that message's snippet
                    raw = data.get(uid, {}).get(key)
                    text = decode_part(raw if isinstance(raw, bytes) else b"", part)
                    texts[uid] = html_to_text(text) if part.mime_type == "text/html" else text
                except Exception:
                    texts[uid] = ""
        return texts

    def _get_message(self, client: ImapClientLike, ref: MessageRef) -> MailDetail:
        if _uidvalidity(client.select_folder(ref.folder, readonly=True)) != ref.uidvalidity:
            raise ProviderError("not_found", "UidValidityChanged")
        row = client.fetch([ref.uid], list(SUMMARY_ITEMS)).get(ref.uid)
        if row is None:
            raise ProviderError("not_found", "UnknownUid")
        received = _aware(row.get(b"INTERNALDATE")) or datetime.now(UTC)
        body = ""
        source: Literal["text/plain", "text/html-converted", "none"] = "none"
        cut = False
        parts: list[Part] = []
        text_part: Part | None = None
        try:  # each step degrades on its own; a hostile message still opens
            parts = _structure(row)
            text_part = choose_text_part(parts)
        except Exception as exc:
            log_message_skipped(ref, "mail", exc)
        if text_part is not None:
            data = client.fetch([ref.uid], [f"BODY.PEEK[{text_part.section}]<0.{MAX_TEXT_PART_BYTES}>"])
            raw = data.get(ref.uid, {}).get(f"BODY[{text_part.section}]<0>".encode())
            raw_bytes = raw if isinstance(raw, bytes) else b""
            cut = text_part.size > MAX_TEXT_PART_BYTES or len(raw_bytes) >= MAX_TEXT_PART_BYTES
            try:
                body = decode_part(raw_bytes, text_part)
                if text_part.mime_type == "text/html":
                    body, source = html_to_text(body), "text/html-converted"
                else:
                    source = "text/plain"
            except Exception as exc:
                log_message_skipped(ref, "mail", exc)
                body, source = "", "none"
        try:
            headers = _PARSER.parsebytes(_header_bytes(row))
            sender, subject = _sender_and_subject(headers)
            to = [a for _, a in _addresses(headers, "To") if a]
            cc = [a for _, a in _addresses(headers, "Cc") if a]
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
