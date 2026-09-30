from datetime import UTC, datetime
from pathlib import Path

from mcp_hub.config import load_settings
from mcp_hub.health import StatusStore
from mcp_hub.registry import load_registry
from mcp_hub.tools import HubContext
from mcp_hub.tools.accounts import build_list_accounts
from mcp_hub.tools.common import UNTRUSTED_CONTENT_NOTICE

T0 = datetime(2026, 9, 28, 5, 0, tzinfo=UTC)


def ctx(secrets_dir: Path, store: StatusStore | None = None) -> HubContext:
    settings = load_settings({"HUB_SECRETS_DIR": str(secrets_dir)})
    return HubContext(settings, load_registry(secrets_dir / "accounts.json"), store or StatusStore(clock=lambda: T0))


def test_reports_capabilities_and_status_before_adapters(secrets_dir: Path) -> None:
    result = build_list_accounts(ctx(secrets_dir)).model_dump()
    assert result["untrusted_content_notice"] == UNTRUSTED_CONTENT_NOTICE
    assert result["default_timezone"] == "Europe/Zurich"
    assert result["account_errors"] == []
    by_id = {a["id"]: a for a in result["accounts"]}
    assert list(by_id) == ["icloud", "gmail", "outlook", "uzh"]
    assert [(c["capability"], c["protocol"], c["status"]) for c in by_id["icloud"]["capabilities"]] == [
        ("mail", "imap", "unknown"),
        ("calendar", "caldav", "unknown"),
    ]
    assert [(c["capability"], c["status"]) for c in by_id["gmail"]["capabilities"]] == [("mail", "disabled")]
    assert [(c["capability"], c["status"]) for c in by_id["outlook"]["capabilities"]] == [("mail", "disabled")]
    assert {c["status"] for c in by_id["uzh"]["capabilities"]} == {"disabled"}
    assert by_id["icloud"] | {"capabilities": None} == {
        "id": "icloud",
        "label": "iCloud",
        "provider": "icloud",
        "capabilities": None,
    }


def test_recorded_status_and_timestamps_are_reported(secrets_dir: Path) -> None:
    store = StatusStore(clock=lambda: T0)
    store.record_failure("icloud", "calendar", "auth_expired")
    calendar = build_list_accounts(ctx(secrets_dir, store)).model_dump()["accounts"][0]["capabilities"][1]
    assert calendar == {
        "capability": "calendar",
        "protocol": "caldav",
        "status": "auth_expired",
        "last_success_at": None,
        "last_error_at": "2026-09-28T05:00:00+00:00",
        "last_error_code": "auth_expired",
    }
