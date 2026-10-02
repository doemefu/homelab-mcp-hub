import base64
import os
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

from mcp_hub.config import load_settings
from mcp_hub.tokenstore.migrate import ensure_schema
from mcp_hub.tokenstore.store import (
    LOGIN_WAIT_MS,
    StoreConfig,
    TokenStore,
    TokenStoreUnavailableError,
    forget_migrations,
)
from tests.support import postgres

pytestmark = pytest.mark.provider


def make_store(tmp_path: Path, **env: str) -> TokenStore:
    secrets = postgres.secrets_with_store(tmp_path)
    return TokenStore(StoreConfig.from_settings(load_settings(postgres.store_env(secrets) | env)))


@pytest.fixture
def store(tmp_path: Path) -> Iterator[TokenStore]:
    postgres.require_postgres()
    postgres.reset_schema()
    forget_migrations()
    yield make_store(tmp_path)
    postgres.reset_schema()
    forget_migrations()


def login(store: TokenStore, token: str, account: str = "outlook", wait_ms: int = 5000) -> None:
    sealed = store.cipher().seal(token, account_id=account, provider="microsoft")
    with store.locked(account, wait_ms=wait_ms) as row:
        row.replace_login(sealed, "microsoft", "Mail.Read offline_access")


def current(store: TokenStore, account: str = "outlook") -> str | None:
    with store.locked(account) as row:
        if row.sealed is None:
            return None
        return store.cipher().open(row.sealed, account_id=account, provider="microsoft")


def rotate_then_fail(store: TokenStore) -> None:
    with store.locked("outlook") as row:
        row.rotate(store.cipher().seal("rt-2", account_id="outlook", provider="microsoft"), "Mail.Read")
        raise RuntimeError("caller failed after the write")


def rotate_once(store: TokenStore, value: str = "rt-2") -> None:
    with store.locked("outlook") as row:
        row.rotate(store.cipher().seal(value, account_id="outlook", provider="microsoft"), "Mail.Read")


def enter(store: TokenStore) -> None:
    with store.locked("outlook"):
        pass


def test_tests_run_as_non_superuser(store: TokenStore) -> None:
    with postgres.connect() as conn:
        assert conn.execute("SELECT rolsuper FROM pg_roles WHERE rolname = current_user").fetchone() == (False,)


def test_migrations_run_once_and_create_schema(store: TokenStore) -> None:
    login(store, "rt-1")
    login(store, "rt-2")
    with postgres.connect() as conn:
        assert conn.execute("SELECT count(*) FROM mcp_hub.schema_migrations").fetchone() == (1,)
        assert conn.execute("SELECT version, last_invalid_grant_at FROM mcp_hub.provider_tokens").fetchone() == (
            2,
            None,
        )
    assert current(store) == "rt-2"


def test_rotation_is_visible_from_a_new_connection(store: TokenStore) -> None:
    login(store, "rt-1")
    rotate_once(store)
    with postgres.connect() as conn:  # a separate connection proves a real COMMIT, not a released savepoint (P11)
        version, ct = conn.execute("SELECT version, refresh_token_ct FROM mcp_hub.provider_tokens").fetchone()
    assert version == 2
    assert b"rt-2" not in bytes(ct)
    assert current(store) == "rt-2"


def test_exception_inside_rolls_back(store: TokenStore) -> None:
    login(store, "rt-1")
    with pytest.raises(RuntimeError):
        rotate_then_fail(store)
    assert current(store) == "rt-1"


def test_commit_failure_keeps_old_row(store: TokenStore) -> None:
    login(store, "rt-1")
    with postgres.connect() as conn:  # deferred trigger: the UPDATE succeeds, the COMMIT fails
        conn.execute(
            "CREATE FUNCTION mcp_hub.fail_commit() RETURNS trigger LANGUAGE plpgsql AS "
            "$$BEGIN RAISE EXCEPTION 'test commit failure'; END$$"
        )
        conn.execute(
            "CREATE CONSTRAINT TRIGGER fail_commit AFTER UPDATE ON mcp_hub.provider_tokens "
            "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION mcp_hub.fail_commit()"
        )
    with pytest.raises(TokenStoreUnavailableError) as info:
        rotate_once(store)
    assert "test commit failure" not in str(info.value)
    with postgres.connect() as conn:
        conn.execute("DROP TRIGGER fail_commit ON mcp_hub.provider_tokens")
    assert current(store) == "rt-1"


def test_open_transaction_before_the_block_is_refused(store: TokenStore, tmp_path: Path) -> None:
    def leaky_connect(**kwargs: object) -> psycopg.Connection[tuple[object, ...]]:
        conn = psycopg.connect(**kwargs)  # type: ignore[arg-type]
        conn.execute("SELECT 1")  # opens a transaction: a later transaction() would only be a savepoint
        return conn

    login(store, "rt-1")
    config = StoreConfig.from_settings(load_settings(postgres.store_env(tmp_path)))  # same files as the fixture
    leaky = TokenStore(config, connect=leaky_connect)
    with pytest.raises(TokenStoreUnavailableError) as info:
        enter(leaky)
    assert str(info.value) == "TransactionOpen"


def test_mark_invalid_grant_and_login_clears_it(store: TokenStore) -> None:
    login(store, "rt-1")
    with store.locked("outlook") as row:
        row.mark_invalid_grant()
    with store.locked("outlook") as row:
        assert row.invalid_grant
    login(store, "rt-2")
    with store.locked("outlook") as row:
        assert not row.invalid_grant


def test_row_lock_serialises_two_connections(store: TokenStore) -> None:
    login(store, "rt-1")
    order: list[str] = []
    entered = threading.Event()

    def holder() -> None:
        with store.locked("outlook") as row:
            entered.set()
            order.append("holder-in")
            time.sleep(1.0)
            row.rotate(store.cipher().seal("rt-holder", account_id="outlook", provider="microsoft"), "Mail.Read")
            order.append("holder-out")

    thread = threading.Thread(target=holder)
    thread.start()
    assert entered.wait(5)
    with store.locked("outlook") as row:
        order.append("second-in")
        assert row.sealed is not None
        assert store.cipher().open(row.sealed, account_id="outlook", provider="microsoft") == "rt-holder"
    thread.join(5)
    assert order == ["holder-in", "holder-out", "second-in"]


def test_login_waits_longer_than_the_server_default(store: TokenStore) -> None:
    """A refresh may hold the row up to the 20 s call deadline; the login's write must outwait it (P4, L20)."""
    login(store, "rt-1")
    entered = threading.Event()

    def holder() -> None:
        with store.locked("outlook"):
            entered.set()
            time.sleep(6.0)

    thread = threading.Thread(target=holder)
    thread.start()
    assert entered.wait(5)
    with pytest.raises(TokenStoreUnavailableError):
        login(store, "rt-server-default")  # 5 s wait < 6 s hold
    login(store, "rt-login", wait_ms=LOGIN_WAIT_MS)  # 35 s wait > remaining hold
    thread.join(10)
    assert current(store) == "rt-login"


def test_missing_database_has_its_own_cause(tmp_path: Path) -> None:
    postgres.require_postgres()
    store = make_store(tmp_path, DB_NAME="mcp_hub_missing")
    with pytest.raises(TokenStoreUnavailableError) as info:
        enter(store)
    # psycopg 3.3.6 sets no sqlstate on a failed connection (Task 5 Step 2a probe): the cause is the class name.
    assert str(info.value) == "OperationalError"


def test_unreachable_database_is_unavailable(tmp_path: Path) -> None:
    postgres.require_postgres()
    store = make_store(tmp_path, DB_PORT="1")
    with pytest.raises(TokenStoreUnavailableError) as info:
        enter(store)
    assert postgres.password() not in str(info.value)


def test_stored_key_id_is_read_only(store: TokenStore) -> None:
    assert store.stored_key_id("outlook") is None  # table does not exist yet
    login(store, "rt-1")
    assert store.stored_key_id("outlook") == store.cipher().current_id
    assert store.stored_key_id("uzh") is None


def test_configured_needs_all_three_files(tmp_path: Path) -> None:
    store = TokenStore(StoreConfig.from_settings(load_settings({"HUB_SECRETS_DIR": str(tmp_path)})))
    assert not store.configured()
    (tmp_path / "db-username").write_text("u")
    (tmp_path / "db-password").write_text("p")
    assert not store.configured()
    (tmp_path / "token-encryption-key").write_text(base64.b64encode(os.urandom(32)).decode())
    assert store.configured()


def migrate_concurrently(connections: int) -> tuple[list[str], list[int]]:
    barrier = threading.Barrier(connections)
    errors: list[str] = []
    applied: list[int] = []

    def migrate() -> None:
        with postgres.connect() as conn:
            barrier.wait(5)
            try:
                applied.append(ensure_schema(conn))
            except psycopg.Error as exc:
                errors.append(type(exc).__name__)

    threads = [threading.Thread(target=migrate) for _ in range(connections)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(15)
    return errors, applied


def test_concurrent_first_use_migrates_once(store: TokenStore) -> None:
    """Server and login process migrating a fresh database at the same moment: the advisory lock serialises them
    (without it, concurrent CREATE SCHEMA/TABLE fail with unique violations; review F4)."""
    for _ in range(3):
        postgres.reset_schema()
        errors, applied = migrate_concurrently(4)
        assert errors == []
        assert sorted(applied) == [0, 0, 0, 1]
