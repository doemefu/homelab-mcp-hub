"""Read-only IMAP folder and search probe (WP13a, O35). Run by the owner on their own machine, never in the image.

Measures what the mail history tools depend on: which special-use mailboxes a server announces, how many other
mailboxes there are, and how long UID SEARCH takes per folder for the criteria the hub plans to use. The hub is not
imported and the cluster Secret is not read: the username and the password are typed at a prompt, nothing is written
to disk, and the output carries counts, roles and timings only.

    uv run python scripts/imap_probe.py --host imap.mail.me.com
    uv run python scripts/imap_probe.py --host imap.gmail.com

Paste only the part after the `--- result ---` line. With --show-names the output contains mailbox names, a banner
says so, and the marker line is left out.

The connection goes through ReadOnlyProxy, a runtime allowlist: only CAPABILITY, LOGIN, LIST, EXAMINE (never SELECT),
UID SEARCH and UID FETCH with BODY.PEEK reach the server; every other attribute raises PermissionError.
"""

import argparse
import contextlib
import getpass
import re
import ssl
import sys
import time
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Final, TextIO

from imapclient import IMAPClient

ALLOWED_HOSTS: Final = frozenset({"imap.mail.me.com", "imap.gmail.com"})
PORT: Final = 993

_FETCH_INTERNALDATE: Final = "INTERNALDATE"
_FETCH_MESSAGE_ID: Final = "BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)]<0.2048>"
_ALLOWED_FETCH_ITEMS: Final = frozenset({_FETCH_INTERNALDATE, _FETCH_MESSAGE_ID})
_ALLOWED_CALLS: Final = frozenset({"capabilities", "login", "list_folders", "logout"})

_ROLE_FLAGS: Final = {
    b"\\sent": "sent",
    b"\\archive": "archive",
    b"\\trash": "trash",
    b"\\drafts": "drafts",
    b"\\junk": "junk",
    b"\\all": "all",
}
_ROLES: Final = ("sent", "archive", "trash", "drafts", "junk", "all")
_SKIPPED_ROLES: Final = frozenset({"junk", "drafts"})
_SKIPPED_NAMES: Final = frozenset({"junk", "junk e-mail", "junk email", "spam", "drafts", "draft"})
_FALLBACK_NAMES: Final = ("Sent Messages", "Sent", "Archive", "Deleted Messages", "Trash")
_NOT_SELECTABLE: Final = frozenset({b"\\noselect", b"\\nonexistent"})
_MONTHS: Final = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_MESSAGE_ID: Final = re.compile(r"<[!-;=?-~]{1,200}>")
_OR_VALUES: Final = 20
_ROUNDS: Final = ("header_message_id", "header_references", "text", "or20")

type Folder = tuple[tuple[bytes, ...], bytes, str]


class ReadOnlyProxy:
    """Forwards only the read-only calls the probe needs; every other attribute raises PermissionError."""

    def __init__(self, client: Any) -> None:
        self._client = client

    def __getattr__(self, name: str) -> Any:
        if name in _ALLOWED_CALLS:
            return getattr(self._client, name)
        raise PermissionError(name)

    def select_folder(self, folder: str) -> Any:
        return self._client.select_folder(folder, readonly=True)

    def search(self, criteria: bytes) -> list[int]:
        if not isinstance(criteria, bytes) or b"X-GM-RAW" in criteria.upper():
            raise PermissionError("search")
        return list(self._client.search(criteria))

    def fetch(self, messages: Sequence[int], items: Sequence[str]) -> dict[int, dict[bytes, Any]]:
        if not items or any(item not in _ALLOWED_FETCH_ITEMS for item in items):
            raise PermissionError("fetch")
        return dict(self._client.fetch(list(messages), list(items)))


def quoted(value: str) -> str:
    """Hub-style IMAP quoted string: backslash and double quote escaped, control characters refused."""
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        raise ValueError("control character in search value")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _imap_date(moment: datetime) -> str:
    return f"{moment.day:02d}-{_MONTHS[moment.month - 1]}-{moment.year}"


def _role(flags: Sequence[bytes], name: str) -> str | None:
    for flag in flags:
        role = _ROLE_FLAGS.get(flag.lower())
        if role:
            return role
    return "inbox" if name.upper() == "INBOX" else None


def _last_component(name: str, delimiter: bytes | None) -> str:
    sep = delimiter.decode("ascii", "ignore") if delimiter else ""
    return name.rsplit(sep, 1)[-1] if sep else name


def _or_tree(keys: Sequence[str]) -> str:
    """Balanced `OR (a) (b)` tree; a single key stays as it is."""
    if len(keys) == 1:
        return keys[0]
    half = len(keys) // 2
    return f"OR ({_or_tree(keys[:half])}) ({_or_tree(keys[half:])})"


def _message_id(fetched: dict[bytes, Any]) -> str | None:
    for key, value in fetched.items():
        if not key.upper().startswith(b"BODY[") or not isinstance(value, bytes):
            continue
        header = value.decode("ascii", "ignore").replace("\r\n ", " ").replace("\r\n\t", " ")
        for line in header.splitlines():
            label, _, rest = line.partition(":")
            if label.strip().lower() == "message-id" and _MESSAGE_ID.fullmatch(rest.strip()):
                return rest.strip()
    return None


def _ms(started: float) -> int:
    return round((time.perf_counter() - started) * 1000)


def _recent_message_ids(client: ReadOnlyProxy, uids: list[int]) -> list[str]:
    ids: list[str] = []
    fetched_by_uid = client.fetch(uids[-_OR_VALUES:], [_FETCH_MESSAGE_ID])
    for uid in sorted(fetched_by_uid, reverse=True):
        found = _message_id(fetched_by_uid[uid])
        if found is not None and found not in ids:
            ids.append(found)
    return ids


def probe(
    client: ReadOnlyProxy,
    *,
    since_days: int = 14,
    max_folders: int = 30,
    text_word: str = "the",
    show_names: bool = False,
    out: TextIO,
) -> None:
    def emit(line: str) -> None:
        print(line, file=out)

    if show_names:
        emit("!!! --show-names: this output contains mailbox names. DO NOT PASTE IT anywhere. !!!")
    else:
        emit("--- result ---")

    caps = {c.upper() for c in client.capabilities()}

    def flag(name: bytes) -> str:
        return "yes" if name in caps else "no"

    emit(
        f"capabilities: special_use={flag(b'SPECIAL-USE')} x_gm_ext_1={flag(b'X-GM-EXT-1')} "
        f"literal_plus={flag(b'LITERAL+')} move={flag(b'MOVE')}"
    )

    folders: list[Folder] = list(client.list_folders())
    selectable = [f for f in folders if not {x.lower() for x in f[0]} & _NOT_SELECTABLE]
    with_role = [f for f in selectable if any(x.lower() in _ROLE_FLAGS for x in f[0])]
    emit(
        f"mailboxes: total={len(folders)} selectable={len(selectable)} with_special_use={len(with_role)} "
        f"other={len(selectable) - len(with_role)}"
    )
    emit("roles: " + " ".join(f"{role}={sum(1 for f in selectable if _role(f[0], f[2]) == role)}" for role in _ROLES))
    names = {f[2] for f in folders}
    emit("fallback_names_present: " + " ".join(f"{n}={'yes' if n in names else 'no'}" for n in _FALLBACK_NAMES))

    targets: list[tuple[str, str]] = []  # (name, role)
    for flags, delimiter, name in selectable:
        role = _role(flags, name) or "other"
        if role in _SKIPPED_ROLES or _last_component(name, delimiter).lower() in _SKIPPED_NAMES:
            continue
        targets.append((name, role))
    targets.sort(key=lambda t: t[1] != "inbox")  # INBOX first; the sort is stable
    targets = targets[:max_folders]

    since = _imap_date(datetime.now(UTC) - timedelta(days=since_days))
    inbox_ids: list[str] = []
    for index, (name, role) in enumerate(targets):
        started = time.perf_counter()
        client.select_folder(name)
        uids = client.search(f"NOT DRAFT SINCE {since}".encode("ascii"))
        elapsed = _ms(started)
        label = f" name={name!r}" if show_names else ""
        emit(f"examine_search_since: folder#{index} role={role} hits={len(uids)} ms={elapsed}{label}")
        if role == "inbox" and uids and not inbox_ids:
            inbox_ids = _recent_message_ids(client, uids)

    if not inbox_ids:
        emit("header rounds skipped: no readable Message-ID in the newest inbox messages")
    else:
        first = inbox_ids[0]
        criteria = {
            "header_message_id": f"NOT DRAFT HEADER Message-ID {quoted(first)}",
            "header_references": f"NOT DRAFT HEADER References {quoted(first)}",
            "text": f"NOT DRAFT TEXT {quoted(text_word)}",
            "or20": "NOT DRAFT " + _or_tree([f"HEADER Message-ID {quoted(i)}" for i in inbox_ids[:_OR_VALUES]]),
        }
        for round_name in _ROUNDS:
            data = criteria[round_name].encode("ascii")
            round_started = time.perf_counter()
            slowest = 0
            found_in = "none"
            for name, role in targets:
                started = time.perf_counter()
                client.select_folder(name)
                hits = client.search(data)
                slowest = max(slowest, _ms(started))
                if hits and found_in == "none":
                    found_in = role
            emit(
                f"{round_name}: folders={len(targets)} total_ms={_ms(round_started)} max_ms={slowest} "
                f"found_in={found_in}"
            )

    if b"X-GM-EXT-1" in caps:
        all_mail = next((n for n, role in targets if role == "all"), None)
        if all_mail is None:
            all_mail = next((f[2] for f in selectable if _role(f[0], f[2]) == "all"), "INBOX")
        started = time.perf_counter()
        client.select_folder(all_mail)
        hits = client.search(b'X-GM-LABELS "\\\\Sent"')
        emit(f"gmail_label_search: hits={len(hits)} ms={_ms(started)}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only IMAP folder and search probe (O35).")
    parser.add_argument("--host", required=True)
    parser.add_argument("--since-days", type=int, default=14)
    parser.add_argument("--max-folders", type=int, default=30)
    parser.add_argument("--text-word", default="the")
    parser.add_argument("--show-names", action="store_true")
    args = parser.parse_args(argv)
    if args.host not in ALLOWED_HOSTS:
        print("host not allowed; use one of: " + ", ".join(sorted(ALLOWED_HOSTS)), file=sys.stderr)
        return 2
    try:
        username = getpass.getpass("Username: ")
        password = getpass.getpass("Password (app-specific): ")
        raw = IMAPClient(args.host, port=PORT, ssl=True, ssl_context=ssl.create_default_context())
        client = ReadOnlyProxy(raw)
        try:
            client.login(username, password)
            probe(
                client,
                since_days=args.since_days,
                max_folders=args.max_folders,
                text_word=args.text_word,
                show_names=args.show_names,
                out=sys.stdout,
            )
        finally:
            with contextlib.suppress(Exception):  # best-effort logout
                client.logout()
    except Exception as exc:  # the class name only, never the message
        print(f"error: {type(exc).__name__}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
