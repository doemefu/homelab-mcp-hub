"""WP13a T1 (O35): the read-only IMAP probe script, driven by a recording stub client (no network)."""

import importlib.util
import io
import re
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


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
    proxy.fetch([1], ["INTERNALDATE"])
    proxy.fetch([1], ["BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)]<0.2048>"])


def test_probe_uses_only_read_only_commands() -> None:
    _, stub = _run(StubClient(_folders(), capabilities=(b"X-GM-EXT-1",)))
    searches = [text for cmd, text in stub.commands if cmd == "SEARCH"]
    assert searches
    assert all(text.startswith("NOT DRAFT") or text.startswith("X-GM-LABELS") for text in searches)
    assert not any("X-GM-RAW" in text.upper() for text in searches)
    fetches = [text for cmd, text in stub.commands if cmd == "FETCH"]
    allowed = {"INTERNALDATE", "BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)]<0.2048>"}
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
    assert first.count('HEADER Message-ID "<id') == 20
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
    assert len(examines) == 30 * 5
    assert "header_message_id: folders=30" in out


def test_probe_reports_gmail_label_search_when_x_gm_ext_1() -> None:
    out, stub = _run(StubClient(_folders(), capabilities=(b"X-GM-EXT-1", b"SPECIAL-USE")))
    labels = [text for cmd, text in stub.commands if cmd == "SEARCH" and "X-GM-LABELS" in text]
    assert labels == ['X-GM-LABELS "\\\\Sent"']
    assert re.search(r"^gmail_label_search: hits=3 ms=\d+$", out, re.MULTILINE)
    out2, stub2 = _run(StubClient(_folders()))
    assert "gmail_label_search" not in out2
    assert not any("X-GM-LABELS" in text for _, text in stub2.commands)


def test_search_criteria_have_expected_shape() -> None:
    _, stub = _run(StubClient(_folders()), since_days=14, text_word="the")
    searches = [text for cmd, text in stub.commands if cmd == "SEARCH"]
    assert re.fullmatch(r"NOT DRAFT SINCE \d{2}-[A-Z][a-z]{2}-\d{4}", searches[0])
    assert any(re.fullmatch(r'NOT DRAFT HEADER Message-ID "<id\d+@example\.test>"', s) for s in searches)
    assert any(re.fullmatch(r'NOT DRAFT HEADER References "<id\d+@example\.test>"', s) for s in searches)
    assert 'NOT DRAFT TEXT "the"' in searches


def test_quoted_rejects_control_characters_and_escapes() -> None:
    assert probe_mod.quoted('a"b\\c') == '"a\\"b\\\\c"'
    with pytest.raises(ValueError, match="control character"):
        probe_mod.quoted("a\r\nb")


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
