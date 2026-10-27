"""In-memory IMAP fake that only speaks the hub's read-only command allowlist (spec 080 S5)."""

import email
import email.policy
import imaplib
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

from imapclient import imap_utf7
from imapclient.exceptions import IMAPClientError

from tests.support.clock import FakeClock

COMMAND_ALLOWLIST = frozenset({"CAPABILITY", "LOGIN", "LIST", "EXAMINE", "UID SEARCH", "UID FETCH", "NOOP", "LOGOUT"})
PLAIN = (b"text", b"plain", (b"charset", b"utf-8"), None, None, b"7bit", 64, 2, None, None, None, None)
MAX_BODY_BYTES = 262_144
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_PLAIN_ITEMS = {"FLAGS", "INTERNALDATE", "BODYSTRUCTURE", "X-GM-LABELS", "X-GM-THRID"}
_PEEK = re.compile(r"BODY\.PEEK\[([^\]]*)\]<0\.(\d+)>")
_TOKEN = re.compile(r'\s*(\(|\)|"(?:[^"\\]|\\.)*"|[^\s()"]+)')
_MSGID_FIELDS = frozenset({"message-id", "in-reply-to", "references"})
_MSGID_TOKEN = re.compile(r"<[^<>]*>")
_HEADER_FIELDS = re.compile(r"HEADER\.FIELDS\s*\(([^)]*)\)", re.IGNORECASE)


@dataclass
class FakeMessage:
    uid: int
    internaldate: datetime | None
    flags: tuple[bytes, ...] = ()
    headers: bytes | None = b""
    text: bytes = b"Hello"
    bodystructure: object = PLAIN
    labels: tuple[bytes, ...] = ()
    thrid: int | None = None


@dataclass
class FakeFolder:
    raw_name: str | int
    attributes: tuple[bytes, ...] = ()
    uidvalidity: int | None = 7
    messages: dict[int, FakeMessage] = field(default_factory=dict)
    delimiter: bytes | None = b"/"


def no(command: str) -> IMAPClientError:
    return IMAPClientError(f"{command} command error: NO [x]")


def bad(command: str) -> IMAPClientError:
    return IMAPClientError(f"{command} command error: BAD [x]")


def abort() -> imaplib.IMAP4.abort:
    return imaplib.IMAP4.abort("connection lost")


class _FailMap(dict[str, Exception]):
    """A typo in a command name must not silently disable the injected failure."""

    def __setitem__(self, key: str, value: Exception) -> None:
        if key not in COMMAND_ALLOWLIST:
            raise AssertionError(f"command outside the allowlist: {key}")
        super().__setitem__(key, value)


class _FakeSocket:
    def __init__(self, server: "FakeImapServer") -> None:
        self._server = server

    def settimeout(self, value: float) -> None:
        self._server.socket_timeouts.append(value)


class FakeImapServer:
    def __init__(
        self,
        folders: Sequence[FakeFolder],
        *,
        capabilities: tuple[bytes, ...] = (b"IMAP4REV1",),
        clock: FakeClock | None = None,
        step_seconds: float = 0.0,
    ) -> None:
        self.folders = list(folders)
        self.capabilities = capabilities
        self.clock, self.step_seconds = clock, step_seconds
        self.commands: list[tuple[str, object]] = []
        self.fail: dict[str, Exception] = _FailMap()
        self.honour_not_draft = True
        # iCloud-like (O35): HEADER on a msg-id field matches a whole bracketed `<id>` token, never a substring.
        self.whole_msgid_tokens = False
        # Search value keys: a `CHARSET` search answers NO (server without UTF-8 search); `word_match` matches whole
        # words only, like Gmail and iCloud do for TEXT/FROM/TO/SUBJECT.
        self.badcharset = False
        self.word_match = False
        self.socket_timeouts: list[float] = []
        self.no, self.bad, self.abort = no, bad, abort

    def client(self) -> "FakeImapClient":
        return FakeImapClient(self)

    def run(self, command: str, argument: object = None) -> None:
        if command not in COMMAND_ALLOWLIST:
            raise AssertionError(f"command outside the allowlist: {command}")
        self.commands.append((command, argument))
        if self.clock is not None:
            self.clock.advance(self.step_seconds)
        failure = self.fail.pop(command, None)
        if failure is not None:
            raise failure


class FakeImapClient:
    def __init__(self, server: FakeImapServer) -> None:
        self._server = server
        self._folder: FakeFolder | None = None
        self.normalise_times = True
        self.folder_encode = True
        self.use_uid = True

    def __getattr__(self, name: str) -> object:
        if name.startswith("_"):
            raise AttributeError(name)
        raise AssertionError(f"command outside the allowlist: {name}")

    def capabilities(self) -> tuple[bytes, ...]:
        self._server.run("CAPABILITY")
        return self._server.capabilities

    def login(self, username: str, password: str) -> bytes:
        self._server.run("LOGIN")
        return b"Logged in"

    def list_folders(
        self, directory: str = "", pattern: str = "*"
    ) -> list[tuple[tuple[bytes, ...], bytes | None, bytes | str | int]]:
        """Like IMAPClient 4.1.0: with `folder_encode = False` the raw names come back as bytes (int names stay)."""
        self._server.run("LIST", (directory, pattern))
        return [(f.attributes, f.delimiter, self._listed_name(f.raw_name)) for f in self._server.folders]

    def _listed_name(self, name: str | int) -> bytes | str | int:
        if isinstance(name, int) or self.folder_encode:
            return name
        return name.encode("utf-8")  # the bytes on the wire

    def select_folder(self, folder: str, readonly: bool = False) -> dict[bytes, object]:
        if not readonly:
            raise AssertionError("SELECT refused")
        if self.folder_encode:  # like IMAPClient: the name is re-encoded as modified UTF-7 before it is sent
            folder = imap_utf7.encode(folder).decode("ascii")
        self._server.run("EXAMINE", folder)
        found = next((f for f in self._server.folders if f.raw_name == folder or str(f.raw_name) == folder), None)
        if found is None:
            raise no("EXAMINE")
        self._folder = found
        info: dict[bytes, object] = {b"EXISTS": len(found.messages), b"READ-ONLY": [b""]}
        if found.uidvalidity is not None:
            info[b"UIDVALIDITY"] = found.uidvalidity
        return info

    def _selected(self, command: str) -> FakeFolder:
        if self._folder is None:
            raise bad(command)
        return self._folder

    def search(self, criteria: bytes) -> list[int]:
        self._server.run("UID SEARCH", criteria)
        return self._evaluate([m.group(1) for m in _TOKEN.finditer(criteria.decode("utf-8"))])

    def _raw_command_untagged(self, command: bytes, args: Sequence[bytes], uid: bool = False) -> list[bytes]:
        """The synchronising-literal search of the guard: `UID SEARCH CHARSET UTF-8 <prefix> <key> <value>…`. Only
        that command exists; a non-ASCII value arrives as raw UTF-8 bytes (the literal), an ASCII one quoted."""
        if command != b"SEARCH" or uid is not True:
            raise AssertionError(f"raw command outside the allowlist: {command!r} uid={uid}")
        self._server.run("UID SEARCH", list(args))
        tokens: list[str] = []
        for arg in args:
            text = arg.decode("utf-8")
            if text.upper() in ("CHARSET", "UTF-8") or arg.startswith(b'"') or b" " in arg or arg.isascii():
                tokens.extend(m.group(1) for m in _TOKEN.finditer(text))
            else:  # a literal: one value token whatever it contains
                tokens.append('"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"')
        return [b" ".join(str(uid_).encode() for uid_ in self._evaluate(tokens))]

    def _evaluate(self, tokens: list[str]) -> list[int]:
        folder = self._selected("UID SEARCH")
        server = self._server
        if tokens[:1] and tokens[0].upper() == "CHARSET" and server.badcharset:
            raise no("UID SEARCH")
        pos, node = _parse_list(tokens, 0, server.whole_msgid_tokens, b"X-GM-EXT-1" in server.capabilities, server)
        if pos != len(tokens):
            raise bad("UID SEARCH")
        return [uid for uid, msg in sorted(folder.messages.items()) if node(msg, server.honour_not_draft)]

    def fetch(self, messages: str | Sequence[int], items: str | Sequence[str]) -> dict[int, dict[bytes, object]]:
        self._server.run("UID FETCH", (messages, items))
        folder = self._selected("UID FETCH")
        wanted = [items] if isinstance(items, str) else list(items)
        for item in wanted:
            peek = _PEEK.fullmatch(item)
            if item not in _PLAIN_ITEMS and not (peek and int(peek.group(2)) <= MAX_BODY_BYTES):
                raise AssertionError(f"fetch item outside the allowlist: {item}")
        uids = _uid_set(messages, max(folder.messages, default=0))
        out: dict[int, dict[bytes, object]] = {}
        for seq, (uid, msg) in enumerate(sorted(folder.messages.items()), start=1):
            if uid not in uids:
                continue
            row: dict[bytes, object] = {b"SEQ": seq}
            for item in wanted:
                _fill(row, item, msg)
            out[uid] = row
        return out

    def noop(self) -> bytes:
        self._server.run("NOOP")
        return b"Done"

    def logout(self) -> bytes:
        self._server.run("LOGOUT")
        return b"Logging out"

    def socket(self) -> _FakeSocket:
        return _FakeSocket(self._server)


def _uid_set(messages: str | Sequence[int], highest: int) -> set[int]:
    if not isinstance(messages, str):
        return set(messages)
    out: set[int] = set()
    for chunk in messages.split(","):
        low, _, high = chunk.partition(":")
        start = highest if low == "*" else int(low)
        stop = start if not high else (highest if high == "*" else int(high))
        out.update(range(min(start, stop), max(start, stop) + 1))
    return out


def _fill(row: dict[bytes, object], item: str, msg: FakeMessage) -> None:
    if item == "FLAGS":
        row[b"FLAGS"] = msg.flags
    elif item == "INTERNALDATE":
        if msg.internaldate is not None:
            row[b"INTERNALDATE"] = msg.internaldate
    elif item == "BODYSTRUCTURE":
        row[b"BODYSTRUCTURE"] = msg.bodystructure
    elif item == "X-GM-LABELS":
        row[b"X-GM-LABELS"] = list(msg.labels)
    elif item == "X-GM-THRID":
        if msg.thrid is not None:
            row[b"X-GM-THRID"] = msg.thrid
    else:
        peek = _PEEK.fullmatch(item)
        assert peek is not None
        section, limit = peek.group(1), int(peek.group(2))
        data = _header_subset(msg.headers, section) if section.upper().startswith("HEADER") else msg.text
        row[f"BODY[{section}]<0>".encode()] = None if data is None else data[:limit]


def _header_subset(headers: bytes | None, section: str) -> bytes | None:
    """HEADER.FIELDS (…): only the requested fields with their folded lines, in message order, plus the closing CRLF."""
    if section.upper() == "HEADER":
        return headers
    fields = _HEADER_FIELDS.fullmatch(section)
    if fields is None:  # HEADER.FIELDS.NOT and anything else: fail closed
        raise bad("UID FETCH")
    if headers is None:
        return None
    wanted = {name.casefold() for name in fields.group(1).split()}
    kept: list[bytes] = []
    keep = False
    for line in headers.split(b"\r\n"):
        if not line:
            break
        if line[:1] not in (b" ", b"\t"):
            keep = line.partition(b":")[0].decode("latin-1").strip().casefold() in wanted
        if keep:
            kept.append(line + b"\r\n")
    return b"".join(kept) + b"\r\n"


# --- mini evaluator of the hub's UID SEARCH grammar -------------------------------------------------------------


def _parse_list(
    tokens: list[str], pos: int, whole: bool, gmail: bool, server: "FakeImapServer"
) -> tuple[int, Callable[[FakeMessage, bool], bool]]:
    parts: list[Callable[[FakeMessage, bool], bool]] = []
    while pos < len(tokens) and tokens[pos] != ")":
        pos, node = _parse_key(tokens, pos, whole, gmail, server)
        parts.append(node)
    return pos, lambda msg, honour: all(p(msg, honour) for p in parts)


def _parse_key(
    tokens: list[str], pos: int, whole: bool, gmail: bool, server: "FakeImapServer"
) -> tuple[int, Callable[[FakeMessage, bool], bool]]:
    word = tokens[pos].upper()
    pos += 1
    if word == "(":
        pos, node = _parse_list(tokens, pos, whole, gmail, server)
        if pos >= len(tokens):
            raise bad("UID SEARCH")
        return pos + 1, node
    if word == "NOT":
        if pos < len(tokens) and tokens[pos].upper() == "DRAFT":
            return pos + 1, lambda msg, honour: not honour or b"\\Draft" not in msg.flags
        pos, inner = _parse_key(tokens, pos, whole, gmail, server)
        return pos, lambda msg, honour: not inner(msg, honour)
    if word == "OR":
        pos, left = _parse_key(tokens, pos, whole, gmail, server)
        pos, right = _parse_key(tokens, pos, whole, gmail, server)
        return pos, lambda msg, honour: left(msg, honour) or right(msg, honour)
    if word == "UNSEEN":
        return pos, lambda msg, honour: b"\\Seen" not in msg.flags
    if word in ("SINCE", "BEFORE"):
        limit = _parse_date(tokens[pos])
        since = word == "SINCE"

        def by_date(msg: FakeMessage, honour: bool) -> bool:
            if msg.internaldate is None:
                return False
            day = msg.internaldate.astimezone(UTC).date()
            return day >= limit if since else day < limit

        return pos + 1, by_date
    if word == "HEADER":
        name, needle = tokens[pos], _unquote(tokens[pos + 1]).casefold()
        tokens_only = whole and name.casefold() in _MSGID_FIELDS

        def by_header(msg: FakeMessage, honour: bool) -> bool:
            parsed = email.message_from_bytes(msg.headers or b"", policy=email.policy.compat32)
            value = str(parsed.get(name, "")).casefold()
            return needle in _MSGID_TOKEN.findall(value) if tokens_only else needle in value

        return pos + 2, by_header
    if word == "CHARSET":
        if pos >= len(tokens) or tokens[pos].upper() != "UTF-8":
            raise bad("UID SEARCH")
        return pos + 1, lambda msg, honour: True
    if word in ("TEXT", "FROM", "TO", "SUBJECT"):
        needle = _unquote(tokens[pos]).casefold()
        if not needle:
            raise bad("UID SEARCH")  # an empty value would match everything; the hub never sends one
        return pos + 1, lambda msg, honour: _contains(_haystack(msg, word), needle, server.word_match)
    if word == "X-GM-LABELS":
        label = _unquote(tokens[pos]).encode().lower()
        return pos + 1, lambda msg, honour: label in {x.lower() for x in msg.labels}
    if word == "X-GM-THRID" and gmail:
        if pos >= len(tokens) or not re.fullmatch(r"\d{1,20}", tokens[pos]) or int(tokens[pos]) >= 2**64:
            raise bad("UID SEARCH")
        wanted = int(tokens[pos])
        return pos + 1, lambda msg, honour: msg.thrid is not None and msg.thrid == wanted
    raise bad("UID SEARCH")


def _decoded_headers(msg: FakeMessage) -> email.message.Message:
    return email.message_from_string((msg.headers or b"").decode("utf-8", "replace"), policy=email.policy.default)


def _haystack(msg: FakeMessage, key: str) -> str:
    """Decoded header values (FROM, TO, SUBJECT) or all headers plus the text (TEXT)."""
    parsed = _decoded_headers(msg)
    if key == "TEXT":
        return (
            "\n".join(f"{name}: {value}" for name, value in parsed.items()) + "\n" + msg.text.decode("utf-8", "replace")
        )
    return str(parsed.get(key.title(), ""))


def _contains(haystack: str, needle: str, whole_word: bool) -> bool:
    text = haystack.casefold()
    if not whole_word:
        return needle in text
    return re.search(r"(?<!\w)" + re.escape(needle) + r"(?!\w)", text) is not None


def _unquote(token: str) -> str:
    return re.sub(r"\\(.)", r"\1", token[1:-1]) if token.startswith('"') else token


def _parse_date(token: str) -> date:
    day, month, year = _unquote(token).split("-")
    return date(int(year), _MONTHS.index(month.title()) + 1, int(day))
