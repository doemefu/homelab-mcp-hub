import re
from datetime import UTC, datetime

import pytest
from imapclient.exceptions import IMAPClientError

from tests.support.imap_fake import FakeFolder, FakeImapServer, FakeMessage
from tests.support.imap_wire import WireRecorder, wire_client

D = datetime(2026, 10, 19, 8, 0, tzinfo=UTC)


def server() -> FakeImapServer:
    inbox = FakeFolder(
        "INBOX",
        messages={
            1: FakeMessage(1, D, headers=b"From: Alice <a@example.org>\r\nSubject: Hi\r\n\r\n"),
            2: FakeMessage(2, D, flags=(b"\\Draft",), headers=b"From: Bob <b@example.org>\r\n\r\n"),
            3: FakeMessage(3, datetime(2026, 10, 1, tzinfo=UTC), flags=(b"\\Seen",), labels=(b"work",)),
            4: FakeMessage(4, D, labels=(b"work", b"misc"), headers=b"From: Carol <c@example.org>\r\n\r\n"),
        },
    )
    return FakeImapServer([inbox])


def test_fake_refuses_read_write_select() -> None:
    client = server().client()
    with pytest.raises(AssertionError, match="SELECT refused"):
        client.select_folder("INBOX")
    client.select_folder("INBOX", readonly=True)


@pytest.mark.parametrize(
    "name",
    [
        "add_flags", "remove_flags", "set_flags", "move", "copy", "append", "expunge", "delete_messages",
        "close_folder", "create_folder", "delete_folder", "rename_folder", "subscribe_folder", "gmail_search",
        "xlist_folders", "idle",
    ],
)  # fmt: skip
def test_fake_refuses_commands_outside_the_allowlist(name: str) -> None:
    client = server().client()
    with pytest.raises(AssertionError, match=f"command outside the allowlist: {name}"):
        getattr(client, name)


@pytest.mark.parametrize("item", ["BODY[TEXT]", "RFC822", "ENVELOPE", "BODY.PEEK[]"])
def test_fake_refuses_fetch_items_outside_the_allowlist(item: str) -> None:
    client = server().client()
    client.select_folder("INBOX", readonly=True)
    with pytest.raises(AssertionError, match="outside the allowlist"):
        client.fetch("1:*", [item])


def test_fake_fetch_returns_allowed_items_and_nil_headers() -> None:
    srv = server()
    srv.folders[0].messages[1].headers = None  # type: ignore[assignment]
    client = srv.client()
    client.select_folder("INBOX", readonly=True)
    rows = client.fetch("1:2", ["FLAGS", "INTERNALDATE", "BODY.PEEK[HEADER.FIELDS (FROM)]<0.100>"])
    assert set(rows) == {1, 2}
    assert rows[1][b"BODY[HEADER.FIELDS (FROM)]<0>"] is None
    assert rows[2][b"FLAGS"] == (b"\\Draft",)
    assert [name for name, _ in srv.commands] == ["EXAMINE", "UID FETCH"]


def test_fake_search_honours_not_draft_header_substring_and_nested_or() -> None:
    srv = server()
    client = srv.client()
    client.select_folder("INBOX", readonly=True)
    assert client.search(b"NOT DRAFT SINCE 19-Oct-2026") == [1, 4]
    assert client.search(b"NOT DRAFT BEFORE 19-Oct-2026") == [3]
    assert client.search(b"UNSEEN") == [1, 2, 4]
    assert client.search(b'HEADER From "ALICE"') == [1]
    assert client.search(b'X-GM-LABELS "work"') == [3, 4]
    assert client.search(b'NOT X-GM-LABELS "work"') == [1, 2]
    assert client.search(b'OR (HEADER From "alice") (OR (X-GM-LABELS "misc") (HEADER From "bob"))') == [1, 2, 4]
    srv.honour_not_draft = False
    assert client.search(b"NOT DRAFT") == [1, 2, 3, 4]


def test_fake_whole_msgid_tokens_mode_matches_bracketed_tokens_only() -> None:
    headers = (
        b"Message-ID: <m1@example.test>\r\nReferences: <r0@example.test> <r1@example.test>\r\n"
        b"From: a@example.org\r\n\r\n"
    )
    srv = FakeImapServer([FakeFolder("INBOX", messages={1: FakeMessage(1, D, headers=headers)})])
    client = srv.client()
    client.select_folder("INBOX", readonly=True)
    assert client.search(b'HEADER Message-ID "m1@example.test"') == [1]  # default: substring (RFC 3501)
    srv.whole_msgid_tokens = True  # iCloud-like (O35)
    assert client.search(b'HEADER Message-ID "m1@example.test"') == []
    assert client.search(b'HEADER Message-ID "m1"') == []
    assert client.search(b'HEADER Message-ID "<m1@example.test>"') == [1]
    assert client.search(b'HEADER message-id "<m1@example.test>"') == [1]
    assert client.search(b'HEADER References "r1@example.test"') == []
    assert client.search(b'HEADER References "<r1@example.test>"') == [1]
    assert client.search(b'HEADER From "example"') == [1]  # other fields keep substring matching


def test_fake_failures_raise_once_and_commands_are_recorded() -> None:
    srv = server()
    client = srv.client()
    srv.fail["LIST"] = srv.no("LIST")
    with pytest.raises(IMAPClientError, match="LIST command error: NO"):
        client.list_folders()
    assert client.list_folders() == [((), b"/", "INBOX")]
    assert srv.commands[0][0] == "LIST"


def test_wire_recorder_records_the_uid_search_line() -> None:
    with WireRecorder() as recorder:
        client = wire_client(recorder)
        client.login("user", "pw")
        client.select_folder("INBOX", readonly=True)
        assert client.search(b"NOT DRAFT SINCE 19-Oct-2026") == []
        client.logout()
    rest = [line for line in recorder.lines if not re.match(rb"[A-Za-z]+\d+ (CAPABILITY|LOGIN)", line)]
    assert (
        len([line for line in rest if re.fullmatch(rb"[A-Za-z]+\d+ UID SEARCH NOT DRAFT SINCE 19-Oct-2026", line)]) == 1
    )


def test_wire_recorder_answers_a_search_with_a_literal() -> None:
    with WireRecorder() as recorder:
        client = wire_client(recorder)
        client.login("user", "pw")
        client.select_folder("INBOX", readonly=True)
        assert client.search(["TEXT", "hällo"], charset="UTF-8") == []
        client.logout()
    assert recorder.literals == ["hällo".encode()]
    assert [line for line in recorder.lines if b"UID SEARCH" in line and "hällo".encode() in line]
    assert not [line for line in recorder.lines if line.startswith(b" ")]


def test_wire_recorder_rejects_other_uid_subcommands() -> None:
    with WireRecorder() as recorder:
        client = wire_client(recorder)
        client.login("user", "pw")
        client.select_folder("INBOX", readonly=True)
        with pytest.raises(IMAPClientError, match="BAD"):
            client.fetch([1], ["FLAGS"])
        with pytest.raises(IMAPClientError, match="BAD"):
            client.add_flags([1], [b"\\Seen"])


def test_fake_fail_rejects_keys_outside_the_allowlist() -> None:
    srv = server()
    with pytest.raises(AssertionError, match="command outside the allowlist: LISTT"):
        srv.fail["LISTT"] = srv.no("LIST")
    with pytest.raises(AssertionError, match="command outside the allowlist: STORE"):
        srv.run("STORE")


def test_fake_select_honours_folder_encode() -> None:
    srv = FakeImapServer([FakeFolder("Eing&AOQ-nge"), FakeFolder("Eing&-AOQ-nge")])
    client = srv.client()
    client.select_folder("Eing&AOQ-nge", readonly=True)  # folder_encode=True, like IMAPClient: re-encoded
    client.folder_encode = False
    client.select_folder("Eing&AOQ-nge", readonly=True)
    assert srv.commands == [("EXAMINE", "Eing&-AOQ-nge"), ("EXAMINE", "Eing&AOQ-nge")]


def test_fake_header_fields_subset_in_message_order_and_cut() -> None:
    srv = server()
    srv.folders[0].messages[1].headers = (
        b"Subject: Hi\r\nReferences: <a@example.org>\r\n <b@example.org>\r\nFrom: Alice <a@example.org>\r\n"
        b"Message-ID: <m@example.org>\r\n\r\n"
    )
    client = srv.client()
    client.select_folder("INBOX", readonly=True)
    key = b"BODY[HEADER.FIELDS (MESSAGE-ID REFERENCES)]<0>"
    rows = client.fetch("1", ["BODY.PEEK[HEADER.FIELDS (MESSAGE-ID REFERENCES)]<0.4096>"])
    assert rows[1][key] == b"References: <a@example.org>\r\n <b@example.org>\r\nMessage-ID: <m@example.org>\r\n\r\n"
    rows = client.fetch("1", ["BODY.PEEK[HEADER.FIELDS (MESSAGE-ID REFERENCES)]<0.20>"])
    assert rows[1][key] == b"References: <a@examp"
    rows = client.fetch("1", ["BODY.PEEK[HEADER.FIELDS (X-ABSENT)]<0.100>"])
    assert rows[1][b"BODY[HEADER.FIELDS (X-ABSENT)]<0>"] == b"\r\n"


def thrid_server(capabilities: tuple[bytes, ...] = (b"IMAP4REV1", b"X-GM-EXT-1")) -> FakeImapServer:
    srv = server()
    srv.capabilities = capabilities
    srv.folders[0].messages[1].thrid = 11
    srv.folders[0].messages[3].thrid = 22
    return srv


def test_fake_thrid_search() -> None:
    client = thrid_server().client()
    client.select_folder("INBOX", readonly=True)
    assert client.search(b"NOT DRAFT X-GM-THRID 11") == [1]
    assert client.search(b"X-GM-THRID 99") == []  # messages without thrid (2, 4) never match


def test_fake_thrid_refused_without_gmail_capability() -> None:
    client = thrid_server((b"IMAP4REV1",)).client()
    client.select_folder("INBOX", readonly=True)
    with pytest.raises(IMAPClientError, match="BAD"):
        client.search(b"X-GM-THRID 11")


def test_fake_thrid_in_or_tree() -> None:
    client = thrid_server().client()
    client.select_folder("INBOX", readonly=True)
    assert client.search(b"NOT DRAFT OR X-GM-THRID 11 X-GM-THRID 22") == [1, 3]
    assert client.search(b"OR X-GM-THRID 11 OR X-GM-THRID 22 X-GM-THRID 33") == [1, 3]


@pytest.mark.parametrize(
    "criteria", [b"X-GM-THRID", b"X-GM-THRID abc", b"X-GM-THRID -1", b"X-GM-THRID 18446744073709551616"]
)
def test_fake_malformed_thrid_value_answers_bad(criteria: bytes) -> None:
    client = thrid_server().client()
    client.select_folder("INBOX", readonly=True)
    with pytest.raises(IMAPClientError, match="BAD"):
        client.search(criteria)


@pytest.mark.parametrize("section", ["HEADER.FIELDS.NOT (FROM)", "HEADER.LINES (FROM)", "HEADER.FIELDS FROM"])
def test_fake_unsupported_header_section_answers_bad(section: str) -> None:
    client = server().client()
    client.select_folder("INBOX", readonly=True)
    with pytest.raises(IMAPClientError, match="BAD"):
        client.fetch("1", [f"BODY.PEEK[{section}]<0.100>"])


def test_fake_header_fields_mixed_case_names_and_duplicate_fields() -> None:
    srv = server()
    srv.folders[0].messages[
        1
    ].headers = b"message-ID: <one@example.org>\r\nSubject: Hi\r\nMESSAGE-id: <two@example.org>\r\n\r\n"
    client = srv.client()
    client.select_folder("INBOX", readonly=True)
    rows = client.fetch("1", ["BODY.PEEK[HEADER.FIELDS (Message-Id)]<0.200>"])
    assert rows[1][b"BODY[HEADER.FIELDS (Message-Id)]<0>"] == (
        b"message-ID: <one@example.org>\r\nMESSAGE-id: <two@example.org>\r\n\r\n"
    )


def test_fake_long_from_pushes_message_id_past_the_cap() -> None:
    srv = server()
    long_from = b"From: " + b"x" * 100 + b" <a@example.org>\r\n"
    srv.folders[0].messages[1].headers = long_from + b"Message-ID: <m@example.org>\r\n\r\n"
    client = srv.client()
    client.select_folder("INBOX", readonly=True)
    section = "HEADER.FIELDS (FROM MESSAGE-ID)"
    rows = client.fetch("1", [f"BODY.PEEK[{section}]<0.50>"])
    cut = rows[1][f"BODY[{section}]<0>".encode()]
    assert cut == long_from[:50]
    assert b"Message-ID" not in cut
