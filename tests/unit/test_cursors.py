import base64
import hashlib
import hmac
import json

import pytest

from mcp_hub import cursors
from mcp_hub.cursors import (
    MAX_CURSOR_CHARS,
    CursorCodec,
    Position,
    argument_digest,
)
from mcp_hub.errors import ToolError

KEY = b"k" * 32
DIGEST = "a" * 64
POS = Position(received_at=1_700_000_000, k="0123456789abcdef")
NOW = 1_800_000_000.0


class Clock:
    def __init__(self, now: float = NOW) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _issue(codec: CursorCodec, tool: str = "list_messages") -> str:
    return codec.issue(tool=tool, args_digest=DIGEST, window_start=1_699_000_000, position=POS)


def _signed(payload: bytes, key: bytes = KEY) -> str:
    body = base64.urlsafe_b64encode(payload).rstrip(b"=").decode()
    signed = "c1." + body
    mac = hmac.new(key, signed.encode(), hashlib.sha256).digest()
    return signed + "." + base64.urlsafe_b64encode(mac).rstrip(b"=").decode()


def _payload(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {"a": DIGEST, "i": int(NOW), "k": POS.k, "r": 5, "t": "list_messages", "w": 1}
    data.update(overrides)
    return data


def _open(codec: CursorCodec, value: str, tool: str = "list_messages") -> cursors.CursorState:
    return codec.open(value, tool=tool, args_digest=DIGEST)


def _assert_invalid(codec: CursorCodec, value: str, tool: str = "list_messages") -> None:
    with pytest.raises(ToolError) as info:
        _open(codec, value, tool)
    assert info.value.code == "invalid_cursor"
    assert info.value.message == "Cursor is invalid or expired"


def test_round_trip() -> None:
    codec = CursorCodec(KEY, Clock())
    state = _open(codec, _issue(codec))
    assert state == cursors.CursorState("list_messages", DIGEST, 1_699_000_000, POS, int(NOW))
    assert _issue(codec).startswith("c1.")


def test_bad_signature() -> None:
    codec = CursorCodec(KEY, Clock())
    value = _issue(codec)
    head, _, sig = value.rpartition(".")
    flipped = ("A" if sig[0] != "A" else "B") + sig[1:]
    _assert_invalid(codec, head + "." + flipped)
    _assert_invalid(codec, head + "." + sig[:-1])


def test_tampered_payload() -> None:
    codec = CursorCodec(KEY, Clock())
    value = _issue(codec)
    _, body, sig = value.split(".")
    forged = json.loads(base64.urlsafe_b64decode(body + "=="))
    forged["r"] = 1
    new_body = base64.urlsafe_b64encode(json.dumps(forged).encode()).rstrip(b"=").decode()
    _assert_invalid(codec, f"c1.{new_body}.{sig}")


def test_mac_checked_before_decode(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def spy(text: str) -> bytes:
        calls.append(text)
        return b""

    monkeypatch.setattr(cursors.base64, "urlsafe_b64decode", spy)
    codec = CursorCodec(KEY, Clock())
    _assert_invalid(codec, "c1.A.B")
    _assert_invalid(codec, "c1.!!!".replace("!", "A") + ".AAAA")
    assert calls == []


def test_expiry() -> None:
    clock = Clock()
    codec = CursorCodec(KEY, clock)
    value = _issue(codec)
    clock.now = NOW + 3600
    _open(codec, value)
    clock.now = NOW + 3601
    _assert_invalid(codec, value)


def test_issue_time_more_than_60_s_in_future() -> None:
    clock = Clock()
    codec = CursorCodec(KEY, clock)
    value = _issue(codec)
    clock.now = NOW - 60
    _open(codec, value)
    clock.now = NOW - 61
    _assert_invalid(codec, value)


def test_new_process_key_invalidates() -> None:
    value = _issue(CursorCodec(clock=Clock()))
    _assert_invalid(CursorCodec(clock=Clock()), value)


def test_other_tool() -> None:
    codec = CursorCodec(KEY, Clock())
    _assert_invalid(codec, _issue(codec, "list_unread"), "list_messages")


def test_other_arguments() -> None:
    codec = CursorCodec(KEY, Clock())
    value = _issue(codec)
    other = argument_digest({"folder": "sent"})
    with pytest.raises(ToolError) as info:
        codec.open(value, tool="list_messages", args_digest=other)
    assert info.value.code == "invalid_cursor"


def test_omitted_since_differs_from_explicit() -> None:
    assert argument_digest({"folder": "inbox", "since": None}) != argument_digest(
        {"folder": "inbox", "since": "2026-10-01T00:00:00Z"}
    )


@pytest.mark.parametrize(
    "value",
    [
        "",
        "c1.",
        "c2.x.y",
        "c1.!!.x",
        "c1.AAAA=.AAAA",
        "c1.AAAA.AAAA.AAAA",
    ],
)
def test_malformed(value: str) -> None:
    codec = CursorCodec(KEY, Clock())
    _assert_invalid(codec, value)


def test_too_long() -> None:
    codec = CursorCodec(KEY, Clock())
    value = "c1." + "A" * (MAX_CURSOR_CHARS - 7) + ".AAAA"
    assert len(value) == MAX_CURSOR_CHARS + 1
    _assert_invalid(codec, value)


def _without(key: str) -> dict[str, object]:
    data = _payload()
    del data[key]
    return data


@pytest.mark.parametrize(
    "payload",
    [
        b"not json",
        b"\xff\xfe",
        json.dumps([1, 2]).encode(),
        json.dumps(_without("w")).encode(),
        json.dumps(_payload(extra=1)).encode(),
        json.dumps(_payload(r="5")).encode(),
        json.dumps(_payload(r=True)).encode(),
        json.dumps(_payload(i=1.0)).encode(),
        json.dumps(_payload(w=None)).encode(),
        json.dumps(_payload(k="XYZ")).encode(),
        json.dumps(_payload(k=POS.k.upper())).encode(),
        json.dumps(_payload(a="a" * 63)).encode(),
        json.dumps(_payload(t="get_events")).encode(),
        json.dumps(_payload(t=["list_messages"])).encode(),
    ],
)
def test_malformed_signed_payload(payload: bytes) -> None:
    codec = CursorCodec(KEY, Clock())
    _assert_invalid(codec, _signed(payload))


def test_valid_signed_payload_control() -> None:
    codec = CursorCodec(KEY, Clock())
    assert _open(codec, _signed(json.dumps(_payload()).encode())).position == Position(5, POS.k)


def test_cursor_carries_no_third_party_text() -> None:
    codec = CursorCodec(KEY, Clock())
    digest = argument_digest({"query": "Quarterly secret subject", "folder": "inbox"})
    value = codec.issue(tool="list_messages", args_digest=digest, window_start=1, position=POS)
    body = base64.urlsafe_b64decode(value.split(".")[1] + "==").decode()
    assert "secret" not in value
    assert "secret" not in body
    assert "Quarterly" not in body
    data = json.loads(body)
    assert set(data) == {"a", "i", "k", "r", "t", "w"}
    assert all(type(data[name]) is int for name in "irw")
    assert data["a"] == digest
    assert data["t"] == "list_messages"


def test_position_total_order() -> None:
    pos = Position(100, "bbbbbbbbbbbbbbbb")
    assert pos.precedes(99, "ffffffffffffffff")  # earlier second
    assert pos.precedes(100, "aaaaaaaaaaaaaaaa")  # same second, smaller k
    assert not pos.precedes(100, "bbbbbbbbbbbbbbbb")  # the position itself
    assert not pos.precedes(100, "cccccccccccccccc")  # same second, larger k
    assert not pos.precedes(101, "0000000000000000")  # later second


def test_digest_ignores_cursor_and_limit() -> None:
    base = {"account": None, "folder": "inbox"}
    assert argument_digest(base) == argument_digest({**base, "limit": 5, "cursor": "c1.x.y"})
    assert argument_digest(base) == argument_digest({"folder": "inbox", "account": None})
    assert argument_digest(base) != argument_digest({**base, "account": "icloud"})
