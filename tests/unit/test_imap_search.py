import ast
import locale
import subprocess
import sys
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

from mcp_hub.ids import MESSAGE_ID_PATTERN
from mcp_hub.providers.deadline import CallDeadline
from mcp_hub.providers.imap_guard import GuardedImapClient
from mcp_hub.providers.imap_search import (
    GMAIL_SENT_LABEL_KEY,
    LOCATE_BATCH_VALUES,
    LOOKUP_BATCH_VALUES,
    MAX_OR_BYTES,
    MAX_THRID_VALUES,
    UnsafeSearchValue,
    criteria,
    gmail_label_keys,
    header_or,
    imap_date,
    msg_id_token,
    or_batches,
    quoted,
    search_value_ok,
    thrid_batches,
    thrid_or,
    window_keys,
)
from tests.support.clock import FakeClock
from tests.support.imap_fake import FakeFolder, FakeImapServer, FakeMessage

ROOT = Path(__file__).resolve().parents[2]
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
GERMAN_LOCALES = ("de_DE.UTF-8", "de_DE.utf8", "de_CH.UTF-8", "de_DE")


def ids(n: int, width: int = 1) -> list[str]:
    return [f"{index:0{width}d}@example.org" for index in range(n)]


def paren_depth(data: bytes) -> int:
    depth = deepest = 0
    for byte in data:
        if byte == ord("("):
            depth += 1
            deepest = max(deepest, depth)
        elif byte == ord(")"):
            depth -= 1
            if depth < 0:
                raise AssertionError("unbalanced")
    if depth != 0:
        raise AssertionError("unbalanced")
    return deepest


# --- dates and window keys ----------------------------------------------------------------------------------------


@pytest.fixture
def german_time_locale() -> Iterator[str]:
    previous = locale.setlocale(locale.LC_TIME)
    for name in GERMAN_LOCALES:
        try:
            locale.setlocale(locale.LC_TIME, name)
        except locale.Error:
            continue
        try:
            yield name
        finally:
            locale.setlocale(locale.LC_TIME, previous)
        return
    pytest.skip("no German locale installed")


def test_imap_date_english_months() -> None:
    assert imap_date(date(2026, 3, 5)) == b"05-Mar-2026"
    assert imap_date(date(2026, 10, 22)) == b"22-Oct-2026"
    assert [imap_date(date(2026, month, 1)) for month in range(1, 13)] == [
        f"01-{name}-2026".encode() for name in MONTHS
    ]


def test_imap_date_english_months_under_german_lc_time(german_time_locale: str) -> None:
    # The locale is really active: strftime's month name would be German here (Mär, Okt, Dez).
    assert date(2026, 10, 1).strftime("%b") != "Oct" or date(2026, 12, 1).strftime("%b") != "Dec"
    assert [imap_date(date(2026, month, 9)) for month in range(1, 13)] == [
        f"09-{name}-2026".encode() for name in MONTHS
    ]


def test_window_keys() -> None:
    assert window_keys(since_day=date(2026, 10, 6), before_day=None, unread_only=False) == (
        b"NOT DRAFT SINCE 06-Oct-2026"
    )
    assert window_keys(since_day=date(2026, 10, 6), before_day=date(2026, 10, 22), unread_only=True) == (
        b"NOT DRAFT UNSEEN SINCE 06-Oct-2026 BEFORE 22-Oct-2026"
    )
    assert window_keys(since_day=date(2026, 10, 6), before_day=date(2026, 10, 22), unread_only=False) == (
        b"NOT DRAFT SINCE 06-Oct-2026 BEFORE 22-Oct-2026"
    )


# --- header OR tree -----------------------------------------------------------------------------------------------


def test_header_or_balanced_shapes() -> None:
    mid = ("Message-ID",)
    assert header_or(mid, ["a@x"]) == b'HEADER Message-ID "<a@x>"'
    assert header_or(mid, ["a@x", "b@x"]) == b'OR HEADER Message-ID "<a@x>" HEADER Message-ID "<b@x>"'
    assert header_or(mid, ["a@x", "b@x", "c@x"]) == (
        b'OR (OR HEADER Message-ID "<a@x>" HEADER Message-ID "<b@x>") HEADER Message-ID "<c@x>"'
    )
    assert header_or(mid, ["a@x", "b@x", "c@x", "d@x"]) == (
        b'OR (OR HEADER Message-ID "<a@x>" HEADER Message-ID "<b@x>") '
        b'(OR HEADER Message-ID "<c@x>" HEADER Message-ID "<d@x>")'
    )
    assert header_or(("In-Reply-To", "References"), ["a@x"]) == (
        b'OR HEADER In-Reply-To "<a@x>" HEADER References "<a@x>"'
    )


def test_header_or_leaf_order_is_value_by_value_then_field_by_field() -> None:
    assert header_or(("In-Reply-To", "References"), ["a@x", "b@x"]) == (
        b'OR (OR HEADER In-Reply-To "<a@x>" HEADER References "<a@x>") '
        b'(OR HEADER In-Reply-To "<b@x>" HEADER References "<b@x>")'
    )


def test_msg_id_token_brackets_the_bare_value() -> None:
    # O35: iCloud matches HEADER msg-id searches against whole `<id>` tokens; the bare value is checked, not escaped.
    assert msg_id_token("a@x") == b'"<a@x>"'
    assert msg_id_token("a(b)%*]{5}@x") == b'"<a(b)%*]{5}@x>"'
    for value in ['a"b@x', "a\\b@x", "", "X-GM-RAW@x", "ä@x", "a\r\nb@x", "a<b@x", "a>b@x", "<a@x>"]:
        with pytest.raises(UnsafeSearchValue):
            msg_id_token(value)


def test_or_tree_depth_is_logarithmic() -> None:
    assert paren_depth(header_or(("Message-ID",), ids(20))) <= 6
    assert paren_depth(header_or(("In-Reply-To", "References"), ids(20))) <= 6
    assert paren_depth(header_or(("Message-ID",), ids(1000, 4))) <= 10


def test_header_or_refuses_empty_inputs_and_unknown_fields() -> None:
    with pytest.raises(ValueError, match="no search values"):
        header_or(("Message-ID",), [])
    with pytest.raises(ValueError, match="no header fields"):
        header_or((), ["a@x"])
    with pytest.raises(ValueError, match="not one string"):
        header_or(("Message-ID",), "a@x")  # a bare string is not a list of values
    with pytest.raises(ValueError, match="not one string"):
        header_or("Message-ID", ["a@x"])  # type: ignore[arg-type]  # a bare string is not a list of fields
    with pytest.raises(ValueError, match="not one string"):
        or_batches("a@x", ("Message-ID",), max_values=20)
    bad_fields: list[Any] = [("Subject",), ("Message-ID FLAGGED",), ("message-id",), ("Message-ID\r\n",)]
    for fields in bad_fields:
        with pytest.raises(ValueError, match="unknown header field") as caught:
            header_or(fields, ["a@x"])
        assert type(caught.value) is ValueError  # a programming error, not a refused third-party value


@pytest.mark.parametrize("n", [1, 2, 3, 4, 5, 7, 8, 9, 20])
def test_header_or_selects_exactly_the_listed_message_ids(n: int) -> None:
    # Semantics against the fake's search evaluator: every listed id matches, nothing else does.
    day = datetime(2026, 10, 19, 8, 0, tzinfo=UTC)
    values = ids(25, 2)
    messages = {
        uid: FakeMessage(uid, day, headers=f"Message-ID: <{value}>\r\n\r\n".encode())
        for uid, value in enumerate(values, start=1)
    }
    clock = FakeClock(1000.0)
    server = FakeImapServer([FakeFolder("INBOX", messages=messages)], clock=clock)
    guard = GuardedImapClient(server.client(), CallDeadline.start(clock), registry_inbox_raw="INBOX")
    guard.login("user", "secret")
    guard.examine("INBOX")
    wanted = values[3 : 3 + n]
    found = guard.search(criteria(b"NOT DRAFT", header_or(("Message-ID",), wanted)))
    assert sorted(found) == list(range(4, 4 + n))


# --- batching -----------------------------------------------------------------------------------------------------


def test_or_batches_by_count() -> None:
    values = ids(45, 2)
    batches = or_batches(values, ("Message-ID",), max_values=20)
    assert [len(batch) for batch in batches] == [20, 20, 5]
    assert [value for batch in batches for value in batch] == values


@pytest.mark.parametrize("fields", [("Message-ID",), ("In-Reply-To", "References")])
def test_or_batches_by_bytes(fields: tuple[Any, ...]) -> None:
    values = [f"{index:03d}-{'v' * 250}@example.org" for index in range(60)]
    batches = or_batches(values, fields, max_values=1000)
    assert [value for batch in batches for value in batch] == values
    assert len(batches) > 1
    for index, batch in enumerate(batches):
        assert len(header_or(fields, batch)) <= MAX_OR_BYTES
        if index + 1 < len(batches):
            assert len(header_or(fields, [*batch, batches[index + 1][0]])) > MAX_OR_BYTES


def test_or_batches_byte_cap_is_exact_at_the_boundary() -> None:
    fields = ("Message-ID",)
    values = ids(30, 2)
    for cap in range(40, 700, 7):
        batches = or_batches(values, fields, max_values=1000, max_bytes=cap)
        assert [value for batch in batches for value in batch] == values
        for index, batch in enumerate(batches):
            assert len(header_or(fields, batch)) <= cap
            if index + 1 < len(batches):
                assert len(header_or(fields, [*batch, batches[index + 1][0]])) > cap


def test_or_batches_count_the_brackets_of_each_value() -> None:
    mid = ("Message-ID",)
    assert len(header_or(mid, ["a@x"])) == 25  # `HEADER Message-ID "<a@x>"`: the bare form had 23
    assert or_batches(["a@x"], mid, max_values=20, max_bytes=25) == [["a@x"]]
    with pytest.raises(UnsafeSearchValue):
        or_batches(["a@x"], mid, max_values=20, max_bytes=24)
    assert len(header_or(mid, ["a@x", "b@x"])) == 54
    assert or_batches(["a@x", "b@x"], mid, max_values=20, max_bytes=54) == [["a@x", "b@x"]]
    assert or_batches(["a@x", "b@x"], mid, max_values=20, max_bytes=53) == [["a@x"], ["b@x"]]
    both = ("In-Reply-To", "References")
    assert len(header_or(both, ["a@x"])) == 55  # 26 + 25 + `OR ` and one space
    assert or_batches(["a@x"], both, max_values=20, max_bytes=55) == [["a@x"]]
    with pytest.raises(UnsafeSearchValue):
        or_batches(["a@x"], both, max_values=20, max_bytes=54)


def test_or_batches_edge_cases() -> None:
    assert or_batches([], ("Message-ID",), max_values=20) == []
    with pytest.raises(ValueError, match="max_values must be positive"):
        or_batches(["a@x"], ("Message-ID",), max_values=0)
    bad_caps: list[Any] = [0, -1, 1.5, "4000", True, None]
    for cap in bad_caps:
        with pytest.raises(ValueError, match="max_bytes must be positive") as caught:
            or_batches(["a@x"], ("Message-ID",), max_values=20, max_bytes=cap)
        assert type(caught.value) is ValueError
    bad_counts: list[Any] = [True, 2.0]
    for count in bad_counts:
        with pytest.raises(ValueError, match="max_values must be positive"):
            or_batches(["a@x"], ("Message-ID",), max_values=count)
    with pytest.raises(UnsafeSearchValue):
        or_batches(["a" * 50 + "@x"], ("Message-ID",), max_values=20, max_bytes=40)  # one leaf alone is too long
    with pytest.raises(UnsafeSearchValue):
        or_batches(["a@x", 'b"@x'], ("Message-ID",), max_values=20)
    # Iterators are read once: the fields apply to every value, not only to the first.
    assert or_batches(iter(["a@x", "b@x"]), iter(("In-Reply-To", "References")), max_values=1) == [["a@x"], ["b@x"]]


def test_batch_cap_constants() -> None:
    # Spec 080 §5.4; named constants so that Design B is a change of constants (ruling E-im5).
    assert (LOCATE_BATCH_VALUES, LOOKUP_BATCH_VALUES, MAX_THRID_VALUES, MAX_OR_BYTES) == (20, 10, 20, 4000)


def test_lookup_batches_ten_ids_twenty_keys() -> None:
    both = ("In-Reply-To", "References")
    values = ids(25, 2)
    batches = or_batches(values, both, max_values=LOOKUP_BATCH_VALUES)
    assert [len(batch) for batch in batches] == [10, 10, 5]
    assert [value for batch in batches for value in batch] == values
    for batch in batches:
        built = header_or(both, batch)
        assert built.count(b"HEADER ") == 2 * len(batch)
        assert len(built) <= MAX_OR_BYTES
    long_values = [f"{index:02d}{'v' * 286}@example.org" for index in range(25)]
    assert {len(value) for value in long_values} == {300}
    assert all(MESSAGE_ID_PATTERN.fullmatch(value) for value in long_values)
    long_batches = or_batches(long_values, both, max_values=LOOKUP_BATCH_VALUES)
    assert [value for batch in long_batches for value in batch] == long_values
    assert max(len(batch) for batch in long_batches) < LOOKUP_BATCH_VALUES  # the byte cap comes first
    for index, batch in enumerate(long_batches):
        assert len(header_or(both, batch)) <= MAX_OR_BYTES
        if index + 1 < len(long_batches):
            assert len(header_or(both, [*batch, long_batches[index + 1][0]])) > MAX_OR_BYTES


# --- Gmail thread ids ---------------------------------------------------------------------------------------------


def test_thrid_or_balanced_shapes() -> None:
    assert thrid_or([7]) == b"X-GM-THRID 7"
    assert thrid_or([1, 2**64 - 1]) == b"OR X-GM-THRID 1 X-GM-THRID 18446744073709551615"
    assert thrid_or([1, 2, 3]) == b"OR (OR X-GM-THRID 1 X-GM-THRID 2) X-GM-THRID 3"
    assert thrid_or([0, 5, 0, 5, 6]) == b"OR (OR X-GM-THRID 0 X-GM-THRID 5) X-GM-THRID 6"  # duplicates removed in order
    assert paren_depth(thrid_or(range(1, 21))) <= 6
    assert criteria(b"NOT DRAFT", thrid_or([1, 2])) == b"NOT DRAFT OR X-GM-THRID 1 X-GM-THRID 2"


def test_thrid_values_refused() -> None:
    bad_values: list[Any] = [-1, 2**64, True, False, "5", 5.0, None, b"5"]
    for value in bad_values:
        with pytest.raises(ValueError, match="thread id refused") as caught:
            thrid_or([1, value])
        assert str(caught.value) == "thread id refused"  # fixed text: never the refused value
        with pytest.raises(ValueError, match="thread id refused"):
            thrid_batches([1, value])
    bad_sequences: list[Any] = ["5", b"5"]
    for sequence in bad_sequences:
        with pytest.raises(ValueError, match="not one string"):
            thrid_or(sequence)
        with pytest.raises(ValueError, match="not one string"):
            thrid_batches(sequence)
    with pytest.raises(ValueError, match="no thread ids"):
        thrid_or([])
    assert thrid_batches([]) == []


def test_thrid_batches_caps() -> None:
    values = list(range(1000, 1045))
    batches = thrid_batches(values)
    assert [len(batch) for batch in batches] == [20, 20, 5]
    assert [value for batch in batches for value in batch] == values
    assert thrid_batches([3, 1, 3, 2, 1], max_values=2) == [[3, 1], [2]]  # duplicates removed in order
    largest = [2**64 - 1 - index for index in range(MAX_THRID_VALUES)]
    assert len(thrid_or(largest)) <= MAX_OR_BYTES  # 20 keys of the largest id fit one batch
    assert thrid_batches(largest) == [largest]
    small = list(range(1, 10))  # `X-GM-THRID n` is 12 bytes; two keys under one OR are 28
    assert thrid_batches(small, max_bytes=28) == [[1, 2], [3, 4], [5, 6], [7, 8], [9]]
    assert thrid_batches(small, max_bytes=27) == [[value] for value in small]
    for cap in range(31, 400, 5):
        capped = thrid_batches(largest, max_bytes=cap)
        assert [value for batch in capped for value in batch] == largest
        for index, batch in enumerate(capped):
            assert len(thrid_or(batch)) <= cap
            if index + 1 < len(capped):
                assert len(thrid_or([*batch, capped[index + 1][0]])) > cap
    bad_caps: list[Any] = [0, -1, True, 1.5, None]
    for cap in bad_caps:
        with pytest.raises(ValueError, match="max_values must be positive"):
            thrid_batches(small, max_values=cap)
        with pytest.raises(ValueError, match="max_bytes must be positive"):
            thrid_batches(small, max_bytes=cap)
    with pytest.raises(ValueError, match="max_bytes below one thread id key"):
        thrid_batches([2**64 - 1], max_bytes=30)  # one key alone is longer than the cap


# --- quoting and the final criteria -------------------------------------------------------------------------------


def test_quoted_accepts_printable_ascii() -> None:
    assert quoted("a@x") == b'"a@x"'
    assert quoted("a(b)%*]{5}@x y") == b'"a(b)%*]{5}@x y"'


def test_quoted_accepts_every_message_id_character() -> None:
    allowed = "".join(chr(code) for code in range(0x110) if MESSAGE_ID_PATTERN.fullmatch(chr(code)))
    assert len(allowed) > 80
    assert quoted(allowed) == b'"' + allowed.encode("ascii") + b'"'


@pytest.mark.parametrize(
    "value",
    [
        'a"b@x',
        "a\\b@x",
        "a\r\nb@x",
        "a\nb",
        "a\rb",
        "a\x00b",
        "a\tb",
        "a\x7fb",
        "a\x1bb",
        "äb@x",
        "a\u2028b",
        "",
        "X-GM-RAW@example.org",
        "x-gm-raw@example.org",
        "a.X-Gm-Raw.b@example.org",
    ],
)
def test_quoted_refuses_quote_backslash_control_non_ascii(value: str) -> None:
    with pytest.raises(UnsafeSearchValue) as caught:
        quoted(value)
    assert str(caught.value) == "search value refused"  # fixed text: never the refused value


def test_search_value_ok_matches_quoted() -> None:
    for value in ["a@x", "a(b)%*]{5}@x", "X-GM-RA", "X-GM-RA@W", "x-gm_raw@example.org"]:
        assert search_value_ok(value)
        assert quoted(value) == b'"' + value.encode() + b'"'
    bad_values: list[Any] = [
        'a"b',
        "a\\b",
        "a\r\nb",
        "",
        "äb",
        "X-GM-RAW@example.org",
        "X-GM-RAWX",
        "id.x-gm-raw",
        b"a@x",
        None,
    ]
    for value in bad_values:
        assert not search_value_ok(value)
        with pytest.raises(UnsafeSearchValue):
            quoted(value)


def test_filtered_values_round_trip_through_batches_and_criteria() -> None:
    # Third-party refs are filtered one by one with search_value_ok; one bad ref must not cost its batch.
    refs = [f"{index:02d}@example.org" for index in range(25)]
    refs[3] = "X-GM-RAW@example.org"
    refs[17] = 'a"b@example.org'
    refs[21] = "x-gm-raw.21@example.org"
    usable = [ref for ref in refs if search_value_ok(ref)]
    assert len(usable) == 22
    batches = or_batches(usable, ("In-Reply-To", "References"), max_values=20)
    assert [len(batch) for batch in batches] == [20, 2]
    for batch in batches:
        built = criteria(b"NOT DRAFT", header_or(("In-Reply-To", "References"), batch))
        assert built.startswith(b"NOT DRAFT OR ")
    with pytest.raises(UnsafeSearchValue):
        or_batches(refs, ("Message-ID",), max_values=20)  # unfiltered: the first bad value raises


@pytest.mark.parametrize("value", [b"a@x", None, 5])
def test_quoted_refuses_non_str(value: Any) -> None:
    with pytest.raises(UnsafeSearchValue):
        quoted(value)


@pytest.mark.parametrize(
    "parts",
    [
        (b"NOT DRAFT", b'HEADER Message-ID "a\r\nb"'),
        (b"NOT DRAFT", b'HEADER Message-ID "a\nb"'),
        (b"NOT DRAFT", b'HEADER Message-ID "a\rb"'),
        (b"NOT DRAFT", b'HEADER Message-ID "a\x00b"'),
        (b"NOT DRAFT", 'HEADER Message-ID "äb"'.encode()),
        (b"NOT DRAFT", b'X-GM-RAW "in:sent"'),
        (b"NOT DRAFT", b'x-gm-raw "in:sent"'),
        (b"NOT DRAFT", b"HEADER Message-ID {5}"),
        (b"NOT DRAFT", b""),
        (b"SINCE 06-Oct-2026",),
        (b"NOT DRAFTED",),
        (),
    ],
)
def test_criteria_refuses_crlf_nul_non_ascii_and_x_gm_raw(parts: tuple[bytes, ...]) -> None:
    with pytest.raises(UnsafeSearchValue) as caught:
        criteria(*parts)
    assert str(caught.value) == "search criteria refused"


def test_criteria_refuses_non_bytes_parts() -> None:
    bad_parts: list[Any] = ["NOT DRAFT", bytearray(b"NOT DRAFT"), None]
    for part in bad_parts:
        with pytest.raises(UnsafeSearchValue):
            criteria(part)


def test_criteria_joins_with_single_spaces() -> None:
    window = window_keys(since_day=date(2026, 10, 19), before_day=None, unread_only=False)
    assert criteria(window) == b"NOT DRAFT SINCE 19-Oct-2026"
    assert criteria(window, gmail_label_keys("sent")) == (
        b'NOT DRAFT SINCE 19-Oct-2026 X-GM-LABELS "\\\\Sent" NOT X-GM-LABELS "\\\\Inbox"'
    )
    assert criteria(b"NOT DRAFT", header_or(("Message-ID",), ["a@x"])) == b'NOT DRAFT HEADER Message-ID "<a@x>"'
    assert criteria(b"NOT DRAFT") == b"NOT DRAFT"


def test_gmail_label_keys_exact_bytes() -> None:
    assert gmail_label_keys("sent") == b'X-GM-LABELS "\\\\Sent" NOT X-GM-LABELS "\\\\Inbox"'
    assert gmail_label_keys("archive") == b'NOT X-GM-LABELS "\\\\Inbox" NOT X-GM-LABELS "\\\\Sent"'
    with pytest.raises(ValueError, match="unknown Gmail label filter"):
        gmail_label_keys("trash")  # type: ignore[arg-type]  # outside GmailLabelFilter on purpose


def test_gmail_sent_label_key_is_the_one_sent_literal() -> None:
    # P10: the reply step's key is `\Sent` with or without `\Inbox`, the same literal as the `sent` list filter's.
    assert GMAIL_SENT_LABEL_KEY == b'X-GM-LABELS "\\\\Sent"'
    assert gmail_label_keys("sent").startswith(GMAIL_SENT_LABEL_KEY + b" ")


def test_gmail_label_keys_select_by_label_in_the_fake() -> None:
    day = datetime(2026, 10, 19, 8, 0, tzinfo=UTC)
    messages = {
        1: FakeMessage(1, day, labels=(b"\\Sent",)),
        2: FakeMessage(2, day, labels=(b"\\Inbox",)),
        3: FakeMessage(3, day, labels=(b"\\Sent", b"\\Inbox")),
        4: FakeMessage(4, day, labels=()),
    }
    clock = FakeClock(1000.0)
    server = FakeImapServer([FakeFolder("[Gmail]/All Mail", messages=messages)], clock=clock)
    guard = GuardedImapClient(server.client(), CallDeadline.start(clock), registry_inbox_raw="INBOX")
    guard.login("user", "secret")
    guard.examine("[Gmail]/All Mail")
    window = window_keys(since_day=date(2026, 10, 19), before_day=None, unread_only=False)
    assert guard.search(criteria(window, gmail_label_keys("sent"))) == [1]
    assert guard.search(criteria(window, gmail_label_keys("archive"))) == [4]


def test_refusals_are_raises_not_asserts() -> None:
    source = (ROOT / "src/mcp_hub/providers/imap_search.py").read_text()
    assert not [node for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Assert)]
    code = """
from mcp_hub.providers.imap_search import UnsafeSearchValue, criteria, header_or, or_batches, quoted

cases = [
    lambda: quoted('a"b'),
    lambda: quoted("a\\\\b"),
    lambda: quoted("X-GM-RAW@example.org"),
    lambda: or_batches(["a" * 80], ("Message-ID",), max_values=5, max_bytes=40),
    lambda: criteria(b"NOT DRAFT", b"a\\r\\nb"),
    lambda: criteria(b'NOT DRAFT X-GM-RAW "x"'),
    lambda: criteria(b"SINCE 01-Jan-2026"),
]
refused = 0
for case in cases:
    try:
        case()
    except UnsafeSearchValue:
        refused += 1
print(__debug__, refused, len(cases))
"""
    result = subprocess.run(
        [sys.executable, "-O", "-c", code],
        capture_output=True,
        text=True,
        check=True,
        env={"PYTHONPATH": str(ROOT / "src"), "PATH": ""},
    )
    assert result.stdout.split() == ["False", "7", "7"]
