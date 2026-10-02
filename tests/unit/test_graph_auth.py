import base64
import json
import logging
import threading

import httpx2
import pytest

from mcp_hub.logging import configure_logging
from mcp_hub.providers.base import ProviderError
from mcp_hub.providers.graph_auth import AccessTokenCache, GraphTokenSource
from mcp_hub.providers.msidentity import GraphAccount
from mcp_hub.tokenstore.crypto import KeyUnavailableError, TokenCipher
from tests.support.logcapture import Capture
from tests.support.logfields import allowed_fields
from tests.support.ms_transport import FAKE_CLIENT_ID, MsTransport, json_answer
from tests.support.token_fakes import KEY, FakeStore

ACCOUNT = GraphAccount("outlook", "microsoft", "consumers", FAKE_CLIENT_ID, ("Mail.Read", "offline_access"))
TOKEN_PATH = "/consumers/oauth2/v2.0/token"


def ok(n: int) -> httpx2.Response:
    return json_answer(
        200,
        {
            "access_token": f"AT-SENTINEL-{n}",
            "refresh_token": f"RT-SENTINEL-{n}",
            "expires_in": 3600,
            "scope": "Mail.Read",
        },
    )


def source(
    store: FakeStore, t: MsTransport, now: list[float] | None = None, cache: AccessTokenCache | None = None
) -> GraphTokenSource:
    clock = now or [1000.0]
    return GraphTokenSource(
        ACCOUNT,
        store,
        cache=cache or AccessTokenCache(),
        client_factory=lambda timeout: httpx2.Client(transport=t.transport(), trust_env=False, follow_redirects=False),
        clock=lambda: clock[0],
    )


def test_refresh_commits_before_returning_and_caches() -> None:
    store, t = FakeStore(), MsTransport({TOKEN_PATH: [ok(1)]})
    s = source(store, t)
    assert s.access_token(deadline=1020.0) == "AT-SENTINEL-1"
    assert store.events == ["rotate", "commit"]
    assert store.current() == "RT-SENTINEL-1"
    assert s.access_token(deadline=1020.0) == "AT-SENTINEL-1"
    assert len(t.seen) == 1


def test_refresh_again_inside_margin() -> None:
    now = [1000.0]
    store, t = FakeStore(), MsTransport({TOKEN_PATH: [ok(1), ok(2)]})
    s = source(store, t, now)
    s.access_token(deadline=now[0] + 20)
    now[0] += 3600 - 299
    assert s.access_token(deadline=now[0] + 20) == "AT-SENTINEL-2"
    assert store.current() == "RT-SENTINEL-2"


def test_commit_failure_discards_new_tokens() -> None:
    store, t = FakeStore(fail_commit=True), MsTransport({TOKEN_PATH: [ok(1)]})
    s = source(store, t)
    with pytest.raises(ProviderError) as info:
        s.access_token(deadline=1020.0)
    assert (info.value.code, info.value.cause) == ("upstream_error", "TokenPersist")
    assert store.current() == "RT-SENTINEL-0"
    store.fail_commit = False
    t.answers[TOKEN_PATH] = [ok(2)]
    assert s.access_token(deadline=1020.0) == "AT-SENTINEL-2"  # nothing from the failed round was cached


@pytest.mark.parametrize("error", ["invalid_grant", "interaction_required", "consent_required"])
def test_revoked_grant_with_400_is_recorded_and_short_circuits(error: str) -> None:
    store, t = FakeStore(), MsTransport({TOKEN_PATH: [json_answer(400, {"error": error})]})
    s = source(store, t)
    with pytest.raises(ProviderError) as info:
        s.access_token(deadline=1020.0)
    assert info.value.code == "auth_expired"
    assert store.invalid_grant
    with pytest.raises(ProviderError) as again:
        s.access_token(deadline=1020.0)
    assert again.value.code == "auth_expired"
    assert len(t.seen) == 1


def test_revoked_grant_text_with_other_status_is_not_sticky() -> None:
    store, t = FakeStore(), MsTransport({TOKEN_PATH: [json_answer(401, {"error": "invalid_grant"})]})
    with pytest.raises(ProviderError) as info:
        source(store, t).access_token(deadline=1020.0)
    assert info.value.code == "upstream_error"
    assert not store.invalid_grant


def test_client_rejection_is_upstream_error_not_auth_expired() -> None:
    store, t = FakeStore(), MsTransport({TOKEN_PATH: [json_answer(401, {"error": "invalid_client"})]})
    with pytest.raises(ProviderError) as info:
        source(store, t).access_token(deadline=1020.0)
    assert (info.value.code, info.value.cause) == ("upstream_error", "ClientRejected")
    assert not store.invalid_grant


def test_no_row_and_undecryptable_row_are_auth_expired() -> None:
    t = MsTransport({TOKEN_PATH: [ok(1)]})
    with pytest.raises(ProviderError) as info:
        source(FakeStore(None), t).access_token(deadline=1020.0)
    assert info.value.code == "auth_expired"
    store = FakeStore()
    store.cipher_ = TokenCipher(b"\x07" * 32)  # key changed without rotation: unknown key id
    with pytest.raises(ProviderError) as info:
        source(store, t).access_token(deadline=1020.0)
    assert info.value.code == "auth_expired"
    assert t.seen == []


def test_missing_key_is_upstream_error() -> None:
    class NoKey(FakeStore):
        def cipher(self) -> TokenCipher:
            raise KeyUnavailableError("KeyUnreadable")

    with pytest.raises(ProviderError) as info:
        source(NoKey(), MsTransport({TOKEN_PATH: [ok(1)]})).access_token(deadline=1020.0)
    assert (info.value.code, info.value.cause) == ("upstream_error", "KeyUnavailable")


def test_no_token_call_when_the_deadline_is_nearly_used_up() -> None:
    store, t = FakeStore(), MsTransport({TOKEN_PATH: [ok(1)]})
    with pytest.raises(ProviderError) as info:
        source(store, t).access_token(deadline=1000.5)
    assert info.value.code == "upstream_timeout"
    assert t.seen == []


def test_lock_wait_is_bounded_even_for_a_far_deadline() -> None:
    cache = AccessTokenCache()
    lock = cache.lock_for("outlook")
    lock.acquire()
    try:
        s = source(FakeStore(), MsTransport({TOKEN_PATH: [ok(1)]}), cache=cache)
        with pytest.raises(ProviderError) as info:  # deadline 10**12 would overflow threading.TIMEOUT_MAX
            s.access_token(deadline=1000.0 + 2.5)
        assert info.value.cause == "TokenLock"
        GraphTokenSource.lock_wait(10.0**12, 0.0)  # clamped, no OverflowError
    finally:
        lock.release()


def test_concurrent_callers_refresh_once() -> None:
    store, t = FakeStore(), MsTransport({TOKEN_PATH: [ok(1)]})
    s = source(store, t)
    results: list[str] = []
    threads = [threading.Thread(target=lambda: results.append(s.access_token(deadline=1020.0))) for _ in range(8)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(10)
    assert results == ["AT-SENTINEL-1"] * 8
    assert len(t.seen) == 1


def test_lock_hold_invariant() -> None:
    """Pins the ordering of the configured constants (expected idle gap 25 s < idle-in-transaction limit 30 s < login
    lock wait 35 s). Not a worst-case guarantee: connect, request write and header reads are not deadline-checked
    (confirmation pass C1, Task 7 note (d))."""
    from mcp_hub.providers.graph_auth import MAX_ROW_LOCK_HOLD_SECONDS
    from mcp_hub.tokenstore.store import IDLE_IN_TRANSACTION_MS, LOGIN_WAIT_MS

    assert MAX_ROW_LOCK_HOLD_SECONDS == 25.0
    assert MAX_ROW_LOCK_HOLD_SECONDS * 1000 < IDLE_IN_TRANSACTION_MS < LOGIN_WAIT_MS


def test_token_request_uses_short_phase_timeouts() -> None:
    seen: list[dict[str, float | None]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(dict(request.extensions["timeout"]))
        return ok(1)

    s = GraphTokenSource(
        ACCOUNT,
        FakeStore(),
        cache=AccessTokenCache(),
        clock=lambda: 1000.0,
        client_factory=lambda timeout: httpx2.Client(transport=httpx2.MockTransport(handler), timeout=20.0),
    )  # as default_client(remaining) does;
    # the client default alone would already be 5 s, so only the per-phase override can make this pass (C2)
    s.access_token(deadline=1020.0)
    assert seen[0]["connect"] <= 5.0
    assert seen[0]["read"] <= 5.0
    assert seen[0]["write"] <= 5.0  # type: ignore[operator]


def test_cache_is_per_account_and_client() -> None:
    cache = AccessTokenCache()
    cache.put(("outlook", "consumers", "a"), "AT-A", 9999.0)
    assert cache.get(("outlook", "consumers", "b"), 0.0) is None
    assert cache.get(("uzh", "consumers", "a"), 0.0) is None


def test_no_secret_in_logs_or_exceptions() -> None:
    configure_logging("DEBUG")
    capture = Capture()
    logging.getLogger().addHandler(capture)
    try:
        texts: list[str] = []
        stores_and_answers = [
            (FakeStore(), ok(1)),
            (FakeStore(fail_commit=True), ok(2)),
            (
                FakeStore(),
                json_answer(
                    400,
                    {
                        "error": "invalid_grant",
                        "error_codes": [70000],
                        "error_description": "RT-SENTINEL-0 AADSTS70000",
                    },
                ),
            ),
            (FakeStore(None), ok(3)),
        ]
        undecryptable = FakeStore()
        undecryptable.cipher_ = TokenCipher(b"\x07" * 32)
        stores_and_answers.append((undecryptable, ok(4)))
        for store, answer in stores_and_answers:
            try:
                source(store, MsTransport({TOKEN_PATH: [answer]})).access_token(deadline=1020.0)
            except ProviderError as exc:
                texts += [str(exc), repr(exc), str(exc.cause)]
        joined = "\n".join(capture.lines + [m for _, _, m in capture.raw] + texts)
        for secret in ("RT-SENTINEL", "AT-SENTINEL", "AADSTS", FAKE_CLIENT_ID, base64.b64encode(KEY).decode()):
            assert secret not in joined
        events = [json.loads(line) for line in capture.lines if '"token_refresh"' in line]
        outcomes = {(e["outcome"], e["level"]) for e in events}
        assert {
            ("ok", "INFO"),
            ("persist_failed", "ERROR"),
            ("invalid_grant", "WARNING"),
            ("no_token", "WARNING"),
            ("decrypt_failed", "WARNING"),
        } <= outcomes
        assert any(e.get("error_code") == 70000 for e in events)
        for e in events:
            assert set(e) <= allowed_fields(e["event"])
    finally:
        logging.getLogger().removeHandler(capture)
