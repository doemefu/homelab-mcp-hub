"""The production mailbox factory: credentials go only to the registry host and port, over verified TLS."""

import json
import ssl
from pathlib import Path
from typing import Any

import pytest

from mcp_hub.providers import open_mailbox
from mcp_hub.providers.deadline import CallDeadline
from mcp_hub.providers.imap import default_client
from mcp_hub.registry import load_registry


class RecordingClient:
    instances: list["RecordingClient"] = []  # noqa: RUF012 - test recorder

    def __init__(self, host: str, **kwargs: Any) -> None:
        self.host, self.kwargs = host, kwargs
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.normalise_times = True
        RecordingClient.instances.append(self)

    def login(self, username: str, password: str) -> None:
        self.calls.append(("login", (username, password)))

    def noop(self) -> None:
        self.calls.append(("noop", ()))

    def logout(self) -> None:
        self.calls.append(("logout", ()))


@pytest.fixture
def recording(monkeypatch: pytest.MonkeyPatch) -> type[RecordingClient]:
    RecordingClient.instances = []
    monkeypatch.setattr("mcp_hub.providers.imap.imapclient.IMAPClient", RecordingClient)
    return RecordingClient


def test_open_mailbox_uses_only_the_registry_host_port_and_credentials(
    secrets_dir: Path, recording: type[RecordingClient]
) -> None:
    registry = json.loads((secrets_dir / "accounts.json").read_text())
    mail = registry["accounts"][1]["mail"]  # gmail: IMAP
    mail.update(host="imap.example.test", port=1993, inbox="Custom")
    (secrets_dir / "accounts.json").write_text(json.dumps(registry))
    (secrets_dir / "gmail-username").write_text("user-sentinel\n")
    (secrets_dir / "gmail-app-password").write_text("password-sentinel\n")
    account = load_registry(secrets_dir / "accounts.json").get("gmail")
    assert account is not None

    box = open_mailbox(account, secrets_dir)
    assert box._folder == "Custom"  # type: ignore[attr-defined]
    box.check(CallDeadline.start())

    [client] = recording.instances
    assert client.host == "imap.example.test"
    assert client.kwargs["port"] == 1993
    assert client.kwargs["ssl"] is True
    context = client.kwargs["ssl_context"]
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    assert client.calls[0] == ("login", ("user-sentinel", "password-sentinel"))


def test_default_client_connects_with_tls_and_the_given_context(recording: type[RecordingClient]) -> None:
    context = ssl.create_default_context()
    default_client("imap.example.test", 993, context, 20.0)
    [client] = recording.instances
    assert client.host == "imap.example.test"
    assert client.kwargs == {"port": 993, "ssl": True, "ssl_context": context, "timeout": 20.0}


def _graph_ready(secrets_dir: Path) -> None:
    for ref in ("outlook-ms-client-id", "db-username", "db-password", "token-encryption-key"):
        (secrets_dir / ref).write_text("placeholder")


def test_graph_account_opens_a_graph_mailbox_without_contacting_anything(secrets_dir: Path) -> None:
    from mcp_hub.config import load_settings
    from mcp_hub.providers import MailboxOpener
    from mcp_hub.providers.graph import GraphMailbox
    from mcp_hub.tokenstore.store import StoreConfig

    _graph_ready(secrets_dir)
    settings = load_settings({"HUB_SECRETS_DIR": str(secrets_dir)})
    outlook = load_registry(secrets_dir / "accounts.json").get("outlook")
    assert outlook is not None
    opener = MailboxOpener(StoreConfig.from_settings(settings))
    assert isinstance(opener(outlook, secrets_dir), GraphMailbox)


def test_graph_opener_passes_own_addresses_and_folder_maps(secrets_dir: Path) -> None:
    from mcp_hub.config import load_settings
    from mcp_hub.providers import MailboxOpener
    from mcp_hub.providers.folders import FOLDER_MAPS
    from mcp_hub.tokenstore.store import StoreConfig

    _graph_ready(secrets_dir)
    registry = json.loads((secrets_dir / "accounts.json").read_text())
    [outlook_entry] = [a for a in registry["accounts"] if a["id"] == "outlook"]
    outlook_entry["mail"]["own_addresses_ref"] = "outlook-own-addresses"
    (secrets_dir / "accounts.json").write_text(json.dumps(registry))
    (secrets_dir / "outlook-own-addresses").write_text("me@example.org\n")
    settings = load_settings({"HUB_SECRETS_DIR": str(secrets_dir)})
    outlook = load_registry(secrets_dir / "accounts.json").get("outlook")
    assert outlook is not None
    box = MailboxOpener(StoreConfig.from_settings(settings))(outlook, secrets_dir)
    assert box._own_addresses == frozenset({"me@example.org"})  # type: ignore[attr-defined]
    assert box._folder_maps is FOLDER_MAPS  # type: ignore[attr-defined]


def test_graph_account_without_a_store_configuration_is_refused(secrets_dir: Path) -> None:
    _graph_ready(secrets_dir)
    outlook = load_registry(secrets_dir / "accounts.json").get("outlook")
    assert outlook is not None
    with pytest.raises(ValueError, match="no supported mail block"):
        open_mailbox(outlook, secrets_dir)


def test_graph_client_id_is_read_when_the_mailbox_opens(secrets_dir: Path) -> None:
    from mcp_hub.config import load_settings
    from mcp_hub.providers import MailboxOpener
    from mcp_hub.providers.base import ProviderError
    from mcp_hub.tokenstore.store import StoreConfig

    settings = load_settings({"HUB_SECRETS_DIR": str(secrets_dir)})
    outlook = load_registry(secrets_dir / "accounts.json").get("outlook")
    assert outlook is not None
    with pytest.raises(ProviderError) as info:
        MailboxOpener(StoreConfig.from_settings(settings))(outlook, secrets_dir)
    assert (info.value.code, info.value.cause) == ("upstream_error", "FileNotFoundError")


def _gmail_with_own_addresses(secrets_dir: Path, inbox: str = "INBOX") -> Any:
    registry = json.loads((secrets_dir / "accounts.json").read_text())
    registry["accounts"][1]["mail"].update(own_addresses_ref="gmail-own-addresses", inbox=inbox)
    (secrets_dir / "accounts.json").write_text(json.dumps(registry))
    for ref in ("gmail-username", "gmail-app-password"):
        (secrets_dir / ref).write_text("placeholder")
    account = load_registry(secrets_dir / "accounts.json").get("gmail")
    assert account is not None
    return account


def test_opener_passes_own_addresses_and_folder_maps(secrets_dir: Path) -> None:
    from mcp_hub.providers.folders import FOLDER_MAPS

    (secrets_dir / "gmail-own-addresses").write_text("me@example.org\n")
    box = open_mailbox(_gmail_with_own_addresses(secrets_dir, inbox="Sent Items"), secrets_dir)
    assert box._folder == "Sent Items"  # type: ignore[attr-defined]  # the registry's raw inbox
    assert box._own_addresses == frozenset({"me@example.org"})  # type: ignore[attr-defined]
    assert box._folder_maps is FOLDER_MAPS  # type: ignore[attr-defined]


def test_opener_reads_own_addresses_per_connection(secrets_dir: Path) -> None:
    account = _gmail_with_own_addresses(secrets_dir)
    path = secrets_dir / "gmail-own-addresses"
    path.write_text("me@example.org\n")
    first = open_mailbox(account, secrets_dir)
    path.write_text("me@example.org\nalias@example.org\n")
    second = open_mailbox(account, secrets_dir)
    assert first._own_addresses == frozenset({"me@example.org"})  # type: ignore[attr-defined]
    assert second._own_addresses == frozenset({"me@example.org", "alias@example.org"})  # type: ignore[attr-defined]
