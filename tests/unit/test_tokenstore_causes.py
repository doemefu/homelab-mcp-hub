"""Cause mapping of failed token-store connections without PostgreSQL (spec 080 §7.2). psycopg 3.3.6 reports no
SQLSTATE for a failed connection, so only a stub reaches the 3D000 branch; real failures show the class name."""

from pathlib import Path

import psycopg
import pytest

from mcp_hub.config import load_settings
from mcp_hub.tokenstore.store import StoreConfig, TokenStore, TokenStoreUnavailableError


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
