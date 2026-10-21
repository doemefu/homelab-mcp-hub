import base64
import hashlib
import json

import pytest

from mcp_hub.errors import ToolError
from mcp_hub.ids import (
    MAX_MAILBOX_NAME_CHARS,
    GraphMessageRef,
    MessageRef,
    StableRef,
    ThreadKey,
    decode_message_id,
    decode_ref,
    decode_thread_key,
    encode_message_id,
    encode_ref,
    encode_thread_key,
    location_id,
    location_k,
    mailbox_name_ok,
)


def _raw(parts: list[object]) -> str:
    raw = json.dumps(parts, separators=(",", ":"), ensure_ascii=False).encode()
    return "v1." + base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _invalid(fn: object, value: str) -> None:
    with pytest.raises(ToolError) as info:
        fn(value)  # type: ignore[operator]  # test helper takes any decoder
    assert info.value.code == "invalid_argument"


def test_ref_round_trip() -> None:
    value = encode_ref("icloud", "abc.123@example.org")
    assert value == _raw(["r", "icloud", "abc.123@example.org"])
    assert decode_ref(value or "") == StableRef(account="icloud", message_id="abc.123@example.org")


def test_ref_at_300_and_301_characters() -> None:
    account = "a" * 32
    ok = "m" * 300
    value = encode_ref(account, ok)
    assert value is not None
    assert len(value) <= 512
    assert decode_ref(value).message_id == ok
    assert encode_ref(account, "m" * 301) is None
    _invalid(decode_ref, _raw(["r", account, "m" * 301]))


@pytest.mark.parametrize(
    "message_id",
    ["a b@x", 'a"b@x', "a'b@x", "<a@x", "a>@x", "a\\b@x", "a{b@x", "a}b@x", "a\x07b@x", "aé@x", "a@x\n", ""],
)
def test_ref_rejects_forbidden_characters(message_id: str) -> None:
    assert encode_ref("icloud", message_id) is None
    _invalid(decode_ref, _raw(["r", "icloud", message_id]))


@pytest.mark.parametrize("message_id", ["a(b)%*]@x", "no-at-sign", "a+&#%=;/?@x"])
def test_ref_allows_atom_specials_and_no_at_sign(message_id: str) -> None:
    value = encode_ref("icloud", message_id)
    assert value is not None
    assert decode_ref(value).message_id == message_id


@pytest.mark.parametrize(
    "value",
    [
        "",
        "v2.abc",
        "v1.",
        "v1.!!!",
        _raw(["m", "icloud", "INBOX", 1, 2]),
        _raw(["r", "icloud"]),
        _raw(["r", "icloud", "a@x", "extra"]),
        _raw(["r", 1, "a@x"]),
        _raw(["r", "icloud", 5]),
        _raw(["r", "Bad Account", "a@x"]),
        _raw({"r": 1}),
        _raw(["t", "icloud", "gmail", 1]),
    ],
)
def test_decode_ref_rejects_other_shapes(value: str) -> None:
    _invalid(decode_ref, value)


@pytest.mark.parametrize(
    ("source", "value"),
    [
        ("gmail", 1781234567890123456),
        ("graph", "AAQkADAwATM="),
        ("headers", "root@example.org"),
        ("subject", "0123456789abcdef" * 2),
    ],
)
def test_thread_key_round_trip_per_source(source: str, value: int | str) -> None:
    key = ThreadKey(account="gmail", source=source, value=value)  # type: ignore[arg-type]  # parametrised literal
    encoded = encode_thread_key(key)
    assert encoded == _raw(["t", "gmail", source, value])
    assert decode_thread_key(encoded or "") == key


@pytest.mark.parametrize(
    ("source", "value"),
    [
        ("gmail", -1),
        ("gmail", 2**64),
        ("gmail", True),
        ("gmail", "12"),
        ("graph", "a+b"),
        ("graph", "AAQk\n"),
        ("headers", "a b"),
        ("headers", "root@example.org\n"),
        ("subject", "XYZ"),
        ("subject", "0123456789abcdef" * 2 + "\n"),
        ("imap", "abc"),
    ],
)
def test_thread_key_rejects_foreign_values(source: str, value: object) -> None:
    key = ThreadKey(account="gmail", source=source, value=value)  # type: ignore[arg-type]  # deliberately foreign
    assert encode_thread_key(key) is None
    _invalid(decode_thread_key, _raw(["t", "gmail", source, value]))


def test_oversized_ref_and_thread_key_are_rejected() -> None:
    _invalid(decode_ref, _raw(["r", "icloud", "a" * 600]))
    _invalid(decode_thread_key, _raw(["t", "icloud", "headers", "a" * 600]))


def test_thread_key_rejects_other_shapes() -> None:
    _invalid(decode_thread_key, _raw(["t", "gmail", "gmail"]))
    _invalid(decode_thread_key, _raw(["r", "gmail", "a@x"]))
    _invalid(decode_thread_key, "garbage")


def test_existing_id_shapes_stay_byte_identical() -> None:
    inbox = "v1.WyJtIiwiaWNsb3VkIiwiSU5CT1giLDE3MDAwMDAwMDAsNDJd"
    graph = "v1.WyJnIiwib3V0bG9vayIsIkFBUWtBREF3QVRNPSJd"
    nested = "v1.WyJtIiwiZ21haWwiLCJbR21haWxdLyZBTVEtbHRlciIsNSw3XQ"
    ref = MessageRef(account="icloud", folder="INBOX", uidvalidity=1_700_000_000, uid=42)
    assert decode_message_id(inbox) == ref
    assert encode_message_id(ref) == inbox
    assert decode_message_id(graph) == GraphMessageRef(account="outlook", graph_id="AAQkADAwATM=")
    assert encode_message_id(GraphMessageRef(account="outlook", graph_id="AAQkADAwATM=")) == graph
    assert encode_message_id(MessageRef("gmail", "[Gmail]/&AMQ-lter", 5, 7)) == nested
    assert decode_message_id(nested) == MessageRef(account="gmail", folder="[Gmail]/&AMQ-lter", uidvalidity=5, uid=7)


def test_m_id_mailbox_name_rule() -> None:
    def mid(folder: str) -> str:
        return _raw(["m", "icloud", folder, 1, 2])

    assert isinstance(decode_message_id(mid("a" * MAX_MAILBOX_NAME_CHARS)), MessageRef)
    for bad in ("a" * 201, "Ünterordner", "a\r\nb", "", "a\n"):
        _invalid(decode_message_id, mid(bad))
    assert location_id(MessageRef("icloud", "a" * 201, 1, 2)) is None
    assert location_id(MessageRef("icloud", "\\" * 200, 1, 2)) is None
    assert location_id(MessageRef("icloud", "INBOX", 1, 2)) == encode_message_id(MessageRef("icloud", "INBOX", 1, 2))
    assert location_id(GraphMessageRef("outlook", "AAQk")) == encode_message_id(GraphMessageRef("outlook", "AAQk"))


@pytest.mark.parametrize(
    ("name", "ok"),
    [
        ("INBOX", True),
        ("[Gmail]/&AMQ-lter", True),
        ("a&-b", True),
        ("a&AMQ", False),
        ("a&-", True),
        ("a&!!-b", False),
        ("a&A-b", False),
        ("a&AMQA-b", False),
        ("a&AM-b", False),
        ("&2D3eAA-", True),
        ("&2D0-", False),
        ("&AMR-", False),  # non-canonical trailing bits
        ("&AEoAdQBuAGs-", False),  # "Junk" shifted: printable ASCII only (exclusion bypass)
        ("&AMQ-&AMQ-", False),  # adjacent shifts
        ("&AMQ-x&AMQ-", True),
        ("a&AMQ-", True),
        ("a b", True),
        ("a\x7fb", False),
        ("é", False),
    ],
)
def test_mailbox_name_modified_utf7(name: str, ok: bool) -> None:
    assert mailbox_name_ok(name) is ok


def test_location_k() -> None:
    ref = MessageRef("icloud", "INBOX", 1_700_000_000, 42)
    text = "v1.WyJtIiwiaWNsb3VkIiwiSU5CT1giLDE3MDAwMDAwMDAsNDJd"
    assert location_k(ref) == hashlib.sha256(text.encode()).hexdigest()[:16]
    long_ref = MessageRef("icloud", "\\" * 200, 1, 2)
    assert location_id(long_ref) is None
    assert location_k(long_ref) == hashlib.sha256(_raw(["m", "icloud", "\\" * 200, 1, 2]).encode()).hexdigest()[:16]
    assert len(location_k(long_ref)) == 16
