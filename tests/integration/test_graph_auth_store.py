import base64
import os
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import httpx2
import pytest

from mcp_hub.config import load_settings
from mcp_hub.providers.base import ProviderError
from mcp_hub.providers.graph_auth import AccessTokenCache, GraphTokenSource
from mcp_hub.providers.msidentity import GraphAccount
from mcp_hub.tokenstore.crypto import KEY_FILE, PREVIOUS_KEY_FILE
from mcp_hub.tokenstore.store import StoreConfig, TokenStore, forget_migrations
from tests.support import postgres
from tests.support.ms_transport import FAKE_CLIENT_ID, MsTransport, json_answer

pytestmark = pytest.mark.provider
ACCOUNT = GraphAccount("outlook", "microsoft", "consumers", FAKE_CLIENT_ID, ("Mail.Read", "offline_access"))
PATH = "/consumers/oauth2/v2.0/token"


def soon() -> float:
    return time.monotonic() + 30  # a real, bounded deadline (review 01 M1: 10**12 overflows lock timeouts)


@pytest.fixture
def secrets(tmp_path: Path) -> Iterator[Path]:
    postgres.require_postgres()
    postgres.reset_schema()
    forget_migrations()
    yield postgres.secrets_with_store(tmp_path)
    postgres.reset_schema()
    forget_migrations()


def store_for(secrets: Path) -> TokenStore:
    return TokenStore(StoreConfig.from_settings(load_settings(postgres.store_env(secrets))))


def seed(store: TokenStore, token: str) -> None:
    sealed = store.cipher().seal(token, account_id="outlook", provider="microsoft")
    with store.locked("outlook") as row:
        row.replace_login(sealed, "microsoft", "Mail.Read")


def stored(store: TokenStore) -> str:
    with store.locked("outlook") as row:
        assert row.sealed is not None
        return store.cipher().open(row.sealed, account_id="outlook", provider="microsoft")


def source(store: TokenStore, transport: httpx2.MockTransport) -> GraphTokenSource:
    return GraphTokenSource(
        ACCOUNT,
        store,
        cache=AccessTokenCache(),
        client_factory=lambda timeout: httpx2.Client(transport=transport, trust_env=False),
    )


def answer(n: int) -> httpx2.Response:
    return json_answer(
        200, {"access_token": f"AT-{n}", "refresh_token": f"RT-{n}", "expires_in": 3600, "scope": "Mail.Read"}
    )


def test_rotation_persists_new_token(secrets: Path) -> None:
    store = store_for(secrets)
    seed(store, "RT-0")
    t = MsTransport({PATH: [answer(1)]})
    assert source(store, t.transport()).access_token(deadline=soon()) == "AT-1"
    assert t.seen[0][4]["refresh_token"] == "RT-0"
    assert stored(store) == "RT-1"


def test_invalid_grant_recorded_then_cleared_by_login(secrets: Path) -> None:
    store = store_for(secrets)
    seed(store, "RT-0")
    t = MsTransport({PATH: [json_answer(400, {"error": "invalid_grant"})]})
    with pytest.raises(ProviderError) as info:
        source(store, t.transport()).access_token(deadline=soon())
    assert info.value.code == "auth_expired"
    with pytest.raises(ProviderError):
        source(store, t.transport()).access_token(deadline=soon())
    assert len(t.seen) == 1
    seed(store, "RT-9")
    t2 = MsTransport({PATH: [answer(2)]})
    assert source(store, t2.transport()).access_token(deadline=soon()) == "AT-2"


def test_row_under_previous_key_is_re_encrypted_on_refresh(secrets: Path) -> None:
    store = store_for(secrets)
    seed(store, "RT-0")
    old_id = store.cipher().current_id
    (secrets / PREVIOUS_KEY_FILE).write_text((secrets / KEY_FILE).read_text())  # rotation: old current → previous
    (secrets / KEY_FILE).write_text(base64.b64encode(os.urandom(32)).decode())
    assert store.cipher().key_state(old_id) == "previous"
    assert store.stored_key_id("outlook") == old_id
    t = MsTransport({PATH: [answer(1)]})
    source(store, t.transport()).access_token(deadline=soon())
    assert store.stored_key_id("outlook") == store.cipher().current_id


def test_login_and_refresh_serialise_on_row_lock(secrets: Path) -> None:
    store = store_for(secrets)
    seed(store, "RT-0")
    entered, release = threading.Event(), threading.Event()

    def slow_token(request: httpx2.Request) -> httpx2.Response:
        entered.set()  # reached only while the refresh holds the FOR UPDATE lock (review 01 M3)
        release.wait(5)
        return answer(1)

    worker = threading.Thread(
        target=lambda: source(store, httpx2.MockTransport(slow_token)).access_token(deadline=soon())
    )
    worker.start()
    assert entered.wait(5)
    login_done = threading.Event()

    def login() -> None:
        sealed = store.cipher().seal("RT-LOGIN", account_id="outlook", provider="microsoft")
        with store.locked("outlook", wait_ms=35_000) as row:  # waits for the refresh's commit
            row.replace_login(sealed, "microsoft", "Mail.Read")
        login_done.set()

    other = threading.Thread(target=login)
    other.start()
    assert not login_done.wait(1.0)  # still blocked by the refresh's row lock
    release.set()
    worker.join(10)
    other.join(10)
    assert login_done.is_set()
    assert stored(store) == "RT-LOGIN"  # the later login wins
