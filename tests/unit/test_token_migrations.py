from pathlib import Path

import pytest

from mcp_hub.tokenstore.migrate import migration_files


def test_bundled_migrations_are_contiguous() -> None:
    files = migration_files()
    assert [v for v, _ in files] == list(range(1, len(files) + 1))
    assert b"CREATE TABLE mcp_hub.provider_tokens" in files[0][1]


@pytest.mark.parametrize("names", [["0002_x.sql"], ["0001_a.sql", "0001_b.sql"], ["0001_a.sql", "0003_c.sql"]])
def test_gaps_and_duplicates_refused(tmp_path: Path, names: list[str]) -> None:
    for name in names:
        (tmp_path / name).write_text("SELECT 1;")
    with pytest.raises(RuntimeError):
        migration_files(tmp_path)


def test_other_files_ignored(tmp_path: Path) -> None:
    (tmp_path / "0001_a.sql").write_text("SELECT 1;")
    (tmp_path / "README").write_text("x")
    assert [v for v, _ in migration_files(tmp_path)] == [1]
