import gzip
import json

import httpx2
import pytest

from mcp_hub.providers import msidentity
from mcp_hub.providers.base import ProviderError, load_json_object
from mcp_hub.providers.msidentity import (
    GrantError,
    GraphAccount,
    LoginFailedError,
    poll_device_code,
    refresh,
    start_device_code,
)
from tests.support.ms_transport import FAKE_CLIENT_ID, MsTransport, json_answer, streamed

ACCOUNT = GraphAccount("outlook", "microsoft", "consumers", FAKE_CLIENT_ID, ("Mail.Read", "offline_access"))
RT, AT = "RT-SENTINEL-1", "AT-SENTINEL-1"
OK = {
    "token_type": "Bearer",
    "access_token": AT,
    "refresh_token": "RT-SENTINEL-2",
    "expires_in": 3599,
    "scope": "Mail.Read",
}
TOKEN, DEVICE = "/consumers/oauth2/v2.0/token", "/consumers/oauth2/v2.0/devicecode"
FAR = 10.0**6  # deadline far in the future on the fake clock 0.0


def client(t: MsTransport) -> httpx2.Client:
    return httpx2.Client(transport=t.transport(), trust_env=False, follow_redirects=False)


def do_refresh(t: MsTransport, deadline: float = FAR, clock=lambda: 0.0):  # type: ignore[no-untyped-def]
    return refresh(client(t), ACCOUNT, RT, deadline=deadline, clock=clock)


def test_refresh_sends_exact_form_to_login_host_only() -> None:
    t = MsTransport({TOKEN: [json_answer(200, OK)]})
    answer = do_refresh(t)
    assert answer.refresh_token == "RT-SENTINEL-2"
    assert answer.expires_in == 3599
    [(host, port, method, path, form, headers)] = t.seen
    assert (host, port, method, path) == ("login.microsoftonline.com", 443, "POST", TOKEN)
    assert form == {
        "client_id": FAKE_CLIENT_ID,
        "grant_type": "refresh_token",
        "refresh_token": RT,
        "scope": "Mail.Read offline_access",
    }
    assert "authorization" not in headers
    assert headers["accept-encoding"] == "identity"


@pytest.mark.parametrize(
    "error", ["invalid_grant", "interaction_required", "consent_required", "invalid_client", "weird"]
)
def test_refresh_errors_carry_only_known_codes_status_and_number(error: str) -> None:
    body = {
        "error": error,
        "error_description": "AADSTS70000: secret-ish text RT-SENTINEL-1",
        "error_codes": [70000, "x"],
    }
    t = MsTransport({TOKEN: [json_answer(400, body)]})
    with pytest.raises(GrantError) as info:
        do_refresh(t)
    assert info.value.error == (error if error != "weird" else "other")
    assert (info.value.status, info.value.error_code) == (400, 70000)
    assert "AADSTS" not in str(info.value)
    assert RT not in str(info.value)


def test_redirect_is_not_followed() -> None:
    t = MsTransport({TOKEN: [httpx2.Response(307, headers={"Location": "https://evil.example.test/t"})]})
    with pytest.raises(ProviderError) as info:
        do_refresh(t)
    assert (info.value.code, info.value.cause) == ("upstream_error", "Redirect")
    assert len(t.seen) == 1


def test_default_client_never_follows_redirects_or_reads_proxies() -> None:
    with msidentity.default_client(5.0) as c:
        assert c.follow_redirects is False
        assert c.trust_env is False


def test_redirect_is_not_followed_even_by_a_following_client() -> None:
    """The refresh-token form must never be re-sent to a Location host, whatever the client factory sets (F2)."""
    t = MsTransport({TOKEN: [httpx2.Response(307, headers={"Location": "https://evil.example.test/t"})]})
    following = httpx2.Client(transport=t.transport(), trust_env=False, follow_redirects=True)
    with pytest.raises(ProviderError) as info:
        refresh(following, ACCOUNT, RT, deadline=FAR, clock=lambda: 0.0)
    assert info.value.cause == "Redirect"
    assert [(host, path) for host, _, _, path, _, _ in t.seen] == [("login.microsoftonline.com", TOKEN)]


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example.test/consumers/oauth2/v2.0/token",
        "https://login.microsoftonline.com.evil.test/consumers/oauth2/v2.0/token",
        "https://evilmicrosoftonline.com/consumers/oauth2/v2.0/token",
        "https://other.microsoftonline.com/consumers/oauth2/v2.0/token",
        "https://login.microsoftonline.com:8443/consumers/oauth2/v2.0/token",
        "http://login.microsoftonline.com/consumers/oauth2/v2.0/token",
    ],
)
def test_post_refuses_foreign_hosts_before_sending(url: str) -> None:
    t = MsTransport({TOKEN: [json_answer(200, OK)]})
    with pytest.raises(ProviderError) as info:
        msidentity._post(client(t), url, {"refresh_token": RT}, 65_536, FAR, lambda: 0.0)
    assert (info.value.code, info.value.cause) == ("upstream_error", "ForeignHost")
    assert t.seen == []


def test_compressed_answer_refused_before_reading() -> None:
    response, stream = streamed(200, [gzip.compress(json.dumps(OK).encode())], {"Content-Encoding": "gzip"})
    t = MsTransport({TOKEN: [response]})
    with pytest.raises(ProviderError) as info:
        do_refresh(t)
    assert info.value.cause == "ContentEncoding"
    assert stream.served == 0


def test_oversized_answer_stops_after_first_chunk_beyond_cap() -> None:
    response, stream = streamed(200, [b" " * 16_384] * 10)
    t = MsTransport({TOKEN: [response]})
    with pytest.raises(ProviderError) as info:
        do_refresh(t)
    assert info.value.code == "too_large"
    assert stream.served == 5


def test_deadline_crossed_while_reading_stores_nothing() -> None:
    now = [0.0]

    def tick() -> float:
        now[0] += 1.0
        return now[0]

    response, _ = streamed(200, [b"{"] + [b" "] * 50 + [b"}"])
    t = MsTransport({TOKEN: [response]})
    with pytest.raises(ProviderError) as info:
        refresh(client(t), ACCOUNT, RT, deadline=10.0, clock=tick)
    assert (info.value.code, info.value.cause) == ("upstream_timeout", "CallDeadline")


def test_no_request_after_deadline() -> None:
    t = MsTransport({TOKEN: [json_answer(200, OK)]})
    with pytest.raises(ProviderError):
        do_refresh(t, deadline=-1.0)
    assert t.seen == []


@pytest.mark.parametrize("raw", [b"[" * 100_000, b"not json", b"[1]"])
def test_malformed_answers(raw: bytes) -> None:
    t = MsTransport({TOKEN: [httpx2.Response(200, content=raw)]})
    with pytest.raises(ProviderError) as info:
        do_refresh(t)
    assert info.value.code in ("too_large", "upstream_error")


@pytest.mark.parametrize(
    "patch",
    [{"refresh_token": None}, {"refresh_token": "x" * 16_385}, {"access_token": 5}, {"access_token": ""}],
)
def test_bad_token_fields_store_nothing(patch: dict[str, object]) -> None:
    body = {k: v for k, v in (OK | patch).items() if v is not None}
    t = MsTransport({TOKEN: [json_answer(200, body)]})
    with pytest.raises(ProviderError):
        do_refresh(t)


def test_long_scope_is_stored_empty() -> None:
    t = MsTransport({TOKEN: [json_answer(200, OK | {"scope": "x" * 1_025})]})
    assert do_refresh(t).scope == ""


@pytest.mark.parametrize(("value", "expected"), [(5, 60), (10**9, 86_400), ("3600", 3600), (True, 3600)])
def test_expires_in_clamped(value: object, expected: int) -> None:
    t = MsTransport({TOKEN: [json_answer(200, OK | {"expires_in": value})]})
    assert do_refresh(t).expires_in == expected


def device(**extra: object) -> dict[str, object]:
    return {
        "device_code": "DC-SENTINEL",
        "user_code": "ABCD-EFGH",
        "verification_uri": "https://microsoft.com/devicelogin",
        "expires_in": 900,
        "interval": 5,
        "message": "ignored",
    } | extra


def start(t: MsTransport):  # type: ignore[no-untyped-def]
    c = client(t)
    return c, start_device_code(c, ACCOUNT, deadline=FAR, clock=lambda: 0.0)


def test_device_flow_pending_slow_down_then_success() -> None:
    t = MsTransport(
        {
            DEVICE: [json_answer(200, device())],
            TOKEN: [
                json_answer(400, {"error": "authorization_pending"}),
                json_answer(400, {"error": "slow_down"}),
                json_answer(200, OK),
            ],
        }
    )
    slept: list[float] = []
    c, code = start(t)
    answer = poll_device_code(c, ACCOUNT, code, sleep=slept.append, clock=lambda: 0.0)
    assert answer.access_token == AT
    assert slept == [5, 5, 10]
    assert t.seen[0][4] == {"client_id": FAKE_CLIENT_ID, "scope": "Mail.Read offline_access"}
    assert t.seen[1][4] == {
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        "client_id": FAKE_CLIENT_ID,
        "device_code": "DC-SENTINEL",
    }


def test_server_interval_is_a_floor_not_clamped() -> None:
    t = MsTransport({DEVICE: [json_answer(200, device(interval=45))], TOKEN: [json_answer(200, OK)]})
    slept: list[float] = []
    c, code = start(t)
    poll_device_code(c, ACCOUNT, code, sleep=slept.append, clock=lambda: 0.0)
    assert slept == [45]


def test_huge_interval_ends_without_busy_loop() -> None:
    t = MsTransport({DEVICE: [json_answer(200, device(interval=10**6))], TOKEN: [json_answer(200, OK)]})
    slept: list[float] = []
    c, code = start(t)
    with pytest.raises(LoginFailedError) as info:
        poll_device_code(c, ACCOUNT, code, sleep=slept.append, clock=lambda: 0.0)
    assert info.value.reason == "timeout"
    assert slept == []
    assert len(t.seen) == 1


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        ("authorization_declined", "declined"),
        ("access_denied", "declined"),
        ("expired_token", "expired"),
        ("bad_verification_code", "expired"),
        ("invalid_client", "refused"),
        ("weird", "error"),
    ],
)
def test_device_flow_terminal_errors(error: str, reason: str) -> None:
    t = MsTransport({DEVICE: [json_answer(200, device())], TOKEN: [json_answer(400, {"error": error})]})
    c, code = start(t)
    with pytest.raises(LoginFailedError) as info:
        poll_device_code(c, ACCOUNT, code, sleep=lambda s: None, clock=lambda: 0.0)
    assert info.value.reason == reason


def test_device_flow_times_out() -> None:
    now = [0.0]
    t = MsTransport(
        {
            DEVICE: [json_answer(200, device(expires_in=60))],
            TOKEN: [json_answer(400, {"error": "authorization_pending"})],
        }
    )
    c, code = start(t)

    def sleep(s: float) -> None:
        now[0] += s

    with pytest.raises(LoginFailedError) as info:
        poll_device_code(c, ACCOUNT, code, sleep=sleep, clock=lambda: now[0])
    assert info.value.reason == "timeout"
    assert len(t.seen) == 1 + 12


@pytest.mark.parametrize(
    "uri",
    [
        "https://microsoft.com/devicelogin",
        "https://login.microsoft.com/device",
        "https://www.microsoft.com/link",
        "https://login.live.com/oauth20_remoteconnect.srf",
        "https://login.microsoftonline.com/common/oauth2/deviceauth",
        "https://microsoft.com:443/devicelogin",
    ],
)
def test_allowed_verification_hosts(uri: str) -> None:
    t = MsTransport({DEVICE: [json_answer(200, device(verification_uri=uri))]})
    assert start(t)[1].verification_uri == uri


@pytest.mark.parametrize(
    ("patch", "host"),
    [
        ({"verification_uri": "https://evil.example.test/devicelogin"}, "evil.example.test"),
        ({"verification_uri": "https://microsoft.com.evil.test/x"}, "microsoft.com.evil.test"),
        ({"verification_uri": "https://notmicrosoft.com/x"}, "notmicrosoft.com"),
        ({"verification_uri": "http://microsoft.com/devicelogin"}, None),
        ({"verification_uri": "https://microsoft.com/\x1b[2J"}, None),
        ({"verification_uri": "https://microsoft.com:8443/x"}, None),
        ({"user_code": "AB\x1b[2J"}, None),
        ({"user_code": "x" * 40}, None),
        ({"device_code": 7}, None),
    ],
)
def test_device_answer_validated_before_printing(patch: dict[str, object], host: str | None) -> None:
    t = MsTransport({DEVICE: [json_answer(200, device(**patch))]})
    with pytest.raises(LoginFailedError) as info:
        start(t)
    if host is not None:
        assert (info.value.reason, info.value.host) == ("unexpected_host", host)
    assert "ABCD" not in str(info.value)
    assert "DC-SENTINEL" not in str(info.value)


def test_load_json_object_rejects_non_objects() -> None:
    assert load_json_object(json.dumps({"a": 1}).encode()) == {"a": 1}
    for raw in (b"[]", b"1", b"\xff", b"{" * 50_000):
        with pytest.raises(ProviderError):
            load_json_object(raw)


@pytest.mark.parametrize(
    "uri",
    [
        "https://evil.example\\@microsoft.com/devicelogin",  # urlsplit: microsoft.com; a browser: evil.example
        "https://evil.example\\.microsoft.com/x",
        "https://someone@microsoft.com/devicelogin",
        "https://user:pw@login.live.com/x",
        "https://microsoft.com\\@evil.example.test/x",
    ],
)
def test_backslash_and_userinfo_are_refused_before_the_host_check(uri: str) -> None:
    t = MsTransport({DEVICE: [json_answer(200, device(verification_uri=uri))]})
    with pytest.raises(LoginFailedError) as info:
        start(t)
    assert info.value.reason == "unexpected_host"
    assert info.value.host is None


def test_secrets_absent_from_reprs() -> None:
    t = MsTransport({TOKEN: [json_answer(200, OK)], DEVICE: [json_answer(200, device())]})
    text = repr(do_refresh(t)) + repr(start(t)[1]) + repr(ACCOUNT)
    for secret in ("AT-SENTINEL-1", "RT-SENTINEL-2", "DC-SENTINEL", "ABCD-EFGH", FAKE_CLIENT_ID):
        assert secret not in text


def poll_with(answers: list[httpx2.Response], expires_in: int = 900) -> tuple[MsTransport, list[float], object]:
    """Poll on a fake clock that advances with every sleep; returns the transport, the sleeps and the outcome."""
    now = [0.0]
    t = MsTransport({DEVICE: [json_answer(200, device(expires_in=expires_in))], TOKEN: answers})
    c = client(t)
    code = start_device_code(c, ACCOUNT, deadline=FAR, clock=lambda: now[0])
    slept: list[float] = []

    def sleep(s: float) -> None:
        slept.append(s)
        now[0] += s

    try:
        outcome: object = poll_device_code(c, ACCOUNT, code, sleep=sleep, clock=lambda: now[0])
    except LoginFailedError as exc:
        outcome = exc
    return t, slept, outcome


def connect_error(request: httpx2.Request) -> httpx2.Response:
    raise httpx2.ConnectError("down")


@pytest.mark.parametrize(
    "transient",
    [json_answer(503, {}), json_answer(400, {"error": "temporarily_unavailable"})],
)
def test_server_errors_while_polling_are_transient(transient: httpx2.Response) -> None:
    _, slept, outcome = poll_with([transient, json_answer(200, OK)])
    assert getattr(outcome, "access_token", None) == AT
    assert slept == [5, 5]


def test_every_poll_failing_is_unreachable_not_timeout() -> None:
    now = [0.0]
    t = MsTransport({DEVICE: [json_answer(200, device(expires_in=60))]})
    c = client(t)
    code = start_device_code(c, ACCOUNT, deadline=FAR, clock=lambda: now[0])
    down = httpx2.Client(transport=httpx2.MockTransport(connect_error), trust_env=False)

    def sleep(s: float) -> None:
        now[0] += s

    with pytest.raises(LoginFailedError) as info:
        poll_device_code(down, ACCOUNT, code, sleep=sleep, clock=lambda: now[0])
    assert info.value.reason == "unreachable"
    assert info.value.cause == "ConnectError"


def test_some_polls_failing_still_ends_as_timeout() -> None:
    _, _, outcome = poll_with([json_answer(503, {}), json_answer(400, {"error": "authorization_pending"})], 60)
    assert isinstance(outcome, LoginFailedError)
    assert outcome.reason == "timeout"


@pytest.mark.parametrize(
    ("answer", "cause"),
    [(httpx2.Response(200, content=b"not json"), "MalformedJson"), (json_answer(200, {"x": 1}), "BadTokenAnswer")],
)
def test_bad_poll_answers_carry_their_cause(answer: httpx2.Response, cause: str) -> None:
    _, _, outcome = poll_with([answer])
    assert isinstance(outcome, LoginFailedError)
    assert (outcome.reason, outcome.cause) == ("error", cause)


def test_device_code_request_failure_carries_its_cause() -> None:
    down = httpx2.Client(transport=httpx2.MockTransport(connect_error), trust_env=False)
    with pytest.raises(LoginFailedError) as info:
        start_device_code(down, ACCOUNT, deadline=FAR, clock=lambda: 0.0)
    assert (info.value.reason, info.value.cause) == ("error", "ConnectError")


def test_unknown_poll_error_carries_the_token_endpoint_cause() -> None:
    _, _, outcome = poll_with([json_answer(400, {"error": "weird"})])
    assert isinstance(outcome, LoginFailedError)
    assert (outcome.reason, outcome.cause) == ("error", "TokenEndpoint")


def test_non_transient_transport_failure_while_polling_stops_with_its_cause() -> None:
    t, _, outcome = poll_with([httpx2.Response(307, headers={"Location": "https://evil.example.test/t"})])
    assert isinstance(outcome, LoginFailedError)
    assert (outcome.reason, outcome.cause) == ("error", "Redirect")
    assert len([s for s in t.seen if s[3] == TOKEN]) == 1


def test_temporarily_unavailable_on_every_poll_is_unreachable() -> None:
    _, _, outcome = poll_with([json_answer(400, {"error": "temporarily_unavailable"})], 60)
    assert isinstance(outcome, LoginFailedError)
    assert (outcome.reason, outcome.cause) == ("unreachable", "TemporarilyUnavailable")


def test_refused_device_code_request_has_no_cause() -> None:
    t = MsTransport({DEVICE: [json_answer(400, {"error": "invalid_client"})]})
    with pytest.raises(LoginFailedError) as info:
        start(t)
    assert (info.value.reason, info.value.cause) == ("refused", None)
