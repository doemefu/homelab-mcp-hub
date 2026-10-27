"""WP13a T1 (O35): the read-only IMAP probe script, driven by a recording stub client (no network)."""

import imaplib
import importlib.util
import io
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from imapclient.exceptions import LoginError

from mcp_hub.ids import MESSAGE_ID_PATTERN
from mcp_hub.providers import imap_search
from mcp_hub.providers.folders import EXCLUDED_LAST_NAMES
from mcp_hub.providers.imap_guard import ImapCommandRefused
from mcp_hub.providers.imap_session import FETCH_CHUNK_UIDS, LOCATE_BATCH_VALUES


def _probe_module() -> ModuleType:
    path = Path(__file__).parents[2] / "scripts" / "imap_probe.py"
    spec = importlib.util.spec_from_file_location("imap_probe", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


probe_mod = _probe_module()

Folder = tuple[tuple[bytes, ...], bytes, str]


class StubClient:
    """Records every command the probe sends; answers with canned data."""

    def __init__(
        self,
        folders: list[Folder],
        capabilities: tuple[bytes, ...] = (b"IMAP4REV1", b"SPECIAL-USE", b"LITERAL+", b"MOVE"),
        found_in: str | None = "Sent Messages",
    ) -> None:
        self.folders = folders
        self.caps = capabilities
        self.found_in = found_in
        self.commands: list[tuple[str, str]] = []
        self.selected = ""

    # allowed surface
    def capabilities(self) -> tuple[bytes, ...]:
        self.commands.append(("CAPABILITY", ""))
        return self.caps

    def login(self, username: str, password: str) -> bytes:
        self.commands.append(("LOGIN", ""))
        return b"OK"

    def list_folders(self) -> list[Folder]:
        self.commands.append(("LIST", ""))
        return self.folders

    def select_folder(self, folder: str, readonly: bool = False) -> dict[bytes, object]:
        self.commands.append(("EXAMINE" if readonly else "SELECT", folder))
        self.selected = folder
        return {b"UIDVALIDITY": 1}

    def search(self, criteria: Any = "ALL", charset: Any = None) -> list[int]:
        text = criteria.decode() if isinstance(criteria, bytes) else str(criteria)
        self.commands.append(("SEARCH", text))
        if "HEADER" in text or text.startswith("NOT DRAFT HEADER") or "OR (" in text or "TEXT" in text:
            return [7] if self.selected == self.found_in else []
        if "X-GM-LABELS" in text:
            return [1, 2, 3]
        return list(range(1, 26)) if self.selected == "INBOX" else [1]

    def fetch(self, messages: Any, data: Any) -> dict[int, dict[bytes, bytes]]:
        items = [data] if isinstance(data, (str, bytes)) else list(data)
        self.commands.append(("FETCH", " ".join(i.decode() if isinstance(i, bytes) else i for i in items)))
        return {
            int(uid): {b"BODY[HEADER.FIELDS (MESSAGE-ID)]<0>": f"Message-ID: <id{uid}@example.test>\r\n\r\n".encode()}
            for uid in messages
        }

    def logout(self) -> bytes:
        self.commands.append(("LOGOUT", ""))
        return b"OK"

    # forbidden surface (must never be reachable through the proxy)
    def add_flags(self, *args: object) -> None:
        raise AssertionError("write reached the client")

    move = append = expunge = gmail_search = add_flags


def _folders(extra: list[str] | None = None) -> list[Folder]:
    base: list[Folder] = [
        ((), b"/", "INBOX"),
        ((b"\\Sent",), b"/", "Sent Messages"),
        ((b"\\Trash",), b"/", "Deleted Messages"),
        ((b"\\Junk",), b"/", "Spam Box"),
        ((b"\\Drafts",), b"/", "Drafts"),
        ((), b".", "INBOX.Junk"),
        ((), b"/", "Projects"),
        ((b"\\Noselect",), b"/", "[Container]"),
    ]
    return base + [((), b"/", name) for name in extra or []]


def _or_operands(expr: str) -> tuple[str, str]:
    """Split `OR (a) (b)` into its two parenthesised operands (nesting-aware)."""
    rest = expr.removeprefix("OR ")
    groups: list[str] = []
    depth = 0
    start = 0
    for i, ch in enumerate(rest):
        if ch == "(":
            if depth == 0:
                start = i + 1
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                groups.append(rest[start:i])
    assert len(groups) == 2
    return groups[0], groups[1]


def _run(stub: StubClient, **kwargs: Any) -> tuple[str, StubClient]:
    out = io.StringIO()
    probe_mod.probe(probe_mod.ReadOnlyProxy(stub), out=out, **kwargs)
    return out.getvalue(), stub


def test_proxy_refuses_writes() -> None:
    stub = StubClient(_folders())
    proxy = probe_mod.ReadOnlyProxy(stub)
    for name in ("add_flags", "move", "append", "expunge", "gmail_search", "store", "_client_not_there"):
        with pytest.raises(PermissionError):
            getattr(proxy, name)
    proxy.select_folder("X")
    assert ("EXAMINE", "X") in stub.commands
    assert not any(cmd == "SELECT" for cmd, _ in stub.commands)


def test_proxy_search_and_fetch_allowlists() -> None:
    proxy = probe_mod.ReadOnlyProxy(StubClient(_folders()))
    with pytest.raises(PermissionError):
        proxy.search(b"X-GM-RAW foo")
    with pytest.raises(PermissionError):
        proxy.search(b"x-gm-raw foo")
    with pytest.raises(PermissionError):
        proxy.search("NOT DRAFT")  # type: ignore[arg-type]  # str criteria are refused by design
    with pytest.raises(PermissionError):
        proxy.fetch([1], ["BODY[]"])
    with pytest.raises(PermissionError):
        proxy.fetch([1], ["RFC822"])
    with pytest.raises(PermissionError):
        proxy.fetch([1], ["X-GM-THRID"])
    proxy.fetch([1], ["INTERNALDATE"])
    proxy.fetch([1], ["BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)]<0.2048>"])
    proxy.fetch([1], ["INTERNALDATE", "FLAGS", "X-GM-LABELS"])


def test_probe_uses_only_read_only_commands() -> None:
    _, stub = _run(StubClient(_folders(), capabilities=(b"X-GM-EXT-1",)))
    searches = [text for cmd, text in stub.commands if cmd == "SEARCH"]
    assert searches
    assert all(text.startswith(("NOT DRAFT", "HEADER Message-ID", "X-GM-LABELS")) for text in searches)
    assert not any("X-GM-RAW" in text.upper() for text in searches)
    fetches = [text for cmd, text in stub.commands if cmd == "FETCH"]
    allowed = {"INTERNALDATE", "BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)]<0.2048>", "INTERNALDATE FLAGS X-GM-LABELS"}
    assert fetches
    assert all(text in allowed for text in fetches)
    assert {cmd for cmd, _ in stub.commands} <= {"CAPABILITY", "LOGIN", "LIST", "EXAMINE", "SEARCH", "FETCH", "LOGOUT"}


def test_probe_prints_no_mailbox_names_by_default() -> None:
    out, _ = _run(StubClient(_folders()))
    assert "Projects" not in out
    assert "Sent Messages=yes" in out  # the fallback-name line is the one place a fixed name is allowed
    assert "--- result ---" in out
    assert "special_use=yes" in out
    assert "fallback_names_present: Sent Messages=yes" in out
    assert "@example" not in out
    assert "id7" not in out


def test_show_names_prints_do_not_paste_banner() -> None:
    out, _ = _run(StubClient(_folders()), show_names=True)
    assert "DO NOT PASTE" in out
    assert "--- result ---" not in out
    assert "Projects" in out


def test_probe_skips_junk_and_drafts() -> None:
    _, stub = _run(StubClient(_folders()))
    examined = {text for cmd, text in stub.commands if cmd == "EXAMINE"}
    assert not examined & {"Spam Box", "INBOX.Junk", "Drafts", "[Container]"}
    assert {"INBOX", "Sent Messages", "Projects"} <= examined


def test_probe_reports_other_count_and_or_test() -> None:
    out, stub = _run(StubClient(_folders()))
    assert re.search(r"^mailboxes: total=8 selectable=7 with_special_use=4 other=3$", out, re.MULTILINE)
    ors = [text for cmd, text in stub.commands if cmd == "SEARCH" and " OR (" in text]
    folders = len({text for cmd, text in stub.commands if cmd == "EXAMINE"})
    assert len(ors) == folders
    first = ors[0]
    assert first.count('HEADER Message-ID "<id') == 20  # bracketed tokens, as the hub searches them (O35)
    assert first.startswith("NOT DRAFT OR (")
    left, right = _or_operands(first.removeprefix("NOT DRAFT "))
    assert left.count("Message-ID") == 10
    assert right.count("Message-ID") == 10
    left_a, left_b = _or_operands(left)
    assert {left_a.count("Message-ID"), left_b.count("Message-ID")} == {5}
    assert "id1@" not in out


def test_probe_caps_folders() -> None:
    names = [f"Box{i}" for i in range(40)]
    out, stub = _run(StubClient([((), b"/", "INBOX")] + [((), b"/", n) for n in names[:39]]), max_folders=30)
    examines = [text for cmd, text in stub.commands if cmd == "EXAMINE"]
    assert len(examines) == 30 * 5 + 4  # plus the four INBOX Message-ID variants
    assert "header_message_id: folders=30" in out


def test_probe_gmail_section_needs_x_gm_ext_1() -> None:
    out, stub = _run(StubClient(_folders()))
    assert "gmail_" not in out
    assert not any("X-GM-LABELS" in text for _, text in stub.commands)
    out, _ = _run(StubClient(_folders(), capabilities=(b"X-GM-EXT-1", b"SPECIAL-USE")))
    assert re.search(r"^gmail_all_mail: absent$", out, re.MULTILINE)
    assert re.search(r"^gmail_sent_mailbox: hits=1 ms=\d+$", out, re.MULTILINE)


def test_search_criteria_have_expected_shape() -> None:
    _, stub = _run(StubClient(_folders()), since_days=14, text_word="the")
    searches = [text for cmd, text in stub.commands if cmd == "SEARCH"]
    assert re.fullmatch(r"NOT DRAFT SINCE \d{2}-[A-Z][a-z]{2}-\d{4}", searches[0])
    assert any(re.fullmatch(r'NOT DRAFT HEADER Message-ID "<id\d+@example\.test>"', s) for s in searches)
    assert any(re.fullmatch(r'NOT DRAFT HEADER References "<id\d+@example\.test>"', s) for s in searches)
    assert any(re.fullmatch(r'NOT DRAFT HEADER Message-ID "id\d+@example\.test"', s) for s in searches)  # msgid_bare
    assert 'NOT DRAFT TEXT "the"' in searches


def test_quoted_is_the_hub_rule() -> None:
    """P1: nothing escaped; `"`, `\\`, control and non-ASCII characters, empty values and X-GM-RAW refused."""
    assert probe_mod.quoted("a(b)@x") == '"a(b)@x"'
    for value in ('a"b', "a\\b", "a\r\nb", "a\x00b", "\u00e4@x", "", "x-gm-raw@x"):
        with pytest.raises(ValueError, match="search value refused"):
            probe_mod.quoted(value)
        with pytest.raises(imap_search.UnsafeSearchValue):
            imap_search.quoted(value)
    for value in ("a(b)@x", "x{5}@y", "%*@x", "a+&#%=;/?@x"):
        assert probe_mod.quoted(value).encode() == imap_search.quoted(value)
        assert probe_mod.msg_id_token(value).encode() == imap_search.msg_id_token(value)
    for value in ('a"b', "a\\b", "", "x-gm-raw@x", "a<b@x", "a>b@x", "<a@x>"):
        with pytest.raises(ValueError, match="search value refused"):
            probe_mod.msg_id_token(value)
        with pytest.raises(imap_search.UnsafeSearchValue):
            imap_search.msg_id_token(value)


def test_host_outside_allowlist_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*args: object, **kwargs: object) -> None:
        raise AssertionError("connected")

    monkeypatch.setattr(probe_mod, "IMAPClient", fail)
    monkeypatch.setattr(probe_mod.getpass, "getpass", fail)
    assert probe_mod.main(["--host", "imap.example.org"]) == 2


def test_errors_printed_as_class_name_only(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    class BoomError(Exception):
        pass

    def connect(*args: object, **kwargs: object) -> None:
        raise BoomError("secret-detail imap.mail.me.com user@example.org")

    monkeypatch.setattr(probe_mod, "IMAPClient", connect)
    monkeypatch.setattr(probe_mod.getpass, "getpass", lambda prompt="": "x")
    assert probe_mod.main(["--host", "imap.mail.me.com"]) == 1
    captured = capsys.readouterr()
    assert "BoomError" in captured.err + captured.out
    assert "secret-detail" not in captured.err + captured.out
    assert "user@example.org" not in captured.err + captured.out


def test_scan_targets_and_unseen_timing_reported() -> None:
    out, stub = _run(StubClient(_folders()))
    assert re.search(r"^scan_targets: 4$", out, re.MULTILINE)
    assert len(re.findall(r"^search_unseen_since: folder#\d role=\w+ hits=\d+ ms=\d+$", out, re.MULTILINE)) == 4
    assert any(
        re.fullmatch(r"NOT DRAFT UNSEEN SINCE \d{2}-[A-Z][a-z]{2}-\d{4}", t) for c, t in stub.commands if c == "SEARCH"
    )


def test_client_gets_a_socket_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def factory(*args: object, **kwargs: object) -> None:
        seen.update(kwargs)
        raise RuntimeError

    monkeypatch.setattr(probe_mod, "IMAPClient", factory)
    monkeypatch.setattr(probe_mod.getpass, "getpass", lambda prompt="": "x")
    assert probe_mod.main(["--host", "imap.gmail.com"]) == 1
    assert seen["timeout"] == 30.0


# --- P1-P4: the hub's search shape, cold timings, per-round errors, Gmail keys ---------------------------------------


def test_probe_rules_match_the_hub() -> None:
    assert probe_mod._MESSAGE_ID.pattern == MESSAGE_ID_PATTERN.pattern
    assert probe_mod._QUOTABLE.pattern == imap_search._QUOTABLE.pattern
    assert probe_mod._SKIPPED_NAMES == EXCLUDED_LAST_NAMES
    assert (probe_mod._OR_VALUES, probe_mod._OR_MAX_BYTES) == (LOCATE_BATCH_VALUES, imap_search.MAX_OR_BYTES)
    assert probe_mod._GMAIL_FETCH_UIDS == FETCH_CHUNK_UIDS
    assert {"Junk", "Drafts"} <= set(probe_mod._FALLBACK_NAMES)
    ids = [f"id{i}@example.test" for i in range(20)]
    for n in range(1, 21):
        keys = [f"HEADER Message-ID {probe_mod.msg_id_token(i)}" for i in ids[:n]]
        assert probe_mod._or_tree(keys).encode() == imap_search.header_or(("Message-ID",), ids[:n])
    assert f"{probe_mod._GMAIL_SENT} NOT {probe_mod._GMAIL_INBOX}".encode() == imap_search.gmail_label_keys("sent")
    assert f"NOT {probe_mod._GMAIL_INBOX} NOT {probe_mod._GMAIL_SENT}".encode() == imap_search.gmail_label_keys(
        "archive"
    )


def test_or_batch_keeps_the_hub_byte_cap() -> None:
    ids = [f"{'a' * 280}{i:02d}@example.test" for i in range(20)]
    tree = probe_mod._or_batch(ids)
    assert len(tree) <= 4000
    assert tree.encode() == imap_search.header_or(("Message-ID",), [i for i in ids if f'"<{i}>"' in tree])


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("<ok@example.test>", "ok@example.test"),
        ("  <ok@example.test>  ", "ok@example.test"),
        ("plain@example.test", "plain@example.test"),
        ('<a"b@example.test>', None),
        ("<a\\b@example.test>", None),
        ("<a b@example.test>", None),
        ("<X-GM-RAW@example.test>", None),
        ("<a@x> <b@x>", None),
        ("<>", None),
    ],
)
def test_bare_message_id(header: str, expected: str | None) -> None:
    assert probe_mod._bare_message_id(header) == expected


class TimingOutClient(StubClient):
    """A server whose References search times out on every folder."""

    def search(self, criteria: Any = "ALL", charset: Any = None) -> list[int]:
        found = super().search(criteria, charset)
        if b"References" in criteria:
            raise TimeoutError
        return found


def test_cold_timings_reported() -> None:
    out, _ = _run(StubClient(_folders()))
    assert re.search(r"^list: ms=\d+$", out, re.MULTILINE)
    rows = re.findall(
        r"^examine_search_since: folder#\d role=\w+ hits=\d+ examine_ms=(\d+) search_ms=(\d+) ms=(\d+)$",
        out,
        re.MULTILINE,
    )
    assert len(rows) == 4
    assert all(int(e) + int(s) == int(total) for e, s, total in rows)
    lines = out.splitlines()
    assert lines.index(next(line for line in lines if line.startswith("list:"))) < lines.index(
        next(line for line in lines if line.startswith("examine_search_since:"))
    )


def test_a_failing_round_does_not_lose_the_rest() -> None:
    """P3: an error is reported per round by its class name; the later rounds and the Gmail section still run, and
    the text round comes last."""
    out, _ = _run(TimingOutClient(_folders(), capabilities=(b"X-GM-EXT-1",)))
    rounds = [line.split(":", 1)[0] for line in out.splitlines() if ":" in line]
    assert "header_references: error=TimeoutError" in out
    assert re.search(r"^header_message_id: folders=4 ", out, re.MULTILINE)
    assert re.search(r"^or20: folders=4 ", out, re.MULTILINE)
    assert "gmail_sent_mailbox" in rounds
    assert rounds[-1] == "text"
    assert re.search(r"^text: folders=4 total_ms=\d+ max_ms=\d+ found_in=\w+$", out, re.MULTILINE)


def test_a_refused_text_word_is_reported_not_fatal() -> None:
    out, _ = _run(StubClient(_folders()), text_word='a"b')
    assert out.splitlines()[-1] == "text: error=ValueError"
    assert 'a"b' not in out


def test_a_proxy_refusal_is_never_swallowed() -> None:
    class Refusing(StubClient):
        def search(self, criteria: Any = "ALL", charset: Any = None) -> list[int]:
            if b"References" in criteria:
                raise PermissionError("search")
            return super().search(criteria, charset)

    with pytest.raises(PermissionError):
        _run(Refusing(_folders()))


class GmailStub(StubClient):
    """Gmail with All Mail: the window holds 1,200 messages there; FETCH records its UIDs."""

    def __init__(self) -> None:
        super().__init__(
            [
                ((), b"/", "INBOX"),
                ((b"\\HasChildren", b"\\Noselect"), b"/", "[Gmail]"),
                ((b"\\All",), b"/", "[Gmail]/All Mail"),
                ((b"\\Sent",), b"/", "[Gmail]/Sent Mail"),
                ((b"\\Junk",), b"/", "[Gmail]/Spam"),
                ((b"\\Drafts",), b"/", "[Gmail]/Drafts"),
                ((b"\\Trash",), b"/", "[Gmail]/Trash"),
            ],
            capabilities=(b"IMAP4REV1", b"X-GM-EXT-1", b"SPECIAL-USE"),
        )
        self.fetched: list[tuple[list[int], list[str]]] = []

    def search(self, criteria: Any = "ALL", charset: Any = None) -> list[int]:
        text = criteria.decode()
        if self.selected == "[Gmail]/All Mail" and text.startswith("NOT DRAFT SINCE") and "X-GM-LABELS" not in text:
            self.commands.append(("SEARCH", text))
            return list(range(1, 1201))
        return super().search(criteria, charset)

    def fetch(self, messages: Any, data: Any) -> dict[int, dict[bytes, bytes]]:
        self.fetched.append((list(messages), list(data)))
        return super().fetch(messages, data)


def test_gmail_hub_keys_sent_mailbox_and_candidate_fetch() -> None:
    """P4: the hub's exact `sent` and `archive` searches in All Mail, the `\\Sent` mailbox for comparison, and one cold
    candidate FETCH of the newest 500 All Mail hits."""
    out, stub = _run(GmailStub())
    searches = _searches_by_folder(stub)
    since = re.search(r"SINCE (\d{2}-[A-Z][a-z]{2}-\d{4})", searches[0][1])
    assert since is not None
    day = since.group(1)
    assert ("[Gmail]/All Mail", f'NOT DRAFT SINCE {day} X-GM-LABELS "\\\\Sent" NOT X-GM-LABELS "\\\\Inbox"') in searches
    assert (
        "[Gmail]/All Mail",
        f'NOT DRAFT SINCE {day} NOT X-GM-LABELS "\\\\Inbox" NOT X-GM-LABELS "\\\\Sent"',
    ) in searches
    assert re.search(r"^gmail_sent_keys: hits=3 ms=\d+$", out, re.MULTILINE)
    assert re.search(r"^gmail_archive_keys: hits=3 ms=\d+$", out, re.MULTILINE)
    assert re.search(r"^gmail_sent_mailbox: hits=1 ms=\d+$", out, re.MULTILINE)
    assert re.search(r"^gmail_sent_compare: sent_keys=3 sent_mailbox=1 difference=-2$", out, re.MULTILINE)
    assert re.search(r"^gmail_candidate_fetch: uids=500 ms=\d+$", out, re.MULTILINE)
    candidate = [(uids, items) for uids, items in stub.fetched if "X-GM-LABELS" in items]
    assert candidate == [(list(range(701, 1201)), ["INTERNALDATE", "FLAGS", "X-GM-LABELS"])]
    examined = {text for cmd, text in stub.commands if cmd == "EXAMINE"}
    assert not examined & {"[Gmail]/Spam", "[Gmail]/Drafts", "[Gmail]"}
    assert "Sent Mail" not in out
    assert "All Mail" not in out


def _searches_by_folder(stub: StubClient) -> list[tuple[str, str]]:
    selected, out = "", []
    for cmd, text in stub.commands:
        if cmd == "EXAMINE":
            selected = text
        elif cmd == "SEARCH":
            out.append((selected, text))
    return out


class LongIdClient(StubClient):
    def fetch(self, messages: Any, data: Any) -> dict[int, dict[bytes, bytes]]:
        self.commands.append(("FETCH", ""))
        return {
            int(uid): {
                b"BODY[HEADER.FIELDS (MESSAGE-ID)]<0>": f"Message-ID: <longlocal{uid}@example.test>\r\n".encode()
            }
            for uid in messages
        }


def test_msgid_variants_send_the_exact_criteria() -> None:
    out, stub = _run(LongIdClient(_folders()))
    sent = [text for kind, text in stub.commands if kind == "SEARCH"]
    needle = "longlocal25@example.test"
    expected = [
        f'NOT DRAFT HEADER Message-ID "{needle}"',
        f'NOT DRAFT HEADER Message-ID "<{needle}>"',
        f'HEADER Message-ID "<{needle}>"',
        f'NOT DRAFT HEADER message-id "<{needle}>"',
        'NOT DRAFT HEADER Message-ID "longlocal25"',
    ]
    positions = [sent.index(c) for c in expected]
    assert positions == sorted(positions)
    names = re.findall(r"^msgid_variant: name=(\w+) hits=\d+ ms=\d+$", out, re.MULTILINE)
    assert names == [
        "msgid_bare",
        "msgid_bracketed",
        "msgid_no_not_draft",
        "msgid_lowercase_field",
        "msgid_localpart",
    ]
    assert out.index("msgid_variant") < out.index("header_message_id:")
    assert needle not in out
    assert "longlocal25" not in out


def test_msgid_localpart_needs_eight_characters() -> None:
    out, _ = _run(StubClient(_folders()))
    assert "msgid_bracketed" in out
    assert "msgid_localpart" not in out


class FailingVariant(StubClient):
    def search(self, criteria: Any = "ALL", charset: Any = None) -> list[int]:
        if isinstance(criteria, bytes) and criteria.startswith(b'NOT DRAFT HEADER Message-ID "<'):
            raise TimeoutError
        return super().search(criteria, charset)


def test_a_failing_msgid_variant_does_not_stop_the_others() -> None:
    out, _ = _run(FailingVariant(_folders()))
    assert "msgid_variant: name=msgid_bracketed error=TimeoutError" in out
    assert re.search(r"^msgid_variant: name=msgid_bare hits=\d+ ", out, re.MULTILINE)
    assert re.search(r"^msgid_variant: name=msgid_no_not_draft hits=\d+ ", out, re.MULTILINE)
    assert re.search(r"^text: folders=", out, re.MULTILINE)


# --- WP13b T0: status rounds P1-P7 (ruling E-im5), through the hub's guard and search builders (SR4) -----------------


@dataclass(frozen=True, slots=True)
class Msg:
    uid: int
    message_id: str
    in_reply_to: str = ""
    references: str = ""
    flags: tuple[bytes, ...] = ()
    labels: tuple[bytes, ...] = ()
    thrid: int | None = None


_STATUS_TODAY = date(2026, 10, 22)
_STATUS_SINCE = (_STATUS_TODAY - timedelta(days=180)).strftime("%d-%b-%Y")  # C locale in tests
_STATUS_BEFORE = (_STATUS_TODAY - timedelta(days=1)).strftime("%d-%b-%Y")  # samples at least one day old (T0-1)
_STATUS_COMMANDS = {"CAPABILITY", "LOGIN", "LIST", "EXAMINE", "SEARCH", "FETCH", "LOGOUT"}
_STATUS_FETCH_ITEMS = {
    "BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)]<0.2048>",
    "BODY.PEEK[HEADER.FIELDS (MESSAGE-ID IN-REPLY-TO REFERENCES)]<0.65536>",
    "BODY.PEEK[HEADER.FIELDS (MESSAGE-ID IN-REPLY-TO REFERENCES FROM)]<0.65536>",
    "FLAGS",
    "INTERNALDATE",
    "X-GM-LABELS",
    "X-GM-THRID",
}
_TOKEN = re.compile(r"<([^<>]*)>")


class StatusStub:
    """A raw IMAPClient stand-in for the status rounds: the hub's guard drives it. Header searches match whole
    bracketed tokens, case-sensitive (iCloud, O35); OR trees match when any leaf does."""

    def __init__(
        self,
        mailboxes: dict[str, list[Msg]],
        folders: list[Folder],
        capabilities: tuple[bytes, ...] = (b"IMAP4REV1", b"SPECIAL-USE"),
        *,
        honours_not_draft: bool = True,
    ) -> None:
        self.mailboxes = mailboxes
        self.folders = folders
        self.caps = capabilities
        self.honours_not_draft = honours_not_draft
        self.commands: list[tuple[str, str]] = []
        self.selected = ""
        self.timeouts: list[float] = []
        self.use_uid = False
        self.folder_encode = True
        self.normalise_times = True

    def socket(self) -> "StatusStub":
        return self

    def settimeout(self, value: float) -> None:
        self.timeouts.append(value)

    def capabilities(self) -> tuple[bytes, ...]:
        self.commands.append(("CAPABILITY", ""))
        return self.caps

    def login(self, username: str, password: str) -> bytes:
        self.commands.append(("LOGIN", ""))
        return b"OK"

    def list_folders(self, directory: str = "", pattern: str = "*") -> list[Folder]:
        self.commands.append(("LIST", ""))
        return self.folders

    def select_folder(self, folder: str, readonly: bool = False) -> dict[bytes, object]:
        self.commands.append(("EXAMINE" if readonly else "SELECT", folder))
        self.selected = folder
        return {b"UIDVALIDITY": 1}

    def search(self, criteria: Any) -> list[int]:
        text = criteria.decode() if isinstance(criteria, bytes) else str(criteria)
        self.commands.append(("SEARCH", text))
        rows = list(self.mailboxes.get(self.selected, []))
        if self.honours_not_draft and text.startswith("NOT DRAFT"):
            rows = [m for m in rows if b"\\Draft" not in m.flags]
        if 'X-GM-LABELS "\\\\Sent"' in text:
            rows = [m for m in rows if b"\\Sent" in m.labels]
        leaves = re.findall(r'HEADER (\S+) "<([^"]*)>"', text)
        if leaves:
            rows = [m for m in rows if any(tok in _tokens(m, field) for field, tok in leaves)]
        thrids = {int(v) for v in re.findall(r"X-GM-THRID (\d+)", text)}
        if thrids:
            rows = [m for m in rows if m.thrid in thrids]
        return [m.uid for m in rows]

    def fetch(self, messages: Any, data: Any) -> dict[int, dict[bytes, object]]:
        items = list(data)
        self.commands.append(("FETCH", " ".join(items)))
        by_uid = {m.uid: m for m in self.mailboxes.get(self.selected, [])}
        out: dict[int, dict[bytes, object]] = {}
        for uid in messages:
            m = by_uid.get(uid)
            if m is None:
                continue
            row: dict[bytes, object] = {}
            for item in items:
                if item.startswith("BODY.PEEK[HEADER.FIELDS ("):
                    fields = item[len("BODY.PEEK[HEADER.FIELDS (") : item.index(")")].split()
                    row[f"BODY[HEADER.FIELDS ({' '.join(fields)})]<0>".encode()] = _header_block(m, fields)
                elif item == "FLAGS":
                    row[b"FLAGS"] = m.flags
                elif item == "X-GM-LABELS":
                    row[b"X-GM-LABELS"] = m.labels
                elif item == "X-GM-THRID":
                    row[b"X-GM-THRID"] = m.thrid
                elif item == "INTERNALDATE":
                    row[b"INTERNALDATE"] = datetime(2026, 10, 1, 12, 0)
            out[uid] = row
        return out

    def logout(self) -> bytes:
        self.commands.append(("LOGOUT", ""))
        return b"OK"

    def add_flags(self, *args: object) -> None:
        raise AssertionError("write reached the client")

    move = append = expunge = gmail_search = set_flags = add_flags


def _tokens(m: Msg, field: str) -> list[str]:
    name = field.lower()
    if name == "message-id":
        return [m.message_id]
    return _TOKEN.findall(m.in_reply_to if name == "in-reply-to" else m.references)


def _header_block(m: Msg, fields: list[str]) -> bytes:
    lines = []
    for field in fields:
        if field == "MESSAGE-ID":
            lines.append(f"Message-ID: <{m.message_id}>")
        elif field == "IN-REPLY-TO" and m.in_reply_to:
            lines.append(f"In-Reply-To: {m.in_reply_to}")
        elif field == "REFERENCES" and m.references:
            lines.append(f"References: {m.references}")
        elif field == "FROM":
            lines.append("From: someone@example.test")
    return ("\r\n".join(lines) + "\r\n\r\n").encode()


def _inbox(count: int = 160) -> list[Msg]:
    return [Msg(uid, f"in{uid}@example.test") for uid in range(1, count + 1)]


def _refs(*names: str) -> str:
    return " ".join(f"<{n}@example.test>" for n in names)


def _sent_icloud() -> list[Msg]:
    """Candidates newest first: s6, s5 and s2 have an In-Reply-To; s4 has none and is passed over (T0-1)."""
    return [
        Msg(1, "s1@example.test", "<r9@example.test>", "<r9@example.test>"),
        Msg(2, "s2@example.test", "<r4@example.test>", "<r1@example.test> <r2@example.test>\r\n " + _refs("r3", "r4")),
        Msg(3, "s3@example.test", "<r8@example.test>", _refs("r7", "r8")),
        Msg(4, "s4@example.test", "", _refs("r11", "r12", "r13")),
        Msg(5, "s5@example.test", "<r23@example.test>", _refs("r21", "r22", "r23")),
        Msg(6, "s6@example.test", "<r35@example.test>", _refs("r31", "r32", "r33", "r34", "r35")),
    ]


def _icloud_stub(**kwargs: Any) -> StatusStub:
    folders: list[Folder] = [
        ((b"\\HasNoChildren",), b"/", "INBOX"),
        ((b"\\Sent",), b"/", "Sent Messages"),
        ((b"\\Trash",), b"/", "Deleted Messages"),
        ((b"\\Junk",), b"/", "Junk"),
    ]
    mailboxes = {"INBOX": _inbox(), "Sent Messages": _sent_icloud()}
    return StatusStub(mailboxes, folders, **kwargs)


def _gmail_stub(cls: type[StatusStub] = StatusStub) -> StatusStub:
    folders: list[Folder] = [
        ((), b"/", "INBOX"),
        ((b"\\HasChildren", b"\\Noselect"), b"/", "[Gmail]"),
        ((b"\\All",), b"/", "[Gmail]/All Mail"),
        ((b"\\Sent",), b"/", "[Gmail]/Sent Mail"),
        ((b"\\Trash",), b"/", "[Gmail]/Trash"),
    ]
    all_mail = [Msg(uid, f"in{uid}@example.test", labels=(b"\\Inbox",), thrid=1000 + uid) for uid in range(1, 31)]
    all_mail += [
        Msg(31, "s31@example.test", "<r3@example.test>", _refs("r1", "r2", "r3"), labels=(b"\\Sent",), thrid=2000),
        Msg(
            32,
            "d32@example.test",
            "<r3@example.test>",
            _refs("r1", "r2", "r3"),
            labels=(b"\\Sent", b"\\Draft"),
            thrid=2001,
        ),
    ]
    mailboxes = {"INBOX": _inbox(30), "[Gmail]/All Mail": all_mail, "[Gmail]/Sent Mail": all_mail[30:31]}
    return cls(mailboxes, folders, (b"IMAP4REV1", b"X-GM-EXT-1", b"SPECIAL-USE"))


def _status_run(stub: StatusStub) -> str:
    out = io.StringIO()
    probe_mod.status_rounds(stub, frozenset(stub.caps), out=out, today=_STATUS_TODAY)
    return out.getvalue()


def _hub_or(fields: tuple[str, ...], ids: list[str]) -> str:
    return "NOT DRAFT " + imap_search.header_or(fields, ids).decode()  # type: ignore[arg-type]  # literal fields


def _newest_inbox_ids(start: int, n: int, count: int = 160) -> list[str]:
    """Newest-first inbox ids `start` .. `start + n - 1` of the sample (index 0 is the newest)."""
    return [f"in{count - i}@example.test" for i in range(start, start + n)]


def _reply_keys(gmail: bool, field: str, names: list[str]) -> list[str]:
    sent = 'X-GM-LABELS "\\\\Sent" ' if gmail else ""
    return [f'NOT DRAFT {sent}HEADER {field} "<{n}@example.test>"' for n in names]


def test_status_rounds_shapes() -> None:
    """Every criteria on the wire, per round, as the hub builds it: the aged sample windows; P1 control and
    first/middle/last References and In-Reply-To for up to 3 candidates (In-Reply-To first); P2 upper-cased; P3
    a distinct id slice per run in inbox and sent; P4 three distinct OR-20 and OR-50 batches (T0-1, T0-2)."""
    stub = _icloud_stub()
    out = _status_run(stub)
    searches = _searches_by_folder(stub)
    window = f"NOT DRAFT SINCE {_STATUS_SINCE} BEFORE {_STATUS_BEFORE}"
    sent = "Sent Messages"
    p1 = _reply_keys(False, "Message-ID", ["s6", "s5", "s2"])
    p1 += _reply_keys(False, "References", ["r31", "r21", "r1"])
    p1 += _reply_keys(False, "References", ["r33", "r22", "r3"])
    p1 += _reply_keys(False, "References", ["r35", "r23", "r4"])
    p1 += _reply_keys(False, "In-Reply-To", ["r35", "r23", "r4"])
    p1 += [f'NOT DRAFT HEADER In-Reply-To "<{n}@EXAMPLE.TEST>"' for n in ("R35", "R23", "R4")]  # P2
    p3: list[tuple[str, str]] = []
    for folder in ("INBOX", sent):
        p3.append((folder, _hub_or(("Message-ID",), _newest_inbox_ids(0, 1))))  # warm-up, never a timed shape
        for keys in (60, 40, 20, 10):  # largest first: per-term caching biases against Design B (N2)
            count = keys // 2
            p3 += [
                (folder, _hub_or(("In-Reply-To", "References"), _newest_inbox_ids(r * count, count))) for r in range(3)
            ]
    p4 = [("INBOX", _hub_or(("Message-ID",), _newest_inbox_ids(r * 20, 20))) for r in range(3)]
    p4 += [("INBOX", _hub_or(("Message-ID",), _newest_inbox_ids(r * 50, 50))) for r in range(3)]
    expected = [("INBOX", window), (sent, window), *[(sent, c) for c in p1], *p3, *p4, (sent, window)]
    assert searches == expected
    assert len({c for _, c in p3[1:13]}) == 12  # no timed criteria repeats
    assert re.search(r"^p1_reply_match: candidates=3 with_irt=3 scanned=6 drafts=0 skipped_unsafe=0 ms=\d+$", out, re.M)
    assert re.search(r"^p1_reply_match: control=3/3 ms=\d+$", out, re.M)
    for key in ("references_first", "references_middle", "references_last", "in_reply_to"):
        assert re.search(rf"^p1_reply_match: key={key} target_hit=3/3 control=3/3 result=yes ms=\d+$", out, re.M)
    assert re.search(r"^p2_case: target_hit=0/3 ms=\d+$", out, re.M)
    p3_line = (
        r"^p3_key_scaling: folder=(inbox|sent) keys=\d+ ids=\d+ bytes=\d+ runs_ms=\d+,\d+,\d+ median_ms=\d+ hits=\d+$"
    )
    assert len(re.findall(p3_line, out, re.M)) == 8
    sizes = re.findall(r"^p3_key_scaling: folder=(inbox|sent) keys=(\d+) ", out, re.M)
    assert sizes == [(f, k) for f in ("inbox", "sent") for k in ("60", "40", "20", "10")]
    assert re.search(
        r"^p4_location_width: keys=20 bytes=\d+ runs_ms=\d+,\d+,\d+ median_ms=\d+ found=60/60 "
        r"target_found=yes$",
        out,
        re.M,
    )
    assert re.search(
        r"^p4_location_width: keys=50 bytes=\d+ runs_ms=\d+,\d+,\d+ median_ms=\d+ found=150/150 "
        r"target_found=yes$",
        out,
        re.M,
    )
    assert re.search(r"^p5_gmail_thrid: skipped=not_gmail$", out, re.M)
    assert re.search(r"^p6_reexamine: inbox_ms=\d+ other=sent other_ms=\d+ inbox_again_ms=\d+$", out, re.M)
    assert re.search(
        r"^p7_header_fetch: mailbox=sent uids=6 rows=6 ms=\d+ header_bytes=\d+ max_header_bytes=\d+ cut=0$", out, re.M
    )
    examined = [text for cmd, text in stub.commands if cmd == "EXAMINE"]
    assert examined[-4:] == ["INBOX", sent, "INBOX", sent]  # P6 inbox, sent, inbox; P7 sent


def test_status_rounds_shapes_gmail() -> None:
    """Gmail: P1 in All Mail with exactly the hub's reply criteria (IR7), the control included; the
    `\\Draft`-labelled sent row is dropped; P5 one OR of the newest distinct X-GM-THRIDs; P7 with the Gmail
    items; P3/P4 sizes without enough distinct ids are skipped, never filled by reuse."""
    stub = _gmail_stub()
    out = _status_run(stub)
    searches = _searches_by_folder(stub)
    all_mail = "[Gmail]/All Mail"
    sent = 'X-GM-LABELS "\\\\Sent"'
    assert (all_mail, f"NOT DRAFT SINCE {_STATUS_SINCE} BEFORE {_STATUS_BEFORE} {sent}") in searches
    p1 = [s for s in searches if s[1].startswith(f"NOT DRAFT {sent} HEADER")]
    assert p1 == [
        (all_mail, f'NOT DRAFT {sent} HEADER Message-ID "<s31@example.test>"'),
        (all_mail, f'NOT DRAFT {sent} HEADER References "<r1@example.test>"'),
        (all_mail, f'NOT DRAFT {sent} HEADER References "<r2@example.test>"'),
        (all_mail, f'NOT DRAFT {sent} HEADER References "<r3@example.test>"'),
        (all_mail, f'NOT DRAFT {sent} HEADER In-Reply-To "<r3@example.test>"'),
        (all_mail, f'NOT DRAFT {sent} HEADER In-Reply-To "<R3@EXAMPLE.TEST>"'),
    ]
    assert re.search(r"^p1_reply_match: candidates=1 with_irt=1 scanned=2 drafts=1 skipped_unsafe=0 ms=\d+$", out, re.M)
    assert re.search(r"^p1_reply_match: key=references_middle target_hit=1/1 control=1/1 result=yes ms=\d+$", out, re.M)
    thrids = [2000, *range(1030, 1011, -1)]  # newest first, distinct, the draft's dropped
    assert (all_mail, "NOT DRAFT " + imap_search.thrid_or(thrids).decode()) in searches
    assert re.search(r"^p5_gmail_thrid: accepted=yes keys=20 bytes=\d+ hits=20 members_found=20/20 ms=\d+$", out, re.M)
    assert re.search(r"^p3_key_scaling: folder=inbox keys=20 ids=10 ", out, re.M)  # 30 ids: 3 x 10
    assert re.search(r"^p3_key_scaling: folder=inbox keys=40 skipped=too_few_ids$", out, re.M)
    assert re.search(r"^p4_location_width: keys=20 skipped=too_few_ids$", out, re.M)
    assert re.search(r"^p6_reexamine: inbox_ms=\d+ other=all_mail other_ms=\d+ inbox_again_ms=\d+$", out, re.M)
    p7 = [text for cmd, text in stub.commands if cmd == "FETCH"][-1]
    assert p7 == (
        "BODY.PEEK[HEADER.FIELDS (MESSAGE-ID IN-REPLY-TO REFERENCES FROM)]<0.65536> FLAGS INTERNALDATE "
        "X-GM-LABELS X-GM-THRID"
    )
    assert re.search(r"^p7_header_fetch: mailbox=all_mail uids=2 rows=2 ", out, re.M)


def test_status_rounds_only_read_only_commands() -> None:
    for stub in (_icloud_stub(), _gmail_stub()):
        _status_run(stub)
        assert {cmd for cmd, _ in stub.commands} <= _STATUS_COMMANDS
        searches = [text for cmd, text in stub.commands if cmd == "SEARCH"]
        assert searches
        assert all(text.startswith("NOT DRAFT") for text in searches)
        assert not any("X-GM-RAW" in text.upper() for text in searches)
        fetches = [text for cmd, text in stub.commands if cmd == "FETCH"]
        assert all(set(_split_items(text)) <= _STATUS_FETCH_ITEMS for text in fetches)
        assert (stub.use_uid, stub.folder_encode) == (True, False)  # set by the hub's guard
        assert stub.timeouts
        assert max(stub.timeouts) <= probe_mod.ROUND_SECONDS


def _split_items(text: str) -> list[str]:
    return re.findall(r"BODY\.PEEK\[[^\]]*\]<[^>]*>|[A-Z-]+", text)


_STATUS_LINE = re.compile(r"[a-z0-9_]+: [a-z0-9_]+=[a-z0-9_/,]+( [a-z0-9_]+=[a-z0-9_/,]+)*")


def test_status_rounds_print_no_ids_or_names() -> None:
    for stub in (_icloud_stub(), _gmail_stub()):
        out = _status_run(stub)
        lines = out.splitlines()
        assert lines[0] == "--- result ---"
        for line in lines[1:]:
            assert _STATUS_LINE.fullmatch(line), line
        for leak in ("@", "example", "Sent Messages", "All Mail", "Gmail", "INBOX", "in160", "r31", "s6"):
            assert leak not in out


class _FailingMiddle(StatusStub):
    def search(self, criteria: Any) -> list[int]:
        if criteria == b'NOT DRAFT HEADER References "<r3@example.test>"':
            self.commands.append(("SEARCH", criteria.decode()))
            raise TimeoutError("r3@example.test timed out")
        if b"X-GM-THRID" in criteria:
            raise imaplib.IMAP4.error("UID command error: BAD [CLIENTBUG] r3@example.test")
        return super().search(criteria)


def test_status_round_error_reported_by_class_and_next_round_runs() -> None:
    """An error line keeps the `k=v` grammar (T0-S3): `… key=references_middle error=TimeoutError`."""
    stub = _FailingMiddle(_icloud_stub().mailboxes, _icloud_stub().folders)
    out = _status_run(stub)
    assert "p1_reply_match: key=references_middle error=TimeoutError" in out.splitlines()
    assert re.search(r"^p1_reply_match: key=references_last target_hit=3/3 ", out, re.M)
    assert re.search(r"^p2_case: target_hit=", out, re.M)
    for name in ("p3_key_scaling", "p4_location_width", "p5_gmail_thrid", "p6_reexamine", "p7_header_fetch"):
        assert re.search(rf"^{name}: ", out, re.M)
    for line in out.splitlines()[1:]:  # error values are class names
        assert re.fullmatch(r"[a-z0-9_]+: [a-z0-9_]+=[A-Za-z0-9_/,]+( [a-z0-9_]+=[A-Za-z0-9_/,]+)*", line), line
    assert "timed out" not in out
    assert "r3@" not in out


class _RefusingThridOr(StatusStub):
    def search(self, criteria: Any) -> list[int]:
        if b"X-GM-THRID" in criteria and b"OR " in criteria:
            raise imaplib.IMAP4.error("UID command error: BAD [CLIENTBUG] r3@example.test")
        return super().search(criteria)


@pytest.mark.parametrize(("cls", "single"), [(_FailingMiddle, "no"), (_RefusingThridOr, "yes")])
def test_status_round_refused_thrid_or_reported_as_not_accepted(cls: type[StatusStub], single: str) -> None:
    """T0-3: a refused OR is followed by one single-key X-GM-THRID search, reported as accepted or not."""
    stub = _gmail_stub(cls)
    out = _status_run(stub)
    assert re.search(rf"^p5_gmail_thrid: accepted=no status=bad keys=20 ms=\d+ single_accepted={single}$", out, re.M)
    singles = [t for c, t in stub.commands if c == "SEARCH" and re.fullmatch(r"NOT DRAFT X-GM-THRID \d+", t)]
    assert singles == (["NOT DRAFT X-GM-THRID 2000"] if single == "yes" else [])
    assert re.search(r"^p6_reexamine: ", out, re.M)
    assert "CLIENTBUG" not in out


def test_status_round_guard_refusal_is_never_swallowed() -> None:
    stub = _icloud_stub()
    stub.folders[1] = ((b"\\Sent",), b"/", "Sent\x7fMessages")  # the guard refuses this name for EXAMINE
    with pytest.raises(ImapCommandRefused):
        _status_run(stub)


def test_status_rounds_skip_unsafe_ids() -> None:
    """SR4: Message-IDs with `"`, `{5}` or `X-GM-RAW` never reach the wire; the skips are counted and printed."""
    inbox = _inbox()
    inbox += [Msg(161, 'a"b@example.test'), Msg(162, "c{5}@example.test"), Msg(163, "X-GM-RAW@example.test")]
    unsafe_refs = '<a"b@example.test> <r2@example.test> <X-GM-RAW@example.test>'
    sent = [*_sent_icloud(), Msg(7, "s7@example.test", "<x{5}@example.test>", unsafe_refs)]
    stub = _icloud_stub()
    stub.mailboxes = {"INBOX": inbox, "Sent Messages": sent}
    out = _status_run(stub)
    wire = " ".join(text for cmd, text in stub.commands if cmd == "SEARCH")
    assert 'a"b' not in wire
    assert "{5}" not in wire
    assert "X-GM-RAW" not in wire.upper()
    assert re.search(r"^status_ids: rows=160 usable=157 drafts=0 skipped_unsafe=3 ms=\d+$", out, re.M)
    assert re.search(r"^p1_reply_match: candidates=3 with_irt=3 scanned=7 drafts=0 skipped_unsafe=2 ms=\d+$", out, re.M)
    assert 'NOT DRAFT HEADER References "<r1@example.test>"' in wire  # the next safe candidates are used


def test_p1_no_candidate_prints_skipped() -> None:
    """IR7: no sent row with three References tokens (the draft that has them is dropped even when the server
    ignores NOT DRAFT) → `skipped=no_candidate`, no P1/P2 search; never a `target_hit`."""
    sent = [
        Msg(1, "s1@example.test", "<r9@example.test>", _refs("r8", "r9")),
        Msg(2, "d2@example.test", "<r3@example.test>", _refs("r1", "r2", "r3"), flags=(b"\\Draft",)),
    ]
    stub = _icloud_stub(honours_not_draft=False)
    stub.mailboxes = {"INBOX": _inbox(), "Sent Messages": sent}
    out = _status_run(stub)
    assert re.search(r"^p1_reply_match: skipped=no_candidate scanned=2 drafts=1 skipped_unsafe=0 ms=\d+$", out, re.M)
    assert re.search(r"^p2_case: skipped=no_candidate$", out, re.M)
    assert "target_hit" not in out
    p1_shape = r'NOT DRAFT HEADER (References|In-Reply-To) "<[^"]*>"|NOT DRAFT HEADER Message-ID "<[sd]\d+@[^"]*>"'
    assert [t for c, t in stub.commands if c == "SEARCH" and re.fullmatch(p1_shape, t)] == []


def test_p1_prefers_candidates_with_in_reply_to() -> None:
    """T0-1: candidates with a usable In-Reply-To come first; a row without one fills up only when fewer than 3
    have it, and the In-Reply-To key then counts over its own subset."""
    sent = [m for m in _sent_icloud() if m.uid != 6]
    stub = _icloud_stub()
    stub.mailboxes = {"INBOX": _inbox(), "Sent Messages": sent}
    out = _status_run(stub)
    wire = [t for c, t in stub.commands if c == "SEARCH"]
    assert _reply_keys(False, "Message-ID", ["s5", "s2", "s4"]) == [t for t in wire if "HEADER Message-ID" in t][:3]
    assert re.search(r"^p1_reply_match: candidates=3 with_irt=2 ", out, re.M)
    assert re.search(r"^p1_reply_match: key=references_first target_hit=3/3 control=3/3 result=yes ", out, re.M)
    assert re.search(r"^p1_reply_match: key=in_reply_to target_hit=2/2 control=2/2 result=yes ", out, re.M)


class _NoMiddleMatch(StatusStub):
    """A server that never matches a References token in the middle of the list."""

    def search(self, criteria: Any) -> list[int]:
        if criteria in {f'NOT DRAFT HEADER References "<{n}@example.test>"'.encode() for n in ("r33", "r22", "r3")}:
            self.commands.append(("SEARCH", criteria.decode()))
            return []
        return super().search(criteria)


class _Lagging(StatusStub):
    """A server whose index does not know s5 yet: no HEADER search finds it, its own Message-ID included."""

    def search(self, criteria: Any) -> list[int]:
        hits = super().search(criteria)
        if self.selected == "Sent Messages" and b"HEADER" in criteria:
            return [uid for uid in hits if uid != 5]
        return hits


def test_p1_key_fails_only_at_zero_with_full_control() -> None:
    """T0-1 gate: `result=no` only at 0/n with control n/n; a partial hit or a partial control is `inconclusive`."""
    stub = _NoMiddleMatch(_icloud_stub().mailboxes, _icloud_stub().folders)
    out = _status_run(stub)
    assert re.search(r"^p1_reply_match: key=references_middle target_hit=0/3 control=3/3 result=no ms=\d+$", out, re.M)
    assert re.search(r"^p1_reply_match: key=references_first target_hit=3/3 control=3/3 result=yes ", out, re.M)
    stub = _Lagging(_icloud_stub().mailboxes, _icloud_stub().folders)
    out = _status_run(stub)
    assert re.search(r"^p1_reply_match: control=2/3 ms=\d+$", out, re.M)
    for key in ("references_first", "references_middle", "references_last", "in_reply_to"):
        assert re.search(rf"^p1_reply_match: key={key} target_hit=2/3 control=2/3 result=inconclusive ", out, re.M)
    assert "result=no" not in out


def test_status_rounds_main_logs_in_through_the_guard(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    stub = _icloud_stub()
    monkeypatch.setattr(probe_mod, "IMAPClient", lambda *args, **kwargs: stub)
    monkeypatch.setattr(probe_mod.getpass, "getpass", lambda prompt="": "x")
    assert probe_mod.main(["--host", "imap.mail.me.com", "--status-rounds"]) == 0
    commands = [cmd for cmd, _ in stub.commands]
    assert commands[:3] == ["LOGIN", "CAPABILITY", "LIST"]
    assert commands[-1] == "LOGOUT"
    out = capsys.readouterr().out
    assert out.startswith("--- result ---\ncapabilities: ")
    assert "p7_header_fetch: " in out
    assert "examine_search_since" not in out  # the O35 rounds do not run in this mode


def test_status_rounds_login_failure_prints_class_name_only(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """T0-S2: a refused LOGIN ends the run with the class name only; its text never reaches the output."""

    class Refusing(StatusStub):
        def login(self, username: str, password: str) -> bytes:
            raise LoginError("[AUTHENTICATIONFAILED] user@example.test")

    stub = Refusing(_icloud_stub().mailboxes, _icloud_stub().folders)
    monkeypatch.setattr(probe_mod, "IMAPClient", lambda *args, **kwargs: stub)
    monkeypatch.setattr(probe_mod.getpass, "getpass", lambda prompt="": "x")
    assert probe_mod.main(["--host", "imap.mail.me.com", "--status-rounds"]) == 1
    captured = capsys.readouterr()
    assert captured.err == "error: LoginError\n"
    assert captured.out == ""
    assert [cmd for cmd, _ in stub.commands] == ["LOGOUT"]


def test_help_starts_with_owner_usage(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        probe_mod.main(["--help"])
    assert exit_info.value.code == 0
    out = capsys.readouterr().out
    assert "Run by the owner" in out
    assert "uv run python scripts/imap_probe.py --host imap.mail.me.com --status-rounds" in out
    assert "uv run python scripts/imap_probe.py --host imap.gmail.com --status-rounds" in out
    assert out.index("Run by the owner") < out.index("--status-rounds  ")  # the usage block comes before the options


# --- WP13d T9: search rounds -------------------------------------------------------------------------------------

_SEARCH_TODAY = date(2026, 10, 27)
_SEARCH_SINCE = (_SEARCH_TODAY - timedelta(days=30)).strftime("%d-%b-%Y")  # C locale in tests
_W1, _W2, _W3 = "tomatoes", "cucumbers", "zucchinis"
_SENDER, _FOREIGN, _CC = "gardenerjoe", "Überraschung", "hiddencc"
_SEARCH_COMMANDS = {"CAPABILITY", "LIST", "EXAMINE", "SEARCH", "FETCH"}
_CONTROL_ITEM = "BODY.PEEK[HEADER.FIELDS (FROM SUBJECT)]<0.2048>"
_SECRETS = (_W1, _W2, _W3, _SENDER, _FOREIGN, _FOREIGN.upper(), _CC, "tomat", "mato", "omat", "gardene", "rdene")


def _words(**kwargs: Any) -> Any:
    values: dict[str, Any] = {"w1": _W1, "w2": _W2, "w3": _W3, "sender": _SENDER, "foreign": _FOREIGN, "cc": _CC}
    return probe_mod.SearchWords(**{**values, **kwargs})


@dataclass
class SMsg:
    uid: int
    subject: str = ""
    sender: str = ""
    to: str = ""
    text: str = ""
    raw_from: str | None = None  # the raw header field the control FETCH sees
    raw_subject: str | None = None


_Q = re.compile(r'(TEXT|FROM|TO|SUBJECT) "([^"]*)"')


class SearchStub:
    """A raw client for the search rounds: substring matching on quoted values, a literal path via
    `_raw_command_untagged`, and a recording of every command. Header text never leaves the stub except through
    the control FETCH."""

    def __init__(
        self,
        mailboxes: dict[str, list[SMsg]],
        folders: list[Folder],
        capabilities: tuple[bytes, ...] = (b"IMAP4REV1", b"SPECIAL-USE", b"LITERAL+"),
    ) -> None:
        self.mailboxes, self.folders, self.caps = mailboxes, folders, capabilities
        self.commands: list[tuple[str, str]] = []
        self.literal_args: list[list[bytes]] = []
        self.selected = ""
        self.use_uid = False
        self.folder_encode = True
        self.normalise_times = True
        self.literal_error: Exception | None = None
        self.search_error: tuple[str, Exception] | None = None

    def socket(self) -> "SearchStub":
        return self

    def settimeout(self, value: float) -> None:
        pass

    def capabilities(self) -> tuple[bytes, ...]:
        self.commands.append(("CAPABILITY", ""))
        return self.caps

    def login(self, username: str, password: str) -> bytes:
        self.commands.append(("LOGIN", ""))
        return b"OK"

    def list_folders(self, directory: str = "", pattern: str = "*") -> list[Folder]:
        self.commands.append(("LIST", ""))
        return self.folders

    def select_folder(self, folder: str, readonly: bool = False) -> dict[bytes, object]:
        self.commands.append(("EXAMINE" if readonly else "SELECT", folder))
        self.selected = folder
        return {b"UIDVALIDITY": 1}

    def _match(self, rows: list[SMsg], pairs: list[tuple[str, str]]) -> list[int]:
        fields = {"TEXT": "text", "FROM": "sender", "TO": "to", "SUBJECT": "subject"}
        hits = [m for m in rows if all(value.lower() in str(getattr(m, fields[key])).lower() for key, value in pairs)]
        return [m.uid for m in hits]

    def search(self, criteria: Any) -> list[int]:
        text = criteria.decode() if isinstance(criteria, bytes) else str(criteria)
        self.commands.append(("SEARCH", text))
        if self.search_error is not None and self.search_error[0] in text:
            raise self.search_error[1]
        return self._match(self.mailboxes.get(self.selected, []), _Q.findall(text))

    def _raw_command_untagged(self, command: bytes, args: list[bytes], uid: bool = False) -> list[Any]:
        self.commands.append(("SEARCH", "literal"))
        self.literal_args.append(list(args))
        if self.literal_error is not None:
            raise self.literal_error
        key, value = args[3].decode(), args[4].decode("utf-8")
        rows = self.mailboxes.get(self.selected, [])
        return [" ".join(str(u) for u in self._match(rows, [(key, value)])).encode()]

    def fetch(self, messages: Any, data: Any) -> dict[int, dict[bytes, object]]:
        items = list(data)
        self.commands.append(("FETCH", " ".join(items)))
        by_uid = {m.uid: m for m in self.mailboxes.get(self.selected, [])}
        out: dict[int, dict[bytes, object]] = {}
        for uid in messages:
            m = by_uid[uid]
            block = f"From: {m.raw_from or m.sender}\r\nSubject: {m.raw_subject or m.subject}\r\n\r\n"
            out[uid] = {b"BODY[HEADER.FIELDS (FROM SUBJECT)]<0>": block.encode()}
        return out

    def logout(self) -> bytes:
        self.commands.append(("LOGOUT", ""))
        return b"OK"

    def add_flags(self, *args: object) -> None:
        raise AssertionError("write reached the client")

    move = append = expunge = gmail_search = set_flags = add_flags


def _inbox_msgs() -> list[SMsg]:
    return [
        SMsg(1, f"{_W1} soup", f"{_SENDER}@example.test", text=f"{_W1} {_W2}", raw_from="=?UTF-8?B?Zm9v?= <a@b.test>"),
        SMsg(2, "hello", "other@example.test", text=f"{_W2} {_W3}", raw_subject="=?utf-8?q?caf=C3=A9?="),
        SMsg(3, f"{_FOREIGN} today", "third@example.test", text="plain", to=f"{_CC}@example.test"),
        SMsg(4, "none", "fourth@example.test", text=f"{_W2} {_W1} {_W3}"),
    ]


def _icloud_search_stub() -> SearchStub:
    folders: list[Folder] = [
        ((b"\\HasNoChildren",), b"/", "INBOX"),
        ((b"\\Sent",), b"/", "Sent Messages"),
        ((b"\\Archive",), b"/", "Archive"),
        ((b"\\Trash",), b"/", "Deleted Messages"),
        ((b"\\Junk",), b"/", "Junk"),
    ]
    mailboxes = {"INBOX": _inbox_msgs(), "Sent Messages": _inbox_msgs()[:2], "Archive": _inbox_msgs()[2:]}
    return SearchStub(mailboxes, folders)


def _gmail_search_stub() -> SearchStub:
    folders: list[Folder] = [
        ((), b"/", "INBOX"),
        ((b"\\HasChildren", b"\\Noselect"), b"/", "[Gmail]"),
        ((b"\\All",), b"/", "[Gmail]/All Mail"),
        ((b"\\Sent",), b"/", "[Gmail]/Sent Mail"),
    ]
    mailboxes = {"INBOX": _inbox_msgs()[:3], "[Gmail]/All Mail": _inbox_msgs()}
    return SearchStub(mailboxes, folders, (b"IMAP4REV1", b"X-GM-EXT-1", b"LITERAL-", b"UTF8=ACCEPT", b"ENABLE"))


def _search_run(stub: SearchStub, words: Any = None) -> str:
    out = io.StringIO()
    probe_mod.search_rounds(stub, frozenset(stub.caps), words or _words(), out=out, today=_SEARCH_TODAY)
    return out.getvalue()


def _searches(stub: SearchStub) -> list[tuple[str, str]]:
    """(folder, criteria) of every UID SEARCH."""
    selected = ""
    found: list[tuple[str, str]] = []
    for command, argument in stub.commands:
        if command == "EXAMINE":
            selected = argument
        elif command == "SEARCH":
            found.append((selected, argument))
    return found


@pytest.mark.parametrize("word", ["short", "tomato", "Überraschung", "tomato-es", "with space", "tomatoes1", ""])
def test_s2_words_need_seven_ascii_letters(word: str) -> None:
    with pytest.raises(ValueError, match=r"^word refused: use at least 7 ASCII letters$") as refused:
        probe_mod.ascii_word(word)
    assert word not in str(refused.value) or word == ""
    assert probe_mod.ascii_word("tomatoes") == "tomatoes"


def test_other_inputs_have_fixed_refusals() -> None:
    for call, bad_value in (
        (probe_mod.sender_part, "short"),
        (probe_mod.sender_part, 'has"quote123'),
        (probe_mod.foreign_word, "ascii-only"),
        (probe_mod.foreign_word, "Übe\x07rraschung"),
        (probe_mod.foreign_word, "Ü" * 300),
        (probe_mod.cc_fragment, "abc"),
        (probe_mod.cc_fragment, "has space"),
    ):
        with pytest.raises(ValueError, match=r"refused: ") as refused:
            call(bad_value)
        assert bad_value not in str(refused.value)
    assert probe_mod.sender_part("gardener.joe") == "gardener.joe"
    assert probe_mod.foreign_word("Überraschung") == "Überraschung"
    assert probe_mod.cc_fragment("hidden@cc") == "hidden@cc"


def test_search_rounds_shape_icloud() -> None:
    stub = _icloud_search_stub()
    out = _search_run(stub)
    assert out.startswith(
        "--- result ---\ns1_capabilities: literal_plus=yes literal_minus=no utf8_accept=no enable=no\n"
    )
    for line in out.splitlines()[1:]:
        assert re.fullmatch(r"[a-z0-9_]+: .+", line), line
    for role in ("inbox", "sent", "archive"):
        assert re.search(rf"^s2_text: folder={role} form=word hits=\d+ ms=\d+$", out, re.M)
    assert re.search(r"^s2_text: folder=inbox form=three_keys hits=1 ms=\d+$", out, re.M)
    assert re.search(r"^s2_text: folder=inbox form=word hits=2 ms=\d+$", out, re.M)  # tomatoes: messages 1
    assert 4
    assert re.search(r"^s2_text: folder=inbox form=prefix prefix_hits=2 ms=\d+$", out, re.M)
    assert re.search(r"^s2_text: folder=inbox form=middle middle_hits=2 ms=\d+$", out, re.M)
    assert re.search(r"^s2_subject: folder=inbox form=word hits=1 ms=\d+$", out, re.M)
    assert re.search(r"^s2_subject: folder=inbox form=middle hits=1 ms=\d+$", out, re.M)
    assert re.search(r"^s2_from: folder=inbox form=word hits=1 ms=\d+$", out, re.M)
    assert re.search(r"^s2_from: folder=inbox form=middle hits=1 ms=\d+$", out, re.M)
    assert "s3_" not in out.replace("s3_gmail: skipped=not_gmail", "")
    assert "s3_gmail: skipped=not_gmail" in out
    assert "s8_to_cc: skipped=not_gmail" in out
    assert "Trash" not in str(stub.commands)
    assert "Junk" not in str(stub.commands)


def test_search_rounds_print_no_word_name_or_header_text() -> None:
    for stub in (_icloud_search_stub(), _gmail_search_stub()):
        out = _search_run(stub)
        lowered = out.lower()
        for secret in (*_SECRETS, "example.test", "soup", "sent messages", "all mail", "=?"):
            assert secret.lower() not in lowered, secret
    assert out.count("\n") > 20


def test_search_rounds_only_read_only_allowlisted_commands() -> None:
    for stub in (_icloud_search_stub(), _gmail_search_stub()):
        _search_run(stub)
        assert {command for command, _ in stub.commands} <= _SEARCH_COMMANDS
        assert all(b"X-GM-RAW" not in argument.upper().encode() for _, argument in stub.commands)
        fetches = [argument for command, argument in stub.commands if command == "FETCH"]
        assert fetches
        assert set(fetches) == {_CONTROL_ITEM}
        for _, criteria in _searches(stub):
            assert criteria in ("literal",) or criteria.startswith(f"NOT DRAFT SINCE {_SEARCH_SINCE}")


def test_s2_criteria_keys_and_fragments() -> None:
    stub = _icloud_search_stub()
    _search_run(stub)
    inbox = [criteria for folder, criteria in _searches(stub) if folder == "INBOX"]
    base = f"NOT DRAFT SINCE {_SEARCH_SINCE}"
    assert f'{base} TEXT "{_W1}"' in inbox
    assert f'{base} TEXT "{_W1}" TEXT "{_W2}" TEXT "{_W3}"' in inbox
    assert f'{base} TEXT "toma"' in inbox  # first four letters
    assert f'{base} TEXT "mato"' in inbox  # letters 3 to 6
    assert f'{base} SUBJECT "{_W1}"' in inbox
    assert f'{base} SUBJECT "mato"' in inbox
    assert f'{base} FROM "{_SENDER}"' in inbox
    assert f'{base} FROM "rden"' in inbox


def test_gmail_search_rounds_probe_operators_inside_text_and_subject() -> None:
    stub = _gmail_search_stub()
    out = _search_run(stub)
    assert "literal_minus=yes utf8_accept=yes enable=yes" in out
    base = f"NOT DRAFT SINCE {_SEARCH_SINCE}"
    for folder, role in (("INBOX", "inbox"), ("[Gmail]/All Mail", "all_mail")):
        criteria = [c for f, c in _searches(stub) if f == folder]
        for expected in (
            f'{base} TEXT "in:anywhere"',
            f'{base} TEXT "anywhere"',
            f'{base} TEXT "-{_W1}"',
            f'{base} TEXT "{_W1}"',
            f'{base} TEXT "OR"',
            f'{base} SUBJECT "{_W1} OR {_W2}"',
            f'{base} SUBJECT "-{_W1}"',
            f'{base} SUBJECT "{_W1}"',
            f'{base} SUBJECT "{_W2}"',
            base,
        ):
            assert expected in criteria, (folder, expected)
        for form in ("in_anywhere", "anywhere", "neg_w1", "w1", "or"):
            assert re.search(rf"^s3_text: folder={role} form={form} hits=\d+ ms=\d+$", out, re.M), (role, form)
        for form in ("or_w1_w2", "neg_w1", "w1", "w2"):
            assert re.search(rf"^s3_subject: folder={role} form={form} hits=\d+ ms=\d+$", out, re.M), (role, form)
        assert re.search(rf"^s3_window: folder={role} hits=\d+$", out, re.M)
        assert re.search(
            rf"^s3_hint: folder={role} neg_complement=(yes|no) or_union=(yes|no) in_anywhere_eq_window=(yes|no)$",
            out,
            re.M,
        )
    assert "s2_text: folder=all_mail " in out
    assert "folder=sent" not in out  # Gmail: All Mail and INBOX


def test_s6_counts_encoded_headers_without_printing_them() -> None:
    stub = _icloud_search_stub()
    out = _search_run(stub)
    assert re.search(r"^s6_raw_match: form=from_utf8 hits=0 ms=\d+$", out, re.M)
    assert re.search(r"^s6_raw_match: form=subject_encoded hits=0 ms=\d+$", out, re.M)
    assert re.search(
        r"^s6_control: window_hits=4 scanned=4 encoded_from=1 encoded_subject=1 "
        r"from_utf8_hits_in_scanned=0 subject_encoded_hits_in_scanned=0 ms=\d+$",
        out,
        re.M,
    )
    sent_to_fetch = [argument for command, argument in stub.commands if command == "FETCH"]
    assert sent_to_fetch == [_CONTROL_ITEM]


def test_s6_control_scans_at_most_500_newest_uids() -> None:
    stub = _icloud_search_stub()
    stub.mailboxes["INBOX"] = [SMsg(uid, "x", "y@z.test") for uid in range(1, 701)]
    out = _search_run(stub)
    assert re.search(r"^s6_control: window_hits=700 scanned=500 encoded_from=0 encoded_subject=0 ", out, re.M)


def test_s7_phrase_against_two_keys() -> None:
    stub = _icloud_search_stub()
    out = _search_run(stub)
    base = f"NOT DRAFT SINCE {_SEARCH_SINCE}"
    inbox = [criteria for folder, criteria in _searches(stub) if folder == "INBOX"]
    assert f'{base} TEXT "{_W1} {_W2}"' in inbox
    assert f'{base} TEXT "{_W1}" TEXT "{_W2}"' in inbox
    assert re.search(r"^s7_text: form=phrase hits=1 ms=\d+$", out, re.M)
    assert re.search(r"^s7_text: form=two_keys hits=2 ms=\d+$", out, re.M)


def test_s8_asks_whether_to_covers_cc_on_gmail_only() -> None:
    stub = _gmail_search_stub()
    out = _search_run(stub)
    assert f'NOT DRAFT SINCE {_SEARCH_SINCE} TO "{_CC}"' in [c for f, c in _searches(stub) if f == "[Gmail]/All Mail"]
    assert re.search(r"^s8_to_cc: folder=all_mail hits=1 ms=\d+$", out, re.M)
    assert "s8_to_cc: skipped=no_input" in _search_run(_gmail_search_stub(), _words(cc=None))


def test_s4_s5_use_the_literal_path_and_run_last() -> None:
    stub = _icloud_search_stub()
    out = _search_run(stub)
    assert re.search(r"^s4_literal: status=ok hits=1 ms=\d+$", out, re.M)
    assert re.search(r"^s5_upper: status=ok hits=1 ms=\d+$", out, re.M)
    assert "s5_upper: same_hits_as_s4=yes\n" in out
    args = stub.literal_args
    assert len(args) == 2
    assert args[0][:2] == [b"CHARSET", b"UTF-8"]
    assert args[0][3:] == [b"SUBJECT", _FOREIGN.encode()]
    assert args[1][3:] == [b"SUBJECT", _FOREIGN.upper().encode()]
    assert out.index("s6_control") < out.index("s4_literal") < out.index("s5_upper")
    assert out.index("s8_to_cc") < out.index("s4_literal")


def test_s4_without_a_foreign_word_is_skipped() -> None:
    stub = _icloud_search_stub()
    out = _search_run(stub, _words(foreign=None))
    assert "s4_literal: skipped=no_input" in out
    assert "s5_upper: skipped=no_input" in out
    assert stub.literal_args == []


@pytest.mark.parametrize(
    ("raised", "status"),
    [
        (imaplib.IMAP4.error("SEARCH command error: BAD [BADCHARSET] nope"), "bad"),
        (imaplib.IMAP4.error("SEARCH command error: NO [BADCHARSET] nope"), "no"),
    ],
)
def test_s4_refusal_reports_the_status_and_skips_s5(raised: Exception, status: str) -> None:
    stub = _icloud_search_stub()
    stub.literal_error = raised
    out = _search_run(stub)
    assert re.search(rf"^s4_literal: status={status}(?: code=\w+)? ms=\d+$", out, re.M)
    assert "s5_upper: skipped=after_refusal" in out
    assert len(stub.literal_args) == 1
    assert "nope" not in out
    assert "BADCHARSET" not in out


def test_s4_upper_case_that_changes_nothing_is_skipped() -> None:
    out = _search_run(_icloud_search_stub(), _words(foreign="ÄÖÜÄÖÜÄ"))
    assert "s5_upper: skipped=no_case_change" in out


def test_a_failing_search_is_reported_by_class_and_the_next_round_runs() -> None:
    stub = _icloud_search_stub()
    stub.search_error = (f'TEXT "{_W1}" TEXT', TimeoutError("secret text"))
    out = _search_run(stub)
    assert re.search(r"^s2_text: folder=inbox form=three_keys error=TimeoutError$", out, re.M)
    assert "secret text" not in out
    assert re.search(r"^s2_text: folder=inbox form=middle middle_hits=2 ms=\d+$", out, re.M)


def test_s4_matches_the_wire_with_a_synchronising_literal_on_a_literal_plus_server() -> None:
    from tests.support.imap_wire import WireRecorder, hub_client

    with WireRecorder() as recorder:
        raw = hub_client(recorder)
        raw.login("u", "p")
        raw.select_folder("INBOX", readonly=True)
        lines: list[str] = []
        probe_mod._literal_round(
            raw, "s4_literal", probe_mod.imap_search.criteria(b"NOT DRAFT"), _FOREIGN, lines.append
        )
        raw.logout()
    sent = [line for line in recorder.lines if b"SEARCH" in line]
    assert len(sent) == 1
    assert re.search(rb"\{\d+\}", sent[0])
    assert recorder.literals == [_FOREIGN.encode()]
    assert not re.search(rb"\{\d+\+\}", b" ".join(recorder.lines))
    assert not recorder.early_bytes
    assert lines
    assert lines[0].startswith("s4_literal: status=ok hits=0 ms=")


def test_the_probe_builds_the_hub_client_class() -> None:
    from mcp_hub.providers.imap import HubImapClient

    assert probe_mod.HubImapClient is HubImapClient


def _main_search(
    monkeypatch: pytest.MonkeyPatch, stub: SearchStub, answers: list[str], *, host: str = "imap.mail.me.com"
) -> tuple[int, list[str]]:
    built: list[str] = []
    prompts: list[str] = []
    replies = iter(answers)

    def factory(*args: Any, **kwargs: Any) -> SearchStub:
        built.append("hub")
        return stub

    def fake_getpass(prompt: str = "") -> str:
        prompts.append(prompt)
        return next(replies)

    monkeypatch.setattr(probe_mod, "HubImapClient", factory)
    monkeypatch.setattr(probe_mod, "IMAPClient", lambda *a, **k: pytest.fail("plain IMAPClient used"))
    monkeypatch.setattr(probe_mod.getpass, "getpass", fake_getpass)
    return probe_mod.main(["--host", host, "--search-rounds"]), prompts


def test_search_rounds_main_prompts_hides_the_words_and_uses_the_hub_client(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    stub = _icloud_search_stub()
    answers = ["user", "pw", _W1, _W2, _W3, _SENDER, _FOREIGN, ""]
    code, prompts = _main_search(monkeypatch, stub, answers)
    assert code == 0
    assert len(prompts) == 8
    commands = [command for command, _ in stub.commands]
    assert commands[:3] == ["LOGIN", "CAPABILITY", "LIST"]
    assert commands[-1] == "LOGOUT"
    captured = capsys.readouterr()
    assert captured.out.startswith("--- result ---\ns1_capabilities: ")
    assert captured.err == ""
    for secret in (*_SECRETS, "pw"):
        assert secret.lower() not in (captured.out + captured.err + " ".join(prompts)).lower()
    assert "s8_to_cc: skipped=not_gmail" in captured.out


def test_search_rounds_main_refuses_a_short_word_before_connecting(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    stub = _icloud_search_stub()
    code, _ = _main_search(monkeypatch, stub, ["user", "pw", "tiny", "tiny", "tiny"])
    assert code == 2
    assert stub.commands == []
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.count("word refused: use at least 7 ASCII letters") == 3
    assert "tiny" not in captured.err


def test_search_rounds_login_failure_prints_class_name_only(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class Refusing(SearchStub):
        def login(self, username: str, password: str) -> bytes:
            raise LoginError("[AUTHENTICATIONFAILED] user@example.test")

    stub = Refusing({}, [])
    code, _ = _main_search(monkeypatch, stub, ["user", "pw", _W1, _W2, _W3, "", "", ""])
    assert code == 1
    captured = capsys.readouterr()
    assert captured.err == "error: LoginError\n"
    assert captured.out == ""


def test_search_rounds_and_status_rounds_exclude_each_other(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        probe_mod.main(["--host", "imap.gmail.com", "--search-rounds", "--status-rounds"])
    assert exit_info.value.code == 2


def test_help_names_the_search_rounds(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        probe_mod.main(["--help"])
    out = capsys.readouterr().out
    assert "uv run python scripts/imap_probe.py --host imap.mail.me.com --search-rounds" in out
    assert "uv run python scripts/imap_probe.py --host imap.gmail.com --search-rounds" in out


def test_icloud_archive_without_a_special_use_flag_is_found_by_name() -> None:
    stub = _icloud_search_stub()
    stub.folders = [
        (tuple(f for f in flags if f != b"\\Archive" and f != b"\\Sent"), d, n) for flags, d, n in stub.folders
    ]
    out = _search_run(stub)
    for role in ("sent", "archive"):
        assert re.search(rf"^s2_text: folder={role} form=word hits=\d+ ms=\d+$", out, re.M), role
    assert "skipped=not_found" not in out


def test_a_missing_role_prints_a_line() -> None:
    stub = _icloud_search_stub()
    stub.folders = [f for f in stub.folders if f[2] != "Archive"]
    out = _search_run(stub)
    assert "s2_folder: role=archive skipped=not_found\n" in out
    assert "s2_folder: role=sent" not in out
    assert "folder=archive" not in out


def test_s6_control_counts_only_utf8_words_and_the_hits_in_the_scanned_rows() -> None:
    stub = _icloud_search_stub()
    rows = [SMsg(uid, "x", "y@z.test", raw_from="=?iso-8859-1?Q?caf=E9?= <a@b.test>") for uid in range(1, 601)]
    rows[0] = SMsg(1, "x", "utf-8 old", raw_from="=?UTF-8?B?Zm9v?= <a@b.test>")  # outside the newest 500
    rows[599] = SMsg(600, "x", "utf-8 new", raw_from="=?utf-8?B?Zm9v?= <a@b.test>")
    stub.mailboxes["INBOX"] = rows
    out = _search_run(stub)
    assert re.search(r"^s6_raw_match: form=from_utf8 hits=2 ms=\d+$", out, re.M)
    assert re.search(
        r"^s6_control: window_hits=600 scanned=500 encoded_from=1 encoded_subject=0 "
        r"from_utf8_hits_in_scanned=1 subject_encoded_hits_in_scanned=0 ms=\d+$",
        out,
        re.M,
    )
    assert "=?" not in out


def test_s7_hint_says_when_the_two_keys_never_meet() -> None:
    stub = _icloud_search_stub()
    stub.mailboxes["INBOX"] = [SMsg(1, "a", "b@c.test", text=_W1), SMsg(2, "a", "b@c.test", text=_W2)]
    assert "s7_hint: decisive=no\n" in _search_run(stub)
    assert "s7_hint: decisive=yes\n" in _search_run(_icloud_search_stub())


def test_s4_no_with_badcharset_is_told_apart_without_printing_the_text() -> None:
    stub = _icloud_search_stub()
    stub.literal_error = imaplib.IMAP4.error("SEARCH command error: NO [BADCHARSET (US-ASCII)] nope")
    out = _search_run(stub)
    assert re.search(r"^s4_literal: status=no code=badcharset ms=\d+$", out, re.M)
    stub = _icloud_search_stub()
    stub.literal_error = imaplib.IMAP4.error("SEARCH command error: NO nope")
    assert re.search(r"^s4_literal: status=no code=none ms=\d+$", _search_run(stub), re.M)


def test_prompts_tell_the_owner_where_the_words_must_occur(monkeypatch: pytest.MonkeyPatch) -> None:
    prompts: list[str] = []
    answers = iter(["tomatoes", "potatoes", "cucumber", "", "", ""])
    monkeypatch.setattr(probe_mod.getpass, "getpass", lambda prompt="": (prompts.append(prompt), next(answers))[1])
    probe_mod._read_words()
    assert "inbox mail" in prompts[0]
    assert "inbox mail" in prompts[1]
    assert "not next to each other" in prompts[1]
    assert "subject of an inbox mail" in prompts[4]
