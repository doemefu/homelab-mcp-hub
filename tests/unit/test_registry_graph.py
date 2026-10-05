import json
from pathlib import Path

import pytest

from mcp_hub.registry import Registry, RegistryError, load_registry

OUTLOOK = {
    "id": "outlook",
    "label": "Outlook.com",
    "provider": "microsoft",
    "enabled": True,
    "capabilities": {"mail": True, "calendar": False},
    "graph": {
        "tenant": "consumers",
        "client_id_ref": "outlook-ms-client-id",
        "scopes": ["Mail.Read", "offline_access"],
    },
    "mail": {"protocol": "graph"},
}
# The production registry since stage b (2026-10-02): only icloud, IMAP + CalDAV (spec 080 §8.2 shape).
ICLOUD = {
    "id": "icloud",
    "label": "iCloud",
    "provider": "icloud",
    "enabled": True,
    "capabilities": {"mail": True, "calendar": True},
    "mail": {
        "protocol": "imap",
        "host": "imap.mail.me.com",
        "port": 993,
        "username_ref": "icloud-username",
        "password_ref": "icloud-app-password",
        "inbox": "INBOX",
    },
    "calendar": {
        "protocol": "caldav",
        "url": "https://caldav.icloud.com/",
        "username_ref": "icloud-username",
        "password_ref": "icloud-app-password",
        "include_calendars": "all",
    },
}


def load(tmp_path: Path, *accounts: dict[str, object]) -> Registry:
    path = tmp_path / "accounts.json"
    path.write_text(json.dumps({"version": 1, "accounts": list(accounts)}))
    return load_registry(path)


def test_production_shape_loads_unchanged(tmp_path: Path) -> None:
    assert [a.id for a in load(tmp_path, ICLOUD).accounts] == ["icloud"]


def test_fixture_registry_still_loads() -> None:
    fixture = Path(__file__).parent.parent / "fixtures" / "accounts.example.json"
    assert [a.id for a in load_registry(fixture).accounts] == ["icloud", "gmail", "outlook", "uzh"]


def test_valid_outlook_entry_offline_access_and_refs(tmp_path: Path) -> None:
    registry = load(tmp_path, ICLOUD, OUTLOOK)
    outlook = registry.get("outlook")
    assert outlook is not None
    assert outlook.graph is not None
    assert "offline_access" in outlook.graph.scopes
    assert outlook.credential_refs("mail") == (
        "outlook-ms-client-id",
        "db-username",
        "db-password",
        "token-encryption-key",
    )


@pytest.mark.parametrize(
    "scopes",
    [
        ["Mail.Read"],
        ["offline_access"],
        ["Mail.Read", "offline_access", "https://graph.microsoft.com/Mail.Read"],
        ["Mail.Read", "offline_access", "a b"],
        ["Mail.Read", "offline_access", "Mail.Send"],
        ["Mail.Read", "offline_access", "Calendars.Read"],
        ["Mail.Read", "offline_access", "Mail.ReadWrite"],
    ],
)
def test_bad_scopes_refused(tmp_path: Path, scopes: list[str]) -> None:
    graph = OUTLOOK["graph"] | {"scopes": scopes}  # type: ignore[operator]
    with pytest.raises(RegistryError):
        load(tmp_path, OUTLOOK | {"graph": graph})


def test_calendar_scope_allowed_for_the_org_provider(tmp_path: Path) -> None:
    uzh = OUTLOOK | {
        "id": "uzh",
        "provider": "microsoft-org",
        "graph": {
            "tenant": "organizations",
            "client_id_ref": "uzh-ms-client-id",
            "scopes": ["Mail.Read", "Calendars.Read", "offline_access"],
        },
    }
    assert load(tmp_path, uzh).get("uzh") is not None


def test_graph_needs_microsoft_provider(tmp_path: Path) -> None:
    with pytest.raises(RegistryError):
        load(tmp_path, OUTLOOK | {"provider": "google"})
