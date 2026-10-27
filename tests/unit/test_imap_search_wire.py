"""D74 wire bytes: what a real IMAPClient 4.1.0 sends for hub-built criteria, recorded on a loopback socket."""

import ast
import imaplib
import logging
import ssl
import time
from pathlib import Path
from typing import Any

import imapclient
import pytest
from imapclient import response_parser

from mcp_hub.logging import configure_logging
from mcp_hub.providers.base import ProviderError
from mcp_hub.providers.deadline import CallDeadline
from mcp_hub.providers.imap import HubImapClient, ImapMailbox, default_client
from mcp_hub.providers.imap_guard import GuardedImapClient, imap_error_kind
from mcp_hub.providers.imap_search import (
    GMAIL_SENT_LABEL_KEY,
    UnsafeSearchValue,
    criteria,
    header_or,
    mixed_or,
    thrid_or,
)
from tests.support.imap_wire import WireRecorder, hub_client, wire_client

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


def test_mixed_or_wire_bytes() -> None:
    search = criteria(b"NOT DRAFT", mixed_or([("In-Reply-To", "a@x"), ("Message-ID", "b@x")]))
    lines, literals = _search_once(search)
    assert [line.partition(b" ")[2] for line in lines if _command(line) == b"UID SEARCH"] == [
        b'UID SEARCH NOT DRAFT OR HEADER In-Reply-To "<a@x>" HEADER Message-ID "<b@x>"'
    ]
    assert literals == []


# --- non-ASCII values: one synchronising literal (D74, T3) --------------------------------------------------------

PREFIX = b"NOT DRAFT SINCE 26-Sep-2026"
_RAW_COMMANDS = ("_raw_command", "_raw_command_untagged")
SRC = Path(__file__).resolve().parents[2] / "src" / "mcp_hub"


def _values_once(
    prefix: bytes,
    values: list[tuple[Any, str]],
    *,
    capability: bytes = b"IMAP4rev1 LITERAL+",
) -> tuple[list[bytes], list[bytes], bool]:
    with WireRecorder(capability) as recorder:
        guard = GuardedImapClient(hub_client(recorder), CallDeadline.start(), registry_inbox_raw="INBOX")
        guard.login("user", "secret")
        guard.examine("INBOX")
        guard.search_values(prefix, values)
        guard.logout()
        return list(recorder.lines), list(recorder.literals), recorder.early_bytes


def _search_line(lines: list[bytes]) -> bytes:
    (line,) = [line.partition(b" ")[2] for line in lines if _command(line) == b"UID SEARCH"]
    return line


def test_non_ascii_value_is_one_synchronising_literal() -> None:
    lines, literals, early = _values_once(PREFIX, [("TEXT", "grüße")])  # the recorder advertises LITERAL+
    assert _search_line(lines) == b"UID SEARCH CHARSET UTF-8 NOT DRAFT SINCE 26-Sep-2026 TEXT {7}" + "grüße".encode()
    assert literals == ["grüße".encode()]
    assert not early


@pytest.mark.parametrize("value", ["a(b)", "{5}", "x{5}", "%*", "a]b", "word"])
def test_ascii_value_is_one_quoted_string(value: str) -> None:
    lines, literals, _ = _values_once(PREFIX, [("TEXT", value)])
    line = _search_line(lines)
    assert line == b'UID SEARCH NOT DRAFT SINCE 26-Sep-2026 TEXT "' + value.encode() + b'"'
    assert b"CHARSET" not in line
    assert literals == []


@pytest.mark.parametrize("value", ["Müller", "ü" * 200])
def test_non_ascii_values_are_one_literal_with_charset(value: str) -> None:
    lines, literals, _ = _values_once(PREFIX, [("TEXT", value)])
    data = value.encode()
    assert _search_line(lines) == b"UID SEARCH CHARSET UTF-8 NOT DRAFT SINCE 26-Sep-2026 TEXT {%d}" % len(data) + data
    assert literals == [data]


def test_mixed_ascii_and_non_ascii_values() -> None:
    lines, literals, _ = _values_once(PREFIX, [("TEXT", "report"), ("FROM", "Jürg")])
    assert (
        _search_line(lines)
        == b'UID SEARCH CHARSET UTF-8 NOT DRAFT SINCE 26-Sep-2026 TEXT "report" FROM {5}' + "Jürg".encode()
    )
    assert literals == ["Jürg".encode()]


@pytest.mark.parametrize("capability", [b"IMAP4rev1", b"IMAP4rev1 LITERAL-"])
def test_without_literal_plus_same_bytes(capability: bytes) -> None:
    reference, _, _ = _values_once(PREFIX, [("TEXT", "grüße")])
    lines, literals, early = _values_once(PREFIX, [("TEXT", "grüße")], capability=capability)
    assert _search_line(lines) == _search_line(reference)
    assert literals == ["grüße".encode()]
    assert not early


def test_gmail_label_keys_and_two_literals() -> None:
    prefix = b'NOT DRAFT X-GM-LABELS "\\\\Sent" NOT X-GM-LABELS "\\\\Inbox"'
    values = [("TEXT", "ä"), ("TEXT", "b"), ("FROM", "ö"), ("SUBJECT", "x")]
    lines, literals, _ = _values_once(prefix, values)
    assert (
        _search_line(lines)
        == b"UID SEARCH CHARSET UTF-8 " + prefix + b' TEXT {2}\xc3\xa4 TEXT "b" FROM {2}\xc3\xb6 SUBJECT "x"'
    )
    assert literals == [b"\xc3\xa4", b"\xc3\xb6"]
    assert not any(b"X-GM-RAW" in line.upper() for line in lines)


def test_no_hidden_capability_on_literal_path() -> None:
    with WireRecorder() as recorder:
        guard = GuardedImapClient(hub_client(recorder), CallDeadline.start(), registry_inbox_raw="INBOX")
        guard.login("user", "secret")
        guard.examine("INBOX")
        guard.search_values(PREFIX, [("TEXT", "grüße")])
        guard.logout()
        commands = [_command(line) for line in recorder.lines]
    start = commands.index(b"EXAMINE")
    assert commands[start:] == [b"EXAMINE", b"UID SEARCH", b"LOGOUT"]


def test_literal_wire_bytes_sizes() -> None:
    for value in ("\U0001f600", "ü" * 200):
        lines, literals, early = _values_once(PREFIX, [("TEXT", value)])
        data = value.encode()
        assert _search_line(lines).endswith(b"TEXT {%d}" % len(data) + data)
        assert literals == [data]
        assert not early  # no literal byte arrived before the server's `+`


def test_has_capability_literal_plus_off() -> None:
    with WireRecorder() as recorder:
        client = hub_client(recorder)
        client.login("user", "secret")
        assert b"LITERAL+" in client.capabilities()  # the server did advertise it
        assert client.has_capability("LITERAL+") is False
        assert client.has_capability(b"LITERAL+") is False
        assert client.has_capability("literal+") is False
        assert client.has_capability("IMAP4rev1") is True
        client.logout()


def _mailbox(recorder: WireRecorder) -> ImapMailbox:
    def factory(host: str, port: int, context: ssl.SSLContext, timeout: float) -> Any:
        return hub_client(recorder, timeout=timeout)  # production's client class on the recorder's port (I3)

    return ImapMailbox(
        "acct", host="127.0.0.1", port=1, folder="INBOX", username="u", password="p", client_factory=factory
    )


def _refused_run(recorder: WireRecorder, deadline: CallDeadline) -> ProviderError:
    with pytest.raises(ProviderError) as caught:
        _mailbox(recorder)._run(lambda session: session.search_values(PREFIX, [("TEXT", "grüße")]), deadline)
    return caught.value


def _commands(recorder: WireRecorder) -> list[bytes]:
    """The recorded commands once the client's LOGOUT has arrived (the server thread records it a moment later)."""
    for _ in range(100):
        commands = [_command(line) for line in recorder.lines]
        if b"LOGOUT" in commands:
            break
        time.sleep(0.02)
    return commands


def test_refuse_before_continuation() -> None:
    with WireRecorder(refuse="before_continuation") as recorder:
        guard = GuardedImapClient(hub_client(recorder), CallDeadline.start(), registry_inbox_raw="INBOX")
        guard.login("user", "secret")
        with pytest.raises(imaplib.IMAP4.abort) as aborted:
            guard.search_values(PREFIX, [("TEXT", "grüße")])
        assert imap_error_kind(aborted.value) == "other"
        assert recorder.literals == []
    with WireRecorder(refuse="before_continuation") as recorder:
        error = _refused_run(recorder, CallDeadline.start())
        assert error.code == "upstream_error"
        commands = _commands(recorder)
        assert commands[commands.index(b"UID SEARCH") :] == [b"UID SEARCH", b"LOGOUT"]
        assert recorder.literals == []
        search_at = recorder.stamps[commands.index(b"UID SEARCH")]
        assert recorder.stamps[commands.index(b"LOGOUT")] - search_at < 1.0


def test_refuse_after_literal() -> None:
    with WireRecorder(refuse="after_literal") as recorder:
        guard = GuardedImapClient(hub_client(recorder), CallDeadline.start(), registry_inbox_raw="INBOX")
        guard.login("user", "secret")
        with pytest.raises(imaplib.IMAP4.error) as refused:
            guard.search_values(PREFIX, [("TEXT", "grüße")])
        assert imap_error_kind(refused.value) == "no"


def test_stall_before_continuation() -> None:
    started = time.monotonic()
    deadline = CallDeadline(started, 0.3, 0.8)
    with WireRecorder(refuse="stall") as recorder:
        error = _refused_run(recorder, deadline)
        assert error.code == "upstream_timeout"
        assert time.monotonic() - started < 5.0
        commands = _commands(recorder)
        assert commands[commands.index(b"UID SEARCH") :] == [b"UID SEARCH", b"LOGOUT"]
        assert recorder.literals == []


def test_default_client_builds_hub_client(monkeypatch: pytest.MonkeyPatch) -> None:
    built: list[type] = []

    def record(self: object, *args: object, **kwargs: object) -> None:
        built.append(type(self))
        raise RuntimeError("stop")

    monkeypatch.setattr(imapclient.IMAPClient, "__init__", record)
    with pytest.raises(RuntimeError):
        default_client("example.org", 993, ssl.create_default_context(), 1.0)
    assert built == [HubImapClient]


def test_raw_command_single_caller() -> None:
    uses: list[tuple[str, ast.Call | None]] = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text())
        parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        for node in ast.walk(tree):
            named = (isinstance(node, ast.Attribute) and node.attr in _RAW_COMMANDS) or (
                isinstance(node, ast.Constant) and node.value in _RAW_COMMANDS  # getattr("...") and the like
            )
            if not named:
                continue
            owner = node
            while owner in parents and not isinstance(owner, ast.FunctionDef):
                owner = parents[owner]
            call = parents.get(node)
            assert isinstance(owner, ast.FunctionDef)
            uses.append((f"{path.name}:{owner.name}", call if isinstance(call, ast.Call) else None))
    assert [where for where, _ in uses] == ["imap_guard.py:search_values"]
    call = uses[0][1]
    assert call is not None
    first = call.args[0]
    assert isinstance(first, ast.Constant)
    assert first.value == b"SEARCH"
    assert [(k.arg, ast.dump(k.value)) for k in call.keywords] == [("uid", ast.dump(ast.Constant(True)))]
    assert len(call.args) == 2


def test_private_imapclient_names_exist() -> None:
    assert imapclient.__version__ == "4.1.0"
    assert hasattr(imapclient.IMAPClient, "_raw_command_untagged")
    assert callable(response_parser.parse_message_list)


def test_debug_logging_never_carries_the_literal_value() -> None:
    # T3-S1: this is the one place where the real IMAPClient sends a literal; it logs the sent bytes at DEBUG.
    canary = "kanarienvögelßecret"
    records: list[logging.LogRecord] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    root = logging.getLogger()
    saved = (list(root.handlers), root.level, logging.getLogger("imapclient").level)
    try:
        configure_logging("DEBUG")
        root.addHandler(Capture())
        _values_once(PREFIX, [("TEXT", canary)])
    finally:
        root.handlers[:] = saved[0]
        root.setLevel(saved[1])
        logging.getLogger("imapclient").setLevel(saved[2])
    texts = [f"{record.getMessage()} {record.args!r} {record.__dict__.get('msg')!r}" for record in records]
    for needle in (canary, repr(canary), repr(canary.encode()), "NOT DRAFT SINCE"):
        assert not any(needle in text for text in texts), needle
