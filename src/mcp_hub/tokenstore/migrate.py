"""Built-in migration runner (spec 080 §7.2, rev. 4.6 S7): plain SQL files, applied on the first token-store use of a
process inside one transaction under an advisory lock, so the server and a login run never race (check-registry only
reads)."""

import re
from pathlib import Path
from typing import Any, Final

import psycopg

MIGRATIONS_DIR: Final = Path(__file__).parent / "migrations"
ADVISORY_LOCK_KEY: Final = 0x6D63_7068_7562  # "mcphub"
_FILE: Final = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")


def migration_files(directory: Path = MIGRATIONS_DIR) -> list[tuple[int, bytes]]:
    found = sorted((int(m.group(1)), p) for p in directory.iterdir() if (m := _FILE.fullmatch(p.name)))
    versions = [v for v, _ in found]
    if versions != list(range(1, len(versions) + 1)):
        raise RuntimeError("migration versions must be 1..n without gaps or duplicates")
    return [(v, p.read_bytes()) for v, p in found]


def ensure_schema(conn: psycopg.Connection[Any]) -> int:
    applied = 0
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (ADVISORY_LOCK_KEY,))
        conn.execute("CREATE SCHEMA IF NOT EXISTS mcp_hub")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS mcp_hub.schema_migrations "
            "(version integer PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
        )
        done = {row[0] for row in conn.execute("SELECT version FROM mcp_hub.schema_migrations")}
        for version, sql in migration_files():
            if version in done:
                continue
            conn.execute(sql)  # bytes: several statements, no parameters
            conn.execute("INSERT INTO mcp_hub.schema_migrations (version) VALUES (%s)", (version,))
            applied += 1
    return applied
