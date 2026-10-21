import ast
import imaplib
import inspect
import logging
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from imapclient import IMAPClient
from imapclient import util as imapclient_util
from imapclient.exceptions import IMAPClientError, InvalidCriteriaError

from mcp_hub.providers.base import ProviderError
from mcp_hub.providers.deadline import CallDeadline, SoftStop
from mcp_hub.providers.imap_guard import (
    MAX_PARTIAL_BYTES,
    PASS_THROUGH,
    PLAIN_ITEMS,
    GuardedImapClient,
    ImapCommandRefused,
    imap_error_kind,
    uid_set,
)
from tests.support.clock import FakeClock
from tests.support.imap_fake import COMMAND_ALLOWLIST, FakeFolder, FakeImapServer, FakeMessage, bad, no
from tests.support.imap_wire import WireRecorder, wire_client

ROOT = Path(__file__).resolve().parents[2]
D = datetime(2026, 10, 19, 8, 0, tzinfo=UTC)
# Same value as T7's headers.SUMMARY_HEADER_ITEM (a parallel task); spelled out so this lane does not depend on it.
SUMMARY_HEADER_ITEM = "BODY.PEEK[HEADER.FIELDS (FROM TO CC SUBJECT MESSAGE-ID IN-REPLY-TO REFERENCES)]<0.65536>"


def make(
    *, registry_inbox: str = "INBOX", soft: float = 16.0, hard: float = 20.0, step: float = 0.0
) -> tuple[FakeImapServer, FakeClock, GuardedImapClient]:
    clock = FakeClock(1000.0)
    inbox = FakeFolder("INBOX", messages={uid: FakeMessage(uid, D) for uid in (1, 2, 3, 5, 7, 8)})
    server = FakeImapServer([inbox, FakeFolder("Eing&AOQ-nge"), FakeFolder("a" * 201)], clock=clock, step_seconds=step)
    deadline = CallDeadline(clock(), soft, hard, clock)
    return server, clock, GuardedImapClient(server.client(), deadline, registry_inbox_raw=registry_inbox)


def logged_in(**kwargs: Any) -> tuple[FakeImapServer, FakeClock, GuardedImapClient]:
    server, clock, guard = make(**kwargs)
    guard.login("user", "secret")
    guard.examine("INBOX")
    return server, clock, guard


def names(server: FakeImapServer) -> list[str]:
    return [name for name, _ in server.commands]


# --- read-only commands and input checks -----------------------------------------------------------------------


def test_examine_is_read_only() -> None:
    server, _, guard = make()
    guard.login("user", "secret")
    info = guard.examine("INBOX")
    assert server.commands[-1] == ("EXAMINE", "INBOX")
    assert info[b"UIDVALIDITY"] == 7
    assert not hasattr(guard, "select_folder")


def test_examine_refuses_unsafe_names_except_registry_inbox() -> None:
    server, _, guard = make(registry_inbox="Eing&AOQ-nge")
    guard.login("user", "secret")
    for unsafe in ("a\r\nb", "a" * 201, "", "a&b", "Eingänge"):
        with pytest.raises(ImapCommandRefused):
            guard.examine(unsafe)
    assert names(server) == ["LOGIN", "CAPABILITY"]
    guard.examine("Eing&AOQ-nge")
    assert server.commands[-1] == ("EXAMINE", "Eing&AOQ-nge")

    # The registry inbox is exempt from the name rule even when it fails it (here: longer than 200 characters).
    server, _, guard = make(registry_inbox="a" * 201)
    guard.login("user", "secret")
    guard.examine("a" * 201)
    assert server.commands[-1] == ("EXAMINE", "a" * 201)


@pytest.mark.parametrize("value", ["a\r\nb", "INBOX\x00", "", "Eingänge"])
def test_registry_inbox_must_be_printable_ascii(value: str) -> None:
    clock = FakeClock(1000.0)
    with pytest.raises(ImapCommandRefused):
        GuardedImapClient(FakeImapServer([]).client(), CallDeadline.start(clock), registry_inbox_raw=value)


@pytest.mark.parametrize(
    "item",
    [
        "BODY[TEXT]",
        "BODY.PEEK[]",
        "BODY.PEEK[TEXT]",
        "BODY.PEEK[1]<0.262145>",
        "BODY.PEEK[1]<5.10>",
        "BODY.PEEK[HEADER.FIELDS (FROM)\r\nX]<0.10>",
        "BODY.PEEK[TEXT]<0.10>\r\n",
        "BODY.PEEK[text]<0.10>",
        "BODY.PEEK[TEXT]<0.1234567>",
        "RFC822",
        "ENVELOPE",
        "flags",
    ],
)
def test_fetch_item_allowlist(item: str) -> None:
    server, _, guard = logged_in()
    before = list(server.commands)
    with pytest.raises(ImapCommandRefused):
        guard.fetch([1], ["FLAGS", item])
    assert server.commands == before


@pytest.mark.parametrize(
    "item",
    [
        "BODY.PEEK[1.2]<0.4096>",
        f"BODY.PEEK[TEXT]<0.{MAX_PARTIAL_BYTES}>",
        SUMMARY_HEADER_ITEM,
        *sorted(PLAIN_ITEMS),
    ],
)
def test_fetch_item_allowlist_accepts(item: str) -> None:
    server, _, guard = logged_in()
    rows = guard.fetch([1], [item])
    assert set(rows) == {1}
    assert server.commands[-1] == ("UID FETCH", ([1], [item]))


def test_fetch_refuses_bare_string_or_empty_items() -> None:
    server, _, guard = logged_in()
    before = list(server.commands)
    for items in ("FLAGS", [], ()):
        with pytest.raises(ImapCommandRefused):
            guard.fetch([1], items)
    assert server.commands == before


@pytest.mark.parametrize("uids", [[True], [0], ["1"], [-1], [1.0], [2**32], [], "12", b"\x01"])
def test_fetch_refuses_non_int_or_zero_uids(uids: Any) -> None:
    server, _, guard = logged_in()
    before = list(server.commands)
    with pytest.raises(ImapCommandRefused):
        guard.fetch(uids, ["FLAGS"])
    with pytest.raises(ImapCommandRefused):
        uid_set(uids)
    assert server.commands == before


def test_fetch_collapses_uid_ranges() -> None:
    assert uid_set([1, 2, 3, 5, 7, 8]) == "1:3,5,7:8"
    assert uid_set([8, 7, 5, 3, 2, 1, 2]) == "1:3,5,7:8"
    assert uid_set([4]) == "4"
    assert uid_set([2**32 - 1, 1]) == f"1,{2**32 - 1}"


def test_fetch_sends_sorted_unique_uids() -> None:
    # IMAPClient 4.1.0 parses the FETCH reply with int(messages): a range string such as "1:3,5,7:8" reaches the
    # wire but the reply is lost to a ValueError (see test_real_client_cannot_fetch_a_uid_range_string). The guard
    # therefore hands the client a sorted, de-duplicated int list.
    server, _, guard = logged_in()
    rows = guard.fetch([8, 7, 5, 3, 2, 1, 2], ["FLAGS"])
    assert sorted(rows) == [1, 2, 3, 5, 7, 8]
    assert server.commands[-1] == ("UID FETCH", ([1, 2, 3, 5, 7, 8], ["FLAGS"]))


@pytest.mark.parametrize(
    "criteria",
    [
        [b"NOT", b"DRAFT"],
        "NOT DRAFT",
        bytearray(b"NOT DRAFT"),
        b"",
        b'HEADER Subject "\xc3\xa4"',
        b"NOT DRAFT\r",
        b"NOT DRAFT\nUNSEEN",
        b"NOT\x00DRAFT",
        b"NOT\tDRAFT",
        b'X-GM-RAW "in:inbox"',
        b'NOT DRAFT x-gm-raw "in:inbox"',
    ],
)
def test_search_refuses_bad_criteria(criteria: Any) -> None:
    server, _, guard = logged_in()
    before = list(server.commands)
    with pytest.raises(ImapCommandRefused):
        guard.search(criteria)
    assert server.commands == before


def test_search_passes_valid_criteria() -> None:
    server, _, guard = logged_in()
    assert guard.search(b'NOT DRAFT SINCE 19-Oct-2026 HEADER Message-ID "a@example.org"') == []
    assert guard.search(b"NOT DRAFT SINCE 19-Oct-2026") == [1, 2, 3, 5, 7, 8]
    assert server.commands[-1] == ("UID SEARCH", b"NOT DRAFT SINCE 19-Oct-2026")


# --- capabilities -------------------------------------------------------------------------------------------------


def test_capabilities_cached_right_after_login() -> None:
    server, _, guard = make()
    with pytest.raises(ImapCommandRefused):
        guard.capabilities()
    guard.login("user", "secret")
    assert names(server) == ["LOGIN", "CAPABILITY"]
    assert guard.capabilities() == frozenset({b"IMAP4REV1"})
    assert guard.capabilities() == frozenset({b"IMAP4REV1"})
    guard.list_folders()
    guard.examine("INBOX")
    guard.search(b"NOT DRAFT")
    guard.fetch([1, 2], ["FLAGS"])
    guard.noop()
    guard.logout()
    assert names(server) == ["LOGIN", "CAPABILITY", "LIST", "EXAMINE", "UID SEARCH", "UID FETCH", "NOOP", "LOGOUT"]


def test_real_client_sends_only_allowlisted_commands_and_no_capability_after_login() -> None:
    # Python's imaplib re-reads CAPABILITY inside LOGIN itself and consumes the reply, so IMAPClient's own cache stays
    # empty and its search() would ask again before UID SEARCH. The guard's CAPABILITY right after LOGIN fills that
    # cache. Every CAPABILITY up to and including that one is part of login(); none may follow it.
    with WireRecorder() as recorder:
        guard = GuardedImapClient(wire_client(recorder), CallDeadline.start(), registry_inbox_raw="INBOX")
        guard.login("user", "secret")
        assert b"LITERAL+" in guard.capabilities()
        login_lines = len(recorder.lines)
        guard.examine("INBOX")
        guard.search(b"NOT DRAFT SINCE 19-Oct-2026")
        guard.noop()
        guard.logout()
        lines = list(recorder.lines)
    commands = [_wire_command(line) for line in lines]
    assert set(commands) <= COMMAND_ALLOWLIST
    assert "CAPABILITY" not in commands[login_lines:]
    assert [c for c in commands if c != "CAPABILITY"] == ["LOGIN", "EXAMINE", "UID SEARCH", "NOOP", "LOGOUT"]
    assert lines[login_lines].split(b" ", 1)[1] == b'EXAMINE "INBOX"'


def test_real_client_cannot_fetch_a_uid_range_string() -> None:
    # Documents why the guard passes ints, not uid_set(): IMAPClient 4.1.0's fetch() sends the range but then filters
    # the reply with to_ints(messages), i.e. int("1:3,5"), so the parsed reply is lost to a ValueError.
    assert "to_ints(messages)" in inspect.getsource(IMAPClient.fetch)
    with pytest.raises(ValueError, match="invalid literal for int"):
        imapclient_util.to_ints("1:3,5")
    assert imapclient_util.to_ints([1, 2, 3, 5]) == [1, 2, 3, 5]


def _wire_command(line: bytes) -> str:
    words = line.split(b" ")
    verb = words[1].upper()
    return (b"UID " + words[2].upper() if verb == b"UID" else verb).decode("ascii")


# --- deadline and socket timeout ----------------------------------------------------------------------------------

COMMANDS: dict[str, Callable[[GuardedImapClient], object]] = {
    "login": lambda g: g.login("user", "secret"),
    "list_folders": lambda g: g.list_folders(),
    "examine": lambda g: g.examine("INBOX"),
    "search": lambda g: g.search(b"NOT DRAFT"),
    "fetch": lambda g: g.fetch([1], ["FLAGS"]),
    "noop": lambda g: g.noop(),
}


def _prepared(command: str) -> tuple[FakeImapServer, FakeClock, GuardedImapClient]:
    return make() if command == "login" else logged_in()


@pytest.mark.parametrize("command", sorted(COMMANDS))
def test_every_command_checks_the_deadline_first(command: str) -> None:
    server, clock, guard = _prepared(command)
    commands, timeouts = list(server.commands), list(server.socket_timeouts)
    clock.advance(16.5)
    with pytest.raises(SoftStop):
        COMMANDS[command](guard)
    assert server.commands == commands
    assert server.socket_timeouts == timeouts


def test_capability_after_login_checks_the_deadline() -> None:
    server, _, guard = make(step=16.5)  # the clock passes the soft stop while LOGIN runs
    with pytest.raises(SoftStop):
        guard.login("user", "secret")
    assert names(server) == ["LOGIN"]


@pytest.mark.parametrize("command", sorted(COMMANDS))
def test_hard_deadline_is_provider_timeout(command: str) -> None:
    server, clock, guard = _prepared(command)
    commands = list(server.commands)
    clock.advance(20.5)
    with pytest.raises(ProviderError) as caught:
        COMMANDS[command](guard)
    assert (caught.value.code, caught.value.cause) == ("upstream_timeout", "CallDeadline")
    assert server.commands == commands


def test_logout_still_sent_after_the_hard_deadline() -> None:
    server, clock, guard = logged_in()
    clock.advance(25.0)
    guard.logout()
    assert server.commands[-1] == ("LOGOUT", None)
    assert server.socket_timeouts[-1] == 0.5


def test_socket_timeout_tracks_remaining_time() -> None:
    server, clock, guard = make(soft=20.0)  # soft stop moved out of the way to reach 19.8 s
    guard.login("user", "secret")
    guard.examine("INBOX")
    guard.search(b"NOT DRAFT")
    guard.fetch([1], ["FLAGS"])
    guard.list_folders()
    assert server.socket_timeouts == [20.0] * 6  # one per command, CAPABILITY included
    clock.advance(19.0)
    guard.noop()
    assert server.socket_timeouts[-1] == pytest.approx(1.0)
    clock.advance(0.8)
    guard.noop()
    assert server.socket_timeouts[-1] == 0.5
    guard.logout()
    assert len(server.socket_timeouts) == len(server.commands)


# --- boundary -----------------------------------------------------------------------------------------------------


def test_no_write_method_reachable() -> None:
    _, _, guard = make()
    for name in (
        "select_folder", "add_flags", "remove_flags", "set_flags", "move", "copy", "append", "expunge",
        "delete_messages", "create_folder", "delete_folder", "rename_folder", "gmail_search", "_raw_command",
        "_raw_command_untagged", "client", "__dict__",
    ):  # fmt: skip
        assert not hasattr(guard, name), name
    assert GuardedImapClient.__slots__ == ("_caps", "_client", "_deadline", "_inbox_raw")
    with pytest.raises(AttributeError):
        guard.client = None  # type: ignore[attr-defined]  # no __dict__: nothing can be attached
    public = {name for name in dir(guard) if not name.startswith("_")}
    assert public == {"login", "capabilities", "list_folders", "examine", "search", "fetch", "noop", "logout"}


def test_refusals_are_raises_not_asserts() -> None:
    source = (ROOT / "src/mcp_hub/providers/imap_guard.py").read_text()
    assert not [node for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Assert)]
    code = """
from mcp_hub.providers.deadline import CallDeadline
from mcp_hub.providers.imap_guard import GuardedImapClient, ImapCommandRefused


class Stub:
    normalise_times = folder_encode = use_uid = True

    def __getattr__(self, name):
        raise RuntimeError("reached the client")


guard = GuardedImapClient(Stub(), CallDeadline.start(), registry_inbox_raw="INBOX")
cases = [
    lambda: guard.examine("a\\r\\nb"),
    lambda: guard.search([b"ALL"]),
    lambda: guard.search(b'X-GM-RAW "x"'),
    lambda: guard.fetch([0], ["FLAGS"]),
    lambda: guard.fetch([1], ["RFC822"]),
    lambda: guard.capabilities(),
]
refused = 0
for case in cases:
    try:
        case()
    except ImapCommandRefused:
        refused += 1
print(__debug__, refused, len(cases))
"""
    result = subprocess.run(
        [sys.executable, "-O", "-c", code],
        capture_output=True,
        text=True,
        check=True,
        env={"PYTHONPATH": str(ROOT / "src"), "PATH": ""},
    )
    assert result.stdout.split() == ["False", "6", "6"]


# --- error classification -----------------------------------------------------------------------------------------


def _library_no(command: str) -> IMAPClientError:
    """The exception IMAPClient 4.1.0 raises for a tagged NO (`_check_resp`: "<command> failed: <text>")."""
    with pytest.raises(IMAPClientError) as caught:
        IMAPClient._check_resp(None, "OK", command, "NO", [b"[NONEXISTENT] private server text"])
    return caught.value


def _wire_bad() -> Exception:
    """A real imaplib BAD: the wire recorder answers UID FETCH with `BAD unsupported`."""
    with WireRecorder() as recorder:
        guard = GuardedImapClient(wire_client(recorder), CallDeadline.start(), registry_inbox_raw="INBOX")
        guard.login("user", "secret")
        guard.examine("INBOX")
        with pytest.raises(IMAPClientError) as caught:
            guard.fetch([1], ["FLAGS"])
        guard.logout()
    return caught.value


@pytest.mark.parametrize(
    ("make_exc", "kind"),
    [
        (lambda: _library_no("select"), "no"),
        (lambda: _library_no("SEARCH"), "no"),
        (lambda: _library_no("fetch"), "no"),
        (lambda: no("EXAMINE"), "no"),
        (lambda: no("UID SEARCH"), "no"),
        (_wire_bad, "bad"),
        (lambda: bad("UID FETCH"), "bad"),
        (lambda: imaplib.IMAP4.error("EXAMINE command error: BAD [b'private']"), "bad"),
        (lambda: InvalidCriteriaError("private server text"), "bad"),
        (lambda: imaplib.IMAP4.abort("socket error: EOF"), "other"),
        (lambda: imaplib.IMAP4.abort("FETCH command error: BAD [b'x']"), "other"),
        (lambda: IMAPClientError("command: FETCH => socket error: EOF"), "other"),
        (lambda: IMAPClientError("FETCH command error: NOPE"), "other"),
        (lambda: IMAPClientError("private text then FETCH command error: NO"), "other"),
        (lambda: IMAPClientError("private: failed: x"), "other"),
        (lambda: ValueError("FETCH command error: NO x"), "other"),
        (lambda: OSError("x"), "other"),
        (lambda: SoftStop(), "other"),
    ],
)
def test_imap_error_kind(make_exc: Callable[[], Exception], kind: str, caplog: pytest.LogCaptureFixture) -> None:
    exc = make_exc()
    args, attrs = exc.args, dict(vars(exc))
    with caplog.at_level(logging.DEBUG):
        assert imap_error_kind(exc) == kind
    assert caplog.records == []
    assert (exc.args, dict(vars(exc))) == (args, attrs)  # nothing stored on the exception


def test_pass_through_covers_stop_refusal_provider_and_connection_errors() -> None:
    assert (SoftStop, ImapCommandRefused, ProviderError, imaplib.IMAP4.abort, OSError) == PASS_THROUGH
    assert not issubclass(ImapCommandRefused, IMAPClientError)
    for exc in (SoftStop(), ImapCommandRefused("x"), ProviderError("upstream_timeout"), imaplib.IMAP4.abort("x")):
        assert isinstance(exc, PASS_THROUGH)
    assert isinstance(TimeoutError(), PASS_THROUGH)
