"""Read-only IMAP folder and search probe (WP13a, O35). Run by the owner on their own machine, never in the image.

Measures what the mail history tools depend on: which special-use mailboxes a server announces, how many other
mailboxes there are, and how long LIST, EXAMINE and UID SEARCH take for the criteria the hub uses. The hub is not
imported and the cluster Secret is not read: the username and the password are typed at a prompt, nothing is written
to disk, and the output carries counts, roles and timings only.

    uv run python scripts/imap_probe.py --host imap.mail.me.com
    uv run python scripts/imap_probe.py --host imap.gmail.com

Definitions: `other` counts selectable mailboxes without a special-use attribute. It includes INBOX, folders skipped
as junk or drafts by name, and virtual folders such as \\Flagged or \\Important, so it overstates the scan.
`scan_targets` is the number of folders the probe actually scans (INBOX first, junk and drafts skipped by attribute
and by the hub's last-component names, capped at --max-folders); use it to size MAX_LOCATE_FOLDERS.

Timings: `list` and the `examine_search_since` pass are the first commands of the connection on each mailbox, so
their `examine_ms` and `search_ms` are cold; the later rounds run warm. The searches have the hub's shape: bare
Message-IDs (no angle brackets) of the hub's Message-ID pattern, each as one quoted string with nothing escaped, and
the hub's balanced OR tree. Every round is timed on its own and an error (a socket timeout included) is reported by
its class name and does not stop the next round; a timeout can leave the connection unusable, so the `text` round
runs last. On Gmail the hub's `sent` and `archive` label keys are timed in All Mail, compared with the `\\Sent`
mailbox, and one cold candidate FETCH (INTERNALDATE FLAGS X-GM-LABELS) of the newest 500 All Mail hits is timed.

Paste only the part after the `--- result ---` line. With --show-names the output contains mailbox names, a banner
says so, and the marker line is left out.

The connection goes through ReadOnlyProxy, a runtime allowlist: only CAPABILITY, LOGIN, LIST, EXAMINE (never SELECT),
UID SEARCH and UID FETCH of INTERNALDATE, FLAGS, X-GM-LABELS and the BODY.PEEK Message-ID header reach the server;
every other attribute raises PermissionError.
"""

import argparse
import contextlib
import getpass
import re
import ssl
import sys
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any, Final, TextIO

from imapclient import IMAPClient

ALLOWED_HOSTS: Final = frozenset({"imap.mail.me.com", "imap.gmail.com"})
PORT: Final = 993
SOCKET_TIMEOUT: Final = 30.0  # seconds; a hung search must not block the owner's terminal

_FETCH_INTERNALDATE: Final = "INTERNALDATE"
_FETCH_MESSAGE_ID: Final = "BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)]<0.2048>"
_GMAIL_CANDIDATE_ITEMS: Final = ("INTERNALDATE", "FLAGS", "X-GM-LABELS")  # the hub's Gmail candidate FETCH
_ALLOWED_FETCH_ITEMS: Final = frozenset({_FETCH_INTERNALDATE, _FETCH_MESSAGE_ID, *_GMAIL_CANDIDATE_ITEMS})
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
_SKIPPED_NAMES: Final = frozenset({"junk", "spam", "drafts"})  # the hub's folders.EXCLUDED_LAST_NAMES (casefold)
_FALLBACK_NAMES: Final = ("Sent Messages", "Sent", "Archive", "Deleted Messages", "Trash", "Junk", "Drafts")
_NOT_SELECTABLE: Final = frozenset({b"\\noselect", b"\\nonexistent"})
_MONTHS: Final = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
# Local copies of the hub's rules (the probe never imports the hub; tests/unit/test_imap_probe.py keeps them equal):
# ids.MESSAGE_ID_PATTERN and imap_search._QUOTABLE.
_MESSAGE_ID: Final = re.compile(r"[\x21\x23-\x26\x28-\x3B\x3D\x3F-\x5B\x5D-\x7A\x7C\x7E]{1,300}")
_QUOTABLE: Final = re.compile(r"[\x20\x21\x23-\x5b\x5d-\x7e]+")
_BRACKETED: Final = re.compile(r"<([^<>]*)>")
_OR_VALUES: Final = 20  # the hub's LOCATE_BATCH_VALUES
_OR_MAX_BYTES: Final = 4000  # the hub's imap_search.MAX_OR_BYTES
_GMAIL_FETCH_UIDS: Final = 500  # the hub's FETCH_CHUNK_UIDS
_LOCALPART_MIN: Final = 8
_HEADER_ROUNDS: Final = ("header_message_id", "header_references", "or20")
_GMAIL_SENT: Final = 'X-GM-LABELS "\\\\Sent"'
_GMAIL_INBOX: Final = 'X-GM-LABELS "\\\\Inbox"'

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
    """The hub's imap_search.quoted (D74): one IMAP quoted string with nothing escaped. Values with `"`, `\\`, a
    control or non-ASCII character, empty values and `X-GM-RAW` are refused, never escaped."""
    if _QUOTABLE.fullmatch(value) is None or "X-GM-RAW" in value.upper():
        raise ValueError("search value refused")
    return f'"{value}"'


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
    """The hub's balanced tree (imap_search._or_tree): the left side takes the first ceil(n/2) keys; a side with more
    than one key is parenthesised."""
    if len(keys) == 1:
        return keys[0]
    middle = (len(keys) + 1) // 2
    sides = [_or_tree(side) if len(side) == 1 else f"({_or_tree(side)})" for side in (keys[:middle], keys[middle:])]
    return f"OR {sides[0]} {sides[1]}"


def _or_batch(ids: Sequence[str]) -> str:
    """The OR tree of the first ids that fit the hub's batch: at most 20 values and 4,000 bytes."""
    keys = [f"HEADER Message-ID {quoted(i)}" for i in ids[:_OR_VALUES]]
    while len(keys) > 1 and len(_or_tree(keys)) > _OR_MAX_BYTES:
        keys.pop()
    return _or_tree(keys)


def _bare_message_id(value: str) -> str | None:
    """The hub's search value for a Message-ID header (§5.5): the id without angle brackets, only when it matches the
    hub's Message-ID pattern and can be one quoted string."""
    value = value.strip()
    bracketed = _BRACKETED.fullmatch(value)
    bare = bracketed.group(1) if bracketed is not None else (value if "<" not in value else "")
    if _MESSAGE_ID.fullmatch(bare) is None or "X-GM-RAW" in bare.upper():
        return None
    return bare


def _message_id(fetched: dict[bytes, Any]) -> str | None:
    for key, value in fetched.items():
        if not key.upper().startswith(b"BODY[") or not isinstance(value, bytes):
            continue
        header = value.decode("ascii", "ignore").replace("\r\n ", " ").replace("\r\n\t", " ")
        for line in header.splitlines():
            label, _, rest = line.partition(":")
            if label.strip().lower() == "message-id":
                return _bare_message_id(rest)
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


def _guarded(name: str, emit: Callable[[str], None], measure: Callable[[], None]) -> None:
    """One round: an error is reported by its class name only and the next round still runs. A PermissionError of
    the proxy is a bug in the probe and is never swallowed."""
    try:
        measure()
    except PermissionError:
        raise
    except Exception as exc:
        emit(f"{name}: error={type(exc).__name__}")


def _search_round(
    client: ReadOnlyProxy, targets: list[tuple[str, str]], round_name: str, criteria: str, emit: Callable[[str], None]
) -> None:
    data = criteria.encode("ascii")
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
    emit(f"{round_name}: folders={len(targets)} total_ms={_ms(round_started)} max_ms={slowest} found_in={found_in}")


def _msgid_variants(client: ReadOnlyProxy, inbox: str, needle: str, emit: Callable[[str], None]) -> None:
    """Which form of the Message-ID search does the server match? INBOX only, where the message is known to be.
    Prints counts and timings, never the needle."""
    local, _, _ = needle.rpartition("@")
    variants = {
        "msgid_bare": f"NOT DRAFT HEADER Message-ID {quoted(needle)}",
        "msgid_bracketed": f"NOT DRAFT HEADER Message-ID {quoted(f'<{needle}>')}",
        "msgid_no_not_draft": f"HEADER Message-ID {quoted(f'<{needle}>')}",
        "msgid_lowercase_field": f"NOT DRAFT HEADER message-id {quoted(f'<{needle}>')}",
    }
    if len(local) >= _LOCALPART_MIN:
        variants["msgid_localpart"] = f"NOT DRAFT HEADER Message-ID {quoted(local)}"

    def run(name: str, criteria: str) -> Callable[[], None]:
        def measure() -> None:
            started = time.perf_counter()
            client.select_folder(inbox)
            hits = client.search(criteria.encode("ascii"))
            emit(f"msgid_variant: name={name} hits={len(hits)} ms={_ms(started)}")

        return measure

    for name, criteria in variants.items():
        _guarded(f"msgid_variant: name={name}", emit, run(name, criteria))


def _gmail_rounds(
    client: ReadOnlyProxy,
    selectable: list[Folder],
    since: str,
    since_hits: dict[str, list[int]],
    emit: Callable[[str], None],
) -> None:
    """The hub's Gmail `sent` and `archive` searches in All Mail, the `\\Sent` mailbox for comparison, and one cold
    candidate FETCH of the newest 500 All Mail hits."""
    all_mail = next((f[2] for f in selectable if _role(f[0], f[2]) == "all"), None)
    sent_box = next((f[2] for f in selectable if _role(f[0], f[2]) == "sent"), None)
    counts: dict[str, int] = {}

    def keyed(round_name: str, mailbox: str, keys: str) -> Callable[[], None]:
        def measure() -> None:
            started = time.perf_counter()
            client.select_folder(mailbox)
            hits = client.search(f"NOT DRAFT SINCE {since} {keys}".encode("ascii"))
            counts[round_name] = len(hits)
            emit(f"{round_name}: hits={len(hits)} ms={_ms(started)}")

        return measure

    def sent_mailbox(mailbox: str) -> None:
        started = time.perf_counter()
        client.select_folder(mailbox)
        hits = client.search(f"NOT DRAFT SINCE {since}".encode("ascii"))
        counts["gmail_sent_mailbox"] = len(hits)
        emit(f"gmail_sent_mailbox: hits={len(hits)} ms={_ms(started)}")

    def candidate_fetch(mailbox: str) -> None:
        client.select_folder(mailbox)
        uids = since_hits.get(mailbox)
        if uids is None:
            uids = client.search(f"NOT DRAFT SINCE {since}".encode("ascii"))
        newest = sorted(set(uids))[-_GMAIL_FETCH_UIDS:]
        if not newest:
            emit("gmail_candidate_fetch: uids=0")
            return
        started = time.perf_counter()
        client.fetch(newest, list(_GMAIL_CANDIDATE_ITEMS))
        emit(f"gmail_candidate_fetch: uids={len(newest)} ms={_ms(started)}")

    if all_mail is None:
        emit("gmail_all_mail: absent")
    else:
        sent_keys, archive_keys = f"{_GMAIL_SENT} NOT {_GMAIL_INBOX}", f"NOT {_GMAIL_INBOX} NOT {_GMAIL_SENT}"
        _guarded("gmail_sent_keys", emit, keyed("gmail_sent_keys", all_mail, sent_keys))
        _guarded("gmail_archive_keys", emit, keyed("gmail_archive_keys", all_mail, archive_keys))
    if sent_box is None:
        emit("gmail_sent_mailbox: absent")
    else:
        _guarded("gmail_sent_mailbox", emit, lambda: sent_mailbox(sent_box))
    if "gmail_sent_keys" in counts and "gmail_sent_mailbox" in counts:
        emit(
            f"gmail_sent_compare: sent_keys={counts['gmail_sent_keys']} "
            f"sent_mailbox={counts['gmail_sent_mailbox']} "
            f"difference={counts['gmail_sent_mailbox'] - counts['gmail_sent_keys']}"
        )
    if all_mail is not None:
        _guarded("gmail_candidate_fetch", emit, lambda: candidate_fetch(all_mail))


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

    started = time.perf_counter()
    folders: list[Folder] = list(client.list_folders())
    emit(f"list: ms={_ms(started)}")
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
        if role in _SKIPPED_ROLES or _last_component(name, delimiter).casefold() in _SKIPPED_NAMES:
            continue
        targets.append((name, role))
    targets.sort(key=lambda t: t[1] != "inbox")  # INBOX first; the sort is stable
    targets = targets[:max_folders]

    emit(f"scan_targets: {len(targets)}")

    since = _imap_date(datetime.now(UTC) - timedelta(days=since_days))
    inbox_ids: list[str] = []
    since_hits: dict[str, list[int]] = {}
    for index, (name, role) in enumerate(targets):  # the first command per mailbox: cold
        started = time.perf_counter()
        client.select_folder(name)
        examine_ms = _ms(started)
        started = time.perf_counter()
        uids = client.search(f"NOT DRAFT SINCE {since}".encode("ascii"))
        search_ms = _ms(started)
        since_hits[name] = uids
        label = f" name={name!r}" if show_names else ""
        emit(
            f"examine_search_since: folder#{index} role={role} hits={len(uids)} examine_ms={examine_ms} "
            f"search_ms={search_ms} ms={examine_ms + search_ms}{label}"
        )
        started = time.perf_counter()
        unseen = client.search(f"NOT DRAFT UNSEEN SINCE {since}".encode("ascii"))
        emit(f"search_unseen_since: folder#{index} role={role} hits={len(unseen)} ms={_ms(started)}")
        if role == "inbox" and uids and not inbox_ids:
            inbox_ids = _recent_message_ids(client, uids)

    if not inbox_ids:
        emit("header rounds skipped: no readable Message-ID in the newest inbox messages")
    else:
        first = inbox_ids[0]
        inbox_name = next((n for n, r in targets if r == "inbox"), None)
        if inbox_name is not None:
            _msgid_variants(client, inbox_name, first, emit)
        criteria = {
            "header_message_id": f"NOT DRAFT HEADER Message-ID {quoted(first)}",
            "header_references": f"NOT DRAFT HEADER References {quoted(first)}",
            "or20": f"NOT DRAFT {_or_batch(inbox_ids)}",
        }
        for round_name in _HEADER_ROUNDS:
            _guarded(round_name, emit, partial(_search_round, client, targets, round_name, criteria[round_name], emit))

    if b"X-GM-EXT-1" in caps:
        _gmail_rounds(client, selectable, since, since_hits, emit)

    # Last: a full-text search is the slowest, and a timeout may leave the connection unusable.
    _guarded("text", emit, lambda: _search_round(client, targets, "text", f"NOT DRAFT TEXT {quoted(text_word)}", emit))


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
        raw = IMAPClient(
            args.host, port=PORT, ssl=True, ssl_context=ssl.create_default_context(), timeout=SOCKET_TIMEOUT
        )
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
