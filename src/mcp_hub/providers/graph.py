"""Read-only Microsoft Graph mail adapter (spec 080 §6.3, rev. 4.6 S1 to S5).

Inbox only, GET only, fixed host, no redirects, no nextLink, capped streamed reads against one call deadline.
Blocking; runs through providers.base.run_blocking. Returns decoded, unsanitised text; the tool layer sanitises every
string (spec 080 §5.3)."""

import logging
import re
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Final, Literal, Protocol
from urllib.parse import quote, urlsplit

import httpx2

from mcp_hub.ids import GRAPH_ID, AnyMessageRef, GraphMessageRef
from mcp_hub.logging import log_event
from mcp_hub.providers.base import (
    MAX_TEXT_PART_BYTES,
    PROVIDER_TIMEOUT_SECONDS,
    AttachmentMeta,
    MailDetail,
    MailSummary,
    ProviderError,
    UnreadPage,
    load_json_object,
    log_message_skipped,
    placeholder_summary,
    read_capped,
)
from mcp_hub.sanitize import html_to_text

GRAPH_HOST: Final = "graph.microsoft.com"
API: Final = f"https://{GRAPH_HOST}/v1.0"
MAX_LIST_RESPONSE_BYTES: Final = 1_048_576  # L1
MAX_MESSAGE_RESPONSE_BYTES: Final = 2_097_152  # L2
MAX_LIST_ITEMS: Final = 51  # L7
RETRY_AFTER_DEFAULT: Final = 30  # L16
RETRY_AFTER_MAX: Final = 300
MAX_FOLDER_ID_CHARS: Final = 512
PREFER: Final = 'IdType="ImmutableId", outlook.body-content-type="text"'
LIST_SELECT: Final = "id,receivedDateTime,isRead,hasAttachments,from,subject,bodyPreview"
_HEADER_FIELDS: Final = "id,receivedDateTime,isRead,hasAttachments,from,toRecipients,ccRecipients,subject"
MESSAGE_SELECT: Final = _HEADER_FIELDS + ",body,parentFolderId"
FALLBACK_SELECT: Final = _HEADER_FIELDS + ",bodyPreview,parentFolderId"
ATTACHMENT_SELECT: Final = "name,contentType,size,isInline"
_RETRY_AFTER: Final = re.compile(r"[0-9]+")  # ASCII delay-seconds only; anything else is "not a number"
_log = logging.getLogger("mcp_hub.providers.graph")


class TokenSourceLike(Protocol):
    def access_token(self, deadline: float) -> str: ...
    def invalidate(self) -> None: ...


class _UnauthorizedError(Exception):
    """Graph answered 401: refresh once and retry (L15)."""


class MalformedItemError(Exception):
    """A list or message item with an unusable field; the class name is what gets logged."""


class Backoff:
    """Per-account 'not before' after 429/503 (L16); process memory, bounded by the registry."""

    def __init__(self) -> None:
        self._until: dict[str, float] = {}
        self._lock = threading.Lock()

    def check(self, account_id: str, now: float) -> None:
        with self._lock:
            if now < self._until.get(account_id, 0.0):
                raise ProviderError("upstream_error", "Throttled")

    def set(self, account_id: str, now: float, seconds: int) -> None:
        with self._lock:
            self._until[account_id] = now + seconds


BACKOFF: Final = Backoff()
INBOX_IDS: Final[dict[str, str]] = {}
_INBOX_LOCK: Final = threading.Lock()


def retry_after_seconds(value: str | None) -> int:
    text = (value or "").strip()
    if not _RETRY_AFTER.fullmatch(text):
        return RETRY_AFTER_DEFAULT  # missing, negative, HTTP-date or garbage (spec 080 §6.3)
    digits = text.lstrip("0") or "0"
    if len(digits) > 9:
        return RETRY_AFTER_MAX  # far above the ceiling; never int() an absurdly long string
    return min(max(int(digits), 1), RETRY_AFTER_MAX)


def default_client(timeout: float) -> httpx2.Client:
    return httpx2.Client(timeout=timeout, trust_env=False, follow_redirects=False)


def _provider_error(exc: Exception) -> ProviderError:
    if isinstance(exc, ProviderError):
        return exc
    cause = type(exc).__name__
    if isinstance(exc, TimeoutError | httpx2.TimeoutException):
        return ProviderError("upstream_timeout", cause)
    if isinstance(exc, OSError | httpx2.TransportError):
        return ProviderError("unreachable", cause)
    return ProviderError("upstream_error", cause)


def _str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _address(value: object) -> tuple[str | None, str | None]:
    inner = value.get("emailAddress") if isinstance(value, dict) else None
    if not isinstance(inner, dict):
        return None, None
    return _str(inner.get("name")), _str(inner.get("address"))


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


class GraphMailbox:
    def __init__(
        self,
        account_id: str,
        tokens: TokenSourceLike,
        *,
        client_factory: Callable[[float], httpx2.Client] = default_client,
        clock: Callable[[], float] = time.monotonic,
        timeout: float = PROVIDER_TIMEOUT_SECONDS,
        backoff: Backoff = BACKOFF,
        inbox_ids: dict[str, str] = INBOX_IDS,
    ) -> None:
        self._account, self._tokens, self._factory = account_id, tokens, client_factory
        self._clock, self._timeout, self._backoff, self._inbox_ids = clock, timeout, backoff, inbox_ids

    # --- plumbing -------------------------------------------------------------------------------------------------
    def _run[T](self, work: Callable[[httpx2.Client, str, float], T]) -> T:
        deadline = self._clock() + self._timeout  # one deadline for the whole call, incl. a token refresh (L18)
        try:
            self._backoff.check(self._account, self._clock())  # no refresh, no request during a back-off (L16)
            with self._factory(self._timeout) as client:
                token = self._tokens.access_token(deadline)
                try:
                    return work(client, token, deadline)
                except _UnauthorizedError:
                    self._tokens.invalidate()  # drop the rejected access token; one refresh and one retry (§6.3)
                token = self._tokens.access_token(deadline)
                try:
                    return work(client, token, deadline)
                except _UnauthorizedError:
                    self._tokens.invalidate()  # Graph rejected the refreshed token too: the next call refreshes first
                    raise ProviderError("auth_expired", "GraphUnauthorized") from None
        except Exception as exc:
            raise _provider_error(exc) from None

    def _get(
        self, client: httpx2.Client, token: str, deadline: float, path: str, params: dict[str, str], cap: int
    ) -> dict[str, object]:
        now = self._clock()
        if now > deadline:
            raise ProviderError("upstream_timeout", "CallDeadline")
        self._backoff.check(self._account, now)
        url = API + path
        parts = urlsplit(url)
        if parts.scheme != "https" or parts.hostname != GRAPH_HOST or parts.port not in (None, 443):
            raise ProviderError("upstream_error", "ForeignHost")
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "Prefer": PREFER,
        }
        # follow_redirects per request, not only in default_client: a following client would re-send the bearer
        # header to the Location host before the 3xx check below could refuse it (PR #26 review F2).
        with client.stream(
            "GET",
            url,
            params=params,
            headers=headers,
            timeout=min(deadline - now, PROVIDER_TIMEOUT_SECONDS),
            follow_redirects=False,
        ) as response:
            status = response.status_code
            if status == 200:
                if response.headers.get("content-encoding", "identity").strip().lower() not in ("", "identity"):
                    raise ProviderError("upstream_error", "ContentEncoding")
                return load_json_object(read_capped(response.iter_bytes(), cap, deadline=deadline, clock=self._clock))
            if 300 <= status < 400:
                raise ProviderError("upstream_error", "Redirect")
            if status == 401:
                raise _UnauthorizedError
            if status == 403:
                self._tokens.invalidate()  # D63: permission or licence, not authentication; next call re-fetches
                raise ProviderError("upstream_error", "Forbidden")
            if status == 404:
                raise ProviderError("not_found", "GraphNotFound")
            if status in (429, 503):
                self._backoff.set(
                    self._account, self._clock(), retry_after_seconds(response.headers.get("retry-after"))
                )
                raise ProviderError("upstream_error", "Throttled")
            raise ProviderError("upstream_error", "UnexpectedStatus")

    def _inbox_id(self, client: httpx2.Client, token: str, deadline: float, *, refresh: bool = False) -> str:
        with _INBOX_LOCK:
            cached = None if refresh else self._inbox_ids.get(self._account)
        if cached is not None:
            return cached
        body = self._get(client, token, deadline, "/me/mailFolders/inbox", {"$select": "id"}, MAX_LIST_RESPONSE_BYTES)
        folder = body.get("id")
        if not isinstance(folder, str) or not 0 < len(folder) <= MAX_FOLDER_ID_CHARS:
            raise ProviderError("upstream_error", "NoInboxId")
        with _INBOX_LOCK:
            self._inbox_ids[self._account] = folder
        return folder

    # --- Mailbox protocol -----------------------------------------------------------------------------------------
    def check(self) -> None:
        """Status check (spec 080 §7.4, rev. 4.6 S10): token (refresh only near expiry) + inbox folder id."""
        self._run(lambda client, token, deadline: self._inbox_id(client, token, deadline, refresh=True))

    def list_unread(self, since: datetime, limit: int) -> UnreadPage:
        return self._run(lambda client, token, deadline: self._list(client, token, deadline, since, limit))

    def get_message(self, ref: AnyMessageRef) -> MailDetail:
        if not isinstance(ref, GraphMessageRef) or ref.account != self._account:
            raise ProviderError("not_found", "ForeignRef")
        return self._run(lambda client, token, deadline: self._message(client, token, deadline, ref))

    # --- list -----------------------------------------------------------------------------------------------------
    def _list(self, client: httpx2.Client, token: str, deadline: float, since: datetime, limit: int) -> UnreadPage:
        floor = since.astimezone(UTC).replace(microsecond=0)
        params = {
            "$filter": f"receivedDateTime ge {floor:%Y-%m-%dT%H:%M:%SZ} and isRead eq false",
            "$orderby": "receivedDateTime desc",
            "$select": LIST_SELECT,
            "$top": str(limit + 1),
        }
        body = self._get(client, token, deadline, "/me/mailFolders/inbox/messages", params, MAX_LIST_RESPONSE_BYTES)
        values = body.get("value")
        if not isinstance(values, list):
            raise ProviderError("upstream_error", "MalformedList")
        items: list[MailSummary] = []
        seen: set[AnyMessageRef] = set()
        for raw in values[:MAX_LIST_ITEMS]:
            summary = self._summary(raw, since)
            if summary is not None and summary.ref not in seen:  # a repeated Graph id keeps its first entry
                seen.add(summary.ref)
                items.append(summary)
        items.sort(key=lambda s: s.received_at, reverse=True)
        more = len(values) > limit or "@odata.nextLink" in body or len(items) > limit
        return UnreadPage(items=items[:limit], more=more)

    def _degraded(self, exc: Exception) -> None:
        # No valid id: item_degraded without the `item` field (spec 080 rev. 4.6 S12).
        log_event(
            _log,
            logging.WARNING,
            "item_degraded",
            account=self._account,
            capability="mail",
            exception=type(exc).__name__,
        )

    def _summary(self, raw: object, since: datetime) -> MailSummary | None:
        if not isinstance(raw, dict):
            self._degraded(MalformedItemError())
            return None
        graph_id = raw.get("id")
        if not isinstance(graph_id, str) or not GRAPH_ID.fullmatch(graph_id):
            self._degraded(MalformedItemError())
            return None
        ref = GraphMessageRef(self._account, graph_id)
        received = _timestamp(raw.get("receivedDateTime"))
        if received is None:
            log_message_skipped(ref, "mail", MalformedItemError())
            return None
        if received < since or raw.get("isRead") is True:  # defensive re-filter of the server's answer
            return None
        try:
            name, address = _address(raw.get("from"))
            return MailSummary(
                ref=ref,
                received_at=received,
                unread=True,
                has_attachments=raw.get("hasAttachments") is True,
                from_address=address,
                from_name=name,
                subject=_str(raw.get("subject")),
                snippet_text=_str(raw.get("bodyPreview")) or "",
            )
        except Exception as exc:
            log_message_skipped(ref, "mail", exc)
            return placeholder_summary(ref, received, True)

    # --- message --------------------------------------------------------------------------------------------------
    def _message(self, client: httpx2.Client, token: str, deadline: float, ref: GraphMessageRef) -> MailDetail:
        path = f"/me/messages/{quote(ref.graph_id, safe='')}"
        preview_only = False
        try:
            body = self._get(client, token, deadline, path, {"$select": MESSAGE_SELECT}, MAX_MESSAGE_RESPONSE_BYTES)
        except ProviderError as exc:
            if exc.code != "too_large":
                raise
            preview_only = True
            body = self._get(client, token, deadline, path, {"$select": FALLBACK_SELECT}, MAX_LIST_RESPONSE_BYTES)
        parent = body.get("parentFolderId")
        # A12: comparable ids are assumed (checked at the first live call, V3); a mismatch fails closed (SL6).
        if parent != self._inbox_id(client, token, deadline) and parent != self._inbox_id(
            client, token, deadline, refresh=True
        ):
            raise ProviderError("not_found", "NotInInbox")
        text, source, cut = self._body(body, preview_only)
        name, address = _address(body.get("from"))
        has_attachments = body.get("hasAttachments") is True
        return MailDetail(
            ref=ref,
            # Same fallback as the IMAP adapter (imap.py _get_message): a single read has no list timestamp.
            received_at=_timestamp(body.get("receivedDateTime")) or datetime.now(UTC),
            unread=body.get("isRead") is not True,
            attachments=self._attachments(client, token, deadline, path, ref) if has_attachments else [],
            from_address=address,
            from_name=name,
            to_addresses=self._recipients(body.get("toRecipients")),
            cc_addresses=self._recipients(body.get("ccRecipients")),
            subject=_str(body.get("subject")),
            body_text=text,
            body_source=source,
            body_cut=cut,
        )

    @staticmethod
    def _recipients(value: object) -> list[str]:
        if not isinstance(value, list):
            return []
        return [a for item in value if (a := _address(item)[1])]

    @staticmethod
    def _body(
        message: dict[str, object], preview_only: bool
    ) -> tuple[str, Literal["text/plain", "text/html-converted", "none"], bool]:
        if preview_only:
            return _str(message.get("bodyPreview")) or "", "text/plain", True
        body = message.get("body")
        content = body.get("content") if isinstance(body, dict) else None
        if not isinstance(content, str):
            return "", "none", False
        encoded = content.encode("utf-8", "surrogatepass")
        cut = len(encoded) > MAX_TEXT_PART_BYTES  # L10
        text = encoded[:MAX_TEXT_PART_BYTES].decode("utf-8", "ignore") if cut else content
        if isinstance(body, dict) and body.get("contentType") == "html":
            return html_to_text(text), "text/html-converted", cut
        return text, "text/plain", cut

    def _attachments(
        self, client: httpx2.Client, token: str, deadline: float, path: str, ref: GraphMessageRef
    ) -> list[AttachmentMeta]:
        try:
            body = self._get(
                client, token, deadline, f"{path}/attachments", {"$select": ATTACHMENT_SELECT}, MAX_LIST_RESPONSE_BYTES
            )
            values = body.get("value")
            if not isinstance(values, list):
                raise MalformedItemError
        except ProviderError as exc:
            # The call deadline fails the whole call. A 401 is _UnauthorizedError, not a ProviderError: it reaches _run.
            if exc.code == "upstream_timeout":
                raise
            log_message_skipped(ref, "mail", exc)
            return []
        except MalformedItemError as exc:
            log_message_skipped(ref, "mail", exc)
            return []
        found: list[AttachmentMeta] = []
        for item in values:
            if not isinstance(item, dict) or item.get("isInline") is True:
                continue
            size = item.get("size")
            size_bytes = size if isinstance(size, int) and not isinstance(size, bool) and size >= 0 else 0
            found.append(AttachmentMeta(size_bytes, _str(item.get("name")), _str(item.get("contentType"))))
        return found
