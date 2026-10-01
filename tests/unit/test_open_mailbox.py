"""The production mailbox factory: credentials go only to the registry host and port, over verified TLS."""

import json
import ssl
from pathlib import Path
from typing import Any

import pytest

from mcp_hub.providers import open_mailbox
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
    box.check()

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
