"""D74 wire bytes: what a real IMAPClient 4.1.0 sends for hub-built criteria, recorded on a loopback socket."""

import pytest

from mcp_hub.providers.deadline import CallDeadline
from mcp_hub.providers.imap_guard import GuardedImapClient
from mcp_hub.providers.imap_search import GMAIL_SENT_LABEL_KEY, UnsafeSearchValue, criteria, header_or, thrid_or
from tests.support.imap_wire import WireRecorder, wire_client

ATOM_SPECIALS = ["a(b)@x", "a{b@x", "%*@x", "a]b@x", "x{5}@y", "x{5}", "a+&#%=;/?@x"]


def _command(line: bytes) -> bytes:
    """The command words of a recorded line, without the tag (`UID SEARCH`, `EXAMINE`, ...)."""
    words = line.split(b" ")[1:3]
    return b" ".join(words) if words[0].upper() == b"UID" else words[0]


def _search_once(search: bytes) -> tuple[list[bytes], list[bytes]]:
    with WireRecorder() as recorder:
        guard = GuardedImapClient(wire_client(recorder), CallDeadline.start(), registry_inbox_raw="INBOX")
        guard.login("user", "secret")
        guard.examine("INBOX")
        guard.search(search)
        guard.logout()
        return list(recorder.lines), list(recorder.literals)


@pytest.mark.parametrize("value", ATOM_SPECIALS)
def test_message_id_with_atom_specials_is_one_quoted_string(value: str) -> None:
    lines, literals = _search_once(criteria(b"NOT DRAFT", header_or(("Message-ID",), [value])))
    searches = [line for line in lines if _command(line) == b"UID SEARCH"]
    assert len(searches) == 1
    tag, _, rest = searches[0].partition(b" ")
    assert tag.isalnum()
    assert rest == b'UID SEARCH NOT DRAFT HEADER Message-ID "<' + value.encode("ascii") + b'>"'
    assert literals == []


def test_or_tree_goes_out_unchanged() -> None:
    search = criteria(b"NOT DRAFT", header_or(("In-Reply-To", "References"), ATOM_SPECIALS))
    lines, literals = _search_once(search)
    assert [line.partition(b" ")[2] for line in lines if _command(line) == b"UID SEARCH"] == [b"UID SEARCH " + search]
    assert literals == []


def test_message_id_key_exact_wire_bytes() -> None:
    # O35: the msg-id goes out bracketed inside one quoted string; iCloud matches only whole `<id>` tokens.
    lines, literals = _search_once(criteria(b"NOT DRAFT", header_or(("Message-ID",), ["m1@example.test"])))
    assert [line.partition(b" ")[2] for line in lines if _command(line) == b"UID SEARCH"] == [
        b'UID SEARCH NOT DRAFT HEADER Message-ID "<m1@example.test>"'
    ]
    lines, literals2 = _search_once(criteria(b"NOT DRAFT", header_or(("In-Reply-To", "References"), ["a@x"])))
    assert [line.partition(b" ")[2] for line in lines if _command(line) == b"UID SEARCH"] == [
        b'UID SEARCH NOT DRAFT OR HEADER In-Reply-To "<a@x>" HEADER References "<a@x>"'
    ]
    assert literals == literals2 == []


def test_no_hidden_capability_before_search() -> None:
    # I1: IMAPClient.search() asks for CAPABILITY when its cache is empty; the guard fills that cache at login.
    with WireRecorder() as recorder:
        guard = GuardedImapClient(wire_client(recorder), CallDeadline.start(), registry_inbox_raw="INBOX")
        guard.login("user", "secret")
        assert b"LITERAL+" in guard.capabilities()
        guard.examine("INBOX")
        guard.search(criteria(b"NOT DRAFT", header_or(("Message-ID",), ["a(b)@x", "c@x"])))
        guard.logout()
        commands = [_command(line) for line in recorder.lines]
    start, end = commands.index(b"EXAMINE"), commands.index(b"UID SEARCH")
    assert start < end
    assert commands[start + 1 : end] == []
    assert b"CAPABILITY" not in commands[start:]


def test_thrid_or_wire_bytes() -> None:
    # Gmail thread ids go out as bare numbers of the hub's own formatting, in one command after the login CAPABILITY.
    with WireRecorder() as recorder:
        guard = GuardedImapClient(wire_client(recorder), CallDeadline.start(), registry_inbox_raw="INBOX")
        guard.login("user", "secret")
        guard.examine("INBOX")
        guard.search(criteria(b"NOT DRAFT", thrid_or([1, 2**64 - 1])))
        guard.logout()
        lines = list(recorder.lines)
        literals = list(recorder.literals)
    commands = [_command(line) for line in lines]
    start = commands.index(b"EXAMINE")
    assert commands[start:] == [b"EXAMINE", b"UID SEARCH", b"LOGOUT"]
    assert [line.partition(b" ")[2] for line in lines if _command(line) == b"UID SEARCH"] == [
        b"UID SEARCH NOT DRAFT OR X-GM-THRID 1 X-GM-THRID 18446744073709551615"
    ]
    assert literals == []


def test_gmail_sent_label_key_wire_bytes() -> None:
    search = criteria(b"NOT DRAFT", GMAIL_SENT_LABEL_KEY, header_or(("In-Reply-To", "References"), ["a@x"]))
    lines, literals = _search_once(search)
    assert [line.partition(b" ")[2] for line in lines if _command(line) == b"UID SEARCH"] == [
        b'UID SEARCH NOT DRAFT X-GM-LABELS "\\\\Sent" OR HEADER In-Reply-To "<a@x>" HEADER References "<a@x>"'
    ]
    assert literals == []


def test_library_item_quoting_sends_atoms() -> None:
    # The rejected D74 path: IMAPClient's per-item quoting (`_quoted.maybe`) quotes only items with a space, '"' or
    # '\\' (or empty ones), so an atom-special Message-ID goes out as a bare atom and changes the command's grammar.
    with WireRecorder() as recorder:
        client = wire_client(recorder)
        client.login("user", "secret")
        client.capabilities()
        client.select_folder("INBOX", readonly=True)
        client.search(["NOT", "DRAFT", "HEADER", "Message-ID", "a(b)@x"])
        client.logout()
        lines = list(recorder.lines)
    searches = [line.partition(b" ")[2] for line in lines if _command(line) == b"UID SEARCH"]
    assert searches == [b"UID SEARCH NOT DRAFT HEADER Message-ID a(b)@x"]


@pytest.mark.parametrize("value", ['a"b@x', "a\\b@x", "a\r\nb@x", "a\x00b@x", "ä@x", "", "X-GM-RAW@example.org"])
def test_unsafe_value_never_reaches_the_wire(value: str) -> None:
    with WireRecorder() as recorder:
        guard = GuardedImapClient(wire_client(recorder), CallDeadline.start(), registry_inbox_raw="INBOX")
        guard.login("user", "secret")
        guard.examine("INBOX")
        with pytest.raises(UnsafeSearchValue):
            guard.search(criteria(b"NOT DRAFT", header_or(("Message-ID",), ["ok@x", value])))
        guard.logout()
        lines = list(recorder.lines)
        literals = list(recorder.literals)
    assert b"UID SEARCH" not in [_command(line) for line in lines]
    assert not any(b"ok@x" in line for line in lines)
    assert literals == []
