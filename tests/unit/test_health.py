import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from mcp_hub.health import StatusStore, capability_status, missing_credentials
from mcp_hub.registry import load_registry

EXAMPLE = Path(__file__).parents[1] / "fixtures" / "accounts.example.json"
T0 = datetime(2026, 9, 28, 7, 0, tzinfo=UTC)


def test_unchecked_capability_is_unknown() -> None:
    assert StatusStore(clock=lambda: T0).get("icloud", "mail").status == "unknown"


@pytest.mark.parametrize(
    ("code", "status"),
    [
        ("auth_expired", "auth_expired"),
        ("unreachable", "unreachable"),
        ("upstream_timeout", "unreachable"),
        ("upstream_error", "error"),
        ("too_large", "error"),
    ],
)
def test_failure_codes_map_to_status(code: str, status: str) -> None:
    store = StatusStore(clock=lambda: T0)
    store.record_success("icloud", "mail")
    store.record_failure("icloud", "mail", code)  # type: ignore[arg-type]
    got = store.get("icloud", "mail")
    assert (got.status, got.last_error_code, got.last_error_at, got.last_success_at) == (status, code, T0, T0)


def test_success_clears_status_but_keeps_last_error() -> None:
    store = StatusStore(clock=lambda: T0)
    store.record_failure("icloud", "mail", "unreachable")
    store.record_success("icloud", "mail")
    got = store.get("icloud", "mail")
    assert got.status == "ok"
    assert got.last_error_code == "unreachable"


def test_disabled_when_account_disabled_or_credential_file_missing(tmp_path: Path) -> None:
    registry = load_registry(EXAMPLE)
    store = StatusStore(clock=lambda: T0)
    icloud, uzh = registry.get("icloud"), registry.get("uzh")
    assert icloud is not None
    assert uzh is not None
    assert missing_credentials(icloud, "mail", tmp_path) == ["icloud-username", "icloud-app-password"]
    assert capability_status(icloud, "mail", tmp_path, store).status == "disabled"
    (tmp_path / "icloud-username").write_text("u")
    (tmp_path / "icloud-app-password").write_text("p")
    assert capability_status(icloud, "mail", tmp_path, store).status == "unknown"
    (tmp_path / "uzh-ms-client-id").write_text("c")
    assert capability_status(uzh, "mail", tmp_path, store).status == "disabled"


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root can read mode-000 files")
def test_unreadable_credential_file_disables_only_that_account(tmp_path: Path) -> None:
    # Spec 080 §9.6 case (c): an existing but unreadable credential file -> that account disabled, others unaffected.
    registry = load_registry(EXAMPLE)
    store = StatusStore(clock=lambda: T0)
    icloud, gmail = registry.get("icloud"), registry.get("gmail")
    assert icloud is not None
    assert gmail is not None
    for ref in (*icloud.credential_refs("mail"), *gmail.credential_refs("mail")):
        (tmp_path / ref).write_text("placeholder")
    locked = tmp_path / "icloud-app-password"
    locked.chmod(0o000)
    try:
        assert missing_credentials(icloud, "mail", tmp_path) == ["icloud-app-password"]
        assert capability_status(icloud, "mail", tmp_path, store).status == "disabled"
        assert missing_credentials(gmail, "mail", tmp_path) == []
        assert capability_status(gmail, "mail", tmp_path, store).status == "unknown"
    finally:
        locked.chmod(0o600)  # restore so pytest can clean tmp_path; no delete here
