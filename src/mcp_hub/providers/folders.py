"""Per-account mail folder map, roles and the D75 id binding (spec 080 §5.5, §6.5).

IMAP: one `LIST "" "*"` per build; roles come from the full response (special-use attributes, else the iCloud name
fallback; on Gmail, detected by `X-GM-EXT-1`, only the registry inbox, `\\All`, `\\Trash` and, without `\\All`,
`\\Sent`). Junk and drafts are excluded by attribute and by the last hierarchy component (fail closed), and so is
every name that is not canonical modified UTF-7 in printable ASCII. Graph: the ids of the well-known folders.

The map holds names, attributes and ids only, never message data; it lives in process memory, is never logged
(folder names and Graph folder ids are third-party data) and is lost on restart.
"""

import logging
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Literal

from imapclient import imap_utf7

from mcp_hub.ids import mailbox_name_ok
from mcp_hub.logging import log_event

MailFolder = Literal["inbox", "sent", "archive", "trash", "other"]
ListFolder = Literal["inbox", "sent", "archive", "trash", "all"]
GmailLabelFilter = Literal["sent", "archive"]
FolderRole = Literal["inbox", "sent", "archive", "trash", "all", "other", "excluded"]
FOLDER_MAP_TTL_SECONDS: Final = 3600.0
FORCED_REBUILD_INTERVAL_SECONDS: Final = 60.0  # Sec16: LIST amplification by forged ids
MAX_OTHER_MAILBOXES: Final = 500
MAX_LOCATE_FOLDERS: Final = 30  # confirmed or lowered by the T1 probe (F2)
NAME_FALLBACK: Final = {"sent": "Sent Messages", "archive": "Archive", "trash": "Deleted Messages"}
EXCLUDED_LAST_NAMES: Final = frozenset({"junk", "spam", "drafts"})  # last hierarchy component, casefold (S-b)

_log = logging.getLogger("mcp_hub.providers.folders")
_INBOX: Final = "INBOX"
_NOT_SELECTABLE: Final = frozenset({"\\noselect", "\\nonexistent"})
_JUNK_OR_DRAFTS: Final = frozenset({"\\junk", "\\drafts"})
_VIRTUAL: Final = frozenset({"\\all", "\\flagged", "\\important"})  # excluded on servers other than Gmail
_ROLE_ATTRIBUTE: Final[tuple[tuple[Literal["sent", "archive", "trash"], str], ...]] = (
    ("sent", "\\sent"),
    ("archive", "\\archive"),
    ("trash", "\\trash"),
)
_GMAIL_ROLE_ATTRIBUTE: Final[tuple[tuple[Literal["all", "trash", "sent"], str], ...]] = (
    ("all", "\\all"),
    ("trash", "\\trash"),
    ("sent", "\\sent"),
)
# Roles an id may name. The role rules already leave Gmail only inbox, `all`, trash and (without `\All`) sent (B3).
_BOUND_ROLES: Final = frozenset({"inbox", "sent", "archive", "trash", "all", "other"})


@dataclass(frozen=True, slots=True)
class FolderEntry:
    raw_name: str  # the name on the wire (modified UTF-7, or as listed when excluded); used in ids
    display_name: str  # decoded; third-party text for every folder but the registry inbox
    attributes: frozenset[str]
    role: FolderRole
    from_int: bool = False  # IMAPClient parsed the name as an int: the exact wire text is lost (Sec7)

    @property
    def id_safe(self) -> bool:
        return mailbox_name_ok(self.raw_name) and not self.from_int


@dataclass(frozen=True, slots=True)
class FolderPlan:
    entry: FolderEntry  # the mailbox to EXAMINE
    gmail_labels: GmailLabelFilter | None  # Gmail All Mail label keys for the search, if any
    fixed_folder: MailFolder | None  # the folder of every item found; None: decided per message by X-GM-LABELS


@dataclass(frozen=True, slots=True)
class FolderMap:
    account: str
    gmail: bool
    inbox: FolderEntry  # the registry inbox, always present
    entries: tuple[FolderEntry, ...]  # LIST order, one per raw name: every excluded one, the roles, ≤ 500 `other`
    capped: bool

    def lookup(self, raw_name: str) -> FolderEntry | None:
        if raw_name == self.inbox.raw_name:
            return self.inbox
        for entry in self.entries:
            if entry.raw_name == raw_name:
                return entry
        return None

    def role(self, role: FolderRole) -> FolderEntry | None:
        if role == "inbox":
            return self.inbox
        for entry in self.entries:
            if entry.role == role:
                return entry
        return None

    def binding(self, raw_name: str) -> Literal["allowed", "refused", "unknown"]:
        """D75: may an IMAP id name this mailbox? Refused when ANY listing of the name is excluded (Sec7)."""
        if raw_name == self.inbox.raw_name:
            return "allowed"  # the registry inbox passes without LIST (§5.5)
        matches = [entry for entry in self.entries if entry.raw_name == raw_name]
        if not matches:
            return "unknown"
        if all(entry.role in _BOUND_ROLES and entry.id_safe for entry in matches):
            return "allowed"
        return "refused"

    def list_plan(self, folder: ListFolder) -> list[FolderPlan]:
        """The mailboxes a list over `folder` examines, in order (§5.2: `all` = inbox, archive, sent; never trash)."""
        if folder == "inbox":
            return [FolderPlan(self.inbox, None, "inbox")]
        if self.gmail:
            return self._gmail_plan(folder)
        if folder == "all":
            return [plan for name in ("inbox", "archive", "sent") for plan in self._fixed(name)]
        return self._fixed(folder)

    def locate_order(self) -> tuple[list[FolderEntry], list[FolderEntry]]:
        """(core, other) for locating refs (spec §5.5 matching rule, C3).

        core: inbox, archive, sent, trash (Gmail: All Mail, Trash; without `\\All`: inbox, `\\Sent`, `\\Trash`),
        always searched; other: the `other` mailboxes in LIST order, searched only for refs not found in a core
        folder. Raw names are unique in the map, so no mailbox appears twice.
        """
        if self.gmail:
            names: tuple[FolderRole, ...] = ("all", "trash") if self.role("all") else ("inbox", "sent", "trash")
        else:
            names = ("inbox", "archive", "sent", "trash")
        core = [entry for name in names if (entry := self.role(name)) is not None]
        return core, [entry for entry in self.entries if entry.role == "other"]

    def _fixed(self, folder: MailFolder) -> list[FolderPlan]:
        entry = self.role(folder)
        return [] if entry is None else [FolderPlan(entry, None, folder)]

    def _gmail_plan(self, folder: ListFolder) -> list[FolderPlan]:
        all_mail = self.role("all")
        if all_mail is None:  # All Mail hidden from IMAP: inbox, \Sent and \Trash only (§5.5)
            if folder == "all":
                return [FolderPlan(self.inbox, None, "inbox"), *self._fixed("sent")]
            return [] if folder == "archive" else self._fixed(folder)
        if folder == "sent":
            return [FolderPlan(all_mail, "sent", "sent")]
        if folder == "archive":
            return [FolderPlan(all_mail, "archive", "archive")]
        if folder == "all":
            return [FolderPlan(all_mail, None, None)]
        return self._fixed(folder)


def inbox_entry(registry_inbox: str) -> FolderEntry:
    raw = imap_utf7.encode(registry_inbox).decode("ascii")
    return FolderEntry(raw_name=raw, display_name=registry_inbox, attributes=frozenset(), role="inbox")


def is_gmail(capabilities: Iterable[bytes]) -> bool:
    return any(capability.upper() == b"X-GM-EXT-1" for capability in capabilities)


@dataclass(slots=True)
class _Listed:
    raw_name: str
    display_name: str
    attributes: frozenset[str]
    from_int: bool
    role: FolderRole | None = None  # None until classified


def _decode_name(name: bytes | str | int) -> tuple[str, str | None, bool]:
    """(raw name, decoded name or None when not canonical modified UTF-7 in printable ASCII, from_int)."""
    if isinstance(name, int):
        raw = str(name)
        return raw, raw, True
    raw = name.decode("latin-1") if isinstance(name, bytes) else name
    if not raw or not all(0x20 <= ord(c) <= 0x7E for c in raw):
        return raw, None, False
    # IMAPClient's decoder is lenient ("&Jjo", "a&"); only a canonical round trip counts as modified UTF-7 (I7).
    try:
        decoded: str = imap_utf7.decode(raw.encode("ascii"))
        canonical = imap_utf7.encode(decoded) == raw.encode("ascii")
    except ValueError:  # binascii.Error and the Unicode errors (a lone surrogate) are ValueErrors
        return raw, None, False
    return (raw, decoded, False) if canonical else (raw, None, False)


def _last_component_excluded(name: str, delimiter: str | None) -> bool:
    last = name.rsplit(delimiter, 1)[-1] if delimiter else name
    return last.casefold() in EXCLUDED_LAST_NAMES


def _base_excluded(item: _Listed, decoded: str | None, delimiter: str | None, *, gmail: bool) -> bool:
    if decoded is None:
        return True
    lowered = _lower(item)
    if lowered & (_NOT_SELECTABLE | _JUNK_OR_DRAFTS):
        return True
    if _last_component_excluded(item.raw_name, delimiter) or _last_component_excluded(decoded, delimiter):
        return True
    return not gmail and bool(lowered & _VIRTUAL)


def _is_registry_inbox(raw_name: str, inbox_raw: str) -> bool:
    # RFC 3501 5.1: the name INBOX is case-insensitive
    return raw_name == inbox_raw or (inbox_raw.upper() == _INBOX and raw_name.upper() == _INBOX)


def _pick(candidates: list[_Listed], fallback: str | None) -> _Listed:
    """Tie-break among several mailboxes for one role: the fallback name first, else the lowest raw name."""
    preferred = [item for item in candidates if fallback is not None and item.display_name == fallback]
    return min(preferred or candidates, key=lambda item: item.raw_name)


def _assign_roles(items: list[_Listed], *, gmail: bool) -> None:
    open_items = [item for item in items if item.role is None]
    if gmail:
        for gmail_role, attribute in _GMAIL_ROLE_ATTRIBUTE:
            if gmail_role == "sent" and any(item.role == "all" for item in items):
                continue
            holders = [item for item in open_items if item.role is None and attribute in _lower(item)]
            if holders:
                _pick(holders, None).role = gmail_role
        for item in open_items:
            if item.role is None:
                item.role = "excluded"  # labels, \Sent next to \All, losing duplicates: never read (R5)
        return
    for role, attribute in _ROLE_ATTRIBUTE:
        holders = [item for item in open_items if item.role is None and attribute in _lower(item)]
        if not any(attribute in _lower(item) for item in items):  # fallback only when no mailbox has the attribute
            holders = [item for item in open_items if item.role is None and item.display_name == NAME_FALLBACK[role]]
        if holders:
            _pick(holders, NAME_FALLBACK[role]).role = role


def _lower(item: _Listed) -> frozenset[str]:
    return frozenset(attribute.lower() for attribute in item.attributes)


def build_imap_folder_map(
    account: str,
    registry_inbox: str,
    *,
    gmail: bool,
    listing: Sequence[tuple[Sequence[bytes], bytes | None, bytes | str | int]],
) -> FolderMap:
    inbox = inbox_entry(registry_inbox)
    # One item per raw name, at its first LIST position; repeated listings add their attributes, and one excluded
    # listing excludes the name (Sec7: fail closed). Exclusion is decided per listing, before the merge.
    by_name: dict[str, _Listed] = {}
    tainted: set[str] = set()
    for attributes, delimiter_bytes, name in listing:
        raw, decoded, from_int = _decode_name(name)
        delimiter = delimiter_bytes.decode("latin-1") if delimiter_bytes else None
        item = _Listed(
            raw_name=raw,
            display_name=decoded if decoded is not None else raw,
            attributes=frozenset(attribute.decode("latin-1") for attribute in attributes),
            from_int=from_int,
        )
        if _base_excluded(item, decoded, delimiter, gmail=gmail):
            tainted.add(raw)
        first = by_name.setdefault(raw, item)
        if first is not item:
            first.attributes |= item.attributes
            first.from_int = first.from_int or item.from_int
    items = list(by_name.values())
    for item in items:
        if item.raw_name in tainted:
            item.role = "excluded"
        elif _is_registry_inbox(item.raw_name, inbox.raw_name):
            item.role = "inbox"
    _assign_roles(items, gmail=gmail)
    entries: list[FolderEntry] = []
    others = 0
    for item in items:
        role: FolderRole = item.role if item.role is not None else "other"
        if role == "other":
            others += 1
            if others > MAX_OTHER_MAILBOXES:
                continue
        entries.append(FolderEntry(item.raw_name, item.display_name, item.attributes, role, item.from_int))
    return FolderMap(account, gmail, inbox, tuple(entries), capped=others > MAX_OTHER_MAILBOXES)


GraphWellKnown = Literal["inbox", "sentitems", "archive", "deleteditems", "drafts", "junkemail", "outbox"]
GRAPH_WELL_KNOWN: Final[tuple[GraphWellKnown, ...]] = (
    "inbox",
    "sentitems",
    "archive",
    "deleteditems",
    "drafts",
    "junkemail",
    "outbox",
)
_GRAPH_EXCLUDED: Final[tuple[GraphWellKnown, ...]] = ("drafts", "junkemail", "outbox")
_GRAPH_ROLE: Final[tuple[tuple[GraphWellKnown, MailFolder], ...]] = (
    ("inbox", "inbox"),
    ("sentitems", "sent"),
    ("archive", "archive"),
    ("deleteditems", "trash"),
)


@dataclass(frozen=True, slots=True)
class GraphFolderIds:
    account: str
    ids: Mapping[GraphWellKnown, str | None]  # None: the account has no such folder (404)

    def role_of(self, parent_id: str | None) -> MailFolder | Literal["excluded"]:
        """The folder of a message by its `parentFolderId`; junk, drafts and outbox (and no parent) are excluded."""
        if parent_id is None:
            return "excluded"
        if any(self.ids.get(name) == parent_id for name in _GRAPH_EXCLUDED):
            return "excluded"
        for name, folder in _GRAPH_ROLE:
            if self.ids.get(name) == parent_id:
                return folder
        return "other"  # a child folder of an excluded folder is not recognised (accepted residual, §5.5)


class FolderMapCache:
    """Folder maps and Graph folder ids per account, reused for at most `ttl` seconds (monotonic clock, R8)."""

    def __init__(self, ttl: float = FOLDER_MAP_TTL_SECONDS, clock: Callable[[], float] = time.monotonic) -> None:
        self._ttl = ttl
        self._clock = clock
        self._lock = threading.Lock()
        self._values: dict[str, tuple[float, FolderMap | GraphFolderIds]] = {}
        self._forced: dict[str, float] = {}

    def get(self, account: str) -> FolderMap | GraphFolderIds | None:
        with self._lock:
            cached = self._values.get(account)
            if cached is None:
                return None
            stamp, value = cached
            if self._clock() - stamp >= self._ttl:
                del self._values[account]
                return None
            return value

    def put(self, value: FolderMap | GraphFolderIds) -> None:
        with self._lock:
            self._values[value.account] = (self._clock(), value)
        if isinstance(value, FolderMap):
            count = len(value.entries)
            outcome = "partial" if value.capped else "ok"
        else:
            count = sum(1 for folder_id in value.ids.values() if folder_id is not None)
            outcome = "ok"
        log_event(
            _log,
            logging.INFO,
            "folder_map_refreshed",
            account=value.account,
            capability="mail",
            result_count=count,
            outcome=outcome,
        )

    def may_force_rebuild(self, account: str) -> bool:
        """True at most once per 60 s per account; a True answer starts the next window (Sec16)."""
        with self._lock:
            now = self._clock()
            last = self._forced.get(account)
            if last is not None and now - last < FORCED_REBUILD_INTERVAL_SECONDS:
                return False
            self._forced[account] = now
            return True

    def clear(self) -> None:
        with self._lock:
            self._values.clear()
            self._forced.clear()


FOLDER_MAPS: Final = FolderMapCache()
