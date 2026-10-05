"""Microsoft identity platform client for Graph accounts (spec 080 §6.3, §7.3): device authorization grant and the
refresh-token grant as plain form POSTs over httpx2. Requests go only to https://login.microsoftonline.com:443; no
redirect is followed, proxies from the environment are ignored, bodies are read as capped streams against one call
deadline, and only known error codes plus the first numeric AADSTS code leave this module (never error_description)."""

import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Final, Literal
from urllib.parse import urlsplit

import httpx2

from mcp_hub.providers.base import ProviderError, load_json_object, read_capped

LOGIN_HOST: Final = "login.microsoftonline.com"
MAX_TOKEN_RESPONSE_BYTES: Final = 65_536  # L3
MAX_DEVICE_RESPONSE_BYTES: Final = 16_384  # L4
MAX_TOKEN_BYTES: Final = 16_384  # L14: UTF-8 bytes, the same limit as crypto.seal
MAX_SCOPE_CHARS: Final = 1_024
REQUEST_TIMEOUT_SECONDS: Final = 20.0
TOKEN_CONNECT_TIMEOUT_SECONDS: Final = 5.0  # N3: short per-phase timeouts bound the overshoot past the deadline
TOKEN_READ_TIMEOUT_SECONDS: Final = 5.0
DEVICE_GRANT: Final = "urn:ietf:params:oauth:grant-type:device_code"
REVOKED_GRANT_ERRORS: Final = frozenset({"invalid_grant", "interaction_required", "consent_required"})
CLIENT_ERRORS: Final = frozenset(
    {"invalid_client", "unauthorized_client", "invalid_scope", "invalid_request", "unsupported_grant_type"}
)
_KNOWN_ERRORS: Final = (
    REVOKED_GRANT_ERRORS
    | CLIENT_ERRORS
    | {
        "authorization_pending",
        "slow_down",
        "authorization_declined",
        "access_denied",
        "expired_token",
        "bad_verification_code",
        "temporarily_unavailable",
    }
)
_TENANT: Final = re.compile(r"^(consumers|organizations|[0-9a-f-]{36})$")
_USER_CODE: Final = re.compile(r"^[A-Za-z0-9-]{4,32}$")  # L23
_PRINTABLE_URI: Final = re.compile(r"^[\x21-\x7e]{1,200}$")
_PRINTABLE_HOST: Final = re.compile(r"^[a-z0-9.-]{1,253}$")
_VERIFICATION_SUFFIXES: Final = ("microsoft.com", "live.com", "microsoftonline.com")  # P3: exact or subdomain
BAD_DEVICE_ANSWER: Final = "BadDeviceAnswer"  # one fixed login cause for every malformed device-code answer


@dataclass(frozen=True, slots=True)
class GraphAccount:
    account_id: str
    provider: str
    tenant: str
    client_id: str = field(repr=False)
    scopes: tuple[str, ...]

    @property
    def cache_key(self) -> tuple[str, str, str]:
        return (self.account_id, self.tenant, self.client_id)


@dataclass(frozen=True, slots=True)
class TokenAnswer:
    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    expires_in: int
    scope: str


@dataclass(frozen=True, slots=True)
class DeviceCode:
    device_code: str = field(repr=False)
    user_code: str = field(repr=False)
    verification_uri: str
    expires_in: int
    interval: int


def _first_code(value: object) -> int | None:
    if isinstance(value, list) and value and type(value[0]) is int and 0 < value[0] < 10**8:
        return value[0]
    return None


class GrantError(Exception):
    def __init__(self, error: object, status: int = 400, error_code: int | None = None) -> None:
        self.error = error if isinstance(error, str) and error in _KNOWN_ERRORS else "other"
        self.status, self.error_code = status, error_code
        super().__init__(self.error)


class LoginFailedError(Exception):
    """`cause` is a class name or a fixed hub cause, never message text."""

    def __init__(
        self,
        reason: Literal["declined", "expired", "timeout", "refused", "unexpected_host", "unreachable", "error"],
        host: str | None = None,
        cause: str | None = None,
    ) -> None:
        self.reason, self.host, self.cause = reason, host, cause
        super().__init__(reason)


def default_client(timeout: float) -> httpx2.Client:
    return httpx2.Client(timeout=timeout, trust_env=False, follow_redirects=False)


def endpoint(tenant: str, path: Literal["token", "devicecode"]) -> str:
    if not _TENANT.fullmatch(tenant):
        raise ProviderError("upstream_error", "BadTenant")
    return f"https://{LOGIN_HOST}/{tenant}/oauth2/v2.0/{path}"


def _post(
    client: httpx2.Client, url: str, form: dict[str, str], cap: int, deadline: float, clock: Callable[[], float]
) -> tuple[int, dict[str, object]]:
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.hostname != LOGIN_HOST or parts.port not in (None, 443):
        raise ProviderError("upstream_error", "ForeignHost")
    remaining = deadline - clock()
    if remaining <= 0:
        raise ProviderError("upstream_timeout", "CallDeadline")
    try:
        headers = {"Accept": "application/json", "Accept-Encoding": "identity"}
        phase = min(remaining, TOKEN_READ_TIMEOUT_SECONDS)
        timeout = httpx2.Timeout(
            min(remaining, REQUEST_TIMEOUT_SECONDS),
            connect=min(remaining, TOKEN_CONNECT_TIMEOUT_SECONDS),
            read=phase,
            write=phase,
        )
        # Per request, not only in default_client: a following client would re-send the form (refresh token) to the
        # Location host before the 3xx check below could refuse it (F2).
        with client.stream(
            "POST", url, data=form, headers=headers, timeout=timeout, follow_redirects=False
        ) as response:
            status = response.status_code
            if 300 <= status < 400:
                raise ProviderError("upstream_error", "Redirect")
            if status >= 500:
                raise ProviderError("upstream_error", "TokenEndpointStatus")
            if response.headers.get("content-encoding", "identity").strip().lower() not in ("", "identity"):
                raise ProviderError("upstream_error", "ContentEncoding")
            raw = read_capped(response.iter_bytes(), cap, deadline=deadline, clock=clock)
    except ProviderError:
        raise
    except (TimeoutError, httpx2.TimeoutException) as exc:
        raise ProviderError("upstream_timeout", type(exc).__name__) from None
    except (OSError, httpx2.TransportError) as exc:
        raise ProviderError("unreachable", type(exc).__name__) from None
    return status, load_json_object(raw)


def _fits(value: object, limit: int) -> bool:
    """A non-empty string of at most `limit` UTF-8 bytes; a lone surrogate (valid in JSON) cannot be encoded."""
    if not isinstance(value, str):
        return False
    try:
        return 0 < len(value.encode()) <= limit
    except UnicodeEncodeError:
        return False


def _token_string(body: dict[str, object], name: str, limit: int) -> str:
    value = body.get(name)
    if not isinstance(value, str) or not _fits(value, limit):
        raise ProviderError("upstream_error", "BadTokenAnswer")
    return value


def _int(value: object, low: int, high: int, default: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, str) and value.isascii() and value.isdigit() and len(value) <= 9:
        value = int(value)
    return min(max(value, low), high) if isinstance(value, int) else default


def _answer(body: dict[str, object]) -> TokenAnswer:
    scope = body.get("scope")
    return TokenAnswer(
        access_token=_token_string(body, "access_token", MAX_TOKEN_BYTES),
        refresh_token=_token_string(body, "refresh_token", MAX_TOKEN_BYTES),
        expires_in=_int(body.get("expires_in"), 60, 86_400, 3_600),  # L13
        scope=scope if isinstance(scope, str) and len(scope) <= MAX_SCOPE_CHARS else "",  # L14: informational
    )


def _grant_error(status: int, body: dict[str, object]) -> GrantError:
    return GrantError(body.get("error"), status, _first_code(body.get("error_codes")))


def refresh(
    client: httpx2.Client,
    account: GraphAccount,
    refresh_token: str,
    *,
    deadline: float,
    clock: Callable[[], float] = time.monotonic,
) -> TokenAnswer:
    form = {
        "client_id": account.client_id,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "scope": " ".join(account.scopes),
    }
    status, body = _post(client, endpoint(account.tenant, "token"), form, MAX_TOKEN_RESPONSE_BYTES, deadline, clock)
    if status != 200:
        raise _grant_error(status, body)
    return _answer(body)


def _verification_uri(value: object) -> str:
    if not isinstance(value, str) or not _PRINTABLE_URI.fullmatch(value):
        raise LoginFailedError("error", cause=BAD_DEVICE_ANSWER)
    parts = urlsplit(value)
    host = (parts.hostname or "").lower()
    try:
        port = parts.port
    except ValueError:
        raise LoginFailedError("error", cause=BAD_DEVICE_ANSWER) from None
    if parts.scheme != "https" or port not in (None, 443):
        raise LoginFailedError("error", cause=BAD_DEVICE_ANSWER)
    # Browsers read a backslash as "/" in https URLs (WHATWG) and userinfo hides the real host from a reader: refuse
    # both, and require a plain host name before the suffix check, so the printed address opens the host checked (F1).
    if "\\" in value or "@" in parts.netloc or not _PRINTABLE_HOST.fullmatch(host):
        raise LoginFailedError("unexpected_host")
    if not any(host == s or host.endswith("." + s) for s in _VERIFICATION_SUFFIXES):
        raise LoginFailedError("unexpected_host", host)
    return value


def start_device_code(
    client: httpx2.Client, account: GraphAccount, *, deadline: float, clock: Callable[[], float] = time.monotonic
) -> DeviceCode:
    form = {"client_id": account.client_id, "scope": " ".join(account.scopes)}
    try:
        status, body = _post(
            client, endpoint(account.tenant, "devicecode"), form, MAX_DEVICE_RESPONSE_BYTES, deadline, clock
        )
    except ProviderError as exc:
        raise LoginFailedError("error", cause=exc.cause) from None
    if status != 200:
        if _grant_error(status, body).error in CLIENT_ERRORS:
            raise LoginFailedError("refused")
        raise LoginFailedError("error", cause="TokenEndpoint")
    device_code, user_code = body.get("device_code"), body.get("user_code")
    if not (isinstance(device_code, str) and _fits(device_code, MAX_TOKEN_BYTES)):
        raise LoginFailedError("error", cause=BAD_DEVICE_ANSWER)
    if not (isinstance(user_code, str) and _USER_CODE.fullmatch(user_code)):
        raise LoginFailedError("error", cause=BAD_DEVICE_ANSWER)
    uri = _verification_uri(body.get("verification_uri"))
    return DeviceCode(
        device_code,
        user_code,
        uri,
        _int(body.get("expires_in"), 60, 900, 900),
        _int(body.get("interval"), 1, 86_400, 5),
    )


def poll_device_code(
    client: httpx2.Client,
    account: GraphAccount,
    code: DeviceCode,
    *,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> TokenAnswer:
    deadline = clock() + min(code.expires_in, 900)  # L22
    interval = max(code.interval, 1)  # the server's interval is a floor (P8)
    form = {"grant_type": DEVICE_GRANT, "client_id": account.client_id, "device_code": code.device_code}
    answered = False  # at least one poll got a proper OAuth answer (pending / slow_down)
    last_failure: str | None = None
    while True:
        if clock() + interval > deadline:
            if not answered and last_failure is not None:
                raise LoginFailedError("unreachable", cause=last_failure)
            raise LoginFailedError("timeout")
        sleep(interval)
        try:
            status, body = _post(
                client,
                endpoint(account.tenant, "token"),
                form,
                MAX_TOKEN_RESPONSE_BYTES,
                min(clock() + REQUEST_TIMEOUT_SECONDS, deadline + REQUEST_TIMEOUT_SECONDS),
                clock,
            )
        except ProviderError as exc:
            if exc.code in ("unreachable", "upstream_timeout") or exc.cause == "TokenEndpointStatus":
                last_failure = exc.cause  # transient; the deadline still bounds the loop
                continue
            raise LoginFailedError("error", cause=exc.cause) from None
        if status == 200:
            try:
                return _answer(body)
            except ProviderError as exc:
                raise LoginFailedError("error", cause=exc.cause) from None
        error = _grant_error(status, body).error
        if error == "temporarily_unavailable":
            last_failure = "TemporarilyUnavailable"
            continue
        if error == "authorization_pending":
            answered = True
            continue
        if error == "slow_down":
            answered = True
            interval += 5
            continue
        if error in ("authorization_declined", "access_denied"):
            raise LoginFailedError("declined")
        if error in ("expired_token", "bad_verification_code"):
            raise LoginFailedError("expired")
        if error in CLIENT_ERRORS:
            raise LoginFailedError("refused")
        raise LoginFailedError("error", cause="TokenEndpoint")
