"""Row-locked access to mcp_hub.provider_tokens (spec 080 §6.3 rotation contract, §7.2). Database credentials are
read from HUB_SECRETS_DIR at connection time (rev. 4.6 S8). Errors carry a class name or a fixed cause only."""

import logging
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import psycopg
from psycopg.pq import TransactionStatus

from mcp_hub.config import Settings
from mcp_hub.logging import log_event
from mcp_hub.tokenstore.crypto import KEY_FILE, Sealed, TokenCipher
from mcp_hub.tokenstore.migrate import ensure_schema

TOKEN_STORE_FILES: Final = ("db-username", "db-password", KEY_FILE)
SERVER_WAIT_MS: Final = 5_000
IDLE_IN_TRANSACTION_MS: Final = 30_000
LOGIN_WAIT_MS: Final = 35_000  # > IDLE_IN_TRANSACTION_MS > the 25 s worst refresh hold (L20, N3 invariant)
_OPTIONS: Final = (
    f"-c statement_timeout=5000 -c lock_timeout=5000 -c idle_in_transaction_session_timeout={IDLE_IN_TRANSACTION_MS}"
)
_DATABASE_MISSING: Final = "3D000"
_log = logging.getLogger("mcp_hub.tokenstore")
_migrated: set[tuple[str, int, str]] = set()
_migrated_lock = threading.Lock()


def forget_migrations() -> None:
    """Tests only: they drop the schema between cases, so the per-process 'already migrated' mark must go too."""
    with _migrated_lock:
        _migrated.clear()


class TokenStoreUnavailableError(Exception):
    """Database or credential files unusable; the message is a class name or a fixed cause."""


def _cause(exc: BaseException) -> str:
    # psycopg 3.3.6 reports no SQLSTATE for a failed connection: a missing database, wrong credentials and an
    # unreachable server all give the class name OperationalError (probe, PR A). The 3D000 branch stays for a
    # driver that does report it (spec 080 §7.2); tests/unit/test_tokenstore_causes.py covers both paths.
    if isinstance(exc, psycopg.Error) and getattr(exc, "sqlstate", None) == _DATABASE_MISSING:
        return "DatabaseMissing"
    return type(exc).__name__


@dataclass(frozen=True, slots=True)
class StoreConfig:
    host: str
    port: int
    dbname: str
    secrets_dir: Path

    @classmethod
    def from_settings(cls, settings: Settings) -> "StoreConfig":
        return cls(settings.db_host, settings.db_port, settings.db_name, settings.secrets_dir)


class LockedRow:
    def __init__(self, conn: psycopg.Connection[Any], account_id: str, row: tuple[Any, ...] | None) -> None:
        self._conn, self._account = conn, account_id
        self.sealed: Sealed | None = None if row is None else Sealed(row[0], bytes(row[1]), bytes(row[2]))
        self.invalid_grant: bool = bool(row[3]) if row is not None else False

    def rotate(self, sealed: Sealed, granted_scopes: str) -> None:
        cur = self._conn.execute(
            "UPDATE mcp_hub.provider_tokens SET key_id = %s, nonce = %s, refresh_token_ct = %s, granted_scopes = %s, "
            "rotated_at = now(), version = version + 1, updated_at = now() WHERE account_id = %s",
            (sealed.key_id, sealed.nonce, sealed.ciphertext, granted_scopes, self._account),
        )
        if cur.rowcount != 1:
            raise TokenStoreUnavailableError("RowGone")

    def mark_invalid_grant(self) -> None:
        self._conn.execute(
            "UPDATE mcp_hub.provider_tokens SET last_invalid_grant_at = now(), version = version + 1, "
            "updated_at = now() WHERE account_id = %s",
            (self._account,),
        )

    def replace_login(self, sealed: Sealed, provider: str, granted_scopes: str) -> None:
        self._conn.execute(
            "INSERT INTO mcp_hub.provider_tokens (account_id, provider, key_id, nonce, refresh_token_ct, "
            "granted_scopes, obtained_at, rotated_at) VALUES (%s, %s, %s, %s, %s, %s, now(), now()) "
            "ON CONFLICT (account_id) DO UPDATE SET provider = EXCLUDED.provider, key_id = EXCLUDED.key_id, "
            "nonce = EXCLUDED.nonce, refresh_token_ct = EXCLUDED.refresh_token_ct, "
            "granted_scopes = EXCLUDED.granted_scopes, obtained_at = now(), rotated_at = now(), "
            "last_invalid_grant_at = NULL, version = mcp_hub.provider_tokens.version + 1, updated_at = now()",
            (self._account, provider, sealed.key_id, sealed.nonce, sealed.ciphertext, granted_scopes),
        )


class TokenStore:
    def __init__(
        self, config: StoreConfig, *, connect: Callable[..., psycopg.Connection[Any]] = psycopg.connect
    ) -> None:
        self._config, self._connect_fn = config, connect

    def configured(self) -> bool:
        return all((self._config.secrets_dir / name).is_file() for name in TOKEN_STORE_FILES)

    def cipher(self) -> TokenCipher:
        return TokenCipher.from_files(self._config.secrets_dir)

    def _read(self, name: str) -> str:
        value = (self._config.secrets_dir / name).read_text(encoding="utf-8").rstrip("\r\n")
        if not value:
            raise TokenStoreUnavailableError("EmptyCredential")
        return value

    def _connect(self) -> psycopg.Connection[Any]:
        c = self._config
        try:
            return self._connect_fn(
                host=c.host,
                port=c.port,
                dbname=c.dbname,
                user=self._read("db-username"),
                password=self._read("db-password"),
                connect_timeout=5,
                application_name="mcp-hub",
                options=_OPTIONS,
            )
        except (OSError, UnicodeDecodeError, psycopg.Error) as exc:
            raise TokenStoreUnavailableError(_cause(exc)) from None

    def _ensure(self, conn: psycopg.Connection[Any]) -> None:
        key = (self._config.host, self._config.port, self._config.dbname)
        with _migrated_lock:
            if key in _migrated:
                return
            applied = ensure_schema(conn)
            _migrated.add(key)
        log_event(_log, logging.INFO, "token_store_migrated", result_count=applied)

    @contextmanager
    def locked(self, account_id: str, *, wait_ms: int = SERVER_WAIT_MS) -> Iterator[LockedRow]:
        conn = self._connect()
        try:
            self._ensure(conn)
            # A statement before transaction() would turn the block into a savepoint whose "commit" is discarded
            # by close() (psycopg docs, transactions): refuse instead (review 03 P11).
            if conn.info.transaction_status != TransactionStatus.IDLE:
                raise TokenStoreUnavailableError("TransactionOpen")
            with conn.transaction():
                for name in ("lock_timeout", "statement_timeout"):
                    conn.execute("SELECT set_config(%s, %s, true)", (name, f"{wait_ms}ms"))
                row = conn.execute(
                    "SELECT key_id, nonce, refresh_token_ct, last_invalid_grant_at IS NOT NULL "
                    "FROM mcp_hub.provider_tokens WHERE account_id = %s FOR UPDATE",
                    (account_id,),
                ).fetchone()
                yield LockedRow(conn, account_id, row)
        except psycopg.Error as exc:
            raise TokenStoreUnavailableError(_cause(exc)) from None
        finally:
            conn.close()

    def stored_key_id(self, account_id: str) -> str | None:
        """Read-only (check-registry): the key id of the stored row, or None (no row, no table yet)."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT key_id FROM mcp_hub.provider_tokens WHERE account_id = %s", (account_id,)
            ).fetchone()
            return str(row[0]) if row else None
        except psycopg.errors.UndefinedTable:
            return None
        except psycopg.Error as exc:
            raise TokenStoreUnavailableError(_cause(exc)) from None
        finally:
            conn.close()
