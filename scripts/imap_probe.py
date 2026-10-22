"""Read-only IMAP folder and search probe (WP13a, O35). Run by the owner on their own machine, never in the image.

Measures what the mail history tools depend on: which special-use mailboxes a server announces, how many other
mailboxes there are, and how long LIST, EXAMINE and UID SEARCH take for the criteria the hub uses. These rounds keep
local copies of the hub's rules (the tests keep them equal), and the cluster Secret is not read: the username and the
password are typed at a prompt, nothing is written to disk, and the output carries counts, roles and timings only.

    uv run python scripts/imap_probe.py --host imap.mail.me.com
    uv run python scripts/imap_probe.py --host imap.gmail.com

Definitions: `other` counts selectable mailboxes without a special-use attribute. It includes INBOX, folders skipped
as junk or drafts by name, and virtual folders such as \\Flagged or \\Important, so it overstates the scan.
`scan_targets` is the number of folders the probe actually scans (INBOX first, junk and drafts skipped by attribute
and by the hub's last-component names, capped at --max-folders); use it to size MAX_LOCATE_FOLDERS.

Timings: `list` and the `examine_search_since` pass are the first commands of the connection on each mailbox, so
their `examine_ms` and `search_ms` are cold; the later rounds run warm. The searches have the hub's shape: Message-IDs
of the hub's Message-ID pattern, each sent as the bracketed token `"<id>"` in one quoted string with nothing escaped
(iCloud matches only whole tokens), and the hub's balanced OR tree. Every round is timed on its own and an error (a
socket timeout included) is reported by its class name and does not stop the next round; a timeout can leave the
connection unusable, so the `text` round runs last. On Gmail the hub's `sent` and `archive` label keys are timed in
All Mail, compared with the `\\Sent` mailbox, and one cold candidate FETCH (INTERNALDATE FLAGS X-GM-LABELS) of the
newest 500 All Mail hits is timed.

Paste only the part after the `--- result ---` line. With --show-names the output contains mailbox names, a banner
says so, and the marker line is left out.

The connection goes through ReadOnlyProxy, a runtime allowlist: only CAPABILITY, LOGIN, LIST, EXAMINE (never SELECT),
UID SEARCH and UID FETCH of INTERNALDATE, FLAGS, X-GM-LABELS and the BODY.PEEK Message-ID header reach the server;
every other attribute raises PermissionError.

`--status-rounds` (WP13b, ruling E-im5) runs P1-P7 instead of the rounds above; their answers set the
get_message_status design constants. These rounds import the hub's read-only guard and search builders (and nothing
that reads configuration or the Secret): every command goes through `GuardedImapClient`, one 60 s deadline per round,
and every criteria is built by `imap_search`; a Message-ID failing `valid_message_id` or `search_value_ok` is skipped
and counted (SR4). Samples come from the last 180 days and are at least one day old, so the server's search index has
them; drafts are dropped. Output: yes/no, k/n counts, bytes and ms only.

    uv run python scripts/imap_probe.py --host imap.mail.me.com --status-rounds
    uv run python scripts/imap_probe.py --host imap.gmail.com --status-rounds

P1 reply match: in sent (Gmail: All Mail with the hub's `\\Sent` label key), up to 3 non-draft rows of the newest 200
with at least three References tokens, those with an In-Reply-To first. A control searches each candidate's own
Message-ID (`control=k/n`); then `HEADER References` for the first, middle and last token and `HEADER In-Reply-To`,
each `target_hit=k/n` with `result=yes|no|inconclusive`: `no` only at 0/n with the control at n/n, `yes` at n/n with
the control at n/n, anything else inconclusive. No candidate prints `skipped=no_candidate` (never a miss, IR7). P2:
the In-Reply-To searches upper-cased (informational). P3: warm inbox and sent, OR trees of 60/40/20/10 In-Reply-To +
References keys over the newest inbox ids, 3 runs per size, each on its own ids (no timed criteria repeats), every
run's ms, the median and the criteria bytes; a size without 3 distinct id sets is `skipped=too_few_ids`. P4: 3
distinct OR-20 location batches against 3 distinct ORs within 50 keys and 4,000 bytes, warm inbox, `found=k/n`.
P5 (Gmail): one OR of up to 20 X-GM-THRID keys of the newest All Mail rows, accepted (`members_found=k/n`) or refused
(BAD/NO, then `single_accepted` for one key). P6: EXAMINE inbox, sent (Gmail: All Mail), inbox again. P7: the 64 KiB
re-check header FETCH for the newest 100 sent rows, ms and bytes.

Gates (ruling E-im5): P1 acts only on `result=no`; on Gmail the header reply search is turned off only when both
`references_last` and `in_reply_to` show `result=no`; a `no` on the middle token alone is the R35 residual, and any
`inconclusive` keeps the search on. Design B (P3, iCloud inbox) if median(keys=40) <= 1.3 x median(keys=20) and
median(keys=60) < 1,000 ms; when the 60-key line errors (it can exceed 4,000 bytes), judge B at keys=40 alone and
record that B is then capped by the 4,000 bytes. P3 runs the sizes largest first, so per-term caching biases against B.
P5 `accepted=no` means one SEARCH per thread id, and `single_accepted=no` means the Gmail thread design itself needs a
ruling.
"""

import argparse
import contextlib
import getpass
import re
import ssl
import statistics
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any, Final, TextIO

from imapclient import IMAPClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))  # the hub runs from src/ (uv `package = false`)

from mcp_hub.providers import imap_search
from mcp_hub.providers.deadline import CallDeadline
from mcp_hub.providers.headers import (
    DESCENDANT_HEADER_BYTES,
    DESCENDANT_HEADER_ITEM,
    bare_message_id,
    candidate_header_item,
    header_bytes,
    msg_id_tokens,
    valid_message_id,
)
from mcp_hub.providers.imap_guard import GuardedImapClient, ImapCommandRefused, imap_error_kind
from mcp_hub.providers.imap_search import GMAIL_SENT_LABEL_KEY, LOCATE_BATCH_VALUES, HeaderField

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


def msg_id_token(value: str) -> str:
    """The hub's imap_search.msg_id_token (O35): the bare id as the bracketed token `"<id>"` in one quoted string; the
    bare id must pass `quoted` and hold no `<` or `>`."""
    quoted(value)
    if "<" in value or ">" in value:
        raise ValueError("search value refused")
    return f'"<{value}>"'


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
    keys = [f"HEADER Message-ID {msg_id_token(i)}" for i in ids[:_OR_VALUES]]
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


def _guarded(name: str, emit: Callable[[str], None], measure: Callable[[], None]) -> bool:
    """One round: an error is reported by its class name only and the next round still runs. A refusal of the proxy
    or of the hub's guard is a bug in the probe and is never swallowed. True when the round completed."""
    try:
        measure()
    except (PermissionError, ImapCommandRefused):
        raise
    except Exception as exc:
        separator = " " if ": " in name else ": "  # a sub-round name already has its colon (T0-S3)
        emit(f"{name}{separator}error={type(exc).__name__}")
        return False
    return True


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
            "header_message_id": f"NOT DRAFT HEADER Message-ID {msg_id_token(first)}",
            "header_references": f"NOT DRAFT HEADER References {msg_id_token(first)}",
            "or20": f"NOT DRAFT {_or_batch(inbox_ids)}",
        }
        for round_name in _HEADER_ROUNDS:
            _guarded(round_name, emit, partial(_search_round, client, targets, round_name, criteria[round_name], emit))

    if b"X-GM-EXT-1" in caps:
        _gmail_rounds(client, selectable, since, since_hits, emit)

    # Last: a full-text search is the slowest, and a timeout may leave the connection unusable.
    _guarded("text", emit, lambda: _search_round(client, targets, "text", f"NOT DRAFT TEXT {quoted(text_word)}", emit))


# --- WP13b status rounds P1-P7 (ruling E-im5): the get_message_status lookup shapes --------------------------------
# Unlike the O35 rounds above, every command goes through the hub's GuardedImapClient and every criteria is built by
# the hub's imap_search builders (SR4); a Message-ID failing valid_message_id or search_value_ok is skipped and counted.

ROUND_SECONDS: Final = 60.0  # one CallDeadline per round; the guard sets the socket timeout from it
_INBOX: Final = "INBOX"  # the registry default inbox
_NOT_DRAFT: Final = b"NOT DRAFT"  # every criteria starts with it (imap_search.criteria)
_STATUS_SINCE_DAYS: Final = 180  # window of the UID lists the samples are drawn from
_STATUS_MIN_AGE_DAYS: Final = 1  # samples are at least a day old, so the server's search index has them (T0-1)
_P1_SAMPLE: Final = 200
_P1_CANDIDATES: Final = 3
_P1_MIN_REFERENCES: Final = 3
_IDS_SAMPLE: Final = 160  # newest inbox rows read for the P3/P4 Message-IDs: 3 distinct batches of 50 (T0-2)
_P3_KEYS: Final = (60, 40, 20, 10)  # largest first: per-term caching biases against Design B, not toward it
_RUNS: Final = 3  # each run searches its own ids, so no timed criteria repeats (T0-2)
_P4_WIDE: Final = 50
_P5_SAMPLE: Final = 60
_P7_UIDS: Final = 100
_P1_HEADER_ITEM: Final = f"BODY.PEEK[HEADER.FIELDS (MESSAGE-ID IN-REPLY-TO REFERENCES)]<0.{DESCENDANT_HEADER_BYTES}>"
_REPLY_FIELDS: Final[tuple[HeaderField, ...]] = ("In-Reply-To", "References")
_DRAFT_MARKS: Final = frozenset({b"\\draft", b"\\drafts"})
_HEADER_FOLD: Final = re.compile(r"\r?\n[ \t]+")


@dataclass(frozen=True, slots=True)
class _Candidate:
    """A P1 sent row: kept in memory only, never printed."""

    uid: int
    message_id: str
    references: tuple[str, str, str]  # first, middle, last token
    in_reply_to: str | None


def _round_guard(raw: Any) -> GuardedImapClient:
    deadline = CallDeadline(time.monotonic(), ROUND_SECONDS, ROUND_SECONDS)
    return GuardedImapClient(raw, deadline, registry_inbox_raw=_INBOX)


def _usable(token: str | None) -> str | None:
    """The id when the hub would search for it (`valid_message_id` and `search_value_ok`), else None (SR4)."""
    if token is not None and valid_message_id(token) is not None and imap_search.search_value_ok(token):
        return token
    return None


def _marks(row: Mapping[bytes, object], key: bytes) -> set[bytes]:
    value = row.get(key)
    marks: set[bytes] = set()
    for mark in value if isinstance(value, (tuple, list)) else ():
        if isinstance(mark, str):
            mark = mark.encode("utf-8", "replace")
        if isinstance(mark, bytes):
            marks.add(mark.lower())
    return marks


def _is_draft(row: Mapping[bytes, object]) -> bool:
    """`\\Draft` or `\\Drafts` in any case, as a flag or as a Gmail label (the hub's rule S1)."""
    return bool((_marks(row, b"FLAGS") | _marks(row, b"X-GM-LABELS")) & _DRAFT_MARKS)


def _first_fields(raw: bytes) -> dict[str, str]:
    """The first value of each header field, unfolded; field names lowercased."""
    fields: dict[str, str] = {}
    for line in _HEADER_FOLD.sub(" ", raw.decode("ascii", "replace")).splitlines():
        name, separator, value = line.partition(":")
        if separator:
            fields.setdefault(name.strip().lower(), value.strip())
    return fields


def _newest(uids: Sequence[int], count: int) -> list[int]:
    return sorted(set(uids))[-count:]


def _timed_runs(guard: GuardedImapClient, runs: Sequence[bytes]) -> tuple[list[int], list[list[int]]]:
    """ms and hits of each search, in order."""
    times: list[int] = []
    hits: list[list[int]] = []
    for data in runs:
        started = time.perf_counter()
        hits.append(guard.search(data))
        times.append(_ms(started))
    return times, hits


def _runs_ms(times: Sequence[int]) -> str:
    return f"runs_ms={','.join(str(t) for t in times)} median_ms={round(statistics.median(times))}"


def _status_folders(guard: GuardedImapClient) -> dict[str, str]:
    """Raw names of the first selectable `\\Sent` and `\\All` mailboxes by role; never printed."""
    found: dict[str, str] = {}
    for flags, _delimiter, name in guard.list_folders():
        if {f.lower() for f in flags} & _NOT_SELECTABLE:
            continue
        raw_name = name.decode("ascii", "replace") if isinstance(name, bytes) else str(name)
        role = _role(flags, raw_name)
        if role in ("sent", "all"):
            found.setdefault(role, raw_name)
    return found


def _inbox_ids(raw: Any, gmail: bool, window: bytes, emit: Callable[[str], None]) -> list[tuple[str, int]]:
    """(Message-ID, UID) of the newest inbox rows, newest first, distinct ids, drafts and unusable ids skipped."""
    guard = _round_guard(raw)
    started = time.perf_counter()
    guard.examine(_INBOX)
    uids = _newest(guard.search(imap_search.criteria(window)), _IDS_SAMPLE)
    items = [candidate_header_item(("MESSAGE-ID",)), "FLAGS", *(["X-GM-LABELS"] if gmail else [])]
    rows = guard.fetch(uids, items) if uids else {}
    pairs: list[tuple[str, int]] = []
    seen: set[str] = set()
    drafts = unsafe = 0
    for uid in sorted(rows, reverse=True):
        if _is_draft(rows[uid]):
            drafts += 1
            continue
        token = bare_message_id(_first_fields(header_bytes(rows[uid])).get("message-id"))
        if token is None:
            continue
        if _usable(token) is None:
            unsafe += 1
        elif token not in seen:
            seen.add(token)
            pairs.append((token, uid))
    emit(f"status_ids: rows={len(rows)} usable={len(pairs)} drafts={drafts} skipped_unsafe={unsafe} ms={_ms(started)}")
    return pairs


def _p1_candidates(raw: Any, mailbox: str, gmail: bool, window: bytes, emit: Callable[[str], None]) -> list[_Candidate]:
    """P1 sample: up to 3 non-draft sent rows, newest first and those with a usable In-Reply-To before the others,
    each with a usable own Message-ID (the control) and at least three References tokens whose first, middle and last
    token are usable."""
    guard = _round_guard(raw)
    started = time.perf_counter()
    guard.examine(mailbox)
    listing = imap_search.criteria(window, GMAIL_SENT_LABEL_KEY) if gmail else imap_search.criteria(window)
    uids = _newest(guard.search(listing), _P1_SAMPLE)
    items = [_P1_HEADER_ITEM, "FLAGS", *(["X-GM-LABELS"] if gmail else [])]
    rows = guard.fetch(uids, items) if uids else {}
    eligible: list[_Candidate] = []
    drafts = unsafe = 0
    for uid in sorted(rows, reverse=True):
        if _is_draft(rows[uid]):
            drafts += 1
            continue
        fields = _first_fields(header_bytes(rows[uid]))
        own = bare_message_id(fields.get("message-id"))
        references = msg_id_tokens(fields.get("references", ""))
        if own is None or len(references) < _P1_MIN_REFERENCES:
            continue
        picks = (references[0], references[len(references) // 2], references[-1])
        refused = sum(1 for token in (own, *picks) if _usable(token) is None)
        if refused:
            unsafe += refused
            continue
        in_reply_to = next(iter(msg_id_tokens(fields.get("in-reply-to", ""))), None)
        if in_reply_to is not None and _usable(in_reply_to) is None:
            unsafe += 1
            in_reply_to = None
        eligible.append(_Candidate(uid, own, picks, in_reply_to))
    chosen = sorted(eligible, key=lambda c: c.in_reply_to is None)[:_P1_CANDIDATES]  # stable: newest first
    found = (
        f"candidates={len(chosen)} with_irt={sum(1 for c in chosen if c.in_reply_to is not None)}"
        if chosen
        else "skipped=no_candidate"
    )
    emit(f"p1_reply_match: {found} scanned={len(rows)} drafts={drafts} skipped_unsafe={unsafe} ms={_ms(started)}")
    return chosen


def _reply_criteria(gmail: bool, field: HeaderField, token: str) -> bytes:
    """The hub's search for one id: in All Mail on Gmail with the `\\Sent` label key (IR7)."""
    keys = imap_search.header_or((field,), [token])
    if gmail:
        return imap_search.criteria(_NOT_DRAFT, GMAIL_SENT_LABEL_KEY, keys)
    return imap_search.criteria(_NOT_DRAFT, keys)


def _targets_hit(raw: Any, gmail: bool, field: HeaderField, targets: Sequence[tuple[int, str]]) -> tuple[set[int], int]:
    """The candidate UIDs each one-id search returned, and the total ms (the P1 mailbox is still selected)."""
    guard = _round_guard(raw)
    started = time.perf_counter()
    hit = {uid for uid, token in targets if uid in guard.search(_reply_criteria(gmail, field, token))}
    return hit, _ms(started)


def _result(found: int, controlled: int, n: int) -> str:
    """A key fails (`no`) only when no candidate is hit while the control finds every one; `yes` when every
    candidate is hit and controlled; anything else is `inconclusive`, never a `no` (T0-1)."""
    if controlled == n and found == 0:
        return "no"
    return "yes" if controlled == n and found == n else "inconclusive"


def _p1_p2(raw: Any, candidates: list[_Candidate], gmail: bool, emit: Callable[[str], None]) -> None:
    if not candidates:
        emit("p2_case: skipped=no_candidate")
        return
    control: set[int] = set()

    def run_control() -> None:
        hit, ms = _targets_hit(raw, gmail, "Message-ID", [(c.uid, c.message_id) for c in candidates])
        control.update(hit)
        emit(f"p1_reply_match: control={len(hit)}/{len(candidates)} ms={ms}")

    controlled = _guarded("p1_reply_match: control", emit, run_control)
    with_irt = [c for c in candidates if c.in_reply_to is not None]
    checks: list[tuple[str, HeaderField, list[tuple[int, str]]]] = [
        ("references_first", "References", [(c.uid, c.references[0]) for c in candidates]),
        ("references_middle", "References", [(c.uid, c.references[1]) for c in candidates]),
        ("references_last", "References", [(c.uid, c.references[2]) for c in candidates]),
        ("in_reply_to", "In-Reply-To", [(c.uid, c.in_reply_to) for c in with_irt if c.in_reply_to is not None]),
    ]
    for key, field, targets in checks:
        name = f"p1_reply_match: key={key}"
        if not targets:
            emit(f"{name} skipped=no_usable_id")
            continue

        def check(name: str = name, field: HeaderField = field, targets: list[tuple[int, str]] = targets) -> None:
            hit, ms = _targets_hit(raw, gmail, field, targets)
            n = len(targets)
            seen = sum(1 for uid, _ in targets if uid in control) if controlled else 0
            shown = f"{seen}/{n}" if controlled else "error"
            emit(f"{name} target_hit={len(hit)}/{n} control={shown} result={_result(len(hit), seen, n)} ms={ms}")

        _guarded(name, emit, check)
    upper = [
        (c.uid, c.in_reply_to.upper())
        for c in with_irt
        if c.in_reply_to is not None
        and c.in_reply_to.upper() != c.in_reply_to
        and _usable(c.in_reply_to.upper()) is not None
    ]
    if not with_irt:
        emit("p2_case: skipped=no_usable_id")
    elif not upper:
        emit("p2_case: skipped=no_case_change")
    else:

        def case() -> None:
            hit, ms = _targets_hit(raw, gmail, "In-Reply-To", upper)
            emit(f"p2_case: target_hit={len(hit)}/{len(upper)} ms={ms}")

        _guarded("p2_case", emit, case)


def _p3(raw: Any, role: str, mailbox: str, ids: list[str], emit: Callable[[str], None]) -> None:
    """Warm `mailbox` (EXAMINE + one Message-ID search, a shape never timed), then per OR size 3 runs over distinct
    id slices of In-Reply-To + References keys; a size without 3 distinct slices is skipped, never reused."""

    def warm() -> None:
        guard = _round_guard(raw)
        guard.examine(mailbox)
        guard.search(imap_search.criteria(_NOT_DRAFT, imap_search.header_or(("Message-ID",), ids[:1])))

    if not ids:
        emit(f"p3_key_scaling: folder={role} skipped=too_few_ids")
        return
    if not _guarded(f"p3_key_scaling: folder={role}", emit, warm):
        return  # another mailbox may still be selected
    for keys in _P3_KEYS:
        name = f"p3_key_scaling: folder={role} keys={keys}"
        count = keys // len(_REPLY_FIELDS)
        if len(ids) < _RUNS * count:
            emit(f"{name} skipped=too_few_ids")
            continue

        def measure(name: str = name, count: int = count) -> None:
            runs = [
                imap_search.criteria(_NOT_DRAFT, imap_search.header_or(_REPLY_FIELDS, ids[r * count : (r + 1) * count]))
                for r in range(_RUNS)
            ]
            times, hits = _timed_runs(_round_guard(raw), runs)
            emit(f"{name} ids={count} bytes={max(len(d) for d in runs)} {_runs_ms(times)} hits={sum(map(len, hits))}")

        _guarded(name, emit, measure)


def _p4(raw: Any, pairs: list[tuple[str, int]], emit: Callable[[str], None]) -> None:
    """OR-20 (the hub's location batch) against the widest OR within 50 keys and 4,000 bytes, warm inbox; each run
    searches the next distinct batch."""
    ids = [message_id for message_id, _ in pairs]
    uid_of = dict(pairs)
    guard = _round_guard(raw)
    guard.examine(_INBOX)
    for width in (LOCATE_BATCH_VALUES, _P4_WIDE):
        batches = imap_search.or_batches(ids, ("Message-ID",), max_values=width)
        if len(batches) < _RUNS or (len(batches) == _RUNS and len(batches[-1]) < len(batches[0])):
            emit(f"p4_location_width: keys={width} skipped=too_few_ids")
            continue
        batches = batches[:_RUNS]
        runs = [imap_search.criteria(_NOT_DRAFT, imap_search.header_or(("Message-ID",), b)) for b in batches]
        times, hits = _timed_runs(guard, runs)
        searched = sum(len(b) for b in batches)
        found = sum(1 for b, h in zip(batches, hits, strict=True) for i in b if uid_of[i] in set(h))
        emit(
            f"p4_location_width: keys={len(batches[0])} bytes={max(len(d) for d in runs)} {_runs_ms(times)} "
            f"found={found}/{searched} target_found={'yes' if found == searched else 'no'}"
        )


def _accepted(guard: GuardedImapClient, data: bytes) -> str:
    """`yes` when the server runs the search, `no` on a tagged BAD/NO; anything else propagates."""
    try:
        guard.search(data)
    except Exception as exc:
        if imap_error_kind(exc) == "other":
            raise
        return "no"
    return "yes"


def _p5(raw: Any, all_mail: str, window: bytes, emit: Callable[[str], None]) -> None:
    """One OR of up to 20 X-GM-THRID keys from the newest All Mail rows: accepted, or refused with BAD/NO; a refused
    OR is followed by one single-key search (T0-3)."""
    guard = _round_guard(raw)
    guard.examine(all_mail)
    uids = _newest(guard.search(imap_search.criteria(window)), _P5_SAMPLE)
    rows = guard.fetch(uids, ["X-GM-THRID", "FLAGS", "X-GM-LABELS"]) if uids else {}
    thread_of: dict[int, int] = {}
    for uid, row in rows.items():
        value = row.get(b"X-GM-THRID")
        if not _is_draft(row) and type(value) is int and 0 <= value < 2**64:
            thread_of[uid] = value
    newest_first = [thread_of[uid] for uid in sorted(thread_of, reverse=True)]
    if not newest_first:
        emit("p5_gmail_thrid: skipped=no_thread_ids")
        return
    batch = imap_search.thrid_batches(newest_first)[0]
    data = imap_search.criteria(_NOT_DRAFT, imap_search.thrid_or(batch))
    started = time.perf_counter()
    try:
        hits = guard.search(data)
    except Exception as exc:
        kind = imap_error_kind(exc)
        if kind == "other":
            raise
        ms = _ms(started)
        single = _accepted(guard, imap_search.criteria(_NOT_DRAFT, imap_search.thrid_or(batch[:1])))
        emit(f"p5_gmail_thrid: accepted=no status={kind} keys={len(batch)} ms={ms} single_accepted={single}")
        return
    ms = _ms(started)
    members = {uid for uid, thread in thread_of.items() if thread in batch}
    found = len(members & set(hits))
    emit(
        f"p5_gmail_thrid: accepted=yes keys={len(batch)} bytes={len(data)} hits={len(hits)} "
        f"members_found={found}/{len(members)} ms={ms}"
    )


def _p6(raw: Any, role: str, other: str, emit: Callable[[str], None]) -> None:
    guard = _round_guard(raw)
    times: list[int] = []
    for mailbox in (_INBOX, other, _INBOX):
        started = time.perf_counter()
        guard.examine(mailbox)
        times.append(_ms(started))
    emit(f"p6_reexamine: inbox_ms={times[0]} other={role} other_ms={times[1]} inbox_again_ms={times[2]}")


def _p7(raw: Any, role: str, mailbox: str, gmail: bool, window: bytes, emit: Callable[[str], None]) -> None:
    """The 64 KiB re-check item (`headers.DESCENDANT_HEADER_ITEM`) for the newest 100 sent UIDs."""
    guard = _round_guard(raw)
    guard.examine(mailbox)
    listing = imap_search.criteria(window, GMAIL_SENT_LABEL_KEY) if gmail else imap_search.criteria(window)
    uids = _newest(guard.search(listing), _P7_UIDS)
    if not uids:
        emit(f"p7_header_fetch: mailbox={role} skipped=no_sent_rows")
        return
    items = [DESCENDANT_HEADER_ITEM, "FLAGS", "INTERNALDATE", *(["X-GM-LABELS", "X-GM-THRID"] if gmail else [])]
    started = time.perf_counter()
    rows = guard.fetch(uids, items)
    ms = _ms(started)
    sizes = [len(header_bytes(row)) for row in rows.values()]
    emit(
        f"p7_header_fetch: mailbox={role} uids={len(uids)} rows={len(rows)} ms={ms} header_bytes={sum(sizes)} "
        f"max_header_bytes={max(sizes, default=0)} cut={sum(1 for size in sizes if size >= DESCENDANT_HEADER_BYTES)}"
    )


def status_rounds(raw: Any, caps: frozenset[bytes], *, out: TextIO, today: date | None = None) -> None:
    """P1-P7 on a logged-in raw client; each round runs through its own GuardedImapClient and deadline."""
    caps = frozenset(c.upper() for c in caps)

    def emit(line: str) -> None:
        print(line, file=out)

    def flag(name: bytes) -> str:
        return "yes" if name in caps else "no"

    emit("--- result ---")
    emit(
        f"capabilities: special_use={flag(b'SPECIAL-USE')} x_gm_ext_1={flag(b'X-GM-EXT-1')} "
        f"literal_plus={flag(b'LITERAL+')} move={flag(b'MOVE')}"
    )
    gmail = b"X-GM-EXT-1" in caps
    folders = _status_folders(_round_guard(raw))
    emit(
        f"status_folders: gmail={'yes' if gmail else 'no'} sent={'yes' if 'sent' in folders else 'no'} "
        f"all_mail={'yes' if 'all' in folders else 'no'}"
    )
    day = today or datetime.now(UTC).date()
    window = imap_search.window_keys(
        since_day=day - timedelta(days=_STATUS_SINCE_DAYS),
        before_day=day - timedelta(days=_STATUS_MIN_AGE_DAYS),
        unread_only=False,
    )
    # Gmail: the hub's reply step and the re-check run in All Mail; elsewhere in the sent mailbox.
    reply_role, reply_box = ("all_mail", folders.get("all")) if gmail else ("sent", folders.get("sent"))

    pairs: list[tuple[str, int]] = []
    _guarded("status_ids", emit, lambda: pairs.extend(_inbox_ids(raw, gmail, window, emit)))
    ids = [message_id for message_id, _ in pairs]

    candidates: list[_Candidate] = []
    if reply_box is None:
        emit(f"p1_reply_match: skipped=no_{reply_role}_mailbox")
    else:
        box = reply_box
        _guarded("p1_reply_match", emit, lambda: candidates.extend(_p1_candidates(raw, box, gmail, window, emit)))
    _p1_p2(raw, candidates, gmail, emit)

    for role, mailbox in (("inbox", _INBOX), ("sent", folders.get("sent"))):
        if mailbox is None:
            emit(f"p3_key_scaling: folder={role} skipped=no_mailbox")
        else:
            _p3(raw, role, mailbox, ids, emit)

    if not pairs:
        emit("p4_location_width: skipped=too_few_ids")
    else:
        _guarded("p4_location_width", emit, lambda: _p4(raw, pairs, emit))

    all_mail = folders.get("all")
    if not gmail:
        emit("p5_gmail_thrid: skipped=not_gmail")
    elif all_mail is None:
        emit("p5_gmail_thrid: skipped=no_all_mail_mailbox")
    else:
        _guarded("p5_gmail_thrid", emit, lambda: _p5(raw, all_mail, window, emit))

    if reply_box is None:
        emit(f"p6_reexamine: skipped=no_{reply_role}_mailbox")
        emit(f"p7_header_fetch: skipped=no_{reply_role}_mailbox")
        return
    box = reply_box
    _guarded("p6_reexamine", emit, lambda: _p6(raw, reply_role, box, emit))
    _guarded("p7_header_fetch", emit, lambda: _p7(raw, reply_role, box, gmail, window, emit))


_HELP: Final = """\
Run by the owner, on their own machine, in the repository directory:
  uv run python scripts/imap_probe.py --host imap.mail.me.com --status-rounds
  uv run python scripts/imap_probe.py --host imap.gmail.com --status-rounds
The username and the app-specific password are asked for at a prompt; nothing is stored or changed.
Share only the part after the line "--- result ---".

Read-only IMAP folder and search probe (O35); --status-rounds runs the get_message_status rounds P1-P7 instead.
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=_HELP, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", required=True)
    parser.add_argument("--since-days", type=int, default=14)
    parser.add_argument("--max-folders", type=int, default=30)
    parser.add_argument("--text-word", default="the")
    parser.add_argument("--show-names", action="store_true")
    parser.add_argument(
        "--status-rounds", action="store_true", help="run P1-P7 through the hub's guard instead of the O35 rounds"
    )
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
        if args.status_rounds:
            session = _round_guard(raw)
            try:
                session.login(username, password)
                status_rounds(raw, session.capabilities(), out=sys.stdout)
            finally:
                with contextlib.suppress(Exception):  # best-effort logout
                    session.logout()
            return 0
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
