"""WP13a T1 (O35): the read-only IMAP probe script, driven by a recording stub client (no network)."""

import importlib.util
import io
import re
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from mcp_hub.ids import MESSAGE_ID_PATTERN
from mcp_hub.providers import imap_search
from mcp_hub.providers.folders import EXCLUDED_LAST_NAMES
from mcp_hub.providers.imap import FETCH_CHUNK_UIDS, LOCATE_BATCH_VALUES


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
    assert "msgid_variant: name=msgid_bracketed: error=TimeoutError" in out
    assert re.search(r"^msgid_variant: name=msgid_bare hits=\d+ ", out, re.MULTILINE)
    assert re.search(r"^msgid_variant: name=msgid_no_not_draft hits=\d+ ", out, re.MULTILINE)
    assert re.search(r"^text: folders=", out, re.MULTILINE)
