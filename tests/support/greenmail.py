"""GreenMail test helpers (spec 080 §10.2). Test data only; TLS is verified against GreenMail's pinned certificate."""

import functools
import json
import os
import re
import smtplib
import socket
import ssl
import tempfile
import time
import uuid
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from typing import Final

import imapclient.imapclient
import pytest
from imapclient import IMAPClient

from mcp_hub.providers.base import MailSummary
from mcp_hub.providers.imap import ImapMailbox

HOST = "127.0.0.1"
SMTP_PORT = 3025
IMAPS_PORT = 3993
DOMAIN = "example.test"
RUN = uuid.uuid4().hex[:8]  # every subject carries the run token, so reruns against one container stay exact
STATE = Path(os.environ.get("HUB_PROVIDER_STATE_DIR", Path(tempfile.gettempdir()) / "mcp-hub-provider-tests"))


@functools.cache
def passwords() -> dict[str, str]:
    """Generated per run by scripts/provider_services.sh up."""
    data: dict[str, str] = json.loads((STATE / "credentials.json").read_text())
    return data


def since() -> datetime:
    """Ten minutes back: tolerant of container clock drift; tests assert only on items with the run token."""
    return datetime.now(UTC) - timedelta(minutes=10)


def subject(text: str) -> str:
    return f"{RUN}-{text}"


def ours(items: list[MailSummary]) -> list[MailSummary]:
    return [m for m in items if (m.subject or "").startswith(f"{RUN}-")]


def require() -> None:
    """Skip unless HUB_PROVIDER_TESTS=1; then fail (not skip) if GreenMail does not come up within 60 s."""
    if os.environ.get("HUB_PROVIDER_TESTS") != "1":
        pytest.skip("provider tests need scripts/provider_services.sh up and HUB_PROVIDER_TESTS=1")
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((HOST, SMTP_PORT), timeout=2) as sock:
                if sock.recv(3) == b"220":
                    return
        except OSError:
            pass
        time.sleep(1)
    pytest.fail("GreenMail did not become ready")


_PARTIAL_LITERAL: Final = re.compile(rb"(<\d+>)(\{\d+\})$")
_parse_fetch_response = imapclient.imapclient.parse_fetch_response


def _with_literal_space(data: list[object], *args: object) -> object:
    """GreenMail 2.1.14 answers a partial body-part fetch as `BODY[1]<0>{15}`, without the SP that RFC 3501
    (msg-att-static) requires before the literal, and IMAPClient's parser rejects that. Test-only: the space is
    re-inserted before parsing; the requested items and the hub's code are unchanged."""
    fixed = [
        (_PARTIAL_LITERAL.sub(rb"\1 \2", item[0]), *item[1:])
        if isinstance(item, tuple) and item and isinstance(item[0], bytes)
        else item
        for item in data
    ]
    return _parse_fetch_response(fixed, *args)


def tolerate_greenmail_partial_literals(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(imapclient.imapclient, "parse_fetch_response", _with_literal_space)


def pinned_context() -> ssl.SSLContext:
    """Trust exactly the certificate the local GreenMail container presents (fetched from 127.0.0.1, pinned for
    this run). Verification stays on; only the host-name check is off because the built-in certificate is not
    issued for 127.0.0.1. Production code always uses ssl.create_default_context()."""
    pem = ssl.get_server_certificate((HOST, IMAPS_PORT), timeout=10)
    context = ssl.create_default_context(cadata=pem)
    context.check_hostname = False
    # Python 3.13 adds VERIFY_X509_STRICT, which a test server's built-in certificate typically fails.
    # Test-only: CERT_REQUIRED and the pinned certificate stay; production is untouched.
    context.verify_flags &= ~ssl.VERIFY_X509_STRICT
    return context


def send(login: str, message: EmailMessage) -> None:
    message["To"] = f"{login}@{DOMAIN}"
    if "From" not in message:
        message["From"] = f"Sender <sender@{DOMAIN}>"
    with smtplib.SMTP(HOST, SMTP_PORT, timeout=10) as smtp:
        smtp.send_message(message)


def append(login: str, message: EmailMessage, when: datetime) -> None:
    with IMAPClient(HOST, port=IMAPS_PORT, ssl=True, ssl_context=pinned_context(), timeout=10) as client:
        client.login(login, passwords()[login])
        client.append("INBOX", bytes(message), msg_time=when)


def flags(login: str) -> dict[int, tuple[bytes, ...]]:
    with IMAPClient(HOST, port=IMAPS_PORT, ssl=True, ssl_context=pinned_context(), timeout=10) as client:
        client.login(login, passwords()[login])
        client.select_folder("INBOX", readonly=True)
        return {uid: row[b"FLAGS"] for uid, row in client.fetch(client.search(["ALL"]), ["FLAGS"]).items()}


def mailbox(login: str, password: str | None = None, port: int = IMAPS_PORT) -> ImapMailbox:
    return ImapMailbox(
        "icloud",
        host=HOST,
        port=port,
        folder="INBOX",
        username=login,
        password=password or passwords()[login],
        ssl_context=pinned_context(),
        timeout=10,
    )
