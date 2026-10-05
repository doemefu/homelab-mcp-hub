"""Cause mapping of failed token-store connections without PostgreSQL (spec 080 §7.2). psycopg 3.3.6 reports no
SQLSTATE for a failed connection, so only a stub reaches the 3D000 branch; real failures show the class name."""

import contextlib
from pathlib import Path

import psycopg
import pytest

from mcp_hub.config import load_settings
from mcp_hub.tokenstore.store import StoreConfig, TokenStore, TokenStoreUnavailableError, forget_migrations


class DatabaseMissingStub(psycopg.OperationalError):
    sqlstate = "3D000"


def enter(tmp_path: Path, error: Exception) -> str:
    (tmp_path / "db-username").write_text("u\n")
    (tmp_path / "db-password").write_text("p\n")

    def failing_connect(**kwargs: object) -> psycopg.Connection[tuple[object, ...]]:
        raise error

    store = TokenStore(
        StoreConfig.from_settings(load_settings({"HUB_SECRETS_DIR": str(tmp_path)})), connect=failing_connect
    )
    with pytest.raises(TokenStoreUnavailableError) as info, store.locked("outlook"):
        pass
    return str(info.value)


def test_sqlstate_3d000_is_database_missing(tmp_path: Path) -> None:
    assert enter(tmp_path, DatabaseMissingStub("database missing")) == "DatabaseMissing"


def test_connection_failure_without_sqlstate_is_the_class_name(tmp_path: Path) -> None:
    assert enter(tmp_path, psycopg.OperationalError("connection failed")) == "OperationalError"


class FailingMigrationConnection:
    """Connects fine; every statement fails, so the first-use migration is the first thing to fail (review H3b)."""

    def __init__(self) -> None:
        self.executed = 0
        self.closed = False

    def transaction(self) -> contextlib.AbstractContextManager[None]:
        return contextlib.nullcontext()

    def execute(self, *args: object) -> None:
        self.executed += 1
        raise psycopg.OperationalError("secret text from the server")

    def close(self) -> None:
        self.closed = True


def test_migration_failure_is_unavailable_with_the_class_name_only(tmp_path: Path) -> None:
    (tmp_path / "db-username").write_text("u\n")
    (tmp_path / "db-password").write_text("p\n")
    conns: list[FailingMigrationConnection] = []

    def connect(**kwargs: object) -> FailingMigrationConnection:
        conns.append(FailingMigrationConnection())
        return conns[-1]

    forget_migrations()
    store = TokenStore(
        StoreConfig.from_settings(load_settings({"HUB_SECRETS_DIR": str(tmp_path)})),
        connect=connect,  # type: ignore[arg-type]  # a stub, not a psycopg.Connection
    )
    for _ in range(2):  # a failed migration is not remembered as done: the next use tries again
        with pytest.raises(TokenStoreUnavailableError) as info, store.locked("outlook"):
            pass
        assert str(info.value) == "OperationalError"
        assert info.value.__cause__ is None
        assert info.value.__suppress_context__
        assert "secret text" not in repr(info.value)
    assert [(c.executed, c.closed) for c in conns] == [(1, True), (1, True)]
    forget_migrations()
