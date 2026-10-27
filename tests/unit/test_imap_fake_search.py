"""The IMAP fake evaluates the search value keys (TEXT, FROM, TO, SUBJECT, CHARSET) on both entry points (WP13d T4)."""

from datetime import UTC, datetime

import pytest
from imapclient.exceptions import IMAPClientError

from tests.support.imap_fake import FakeFolder, FakeImapClient, FakeImapServer, FakeMessage

D = datetime(2026, 10, 19, 8, 0, tzinfo=UTC)
DRAFT_FREE = b"NOT DRAFT"


def _message(uid: int, sender: str, to: str, subject: str, text: bytes) -> FakeMessage:
    headers = f"From: {sender}\r\nTo: {to}\r\nSubject: {subject}\r\n\r\n".encode()
    return FakeMessage(uid, D, headers=headers, text=text)


def server() -> FakeImapServer:
    inbox = FakeFolder(
        "INBOX",
        messages={
            1: _message(1, "Alice <a@example.org>", "Owner <o@example.org>", "Rechnung Oktober", b"Please pay"),
            2: _message(2, "Bob <b@example.net>", "Carol <c@example.org>", "Hello", b"The Rechnung is attached"),
            3: _message(3, "Müller <m@example.org>", "Owner <o@example.org>", "Grüsse", b"Hallo"),
            4: _message(4, "Dan <d@example.org>", "Owner <o@example.org>", "=?utf-8?q?Gr=C3=BCsse?=", b"Hi"),
        },
    )
    return FakeImapServer([inbox])


def opened(srv: FakeImapServer) -> FakeImapClient:
    client = srv.client()
    client.select_folder("INBOX", readonly=True)
    return client


def raw(client: FakeImapClient, *args: bytes) -> list[int]:
    data = client._raw_command_untagged(b"SEARCH", list(args), uid=True)
    return [int(x) for x in b" ".join(data).split()]


def test_fake_text_from_to_subject() -> None:
    client = opened(server())
    assert raw(client, b"CHARSET", b"UTF-8", DRAFT_FREE, b"TEXT", b'"rechnung"') == [1, 2]
    assert raw(client, b"CHARSET", b"UTF-8", DRAFT_FREE, b"FROM", b'"example.net"') == [2]
    assert raw(client, b"CHARSET", b"UTF-8", DRAFT_FREE, b"TO", b'"carol"') == [2]
    assert raw(client, b"CHARSET", b"UTF-8", DRAFT_FREE, b"SUBJECT", b'"oktober"') == [1]
    assert raw(client, b"CHARSET", b"UTF-8", DRAFT_FREE, b"SUBJECT", "Grüsse".encode()) == [3, 4]
    assert raw(client, b"CHARSET", b"UTF-8", DRAFT_FREE, b"TEXT", b'"rechnung"', b"FROM", b'"alice"') == [1]


def test_fake_ascii_path_value_keys() -> None:
    client = opened(server())
    assert client.search(b'NOT DRAFT TO "owner"') == [1, 3, 4]
    assert client.search(b'NOT DRAFT SUBJECT "hello" CHARSET UTF-8') == [2]
    assert client.search(b'NOT DRAFT SINCE 19-Oct-2026 TEXT "rechnung" FROM "example.org"') == [1]


def test_fake_badcharset() -> None:
    srv = server()
    srv.badcharset = True
    client = opened(srv)
    with pytest.raises(IMAPClientError, match="UID SEARCH command error: NO"):
        raw(client, b"CHARSET", b"UTF-8", DRAFT_FREE, b"TEXT", "Grüsse".encode())
    assert client.search(b'NOT DRAFT TEXT "rechnung"') == [1, 2]  # a search without CHARSET is unaffected


def test_fake_word_mode() -> None:
    srv = server()
    srv.word_match = True
    client = opened(srv)
    assert client.search(b'NOT DRAFT TEXT "rechn"') == []
    assert client.search(b'NOT DRAFT TEXT "rechnung"') == [1, 2]
    assert client.search(b'NOT DRAFT SUBJECT "okt"') == []
    plain = opened(server())
    assert plain.search(b'NOT DRAFT TEXT "rechn"') == [1, 2]


@pytest.mark.parametrize(
    ("command", "uid"), [(b"FETCH", True), (b"SEARCH", False), (b"STORE", True), (b"EXPUNGE", True)]
)
def test_fake_refuses_other_raw_commands(command: bytes, uid: bool) -> None:
    client = opened(server())
    with pytest.raises(AssertionError):
        client._raw_command_untagged(command, [b"1"], uid=uid)


def test_fake_records_the_raw_search() -> None:
    srv = server()
    raw(opened(srv), b"CHARSET", b"UTF-8", DRAFT_FREE, b"TEXT", b'"x"')
    assert srv.commands[-1] == ("UID SEARCH", [b"CHARSET", b"UTF-8", DRAFT_FREE, b"TEXT", b'"x"'])
