import base64

import pytest

from mcp_hub.errors import ToolError
from mcp_hub.ids import MAX_ID_LENGTH, MessageRef, decode_message_id, encode_event_id, encode_message_id

REF = MessageRef(account="icloud", folder="INBOX", uidvalidity=1_700_000_000, uid=42)


def _raw(payload: bytes) -> str:
    return "v1." + base64.urlsafe_b64encode(payload).decode().rstrip("=")


def test_message_id_round_trip_and_shape() -> None:
    value = encode_message_id(REF)
    assert value.startswith("v1.")
    assert len(value) <= MAX_ID_LENGTH
    assert "@" not in value
    assert decode_message_id(value) == REF


@pytest.mark.parametrize(
    "value",
    [
        "",
        "v2.abc",
        "v1.",
        "v1.!!!",
        "v1." + "A" * 600,
        _raw(b'["m","icloud","INBOX",1]'),
        _raw(b'["e","icloud","INBOX",1,2]'),
        _raw(b'["m","Bad Id","INBOX",1,2]'),
        _raw(b'["m","icloud","INBOX",0,2]'),
        _raw(b'["m","icloud","INBOX",true,2]'),
        _raw(b"not json"),
    ],
)
def test_malformed_message_ids_are_invalid_argument(value: str) -> None:
    with pytest.raises(ToolError) as caught:
        decode_message_id(value)
    assert caught.value.code == "invalid_argument"


def test_event_id_is_stable_opaque_and_per_instance() -> None:
    a = encode_event_id("icloud", "/123456/calendars/home/", "uid-1@example.test", "20261018T080000Z")
    b = encode_event_id("icloud", "/123456/calendars/home/", "uid-1@example.test", "20261018T080000Z")
    c = encode_event_id("icloud", "/123456/calendars/home/", "uid-1@example.test", "20261025T090000Z")
    assert a == b != c
    assert a.startswith("v1.")
    assert len(a) <= MAX_ID_LENGTH
    decoded = base64.urlsafe_b64decode(a[3:] + "=" * (-len(a[3:]) % 4)).decode()
    assert "123456" not in decoded
    assert "uid-1" not in decoded
    assert "@" not in decoded
