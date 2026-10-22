"""GreenMail test helpers (spec 080 §10.2). Test data only; TLS is verified against GreenMail's pinned certificate."""

import contextlib
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
from collections.abc import Iterator, Sequence
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


@contextlib.contextmanager
def connected(login: str) -> Iterator[IMAPClient]:
    """The test's own IMAP client: seeding and inspection only (APPEND, CREATE, COPY, MOVE, EXPUNGE are the test's,
    never the hub's, spec 080 §10.2)."""
    with IMAPClient(HOST, port=IMAPS_PORT, ssl=True, ssl_context=pinned_context(), timeout=10) as client:
        client.login(login, passwords()[login])
        yield client


def append(
    login: str, message: EmailMessage, when: datetime, *, folder: str = "INBOX", flags: Sequence[bytes] = ()
) -> None:
    append_many(login, [message], when, folder=folder, flags=flags)


def append_many(
    login: str,
    messages: Sequence[EmailMessage],
    when: datetime,
    *,
    folder: str = "INBOX",
    flags: Sequence[bytes] = (),
) -> None:
    """APPEND with an explicit INTERNALDATE (`when`) and flags; To and From default as in `send`."""
    with connected(login) as client:
        for message in messages:
            if "To" not in message:
                message["To"] = f"{login}@{DOMAIN}"
            if "From" not in message:
                message["From"] = f"Sender <sender@{DOMAIN}>"
            client.append(folder, bytes(message), flags=tuple(flags), msg_time=when)


def ensure_folders(login: str, *names: str) -> None:
    with connected(login) as client:
        for name in names:
            if not client.folder_exists(name):
                client.create_folder(name)


def _uids(client: IMAPClient, message_id: str) -> list[int]:
    """The UIDs of the selected folder whose Message-ID header holds `<message_id>`."""
    found: list[int] = client.search(f'HEADER Message-ID "<{message_id}>"'.encode())
    if not found:
        raise AssertionError("seeded message not found")
    return found


def copy(login: str, source: str, message_id: str, target: str) -> None:
    with connected(login) as client:
        client.select_folder(source)
        client.copy(_uids(client, message_id), target)


def move(login: str, source: str, message_id: str, target: str) -> str:
    """MOVE where the server announces it (GreenMail 2.1.14 does, O38), else COPY + STORE \\Deleted + EXPUNGE of
    exactly those UIDs. Returns the variant used."""
    with connected(login) as client:
        client.select_folder(source)
        uids = _uids(client, message_id)
        if b"MOVE" in client.capabilities():
            client.move(uids, target)
            return "MOVE"
        client.copy(uids, target)
        client.add_flags(uids, [b"\\Deleted"])
        client.uid_expunge(uids)
        return "COPY+EXPUNGE"


def expunge(login: str, folder: str, message_id: str) -> None:
    with connected(login) as client:
        client.select_folder(folder)
        uids = _uids(client, message_id)
        client.add_flags(uids, [b"\\Deleted"])
        client.uid_expunge(uids)


SNAPSHOT_FLAGS: Final = frozenset({b"\\Seen", b"\\Answered", b"\\Flagged", b"\\Deleted", b"\\Draft"})


def snapshot(login: str, folders: Sequence[str]) -> dict[tuple[str, int], tuple[bytes, frozenset[bytes]]]:
    """Every message of `folders`: (folder, UID) → (Message-ID header, flags). Read through EXAMINE and BODY.PEEK.
    \\Recent is left out: it belongs to the session that first sees a message, not to the message."""
    out: dict[tuple[str, int], tuple[bytes, frozenset[bytes]]] = {}
    with connected(login) as client:
        for folder in folders:
            client.select_folder(folder, readonly=True)
            uids = client.search(["ALL"])
            rows = client.fetch(uids, ["FLAGS", "BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)]"]) if uids else {}
            for uid, row in rows.items():
                header = bytes(row[b"BODY[HEADER.FIELDS (MESSAGE-ID)]"]).strip()
                out[(folder, uid)] = (header, frozenset(row[b"FLAGS"]) & SNAPSHOT_FLAGS)
    return out


def flags(login: str) -> dict[int, tuple[bytes, ...]]:
    with connected(login) as client:
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
