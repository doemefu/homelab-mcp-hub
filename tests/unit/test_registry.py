import copy
import json
from pathlib import Path
from typing import Any

import pytest

from mcp_hub.errors import ToolError
from mcp_hub.registry import Registry, RegistryError, load_registry, select_accounts

EXAMPLE = Path(__file__).parents[1] / "fixtures" / "accounts.example.json"


def example() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(EXAMPLE.read_text())
    return data


def write(tmp_path: Path, data: dict[str, Any]) -> Path:
    path = tmp_path / "accounts.json"
    path.write_text(json.dumps(data))
    return path


def test_example_registry_loads() -> None:
    registry = load_registry(EXAMPLE)
    assert [a.id for a in registry.accounts] == ["icloud", "gmail", "outlook", "uzh"]
    icloud = registry.get("icloud")
    assert icloud is not None
    assert icloud.protocol("mail") == "imap"
    assert icloud.protocol("calendar") == "caldav"
    assert icloud.credential_refs("calendar") == ("icloud-username", "icloud-app-password")
    outlook = registry.get("outlook")
    assert outlook is not None
    assert outlook.credential_refs("mail") == ("outlook-ms-client-id",)
    assert outlook.credential_refs("calendar") == ()


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d["accounts"][0].update(unexpected=True),  # unknown field
        lambda d: d["accounts"][0].update(id="iCloud"),  # id pattern
        lambda d: d["accounts"][0].update(id="a"),  # id too short
        lambda d: d["accounts"][1].update(id="icloud"),  # duplicate id
        lambda d: d["accounts"][0].update(label="x" * 65),  # label length
        lambda d: d["accounts"][0].update(provider="yahoo"),  # provider enum
        lambda d: d["accounts"][0].pop("capabilities"),  # capabilities required
        lambda d: d["accounts"][0]["capabilities"].pop("calendar"),  # both keys required
        lambda d: d["accounts"][0].pop("calendar"),  # true capability without block
        lambda d: d["accounts"][1].update(calendar={"protocol": "ics", "url_ref": "x-ics"}),  # false with block
        lambda d: d["accounts"][2].pop("graph"),  # graph block required
        lambda d: d["accounts"][0]["calendar"].update(include_calendars=[]),  # empty calendar list
        lambda d: d.update(version=2),  # version
    ],
)
def test_invalid_registries_are_rejected(tmp_path: Path, mutate: Any) -> None:
    data = example()
    mutate(data)
    with pytest.raises(RegistryError):
        load_registry(write(tmp_path, data))


@pytest.mark.parametrize(
    "ref",
    [
        "../x",
        "/etc/passwd",
        "a/b",
        "",
        "UPPER",
        ".",
        "..data",
        ".hidden",
        "accounts.json",
        "allowed-subjects",
        "db-password",
        "token-encryption-key-previous",
    ],
)
def test_ref_must_be_a_plain_file_name(tmp_path: Path, ref: str) -> None:
    data = example()
    data["accounts"][0]["mail"]["password_ref"] = ref
    with pytest.raises(RegistryError):
        load_registry(write(tmp_path, data))


def test_dotted_ref_is_accepted(tmp_path: Path) -> None:
    # Spec §7.1 key names allow dots (playbook 59 pattern ^[a-z0-9][a-z0-9.-]*$).
    data = example()
    data["accounts"][0]["mail"]["password_ref"] = "icloud.app-password"
    registry = load_registry(write(tmp_path, data))
    icloud = registry.get("icloud")
    assert icloud is not None
    assert icloud.credential_refs("mail")[1] == "icloud.app-password"


def test_error_message_never_contains_input_values(tmp_path: Path) -> None:
    data = example()
    data["accounts"][0]["label"] = "SENTINEL-" + "x" * 80
    with pytest.raises(RegistryError) as info:
        load_registry(write(tmp_path, data))
    assert "SENTINEL" not in str(info.value)
    assert "accounts.0.label" in str(info.value)


def test_unknown_keys_are_not_echoed(tmp_path: Path) -> None:
    # An operator may paste an address or other private text as a key by mistake; only a placeholder is reported.
    data = example()
    data["accounts"][0]["sentinel@example.org"] = True
    data["accounts"][0]["mail"]["sentinel-key-2"] = 1
    data["sentinel-top"] = 1
    with pytest.raises(RegistryError) as info:
        load_registry(write(tmp_path, data))
    message = str(info.value)
    assert "sentinel" not in message
    parts = set(message.removeprefix("accounts.json is invalid: ").split("; "))
    assert parts == {
        "accounts.0.<unknown key> x1: extra_forbidden",
        "accounts.0.mail.imap.<unknown key> x1: extra_forbidden",
        "<unknown key> x1: extra_forbidden",
    }


def test_missing_or_unparsable_file(tmp_path: Path) -> None:
    with pytest.raises(RegistryError):
        load_registry(tmp_path / "absent.json")
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    with pytest.raises(RegistryError):
        load_registry(bad)


def registry_with_uzh(enabled: bool) -> Registry:
    data = copy.deepcopy(example())
    data["accounts"][3]["enabled"] = enabled
    return Registry.model_validate(data)


def test_calendar_without_account_selects_enabled_calendar_accounts_only() -> None:
    assert [a.id for a in select_accounts(registry_with_uzh(False), "calendar", None)] == ["icloud"]
    assert [a.id for a in select_accounts(registry_with_uzh(True), "calendar", None)] == ["icloud", "uzh"]


def test_mail_without_account_selects_all_enabled_mail_accounts() -> None:
    assert [a.id for a in select_accounts(registry_with_uzh(False), "mail", None)] == ["icloud", "gmail", "outlook"]


@pytest.mark.parametrize(
    ("account", "code"),
    [("gmail", "capability_unavailable"), ("nosuch", "unknown_account"), ("uzh", "capability_unavailable")],
)
def test_named_account_errors(account: str, code: str) -> None:
    with pytest.raises(ToolError) as info:
        select_accounts(registry_with_uzh(False), "calendar", account)
    assert info.value.code == code


@pytest.mark.parametrize("url", ["http://caldav.icloud.com/", "ftp://caldav.icloud.com/", "//caldav.icloud.com/"])
def test_a_calendar_url_must_be_https(tmp_path: Path, url: str) -> None:
    # The CalDAV host rule compares schemes; this validation is what makes every credential destination https.
    data = example()
    data["accounts"][0]["calendar"]["url"] = url
    with pytest.raises(RegistryError):
        load_registry(write(tmp_path, data))
