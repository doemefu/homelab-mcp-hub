"""Folder map, roles, D75 binding and cache (spec 080 §5.5, §6.5; WP13a T9)."""

import json
import logging
from collections.abc import Iterator, Sequence

import pytest

from mcp_hub.providers.folders import (
    EXCLUDED_LAST_NAMES,
    FOLDER_MAP_TTL_SECONDS,
    FORCED_REBUILD_INTERVAL_SECONDS,
    GRAPH_WELL_KNOWN,
    MAX_LOCATE_FOLDERS,
    MAX_OTHER_MAILBOXES,
    NAME_FALLBACK,
    FolderEntry,
    FolderMap,
    FolderMapCache,
    FolderPlan,
    GraphFolderIds,
    build_imap_folder_map,
    inbox_entry,
    is_gmail,
)
from tests.support.clock import FakeClock
from tests.support.logcapture import Capture

Listing = list[tuple[tuple[bytes, ...], bytes | None, bytes | str | int]]

ICLOUD: Listing = [
    ((b"\\HasNoChildren",), b"/", b"INBOX"),
    ((b"\\HasNoChildren", b"\\Sent"), b"/", b"Sent Messages"),
    ((b"\\HasNoChildren",), b"/", b"Projects"),
    ((b"\\HasNoChildren", b"\\Archive"), b"/", b"Archive"),
    ((b"\\HasNoChildren", b"\\Trash"), b"/", b"Deleted Messages"),
    ((b"\\HasNoChildren", b"\\Junk"), b"/", b"Junk"),
    ((b"\\HasNoChildren", b"\\Drafts"), b"/", b"Drafts"),
    ((b"\\HasNoChildren",), b"/", b"Receipts"),
]

GMAIL_EN: Listing = [
    ((b"\\HasNoChildren",), b"/", b"INBOX"),
    ((b"\\HasNoChildren",), b"/", b"Receipts"),
    ((b"\\HasChildren", b"\\Noselect"), b"/", b"[Gmail]"),
    ((b"\\All", b"\\HasNoChildren"), b"/", b"[Gmail]/All Mail"),
    ((b"\\Drafts", b"\\HasNoChildren"), b"/", b"[Gmail]/Drafts"),
    ((b"\\HasNoChildren", b"\\Important"), b"/", b"[Gmail]/Important"),
    ((b"\\HasNoChildren", b"\\Sent"), b"/", b"[Gmail]/Sent Mail"),
    ((b"\\HasNoChildren", b"\\Junk"), b"/", b"[Gmail]/Spam"),
    ((b"\\Flagged", b"\\HasNoChildren"), b"/", b"[Gmail]/Starred"),
    ((b"\\HasNoChildren", b"\\Trash"), b"/", b"[Gmail]/Trash"),
]

GMAIL_DE: Listing = [
    ((b"\\HasNoChildren",), b"/", b"INBOX"),
    ((b"\\HasNoChildren",), b"/", b"Belege"),
    ((b"\\HasChildren", b"\\Noselect"), b"/", b"[Gmail]"),
    ((b"\\All", b"\\HasNoChildren"), b"/", b"[Gmail]/Alle Nachrichten"),
    ((b"\\Drafts", b"\\HasNoChildren"), b"/", b"[Gmail]/Entw&APw-rfe"),
    ((b"\\HasNoChildren", b"\\Sent"), b"/", b"[Gmail]/Gesendet"),
    ((b"\\HasNoChildren", b"\\Important"), b"/", b"[Gmail]/Wichtig"),
    ((b"\\Flagged", b"\\HasNoChildren"), b"/", b"[Gmail]/Markiert"),
    ((b"\\HasNoChildren", b"\\Trash"), b"/", b"[Gmail]/Papierkorb"),
    ((b"\\HasNoChildren", b"\\Junk"), b"/", b"[Gmail]/Spam"),
]


def imap_map(listing: Listing, *, gmail: bool = False, inbox: str = "INBOX", account: str = "icloud") -> FolderMap:
    return build_imap_folder_map(account, inbox, gmail=gmail, listing=listing)


def roles(folder_map: FolderMap) -> dict[str, str]:
    return {e.raw_name: e.role for e in folder_map.entries}


def raw_names(entries: Sequence[FolderEntry]) -> list[str]:
    return [e.raw_name for e in entries]


def plan_view(plans: list[FolderPlan]) -> list[tuple[str, str | None, str | None]]:
    return [(p.entry.raw_name, p.gmail_labels, p.fixed_folder) for p in plans]


@pytest.fixture
def folder_log() -> Iterator[Capture]:
    handler = Capture()
    logger = logging.getLogger("mcp_hub.providers")
    level = logger.level
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    yield handler
    logger.removeHandler(handler)
    logger.setLevel(level)


def test_constants() -> None:
    assert FOLDER_MAP_TTL_SECONDS == 3600.0
    assert FORCED_REBUILD_INTERVAL_SECONDS == 60.0
    assert MAX_OTHER_MAILBOXES == 500
    assert MAX_LOCATE_FOLDERS == 30
    assert NAME_FALLBACK == {"sent": "Sent Messages", "archive": "Archive", "trash": "Deleted Messages"}
    assert frozenset({"junk", "spam", "drafts"}) == EXCLUDED_LAST_NAMES
    assert GRAPH_WELL_KNOWN == ("inbox", "sentitems", "archive", "deleteditems", "drafts", "junkemail", "outbox")


def test_icloud_special_use_roles() -> None:
    folder_map = imap_map(ICLOUD)
    assert folder_map.gmail is False
    assert folder_map.capped is False
    assert roles(folder_map) == {
        "INBOX": "inbox",
        "Sent Messages": "sent",
        "Projects": "other",
        "Archive": "archive",
        "Deleted Messages": "trash",
        "Junk": "excluded",
        "Drafts": "excluded",
        "Receipts": "other",
    }
    assert folder_map.inbox.raw_name == "INBOX"
    assert folder_map.inbox.role == "inbox"
    for role, name in (("sent", "Sent Messages"), ("archive", "Archive"), ("trash", "Deleted Messages")):
        entry = folder_map.role(role)
        assert entry is not None
        assert entry.raw_name == name
    assert folder_map.role("all") is None
    assert plan_view(folder_map.list_plan("inbox")) == [("INBOX", None, "inbox")]
    assert plan_view(folder_map.list_plan("sent")) == [("Sent Messages", None, "sent")]
    assert plan_view(folder_map.list_plan("archive")) == [("Archive", None, "archive")]
    assert plan_view(folder_map.list_plan("trash")) == [("Deleted Messages", None, "trash")]
    # all = inbox + archive + sent in the dedup order of spec §5.2, never trash
    assert plan_view(folder_map.list_plan("all")) == [
        ("INBOX", None, "inbox"),
        ("Archive", None, "archive"),
        ("Sent Messages", None, "sent"),
    ]


def test_icloud_name_fallback_only() -> None:
    listing: Listing = [
        ((), b"/", b"INBOX"),
        ((), b"/", b"Sent Messages"),
        ((), b"/", b"Archive"),
        ((), b"/", b"Deleted Messages"),
        ((), b"/", b"sent messages"),  # the fallback compares the full decoded name exactly
        ((), b"/", b"Old/Archive"),
    ]
    folder_map = imap_map(listing)
    assert roles(folder_map) == {
        "INBOX": "inbox",
        "Sent Messages": "sent",
        "Archive": "archive",
        "Deleted Messages": "trash",
        "sent messages": "other",
        "Old/Archive": "other",
    }


def test_fallback_skipped_when_attribute_present() -> None:
    listing: Listing = [
        ((), b"/", b"INBOX"),
        ((), b"/", b"Sent Messages"),
        ((b"\\Sent",), b"/", b"Outbox Copies"),
        ((), b"/", b"Archive"),
        ((b"\\Archive",), b"/", b"Kept"),
        ((), b"/", b"Deleted Messages"),
    ]
    folder_map = imap_map(listing)
    assert roles(folder_map) == {
        "INBOX": "inbox",
        "Sent Messages": "other",
        "Outbox Copies": "sent",
        "Archive": "other",
        "Kept": "archive",
        "Deleted Messages": "trash",  # no \Trash anywhere: the fallback still applies for this role
    }
    # an excluded mailbox with the attribute still counts: "no mailbox of the account carries" it (§5.5)
    hidden = imap_map(
        [((), b"/", b"INBOX"), ((b"\\Noselect", b"\\Sent"), b"/", b"Parent"), ((), b"/", b"Sent Messages")]
    )
    assert roles(hidden) == {"INBOX": "inbox", "Parent": "excluded", "Sent Messages": "other"}


def test_duplicate_role_tie_break() -> None:
    listing: Listing = [
        ((), b"/", b"INBOX"),
        ((b"\\Sent",), b"/", b"Gesendet"),
        ((b"\\Sent",), b"/", b"Sent Messages"),
        ((b"\\Archive",), b"/", b"B"),
        ((b"\\Archive",), b"/", b"A"),
    ]
    folder_map = imap_map(listing)
    assert roles(folder_map) == {
        "INBOX": "inbox",
        "Gesendet": "other",
        "Sent Messages": "sent",
        "B": "other",
        "A": "archive",
    }


@pytest.mark.parametrize(
    "attribute",
    [b"\\Junk", b"\\Drafts", b"\\JUNK", b"\\drafts"],
)
def test_every_junk_and_drafts_attribute_excluded(attribute: bytes) -> None:
    listing: Listing = [
        ((), b"/", b"INBOX"),
        ((attribute,), b"/", b"Box One"),
        ((attribute,), b"/", b"Box Two"),
        ((attribute, b"\\Sent"), b"/", b"Sent Messages"),
        ((attribute,), b"/", b"Archive"),
    ]
    folder_map = imap_map(listing)
    assert roles(folder_map) == {
        "INBOX": "inbox",
        "Box One": "excluded",
        "Box Two": "excluded",
        "Sent Messages": "excluded",
        "Archive": "excluded",
    }
    assert folder_map.role("sent") is None
    assert folder_map.role("archive") is None
    assert folder_map.list_plan("sent") == []


def test_hierarchy_last_component_exclusion() -> None:
    listing: Listing = [
        ((), b".", b"INBOX"),
        ((), b".", b"INBOX.Junk"),
        ((), b"/", b"INBOX/Drafts"),
        ((), b"/", b"Archiv/SPAM"),
        ((), b"/", b"Projects/spam"),
        ((), b"/", b"JunkMail"),
        ((), b"/", b"Junk/Old"),  # only the last component counts
        ((), b".", b"Spam/Kept"),  # "/" is not the delimiter here: the last component is the whole name
    ]
    folder_map = imap_map(listing)
    assert roles(folder_map) == {
        "INBOX": "inbox",
        "INBOX.Junk": "excluded",
        "INBOX/Drafts": "excluded",
        "Archiv/SPAM": "excluded",
        "Projects/spam": "excluded",
        "JunkMail": "other",
        "Junk/Old": "other",
        "Spam/Kept": "other",
    }
    assert folder_map.binding("INBOX.Junk") == "refused"
    assert folder_map.binding("Archiv/SPAM") == "refused"


def test_hierarchy_last_component_beats_role_attribute() -> None:
    listing: Listing = [((), b"/", b"INBOX"), ((b"\\Archive",), b"/", b"Mail/Spam"), ((b"\\Sent",), b"/", b"Drafts")]
    folder_map = imap_map(listing)
    assert roles(folder_map) == {"INBOX": "inbox", "Mail/Spam": "excluded", "Drafts": "excluded"}
    assert folder_map.role("archive") is None
    assert folder_map.role("sent") is None


def test_none_delimiter() -> None:
    listing: Listing = [
        ((), None, b"INBOX"),
        ((), None, b"Spam"),
        ((), None, b"Mail/Spam"),  # no delimiter: the whole name is the last component
        ((), None, b"Sent Messages"),
    ]
    folder_map = imap_map(listing)
    assert roles(folder_map) == {
        "INBOX": "inbox",
        "Spam": "excluded",
        "Mail/Spam": "other",
        "Sent Messages": "sent",
    }


def test_no_roles_offers_inbox_and_other() -> None:
    listing: Listing = [((), b"/", b"INBOX"), ((), b"/", b"Work"), ((), b"/", b"Family")]
    folder_map = imap_map(listing)
    assert roles(folder_map) == {"INBOX": "inbox", "Work": "other", "Family": "other"}
    for role in ("sent", "archive", "trash", "all"):
        assert folder_map.role(role) is None
    assert folder_map.list_plan("sent") == []
    assert folder_map.list_plan("archive") == []
    assert folder_map.list_plan("trash") == []
    assert plan_view(folder_map.list_plan("all")) == [("INBOX", None, "inbox")]
    core, other = folder_map.locate_order()
    assert raw_names(core) == ["INBOX"]
    assert raw_names(other) == ["Work", "Family"]


def test_virtual_mailboxes_excluded_on_non_gmail() -> None:
    listing: Listing = [
        ((), b"/", b"INBOX"),
        ((b"\\All",), b"/", b"All Mail"),
        ((b"\\Flagged",), b"/", b"Flagged"),
        ((b"\\Important",), b"/", b"Important"),
        ((b"\\Noselect",), b"/", b"Parent"),
        ((b"\\NonExistent",), b"/", b"Gone"),
        ((b"\\Noselect", b"\\Sent"), b"/", b"Sent Messages"),
    ]
    folder_map = imap_map(listing)
    assert roles(folder_map) == {
        "INBOX": "inbox",
        "All Mail": "excluded",
        "Flagged": "excluded",
        "Important": "excluded",
        "Parent": "excluded",
        "Gone": "excluded",
        "Sent Messages": "excluded",
    }
    assert folder_map.list_plan("sent") == []
    _, other = folder_map.locate_order()
    assert other == []


@pytest.mark.parametrize(
    ("listing", "all_mail", "sent", "trash", "label"),
    [
        (GMAIL_EN, "[Gmail]/All Mail", "[Gmail]/Sent Mail", "[Gmail]/Trash", "Receipts"),
        (GMAIL_DE, "[Gmail]/Alle Nachrichten", "[Gmail]/Gesendet", "[Gmail]/Papierkorb", "Belege"),
    ],
)
def test_gmail_english_and_german_names(listing: Listing, all_mail: str, sent: str, trash: str, label: str) -> None:
    folder_map = imap_map(listing, gmail=True, account="gmail")
    assert folder_map.gmail is True
    allowed = {k for k, v in roles(folder_map).items() if v != "excluded"}
    assert allowed == {"INBOX", all_mail, trash}
    assert roles(folder_map)[all_mail] == "all"
    assert roles(folder_map)[trash] == "trash"
    assert roles(folder_map)[sent] == "excluded"  # \Sent is used only when there is no \All
    assert roles(folder_map)[label] == "excluded"
    assert plan_view(folder_map.list_plan("inbox")) == [("INBOX", None, "inbox")]
    assert plan_view(folder_map.list_plan("sent")) == [(all_mail, "sent", "sent")]
    assert plan_view(folder_map.list_plan("archive")) == [(all_mail, "archive", "archive")]
    assert plan_view(folder_map.list_plan("all")) == [(all_mail, None, None)]
    assert plan_view(folder_map.list_plan("trash")) == [(trash, None, "trash")]
    assert folder_map.binding("INBOX") == "allowed"
    assert folder_map.binding(all_mail) == "allowed"
    assert folder_map.binding(trash) == "allowed"
    assert folder_map.binding(sent) == "refused"
    assert folder_map.binding(label) == "refused"
    assert folder_map.binding("[Gmail]/Spam") == "refused"


def test_gmail_labels_with_fallback_names_excluded() -> None:
    listing: Listing = [
        *GMAIL_EN,
        ((b"\\HasNoChildren",), b"/", b"Archive"),
        ((b"\\HasNoChildren",), b"/", b"Sent Messages"),
        ((b"\\HasNoChildren",), b"/", b"Deleted Messages"),
        ((b"\\HasNoChildren",), b"/", b"Junk"),
        ((b"\\HasNoChildren",), b"/", b"Work"),
    ]
    folder_map = imap_map(listing, gmail=True, account="gmail")
    for name in ("Archive", "Sent Messages", "Deleted Messages", "Junk", "Work"):
        assert roles(folder_map)[name] == "excluded"
        assert folder_map.binding(name) == "refused"
    assert plan_view(folder_map.list_plan("archive")) == [("[Gmail]/All Mail", "archive", "archive")]
    assert plan_view(folder_map.list_plan("sent")) == [("[Gmail]/All Mail", "sent", "sent")]


def test_gmail_without_all_mail() -> None:
    listing = [item for item in GMAIL_EN if b"\\All" not in item[0]]
    folder_map = imap_map(listing, gmail=True, account="gmail")
    allowed = {k: v for k, v in roles(folder_map).items() if v != "excluded"}
    assert allowed == {"INBOX": "inbox", "[Gmail]/Sent Mail": "sent", "[Gmail]/Trash": "trash"}
    assert plan_view(folder_map.list_plan("inbox")) == [("INBOX", None, "inbox")]
    assert plan_view(folder_map.list_plan("sent")) == [("[Gmail]/Sent Mail", None, "sent")]
    assert folder_map.list_plan("archive") == []
    assert plan_view(folder_map.list_plan("all")) == [("INBOX", None, "inbox"), ("[Gmail]/Sent Mail", None, "sent")]
    assert plan_view(folder_map.list_plan("trash")) == [("[Gmail]/Trash", None, "trash")]
    assert folder_map.binding("[Gmail]/Sent Mail") == "allowed"  # instead of \All
    assert folder_map.binding("Receipts") == "refused"
    core, other = folder_map.locate_order()
    assert raw_names(core) == ["INBOX", "[Gmail]/Sent Mail", "[Gmail]/Trash"]
    assert other == []


def test_binding() -> None:
    folder_map = imap_map(ICLOUD)
    for name in ("INBOX", "Sent Messages", "Archive", "Deleted Messages", "Projects", "Receipts"):
        assert folder_map.binding(name) == "allowed", name
    assert folder_map.binding("Junk") == "refused"
    assert folder_map.binding("Drafts") == "refused"
    assert folder_map.binding("Nowhere") == "unknown"
    assert folder_map.binding("sent messages") == "unknown"  # exact, case-sensitive raw names
    entry = folder_map.lookup("Projects")
    assert entry is not None
    assert (entry.raw_name, entry.role) == ("Projects", "other")
    assert folder_map.lookup("Nowhere") is None
    # the registry inbox is allowed even when the server does not list it
    unlisted = imap_map([((b"\\Sent",), b"/", b"Sent Messages")])
    assert unlisted.binding("INBOX") == "allowed"
    inbox = unlisted.lookup("INBOX")
    assert inbox is not None
    assert inbox.role == "inbox"


def test_binding_fails_closed_on_duplicate_raw_name() -> None:
    listing: Listing = [
        ((), b"/", b"INBOX"),
        ((b"\\Junk",), b"/", b"Shared"),
        ((), b"/", b"Shared"),
        ((b"\\Sent",), b"/", b"Sent Messages"),
        ((b"\\Drafts",), b"/", b"Sent Messages"),
    ]
    folder_map = imap_map(listing)
    assert folder_map.binding("Shared") == "refused"
    assert folder_map.binding("Sent Messages") == "refused"
    assert folder_map.role("sent") is None
    core, other = folder_map.locate_order()
    assert raw_names(core) == ["INBOX"]
    assert other == []


def test_repeated_listing_merges_into_one_entry() -> None:
    listing: Listing = [
        *GMAIL_EN,
        ((b"\\All",), b"/", b"[Gmail]/All Mail"),  # the same mailbox listed twice
        ((b"\\HasNoChildren",), b"/", b"[Gmail]/Trash"),
    ]
    folder_map = imap_map(listing, gmail=True, account="gmail")
    assert [e.raw_name for e in folder_map.entries].count("[Gmail]/All Mail") == 1
    assert folder_map.binding("[Gmail]/All Mail") == "allowed"
    assert folder_map.binding("[Gmail]/Trash") == "allowed"
    trash = folder_map.role("trash")
    assert trash is not None
    assert trash.attributes == frozenset({"\\HasNoChildren", "\\Trash"})
    icloud = imap_map([*ICLOUD, ((b"\\Sent",), b"/", b"Projects")])
    projects = icloud.lookup("Projects")
    assert projects is not None
    assert projects.role == "other"  # the attribute joins the first listing; "Sent Messages" still wins the tie-break
    assert projects.attributes == frozenset({"\\HasNoChildren", "\\Sent"})


def test_listing_name_types() -> None:
    # folder_encode = False: names arrive as raw bytes; digit atoms as int (lossless); str is taken as raw
    listing: Listing = [((), b"/", b"INBOX"), ((), b"/", 123), ((), b"/", "Gesch&AOQ-ftlich"), ((), b"/", b"Work")]
    folder_map = imap_map(listing)
    assert roles(folder_map) == {"INBOX": "inbox", "123": "other", "Gesch&AOQ-ftlich": "other", "Work": "other"}
    entry = folder_map.lookup("123")
    assert entry is not None
    assert entry.id_safe is True
    assert folder_map.binding("123") == "allowed"
    named = folder_map.lookup("Gesch&AOQ-ftlich")
    assert named is not None
    assert named.display_name == "Geschäftlich"


def test_other_cap_500_roles_from_full_list() -> None:
    listing: Listing = [((), b"/", b"INBOX")]
    listing += [((), b"/", f"Box {i:03d}".encode()) for i in range(600)]
    listing += [((b"\\Sent",), b"/", b"Sent Messages"), ((b"\\Junk",), b"/", b"Junk")]
    folder_map = imap_map(listing)
    assert folder_map.capped is True
    others = [e.raw_name for e in folder_map.entries if e.role == "other"]
    assert others == [f"Box {i:03d}" for i in range(500)]
    sent = folder_map.role("sent")
    assert sent is not None
    assert sent.raw_name == "Sent Messages"
    assert folder_map.binding("Junk") == "refused"
    assert folder_map.binding("Box 499") == "allowed"
    assert folder_map.binding("Box 500") == "unknown"
    assert imap_map(listing[:501]).capped is False


def test_excluded_cap_500() -> None:
    listing: Listing = [((), b"/", b"INBOX")]
    listing += [((b"\\Junk",), b"/", f"Spam {i:03d}".encode()) for i in range(600)]
    listing += [((b"\\Sent",), b"/", b"Sent Messages"), ((), b"/", b"Work")]
    folder_map = imap_map(listing)
    assert folder_map.capped is False  # only the `other` cap marks the map: no dropped name is ever searched
    excluded = [e.raw_name for e in folder_map.entries if e.role == "excluded"]
    assert excluded == [f"Spam {i:03d}" for i in range(500)]
    assert len(folder_map.entries) == 503  # inbox, 500 excluded, sent, one other
    assert folder_map.binding("Spam 499") == "refused"
    assert folder_map.binding("Spam 500") == "unknown"  # dropped: rate-limited rebuild, then not_found
    assert folder_map.binding("Sent Messages") == "allowed"
    assert folder_map.binding("Work") == "allowed"
    assert imap_map(listing[:501]).capped is False


@pytest.mark.parametrize(
    ("raw", "role"),
    [
        (b"x" * 200, "other"),
        (b"a\r\nb", "excluded"),
        (b"a\tb", "excluded"),
        (b"\x7f", "excluded"),
        (b"caf\xc3\xa9", "excluded"),
        (b"&Jjo", "excluded"),  # unterminated shift
        (b"a&", "excluded"),
        (b"&AEoAdQBuAGs-", "excluded"),  # "Junk" in a non-canonical shift (ASCII-only run)
        (b"&AOQ-&AOQ-", "excluded"),  # adjacent shifts
        (b"&AOQAYQ-", "excluded"),  # a printable character inside a shift
        (b"&AOQAJg-", "excluded"),
        (b"&AOQAIA-", "excluded"),
        (b"&2D0-", "excluded"),  # lone surrogate
        (b"", "excluded"),
        ("café", "excluded"),
    ],
)
def test_name_rules(raw: bytes | str, role: str) -> None:
    folder_map = imap_map([((), b"/", b"INBOX"), ((), b"/", raw)])
    [entry] = [e for e in folder_map.entries if e.raw_name != "INBOX"]
    assert entry.role == role
    if role == "other":
        assert entry.id_safe is True


def test_name_rules_201_characters_other_not_id_safe() -> None:
    folder_map = imap_map([((), b"/", b"INBOX"), ((), b"/", b"y" * 201)])
    entry = folder_map.lookup("y" * 201)
    assert entry is not None
    assert entry.role == "other"
    assert entry.id_safe is False
    assert folder_map.binding("y" * 201) == "refused"
    _, other = folder_map.locate_order()
    assert raw_names(other) == ["y" * 201]


def test_modified_utf7_display_name() -> None:
    folder_map = imap_map([((), b"/", b"INBOX"), ((), b"/", b"Gesch&AOQ-ftlich"), ((), b"/", b"R&-D")])
    entry = folder_map.lookup("Gesch&AOQ-ftlich")
    assert entry is not None
    assert (entry.display_name, entry.role, entry.id_safe) == ("Geschäftlich", "other", True)
    amp = folder_map.lookup("R&-D")
    assert amp is not None
    assert amp.display_name == "R&D"


def test_decoded_last_component_excluded() -> None:
    # U+017F LATIN SMALL LETTER LONG S casefolds to "s": the decoded name is checked as well (fail closed)
    folder_map = imap_map([((), b"/", b"INBOX"), ((), b"/", b"Mail/&AX8-pam")])
    entry = folder_map.lookup("Mail/&AX8-pam")
    assert entry is not None
    assert entry.role == "excluded"


def test_inbox_entry_takes_the_raw_registry_name() -> None:
    entry = inbox_entry("Eing&AOQ-nge")
    assert entry.raw_name == "Eing&AOQ-nge"
    assert (entry.display_name, entry.role, entry.id_safe) == ("Eingänge", "inbox", True)
    amp = inbox_entry("R&-D")
    assert (amp.raw_name, amp.display_name) == ("R&-D", "R&D")
    for bad in ("Eingänge", "&AOQAYQ-", "", "a" * 201):
        with pytest.raises(ValueError, match="not a valid raw mailbox name"):
            inbox_entry(bad)
    folder_map = imap_map([((), b"/", b"Eing&AOQ-nge"), ((), b"/", b"INBOX")], inbox="Eing&AOQ-nge")
    assert roles(folder_map) == {"Eing&AOQ-nge": "inbox", "INBOX": "other"}
    assert folder_map.inbox.raw_name == "Eing&AOQ-nge"
    assert plan_view(folder_map.list_plan("inbox")) == [("Eing&AOQ-nge", None, "inbox")]


def test_registry_inbox_listed_as_junk_stays_allowed() -> None:
    folder_map = imap_map([((b"\\Junk",), b"/", b"INBOX"), ((), b"/", b"Work")])
    entry = folder_map.lookup("INBOX")
    assert entry is not None
    assert entry.role == "inbox"  # lookup answers the registry inbox
    assert roles(folder_map)["INBOX"] == "excluded"
    assert folder_map.binding("INBOX") == "allowed"  # the registry inbox passes without LIST (§5.5)
    assert plan_view(folder_map.list_plan("inbox")) == [("INBOX", None, "inbox")]


def test_gmail_excluded_all_still_suppresses_sent() -> None:
    listing: Listing = [
        ((), b"/", b"INBOX"),
        ((b"\\All", b"\\Noselect"), b"/", b"[Gmail]/All Mail"),
        ((b"\\Sent",), b"/", b"[Gmail]/Sent Mail"),
        ((b"\\Trash",), b"/", b"[Gmail]/Trash"),
    ]
    folder_map = imap_map(listing, gmail=True, account="gmail")
    assert roles(folder_map) == {
        "INBOX": "inbox",
        "[Gmail]/All Mail": "excluded",
        "[Gmail]/Sent Mail": "excluded",
        "[Gmail]/Trash": "trash",
    }
    assert folder_map.binding("[Gmail]/Sent Mail") == "refused"
    assert folder_map.list_plan("sent") == []


def test_inbox_matches_case_insensitively() -> None:
    folder_map = imap_map([((), b"/", b"Inbox"), ((b"\\Sent",), b"/", b"Sent")])
    assert roles(folder_map) == {"Inbox": "inbox", "Sent": "sent"}
    core, other = folder_map.locate_order()
    assert raw_names(core) == ["INBOX", "Sent"]
    assert other == []


def test_locate_order_icloud() -> None:
    listing: Listing = [*ICLOUD, ((), b"/", b"Projects"), ((b"\\Archive",), b"/", b"Zeta")]
    folder_map = imap_map(listing)
    core, other = folder_map.locate_order()
    assert raw_names(core) == ["INBOX", "Archive", "Sent Messages", "Deleted Messages"]
    # LIST order; the duplicate listing of "Projects" and the losing \Archive "Zeta" appear once each
    assert raw_names(other) == ["Projects", "Receipts", "Zeta"]
    gmail_core, gmail_other = imap_map(GMAIL_EN, gmail=True, account="gmail").locate_order()
    assert raw_names(gmail_core) == ["[Gmail]/All Mail", "[Gmail]/Trash"]
    assert gmail_other == []


def test_is_gmail() -> None:
    assert is_gmail([b"IMAP4REV1", b"X-GM-EXT-1", b"IDLE"]) is True
    assert is_gmail([b"IMAP4rev1", b"x-gm-ext-1"]) is True
    assert is_gmail([b"IMAP4REV1", b"X-GM-EXT-2", b"XLIST"]) is False
    assert is_gmail([]) is False


def test_cache_ttl_with_injected_clock() -> None:
    clock = FakeClock(0.0)
    cache = FolderMapCache(clock=clock)
    folder_map = imap_map(ICLOUD)
    assert cache.get("icloud") is None
    cache.put(folder_map)
    clock.advance(FOLDER_MAP_TTL_SECONDS - 1)
    assert cache.get("icloud") is folder_map
    assert cache.get("gmail") is None
    clock.advance(1)
    assert cache.get("icloud") is None
    cache.put(folder_map)
    assert cache.get("icloud") is folder_map
    cache.clear()
    assert cache.get("icloud") is None
    short = FolderMapCache(ttl=10.0, clock=clock)
    short.put(folder_map)
    clock.advance(9.5)
    assert short.get("icloud") is folder_map
    clock.advance(0.5)
    assert short.get("icloud") is None


def test_forced_rebuild_at_most_every_60_s() -> None:
    clock = FakeClock(0.0)
    cache = FolderMapCache(clock=clock)
    assert cache.may_force_rebuild("icloud") is True
    clock.now = 59.0
    assert cache.may_force_rebuild("icloud") is False
    assert cache.may_force_rebuild("gmail") is True  # per account
    clock.now = 61.0
    assert cache.may_force_rebuild("icloud") is True
    assert cache.may_force_rebuild("icloud") is False
    cache.put(imap_map(ICLOUD))  # a refresh does not reopen the window
    assert cache.may_force_rebuild("icloud") is False
    cache.clear()
    assert cache.may_force_rebuild("icloud") is True


def test_folder_map_refreshed_log_line(folder_log: Capture) -> None:
    cache = FolderMapCache(clock=FakeClock(0.0))
    cache.put(imap_map(ICLOUD))
    capped_listing: Listing = [((), b"/", b"INBOX")] + [((), b"/", f"Box {i}".encode()) for i in range(501)]
    cache.put(imap_map(capped_listing, account="gmail"))
    cache.put(GraphFolderIds("outlook", {"inbox": "AAA=", "sentitems": "BBB=", "archive": None}))
    lines = [json.loads(line) for line in folder_log.lines]
    assert [line["event"] for line in lines] == ["folder_map_refreshed"] * 3
    assert [line["level"] for line in lines] == ["INFO"] * 3
    fields = [{k: v for k, v in line.items() if k not in ("ts", "level", "logger", "event")} for line in lines]
    assert fields == [
        {"account": "icloud", "capability": "mail", "result_count": 6, "outcome": "ok"},
        {"account": "gmail", "capability": "mail", "result_count": 501, "outcome": "partial"},
        {"account": "outlook", "capability": "mail", "result_count": 2, "outcome": "ok"},
    ]
    text = "\n".join(folder_log.lines)
    for secret in ("INBOX", "Sent Messages", "Junk", "Box 1", "AAA=", "BBB="):
        assert secret not in text


def test_graph_role_of() -> None:
    ids = GraphFolderIds(
        "outlook",
        {
            "inbox": "id-inbox",
            "sentitems": "id-sent",
            "archive": "id-archive",
            "deleteditems": "id-deleted",
            "drafts": "id-drafts",
            "junkemail": "id-junk",
            "outbox": "id-outbox",
        },
    )
    assert ids.role_of("id-inbox") == "inbox"
    assert ids.role_of("id-sent") == "sent"
    assert ids.role_of("id-archive") == "archive"
    assert ids.role_of("id-deleted") == "trash"
    assert ids.role_of("id-drafts") == "excluded"
    assert ids.role_of("id-junk") == "excluded"
    assert ids.role_of("id-outbox") == "excluded"
    assert ids.role_of("id-child-folder") == "other"
    assert ids.role_of("ID-INBOX") == "other"  # Graph ids compare exactly
    assert ids.role_of(None) == "excluded"  # no parent folder: fail closed
    missing = GraphFolderIds("outlook", {"inbox": "id-inbox", "archive": None, "junkemail": None})
    assert missing.role_of("id-archive") == "other"
    # one id under two well-known names: the exclusion wins
    clash = GraphFolderIds("outlook", {"inbox": "same", "junkemail": "same"})
    assert clash.role_of("same") == "excluded"
