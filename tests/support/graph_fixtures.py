"""Canned Microsoft Graph mail answers for unit tests (neutral example.test data only).

The list item shape follows the documented Graph v1.0 `message` resource with the §5.5 `$select` and the
`singleValueExtendedProperties` `$expand` (spec 080 §5.5, §6.5). The owner's Graph Explorer facts (T1b) confirm or
correct it; they are recorded in `T1B_FACTS` and `test_fixture_shapes_match_t1b_facts` checks the fixtures against them.
"""

from datetime import UTC, datetime, timedelta
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
# T17 (request 16): an own sent item can carry an Exchange legacy DN as `from.address`, the SMTP address as the name.
LEGACY_DN: Final = "/O=EXAMPLE/OU=TEST/CN=RECIPIENTS/CN=OWNER"


def sender(address: str, name: str) -> dict[str, object]:
    """A Graph `from` (or recipient) value."""
    return {"emailAddress": {"name": name, "address": address}}


# The facts of the owner's Graph Explorer run for the batched `get_message_status` lookups (T17, plan §4; personal
# Outlook.com account, 2026-10-22; requests 14, 14b, 15 and 16, each with the same `Prefer` header):
# - "status_14", "status_14b", "status_15", "status_16" -> the HTTP status of each request;
# - "count_14", "count_14b", "count_15", "count_16" -> the item count of each answer (16: 10 conversations, unordered);
# - "next_link_14", "next_link_14b", "next_link_15", "next_link_16" -> that answer carried `@odata.nextLink` (GR3);
# - "batch_10_at_cap_ok" -> request 14b (10 values, about 1,800 encoded characters of `$filter`) answered 200 (GR3);
# - "filter_chars_14b" -> the encoded length of request 14b's `$filter`, as the URL builder printed it;
# - "request14_both_found" -> request 14 returned both messages;
# - "request14_replied_last_verb" -> the `Integer 0x1081` value on the replied one, as a string (the `$expand` works
#   on the batch; otherwise the C6 fallback); the other message had no `singleValueExtendedProperties`;
# - "request15_only_c1_c2", "request15_sent_reply_after_receipt" -> request 15 returned only items of C1/C2, among
#   them a `sentitems` item whose `sentDateTime` is after the replied message's `receivedDateTime`;
# - "sent_from_legacy_dn" -> request 16: an own sent item carried an Exchange legacy DN as `from.emailAddress.address`
#   and the SMTP address as the name (see LEGACY_DN).
T1C_FACTS: dict[str, bool | int | str | None] | None = {
    "status_14": 200,
    "status_14b": 200,
    "status_15": 200,
    "status_16": 200,
    "count_14": 2,
    "count_14b": 2,  # the 8 made-up values match nothing
    "count_15": 5,
    "count_16": 14,
    "next_link_14": False,
    "next_link_14b": False,
    "next_link_15": False,
    "next_link_16": False,
    "batch_10_at_cap_ok": True,
    "filter_chars_14b": 1795,
    "request14_both_found": True,
    "request14_replied_last_verb": "102",
    "request15_only_c1_c2": True,
    "request15_sent_reply_after_receipt": True,
    "sent_from_legacy_dn": True,
}


# The facts of the owner's Graph Explorer probes P1/P2 for `$search` (2026-10-27, personal Outlook.com account, same
# `Prefer` header): "p1_status" -> HTTP status of `GET /me/mailFolders/inbox/messages?$search&$select&$expand`;
# "p1_extended_present" -> the answer carried `singleValueExtendedProperties`; "p1_ids_immutable" -> the returned ids
# equal the immutable ids of the list (False: `$search` returns regular ids, owner decision A: hits carry `id: null`).
SEARCH_FACTS: dict[str, bool | int | None] | None = {
    "p1_status": 200,
    "p1_extended_present": True,
    "p1_ids_immutable": False,
}


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


# get_thread (WP13c): one conversation; item n is received n minutes after THREAD_START.
CONVERSATION: Final = "AAQkTHREAD0001="
THREAD_START: Final = datetime(2026, 10, 20, 6, 0, tzinfo=UTC)


def body_object(content: str, kind: str = "text") -> dict[str, str]:
    """A Graph `itemBody` value (`uniqueBody` or `body`)."""
    return {"contentType": kind, "content": content}


def thread_item(n: int, folder_id: str = INBOX_ID, **extra: object) -> dict[str, object]:
    """Item `n` of CONVERSATION with Message-ID `t<n>@example.test` and the `uniqueBody` "Unique <n>"."""
    at = (THREAD_START + timedelta(minutes=n)).strftime("%Y-%m-%dT%H:%M:%SZ")
    fields: dict[str, object] = {
        "receivedDateTime": at,
        "sentDateTime": at,
        "parentFolderId": folder_id,
        "conversationId": CONVERSATION,
        "internetMessageId": f"<t{n}@example.test>",
        "from": {"emailAddress": {"name": "Peer", "address": "peer@example.test"}},
        "uniqueBody": body_object(f"Unique {n}"),
    }
    return message(n, **(fields | extra))


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
