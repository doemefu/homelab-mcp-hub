"""Canned Microsoft Graph mail answers for unit tests (neutral example.test data only).

The list item shape follows the documented Graph v1.0 `message` resource with the §5.5 `$select` and the
`singleValueExtendedProperties` `$expand` (spec 080 §5.5, §6.5). The owner's Graph Explorer facts (T1b) confirm or
correct it; until they are recorded in `T1B_FACTS`, `test_fixture_shapes_match_t1b_facts` is a strict xfail.
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

# The facts of the owner's Graph Explorer run (T1b, plan §4), None until the owner's report is recorded:
# - each name of T1B_FIELDS -> present in request 8; each entry of T1B_EXTENDED -> seen on at least one message;
# - "request10_older_than_e" -> every message of request 10 is older than E (fact 4);
# - "replied_last_verb" -> the `Integer 0x1081` value of a message the owner replied to, as a string (fact 5);
# - "folder_ids_in_pattern" -> the ids of requests 1-7 hold only letters, digits, "=", "_" and "-".
T1B_FACTS: dict[str, bool | str] | None = None
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
