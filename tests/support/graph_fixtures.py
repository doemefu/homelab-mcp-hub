"""Canned Microsoft Graph mail answers for unit tests (neutral example.test data only).

The list item shape follows the documented Graph v1.0 `message` resource with the §5.5 `$select` and the
`singleValueExtendedProperties` `$expand` (spec 080 §5.5, §6.5). The owner's Graph Explorer facts (T1b) confirm or
correct it; they are recorded in `T1B_FACTS` and `test_fixture_shapes_match_t1b_facts` checks the fixtures against them.
"""

from typing import Final

INBOX_ID = "AQMkADAwATM0MDAAMS1iNTcwLWI2NTEtMDACLTAwCgAuAAAD"
SENT_ID = "AQMkADAwATM0MDAAMS1iNTcwLWI2NTEtMDACLTAwCgAuAAAE"
ARCHIVE_ID = "AQMkADAwATM0MDAAMS1iNTcwLWI2NTEtMDACLTAwCgAuAAAF"
TRASH_ID = "AQMkADAwATM0MDAAMS1iNTcwLWI2NTEtMDACLTAwCgAuAAAG"
DRAFTS_ID = "AQMkADAwATM0MDAAMS1iNTcwLWI2NTEtMDACLTAwCgAuAAAH"
JUNK_ID = "AQMkADAwATM0MDAAMS1iNTcwLWI2NTEtMDACLTAwCgAuAAAI"
OUTBOX_ID = "AQMkADAwATM0MDAAMS1iNTcwLWI2NTEtMDACLTAwCgAuAAAJ"
FOLDER_IDS: Final[dict[str, str]] = {
    "inbox": INBOX_ID,
    "sentitems": SENT_ID,
    "archive": ARCHIVE_ID,
    "deleteditems": TRASH_ID,
    "drafts": DRAFTS_ID,
    "junkemail": JUNK_ID,
    "outbox": OUTBOX_ID,
}

# The facts of the owner's Graph Explorer run (T1b, plan §4; personal Outlook.com account, 2026-10-22, every request
# with `Prefer: IdType="ImmutableId", outlook.body-content-type="text"`):
# - each name of T1B_FIELDS -> present in request 8; each entry of T1B_EXTENDED -> seen on at least one message;
# - "request10_older_than_e" -> every message of request 10 is older than E (fact 4);
# - "replied_last_verb" -> the `Integer 0x1081` value of a message the owner replied to, as a string (fact 5);
# - "folder_ids_in_pattern" -> the ids of requests 1-7 hold only letters, digits, "=", "_" and "-".
# - "forwarded_last_verb" -> the `Integer 0x1081` value of a forwarded message: "104" (a forward is not answered);
# - "well_known_folders_ok" -> GET of inbox, sentitems, archive, deleteditems, drafts, junkemail, outbox all gave 200
#   (so `archive` exists for a personal account);
# - "junk_parent_equals_folder_id" -> request 13: a junk message's `parentFolderId` is byte-identical to the id of
#   `GET /me/mailFolders/junkemail?$select=id`; "sent_parent_same_format" -> a sent message's `parentFolderId` has the
#   id family and format of the folder ids (inbox comparability is proven in production);
# - "absent_extended_key_without_properties" -> a message without the properties has no
#   `singleValueExtendedProperties` key at all (not an empty list);
# - "extended_values_are_strings" -> the extended property values arrive as strings;
# - "expand_with_search" -> not confirmed: `$expand` together with `$search` was not exercised (None).
T1B_FACTS: dict[str, bool | str | None] | None = {
    "parentFolderId": True,
    "isDraft": True,
    "internetMessageId": True,  # with angle brackets
    "conversationId": True,
    "flag": True,  # {"flagStatus": ...}
    "toRecipients": True,
    "ccRecipients": True,  # may be []
    "singleValueExtendedProperties": True,
    "Integer 0x1081": True,
    "String 0x1042": True,  # one bracketed msg-id on a reply
    "String 0x1039": True,  # space-separated bracketed msg-ids
    "request10_older_than_e": True,
    "replied_last_verb": "102",
    "forwarded_last_verb": "104",
    "folder_ids_in_pattern": True,
    "well_known_folders_ok": True,
    "junk_parent_equals_folder_id": True,
    "sent_parent_same_format": True,
    "absent_extended_key_without_properties": True,
    "extended_values_are_strings": True,
    "expand_with_search": None,
}
T1B_FIELDS: Final = (
    "parentFolderId",
    "isDraft",
    "internetMessageId",
    "conversationId",
    "flag",
    "toRecipients",
    "ccRecipients",
    "singleValueExtendedProperties",
)
T1B_EXTENDED: Final = ("Integer 0x1081", "String 0x1042", "String 0x1039")


def extended(
    answered: str | None = None, in_reply_to: str | None = None, references: str | None = None
) -> list[dict[str, str]]:
    """`singleValueExtendedProperties` as Graph documents it: only the entries the message has, values as strings."""
    pairs = (("Integer 0x1081", answered), ("String 0x1042", in_reply_to), ("String 0x1039", references))
    return [{"id": name, "value": value} for name, value in pairs if value is not None]


def message(n: int, **extra: object) -> dict[str, object]:
    """A list item of the inbox; an item without extended properties carries no `singleValueExtendedProperties`."""
    return {
        "id": f"AAMkMSG{n:04d}=",
        "receivedDateTime": f"2026-09-29T06:{n % 60:02d}:00Z",
        "isRead": False,
        "hasAttachments": False,
        "from": {"emailAddress": {"name": f"Sender {n}", "address": f"s{n}@example.test"}},
        "subject": f"Subject {n}",
        "bodyPreview": f"Preview {n}",
        "isDraft": False,
        "internetMessageId": f"<m{n}@example.test>",
        "conversationId": f"AAQkCONV{n:04d}=",
        "parentFolderId": INBOX_ID,
        "toRecipients": [{"emailAddress": {"name": "Owner", "address": "owner@example.test"}}],
        "ccRecipients": [],
        "flag": {"flagStatus": "notFlagged"},
        "sentDateTime": f"2026-09-29T06:{n % 60:02d}:00Z",
    } | extra


class FakeTokens:
    """Hands out AT-SENTINEL-1, -2, … ; counts refresh-forcing invalidations."""

    def __init__(self) -> None:
        self.issued = 0
        self.invalidated = 0

    def access_token(self, deadline: float) -> str:
        self.issued += 1
        return f"AT-SENTINEL-{self.issued}"

    def invalidate(self) -> None:
        self.invalidated += 1
