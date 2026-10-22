"""IMAP SEARCH criteria built by the hub (D74): the only path by which third-party values reach an IMAP command.

Every value goes out as a hub-built quoted string; the whole criteria is one printable-ASCII `bytes` value for
`GuardedImapClient.search`, which IMAPClient 4.1.0 sends unchanged. IMAPClient's per-item quoting is not used: it
leaves atom-special values such as `a(b)@x` unquoted. Values that cannot be a quoted string (`"`, `\\`, control
characters, non-ASCII) are refused, never escaped or sent as literals.

Header searches on the msg-id fields send the bracketed token `"<id>"` (O35): iCloud matches them against whole
`<...>` tokens only, so a bare id finds nothing there, while RFC 3501 substring servers match `"<id>"` as well.
"""

import re
from collections.abc import Sequence
from datetime import date
from typing import Final, Literal

from mcp_hub.providers.folders import GmailLabelFilter
from mcp_hub.providers.imap_guard import search_criteria_ok

MAX_OR_BYTES: Final = 4000  # bounds one header OR tree, so a UID SEARCH line stays well below server line limits
HeaderField = Literal["Message-ID", "In-Reply-To", "References"]

_NOT_DRAFT: Final = b"NOT DRAFT"
_HEADER_FIELDS: Final = frozenset({"Message-ID", "In-Reply-To", "References"})
_MONTHS: Final = (b"Jan", b"Feb", b"Mar", b"Apr", b"May", b"Jun", b"Jul", b"Aug", b"Sep", b"Oct", b"Nov", b"Dec")
_QUOTABLE: Final = re.compile(r"[\x20\x21\x23-\x5b\x5d-\x7e]+")  # printable ASCII without '"' and '\'
# Gmail system labels as quoted strings; the backslash of `\Sent` is escaped inside the quotes (I8, SA8).
_GMAIL_SENT: Final = b'X-GM-LABELS "\\\\Sent"'
_GMAIL_INBOX: Final = b'X-GM-LABELS "\\\\Inbox"'
_GMAIL_LABEL_KEYS: Final[dict[str, bytes]] = {
    "sent": _GMAIL_SENT + b" NOT " + _GMAIL_INBOX,
    "archive": b"NOT " + _GMAIL_INBOX + b" NOT " + _GMAIL_SENT,
}


class UnsafeSearchValue(ValueError):  # noqa: N818  # name fixed by the WP13a interface
    """A value or criteria the builder refuses. The message is a fixed text; it never carries the refused value."""


def search_value_ok(value: str) -> bool:
    """Whether `value` can go out as a search value: non-empty printable ASCII without `"` and `\\`, and without
    `X-GM-RAW` in any case, so that a value the criteria check would refuse is refused on its own (D80)."""
    return type(value) is str and _QUOTABLE.fullmatch(value) is not None and "X-GM-RAW" not in value.upper()


def quoted(value: str) -> bytes:
    """`value` as one IMAP quoted string, for values that pass `search_value_ok`: an empty value would match every
    message with the header, and nothing is escaped."""
    if not search_value_ok(value):
        raise UnsafeSearchValue("search value refused")
    return b'"' + value.encode("ascii") + b'"'


def msg_id_token(value: str) -> bytes:
    """The bare msg-id `value` as the quoted bracketed token `"<value>"`, for HEADER searches on the msg-id fields.
    The bare value must pass `search_value_ok` and hold no `<` or `>`, so none of `<`, `>`, `"`, `\\` reaches the wire
    from the value; the brackets add two bytes and need no escaping."""
    if not search_value_ok(value) or "<" in value or ">" in value:
        raise UnsafeSearchValue("search value refused")
    return b'"<' + value.encode("ascii") + b'>"'


def imap_date(day: date) -> bytes:
    """RFC 3501 `date` with fixed English month names (never the locale's): `05-Mar-2026`."""
    return b"%02d-%s-%04d" % (day.day, _MONTHS[day.month - 1], day.year)


def window_keys(*, since_day: date, before_day: date | None, unread_only: bool) -> bytes:
    keys = [_NOT_DRAFT]
    if unread_only:
        keys.append(b"UNSEEN")
    keys.append(b"SINCE " + imap_date(since_day))
    if before_day is not None:
        keys.append(b"BEFORE " + imap_date(before_day))
    return b" ".join(keys)


def gmail_label_keys(kind: GmailLabelFilter) -> bytes:
    keys = _GMAIL_LABEL_KEYS.get(kind) if type(kind) is str else None
    if keys is None:
        raise ValueError("unknown Gmail label filter")
    return keys


def _leaves(fields: Sequence[HeaderField], values: Sequence[str]) -> list[bytes]:
    """`HEADER <field> "<<value>>"` keys with the value as a bracketed msg-id token (`msg_id_token`), value by
    value and field by field within a value."""
    if isinstance(fields, (str, bytes)) or isinstance(values, (str, bytes)):
        raise ValueError("fields and values must be sequences, not one string")
    field_list = list(fields)
    if not field_list:
        raise ValueError("no header fields")
    for field in field_list:
        if type(field) is not str or field not in _HEADER_FIELDS:
            raise ValueError("unknown header field")
    return [b"HEADER " + field.encode("ascii") + b" " + msg_id_token(value) for value in values for field in field_list]


def _or_tree(leaves: Sequence[bytes]) -> bytes:
    """Balanced tree (I10): the left side takes the first ceil(n/2) leaves; a side with more than one leaf is
    parenthesised. The depth grows with log2(n), not n."""
    if len(leaves) == 1:
        return leaves[0]
    middle = (len(leaves) + 1) // 2
    sides = [
        _or_tree(side) if len(side) == 1 else b"(" + _or_tree(side) + b")"
        for side in (leaves[:middle], leaves[middle:])
    ]
    return b"OR " + sides[0] + b" " + sides[1]


def header_or(fields: Sequence[HeaderField], values: Sequence[str]) -> bytes:
    leaves = _leaves(fields, values)
    if not leaves:
        raise ValueError("no search values")
    return _or_tree(leaves)


def _or_tree_size(leaf_bytes: int, leaf_count: int) -> int:
    """`len(_or_tree(leaves))` from the leaves' total size and count: each of the n - 1 ORs adds `OR ` and one
    space, and each of its n - 2 non-root ORs is a parenthesised side."""
    return leaf_bytes + 4 * (leaf_count - 1) + 2 * max(leaf_count - 2, 0)


def or_batches(
    values: Sequence[str], fields: Sequence[HeaderField], *, max_values: int, max_bytes: int = MAX_OR_BYTES
) -> list[list[str]]:
    """Consecutive batches in input order; each holds at most `max_values` values and its `header_or` at most
    `max_bytes` bytes, the two brackets of every value's msg-id token included. A batch is closed when the next
    value would break either cap.

    Raises `UnsafeSearchValue` at the first value that fails `search_value_ok` (or whose own leaves exceed
    `max_bytes`). Callers with third-party values filter them one by one with `search_value_ok` first, so that one
    bad value does not cost the whole search."""
    if type(max_values) is not int or max_values < 1:
        raise ValueError("max_values must be positive")
    if type(max_bytes) is not int or max_bytes < 1:
        raise ValueError("max_bytes must be positive")
    if isinstance(fields, (str, bytes)) or isinstance(values, (str, bytes)):
        raise ValueError("fields and values must be sequences, not one string")
    field_list = list(fields)  # one snapshot: every value's leaves use the same fields
    batches: list[list[str]] = []
    batch: list[str] = []
    batch_bytes = batch_leaves = 0
    for value in values:
        leaves = _leaves(field_list, [value])
        value_bytes = sum(len(leaf) for leaf in leaves)
        if _or_tree_size(value_bytes, len(leaves)) > max_bytes:
            raise UnsafeSearchValue("search value refused")
        if batch and (
            len(batch) >= max_values or _or_tree_size(batch_bytes + value_bytes, batch_leaves + len(leaves)) > max_bytes
        ):
            batches.append(batch)
            batch, batch_bytes, batch_leaves = [], 0, 0
        batch.append(value)
        batch_bytes += value_bytes
        batch_leaves += len(leaves)
    if batch:
        batches.append(batch)
    return batches


def criteria(*parts: bytes) -> bytes:
    """The parts joined by single spaces. Every search excludes drafts, so the criteria must start with `NOT DRAFT`.
    The result must be printable ASCII without `X-GM-RAW` (D80) and must not end in a literal marker."""
    if not parts or any(type(part) is not bytes or not part for part in parts):
        raise UnsafeSearchValue("search criteria refused")
    joined = b" ".join(parts)
    if not (joined == _NOT_DRAFT or joined.startswith(_NOT_DRAFT + b" ")) or not search_criteria_ok(joined):
        raise UnsafeSearchValue("search criteria refused")
    return joined
