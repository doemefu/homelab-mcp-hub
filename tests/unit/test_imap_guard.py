import ast
import imaplib
import logging
import subprocess
import sys
from collections.abc import Callable, Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from imapclient import IMAPClient
from imapclient.exceptions import IMAPClientError, InvalidCriteriaError, LoginError

from mcp_hub.providers import imap_search
from mcp_hub.providers.base import ProviderError
from mcp_hub.providers.deadline import CallDeadline, SoftStop
from mcp_hub.providers.imap_guard import (
    MAX_EXAMINE_NAME_CHARS,
    MAX_FETCH_UIDS,
    MAX_PARTIAL_BYTES,
    PASS_THROUGH,
    PLAIN_ITEMS,
    GuardedImapClient,
    ImapCommandRefused,
    imap_error_kind,
    search_criteria_ok,
)
from tests.support.clock import FakeClock
from tests.support.imap_fake import COMMAND_ALLOWLIST, FakeFolder, FakeImapServer, FakeMessage, bad, no
from tests.support.imap_wire import WireRecorder, hub_client, wire_client

ROOT = Path(__file__).resolve().parents[2]
D = datetime(2026, 10, 19, 8, 0, tzinfo=UTC)
# Same value as T7's headers.SUMMARY_HEADER_ITEM (a parallel task); spelled out so this lane does not depend on it.
SUMMARY_HEADER_ITEM = "BODY.PEEK[HEADER.FIELDS (FROM TO CC SUBJECT MESSAGE-ID IN-REPLY-TO REFERENCES)]<0.65536>"


def make(
    *, registry_inbox: str = "INBOX", soft: float = 16.0, hard: float = 20.0, step: float = 0.0
) -> tuple[FakeImapServer, FakeClock, GuardedImapClient]:
    clock = FakeClock(1000.0)
    inbox = FakeFolder("INBOX", messages={uid: FakeMessage(uid, D) for uid in (1, 2, 3, 5, 7, 8)})
    server = FakeImapServer(
        [inbox, FakeFolder("Eing&AOQ-nge"), FakeFolder("a" * 201), FakeFolder("b" * 1000), FakeFolder("a" * 1001)],
        clock=clock,
        step_seconds=step,
    )
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
    for unsafe in ("a\r\nb", "a" * (MAX_EXAMINE_NAME_CHARS + 1), "", "a&b", "Eingänge"):
        with pytest.raises(ImapCommandRefused):
            guard.examine(unsafe)
    assert names(server) == ["LOGIN", "CAPABILITY"]
    guard.examine("Eing&AOQ-nge")
    assert server.commands[-1] == ("EXAMINE", "Eing&AOQ-nge")

    # The registry inbox is exempt from the name rule even when it fails it (here: longer than 1,000 characters).
    long_inbox = "a" * (MAX_EXAMINE_NAME_CHARS + 1)
    server, _, guard = make(registry_inbox=long_inbox)
    guard.login("user", "secret")
    guard.examine(long_inbox)
    assert server.commands[-1] == ("EXAMINE", long_inbox)


def test_examine_takes_names_longer_than_ids_carry() -> None:
    """A mailbox over 200 characters is listed with `id: null` (§5.3 rule 9), so EXAMINE must open it."""
    server, _, guard = make()
    guard.login("user", "secret")
    guard.examine("a" * 201)
    guard.examine("b" * MAX_EXAMINE_NAME_CHARS)
    assert server.commands[-2:] == [("EXAMINE", "a" * 201), ("EXAMINE", "b" * MAX_EXAMINE_NAME_CHARS)]
    assert MAX_EXAMINE_NAME_CHARS == 1000


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
    assert server.commands == before


def test_fetch_refuses_more_than_max_uids() -> None:
    server, _, guard = logged_in()
    assert MAX_FETCH_UIDS == 500
    guard.fetch(range(1, MAX_FETCH_UIDS + 1), ["FLAGS"])  # the guard owns the command-line length bound
    assert server.commands[-1] == ("UID FETCH", (list(range(1, 501)), ["FLAGS"]))
    before = list(server.commands)
    with pytest.raises(ImapCommandRefused):
        guard.fetch(range(1, MAX_FETCH_UIDS + 2), ["FLAGS"])
    assert server.commands == before


class Flipping(Sequence[Any]):
    """A hostile sequence: `first` on the first iteration, `then` on every later one."""

    def __init__(self, first: list[Any], then: list[Any]) -> None:
        self._passes = (first, then)
        self.iterations = 0

    def _current(self) -> list[Any]:
        return self._passes[min(self.iterations, 1)]

    def __len__(self) -> int:
        return len(self._current())

    def __getitem__(self, index: Any) -> Any:
        return self._current()[index]

    def __iter__(self) -> Iterator[Any]:
        value = self._current()
        self.iterations += 1
        return iter(value)


def test_fetch_checks_and_sends_one_snapshot_of_items() -> None:
    # BODY[TEXT] (no PEEK) would set \Seen: a second pass over the items must never reach the client.
    server, _, guard = logged_in()
    items = Flipping(["FLAGS"], ["BODY[TEXT]"])
    guard.fetch([1], items)
    assert server.commands[-1] == ("UID FETCH", ([1], ["FLAGS"]))
    assert items.iterations == 1


def test_fetch_checks_and_sends_one_snapshot_of_uids() -> None:
    server, _, guard = logged_in()
    uids = Flipping([1], ["1:*"])
    guard.fetch(uids, ["FLAGS"])
    assert server.commands[-1] == ("UID FETCH", ([1], ["FLAGS"]))
    assert uids.iterations == 1


def test_fetch_sends_sorted_unique_uids() -> None:
    # IMAPClient 4.1.0 filters the FETCH reply with int(messages): a range string such as "1:3,5,7:8" reaches the
    # wire but the reply is lost to a ValueError. The guard therefore hands the client a sorted, de-duplicated int list.
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
        b"HEADER Subject {5}",
        b"NOT DRAFT HEADER Subject {12+}",
    ],
)
def test_search_refuses_bad_criteria(criteria: Any) -> None:
    assert not search_criteria_ok(criteria)
    server, _, guard = logged_in()
    before = list(server.commands)
    with pytest.raises(ImapCommandRefused):
        guard.search(criteria)
    assert server.commands == before


def test_search_passes_valid_criteria() -> None:
    assert search_criteria_ok(b'NOT DRAFT HEADER Message-ID "x{5}"')
    assert search_criteria_ok(b"NOT DRAFT {5} SINCE 19-Oct-2026")  # only a trailing marker announces a literal
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


def test_guard_sets_uid_mode_and_raw_folder_names_on_the_client() -> None:
    client = FakeImapServer([]).client()
    client.use_uid, client.folder_encode = False, True  # IMAPClient's folder_encode default re-encodes raw names
    GuardedImapClient(client, CallDeadline.start(FakeClock()), registry_inbox_raw="INBOX")
    assert (client.use_uid, client.folder_encode) == (True, False)


def test_examine_sends_a_raw_mutf7_name_unchanged() -> None:
    server, _, guard = make()
    guard.login("user", "secret")
    guard.examine("Eing&AOQ-nge")
    assert server.commands[-1] == ("EXAMINE", "Eing&AOQ-nge")


def test_real_client_examines_a_raw_mutf7_name_unchanged() -> None:
    # With IMAPClient's default folder_encode=True the raw name would be re-encoded to "Eing&-AOQ-nge".
    with WireRecorder() as recorder:
        guard = GuardedImapClient(wire_client(recorder), CallDeadline.start(), registry_inbox_raw="INBOX")
        guard.login("user", "secret")
        guard.examine("Eing&AOQ-nge")
        guard.search(b"NOT DRAFT")
        guard.logout()
        lines = list(recorder.lines)
    sent = [line.split(b" ", 1)[1] for line in lines]
    assert b'EXAMINE "Eing&AOQ-nge"' in sent
    assert b"UID SEARCH NOT DRAFT" in sent


@pytest.mark.parametrize(
    ("raw_name", "wire"),
    [
        ('a"b', b'"a\\"b"'),
        ("a\\b", b'"a\\\\b"'),
        ("x{5}", b'"x{5}"'),
        ("a%b", b'"a%b"'),
        ("a*b", b'"a*b"'),
        ("a&-b", b'"a&-b"'),
        ('&-"\\{5}%*', b'"&-\\"\\\\{5}%*"'),
    ],
)
def test_examine_sends_each_raw_name_as_one_quoted_string(raw_name: str, wire: bytes) -> None:
    """Security I2: a real IMAPClient sends every raw mailbox name as exactly one quoted string, `"` and `\\`
    escaped, with no literal (a trailing `{5}` stays inside the quotes) and `%`, `*` and `&-` sent unchanged."""
    with WireRecorder() as recorder:
        guard = GuardedImapClient(wire_client(recorder), CallDeadline.start(), registry_inbox_raw="INBOX")
        guard.login("user", "secret")
        guard.examine(raw_name)
        guard.logout()
        lines, literals = list(recorder.lines), list(recorder.literals)
    examines = [line.split(b" ", 1)[1] for line in lines if _wire_command(line) == "EXAMINE"]
    assert examines == [b"EXAMINE " + wire]
    assert literals == []


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
    assert public == {
        "login",
        "capabilities",
        "list_folders",
        "examine",
        "search",
        "search_values",
        "fetch",
        "noop",
        "logout",
    }


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
        (lambda: LoginError("LOGIN command error: BAD [b'private']"), "other"),
        (lambda: LoginError("select failed: private server text"), "other"),
        (lambda: LoginError("x command error: NO private"), "other"),
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


# --- search_values refusals (T3): nothing is sent --------------------------------------------------------------

_NOT_DRAFT = b"NOT DRAFT SINCE 26-Sep-2026"
_REFUSALS: dict[str, tuple[Any, Any]] = {
    "literal-801-bytes": (_NOT_DRAFT, [("TEXT", "ü" * 400 + "a")]),
    "6x700-bytes": (_NOT_DRAFT, [("TEXT", "ü" * 350)] * 6),
    "lone-surrogate": (_NOT_DRAFT, [("TEXT", "\ud800ü")]),
    "crlf": (_NOT_DRAFT, [("TEXT", "a\r\nü")]),
    "nul": (_NOT_DRAFT, [("TEXT", "ü\x00")]),
    "c1": (_NOT_DRAFT, [("TEXT", "ü\x85")]),
    "12-values": (_NOT_DRAFT, [("TEXT", "a")] * 8 + [("FROM", "a"), ("TO", "a"), ("SUBJECT", "a"), ("TEXT", "a")]),
    "9-text": (_NOT_DRAFT, [("TEXT", "a")] * 9),
    "two-from": (_NOT_DRAFT, [("FROM", "a"), ("FROM", "b")]),
    "key-body": (_NOT_DRAFT, [("BODY", "a")]),
    "key-bytes": (_NOT_DRAFT, [(b"TEXT", "a")]),
    "prefix-no-not-draft": (b"TEXT x", [("TEXT", "a")]),
    "prefix-literal-marker": (b"NOT DRAFT x {5}", [("TEXT", "a")]),
    "prefix-x-gm-raw": (b"NOT DRAFT x-gm-raw", [("TEXT", "a")]),
    "prefix-str": ("NOT DRAFT", [("TEXT", "a")]),
    "value-x-gm-raw-literal": (_NOT_DRAFT, [("TEXT", "ü x-gm-raw")]),
    "value-x-gm-raw-quoted": (_NOT_DRAFT, [("TEXT", "x-gm-raw")]),
    "ascii-quote": (_NOT_DRAFT, [("TEXT", 'a"b')]),
    "ascii-backslash": (_NOT_DRAFT, [("TEXT", "a\\b")]),
    "empty-value": (_NOT_DRAFT, [("TEXT", "")]),
    "bytes-value": (_NOT_DRAFT, [("TEXT", b"abc")]),
    "no-values": (_NOT_DRAFT, []),
    "three-tuple": (_NOT_DRAFT, [("TEXT", "a", "b")]),
    "bare-string-pair": (_NOT_DRAFT, ["ab"]),
}


@pytest.mark.parametrize("case", sorted(_REFUSALS))
def test_search_values_refusals_send_nothing(case: str) -> None:
    prefix, values = _REFUSALS[case]
    with WireRecorder() as recorder:
        guard = GuardedImapClient(hub_client(recorder), CallDeadline.start(), registry_inbox_raw="INBOX")
        guard.login("user", "secret")
        sent = list(recorder.lines)
        with pytest.raises(ImapCommandRefused):
            guard.search_values(prefix, values)
        assert recorder.lines == sent
        assert recorder.literals == []


def test_literal_of_exactly_800_bytes_and_a_total_of_exactly_4096_are_accepted() -> None:
    """G7: the accepting side of both caps (`"\U0001f600" * 200` is 800 UTF-8 bytes)."""
    with WireRecorder() as recorder:
        guard = GuardedImapClient(hub_client(recorder), CallDeadline.start(), registry_inbox_raw="INBOX")
        guard.login("user", "secret")
        guard.search_values(_NOT_DRAFT, [("TEXT", "\U0001f600" * 200)])
        assert recorder.literals == [("\U0001f600" * 200).encode()]
        ascii_values = [("TEXT", "a" * 512)] * 8  # 8 x 512 = 4,096 value bytes, quoted strings
        guard.search_values(_NOT_DRAFT, ascii_values)
        with pytest.raises(ImapCommandRefused):
            guard.search_values(_NOT_DRAFT, [*ascii_values[:7], ("TEXT", "a" * 512), ("FROM", "b")])  # 4,097


@pytest.mark.parametrize("values", [["ü"], ["word", "ü"]], ids=["non-ascii", "mixed"])
def test_literal_path_is_refused_for_a_client_that_answers_literal_plus(values: list[str]) -> None:
    """F3 (D82): the guard does not trust its factory. A plain IMAPClient would send `{N+}` to this server."""
    with WireRecorder() as recorder:
        client = wire_client(recorder)
        guard = GuardedImapClient(client, CallDeadline.start(), registry_inbox_raw="INBOX")
        guard.login("user", "secret")
        assert client.has_capability("LITERAL+") is True
        sent = list(recorder.lines)
        with pytest.raises(ImapCommandRefused):
            guard.search_values(_NOT_DRAFT, [("TEXT", v) for v in values])
        assert recorder.lines == sent
        assert recorder.literals == []


def test_ascii_values_do_not_need_the_capability_check() -> None:
    with WireRecorder() as recorder:
        guard = GuardedImapClient(wire_client(recorder), CallDeadline.start(), registry_inbox_raw="INBOX")
        guard.login("user", "secret")
        guard.search_values(_NOT_DRAFT, [("TEXT", "word")])
        assert any(b"TEXT" in line for line in recorder.lines)


@pytest.mark.parametrize("values", [["word"], ["word", "ü"], ["ü"]], ids=["ascii", "mixed", "non-ascii"])
@pytest.mark.parametrize("part", [b"abc", b"x OR ALL", b'"a"b"', b'"ab', b'"x{5}', b'""', b'"a" "b"'])
def test_unquoted_ascii_part_is_refused_at_the_send_step(
    monkeypatch: pytest.MonkeyPatch, part: bytes, values: list[str]
) -> None:
    # The builder is bypassed: IMAPClient would send an all-ASCII part as an atom, so the guard re-checks every part.
    monkeypatch.setattr(imap_search, "value_part", lambda value: part)
    with WireRecorder() as recorder:
        guard = GuardedImapClient(hub_client(recorder), CallDeadline.start(), registry_inbox_raw="INBOX")
        guard.login("user", "secret")
        sent = list(recorder.lines)
        with pytest.raises(ImapCommandRefused):
            guard.search_values(_NOT_DRAFT, [("TEXT", v) for v in values])
        assert recorder.lines == sent
        assert recorder.literals == []
