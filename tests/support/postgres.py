"""PostgreSQL test container helpers (token store, spec 080 §7.2). Test data only; per-run password; the tests connect
as the non-superuser role that owns database mcp_hub, as production does."""

import base64
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Final

import psycopg
import pytest

HOST: Final = "127.0.0.1"
PORT: Final = 5433
USER: Final = "mcp_hub"
DB: Final = "mcp_hub"
STATE = Path(os.environ.get("HUB_PROVIDER_STATE_DIR", Path(tempfile.gettempdir()) / "mcp-hub-provider-tests"))


def password() -> str:
    data: dict[str, str] = json.loads((STATE / "credentials.json").read_text())
    return data["postgres"]


def connect() -> psycopg.Connection[tuple[object, ...]]:
    return psycopg.connect(host=HOST, port=PORT, dbname=DB, user=USER, password=password(), connect_timeout=5)


def require_postgres() -> None:
    """Skip unless HUB_PROVIDER_TESTS=1; then fail (not skip) if PostgreSQL does not answer within 60 s."""
    if os.environ.get("HUB_PROVIDER_TESTS") != "1":
        pytest.skip("provider tests need scripts/provider_services.sh up and HUB_PROVIDER_TESTS=1")
    deadline = time.monotonic() + 60
    while True:
        try:
            with connect() as conn:
                conn.execute("SELECT 1")
            return
        except psycopg.OperationalError:
            if time.monotonic() > deadline:
                pytest.fail("PostgreSQL test container did not come up within 60 s")
            time.sleep(1)


def reset_schema() -> None:
    with connect() as conn:
        conn.execute("DROP SCHEMA IF EXISTS mcp_hub CASCADE")


def secrets_with_store(directory: Path) -> Path:
    (directory / "db-username").write_text(USER + "\n")
    (directory / "db-password").write_text(password() + "\n")
    (directory / "token-encryption-key").write_text(base64.b64encode(os.urandom(32)).decode() + "\n")
    return directory


def store_env(directory: Path) -> dict[str, str]:
    return {"HUB_SECRETS_DIR": str(directory), "DB_HOST": HOST, "DB_PORT": str(PORT), "DB_NAME": DB}
