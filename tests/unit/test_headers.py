import time

import pytest

from mcp_hub.ids import ThreadKey
from mcp_hub.providers import headers as h
from mcp_hub.providers.headers import (
    ParsedHeaders,
    bare_message_id,
    imap_thread_key,
    is_own,
    msg_id_tokens,
    normalise_subject,
    parse_summary_headers,
    subject_digest,
)


def raw(*lines: str) -> bytes:
    return ("\r\n".join(lines) + "\r\n\r\n").encode()


def test_constants_and_items() -> None:
    assert h.SUMMARY_HEADER_ITEM == (
        "BODY.PEEK[HEADER.FIELDS (FROM TO CC SUBJECT MESSAGE-ID IN-REPLY-TO REFERENCES)]<0.65536>"
    )
    assert h.candidate_header_item(["MESSAGE-ID", "FROM"]) == "BODY.PEEK[HEADER.FIELDS (MESSAGE-ID FROM)]<0.2048>"
    assert h.candidate_header_item(["MESSAGE-ID"]) == "BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)]<0.2048>"


def test_header_bytes() -> None:
    assert h.header_bytes({b"UID": 1, b"BODY[HEADER.FIELDS (FROM)]<0>": b"x"}) == b"x"
    assert h.header_bytes({b"UID": 1}) == b""


def test_basic_parse() -> None:
    p = parse_summary_headers(
        raw(
            "From: Alice Example <alice@example.org>",
            "To: Bob <bob@example.org>, carol@example.org",
            "Cc: Dan <dan@example.org>",
            "Subject: =?utf-8?q?Gr=C3=BCsse?=",
            "Message-ID: <m1@example.org>",
            "In-Reply-To: <p@example.org>",
            "References: <r1@example.org> <p@example.org>",
        )
    )
    assert p.from_name == "Alice Example"
    assert p.from_address == "alice@example.org"
    assert p.to == ("bob@example.org", "carol@example.org")
    assert p.cc == ("dan@example.org",)
    assert p.subject == "Grüsse"
    assert p.message_id == "m1@example.org"
    assert p.in_reply_to == "p@example.org"
    assert p.references == ("r1@example.org", "p@example.org")
    assert p.root == "r1@example.org"


@pytest.mark.parametrize(
    ("value", "expected"),
    [("<abc@x>", "abc@x"), ("  <abc@x>  ", "abc@x"), ("abc@x", "abc@x"), ("<<a@x>>", "a@x")],
)
def test_bare_message_id(value: str, expected: str) -> None:
    assert bare_message_id(value) == expected


def test_bare_message_id_none_and_empty() -> None:
    assert bare_message_id(None) is None
    assert bare_message_id("   ") is None


def test_first_message_id_header_wins() -> None:
    p = parse_summary_headers(raw("Message-ID: <first@example.org>", "Message-ID: <second@example.org>"))
    assert p.message_id == "first@example.org"


def test_tokenizer_comment_with_brackets() -> None:
    assert msg_id_tokens("(see <x@y>) <real@example.org>") == ["real@example.org"]


def test_tokenizer_quoted_string_skipped() -> None:
    assert msg_id_tokens('"a <q@x> b" <real@example.org>') == ["real@example.org"]


def test_tokenizer_folded_crlf() -> None:
    assert msg_id_tokens("<a@x>\r\n <b@x>\r\n\t<c@x>") == ["a@x", "b@x", "c@x"]
    p = parse_summary_headers(raw("References: <a@example.org>", " <b@example.org>", "Message-ID: <z@example.org>"))
    assert p.references == ("a@example.org", "b@example.org")


def test_tokenizer_comma_separated() -> None:
    assert msg_id_tokens("<a@x>,<b@x>, <c@x>") == ["a@x", "b@x", "c@x"]


def test_tokenizer_unbracketed() -> None:
    assert msg_id_tokens("a@x, b@x  c-no-at {bad}@x") == ["a@x", "b@x"]


def test_tokenizer_adjacent_brackets() -> None:
    assert msg_id_tokens("<a@x><b@x>") == ["a@x", "b@x"]


def test_tokenizer_unterminated_and_empty() -> None:
    assert msg_id_tokens("<a@x> <b@x") == ["a@x"]
    assert msg_id_tokens("<> <a@x>") == ["a@x"]
    assert msg_id_tokens("") == []


def test_tokenizer_duplicates_keep_first_and_own_id_dropped() -> None:
    assert msg_id_tokens("<a@x> <b@x> <a@x>") == ["a@x", "b@x"]
    p = parse_summary_headers(
        raw("Message-ID: <own@example.org>", "References: <a@example.org> <own@example.org> <a@example.org>")
    )
    assert p.references == ("a@example.org",)


def test_in_reply_to_phrase_form() -> None:
    p = parse_summary_headers(
        raw(
            "From: Alice <alice@example.org>",
            "To: bob@example.org",
            "In-Reply-To: Alice <alice@example.org> <real@example.org> <ref@example.org>",
            "References: <ref@example.org>",
            "Message-ID: <m@example.org>",
        )
    )
    assert p.in_reply_to == "ref@example.org"
    p = parse_summary_headers(
        raw(
            "From: Alice <alice@example.org>",
            "To: bob@example.org",
            "In-Reply-To: <alice@example.org> <bob@example.org> <real@example.org>",
            "Message-ID: <m@example.org>",
        )
    )
    assert p.in_reply_to == "real@example.org"
    p = parse_summary_headers(
        raw(
            "From: alice@example.org", "In-Reply-To: Alice <alice@example.org>'s message", "Message-ID: <m@example.org>"
        )
    )
    assert p.in_reply_to is None
    p = parse_summary_headers(raw("In-Reply-To: <only@example.org>", "Message-ID: <m@example.org>"))
    assert p.in_reply_to == "only@example.org"


def test_references_last_ten_and_long_tokens_skipped() -> None:
    ids = [f"<r{i}@example.org>" for i in range(14)]
    long_id = "<" + "a" * 301 + "@example.org>"
    # spec 4.9 follow-up: pattern-invalid tokens are skipped
    p = parse_summary_headers(raw("References: " + ids[0] + " " + " ".join(ids[1:]) + " " + long_id))
    assert p.references == tuple(f"r{i}@example.org" for i in range(4, 14))
    assert p.root == "r0@example.org"


def test_root_from_first_valid_references_entry() -> None:
    long_id = "<" + "a" * 301 + ">"
    p = parse_summary_headers(
        raw(
            "References: " + long_id + " <ok@example.org>",
            "In-Reply-To: <irt@example.org>",
            "Message-ID: <m@example.org>",
        )
    )
    assert p.root == "ok@example.org"


def test_root_falls_back_to_in_reply_to_then_own() -> None:
    p = parse_summary_headers(raw("In-Reply-To: <irt@example.org>", "Message-ID: <m@example.org>"))
    assert p.root == "irt@example.org"
    p = parse_summary_headers(raw("Message-ID: <m@example.org>"))
    assert p.root == "m@example.org"
    p = parse_summary_headers(raw("Subject: x"))
    assert p.root is None
    long_id = "<" + "a" * 301 + "@example.org>"
    p = parse_summary_headers(raw(f"In-Reply-To: {long_id}", "Message-ID: <m@example.org>", "Subject: S"))
    assert p.root == "m@example.org"
    p = parse_summary_headers(raw(f"In-Reply-To: {long_id}", f"Message-ID: {long_id}", "Subject: S"))
    assert p.root is None
    key = imap_thread_key("icloud", p)
    assert key is not None
    assert key.source == "subject"


def test_references_cut_by_the_64k_cap() -> None:
    entry = "<" + "a" * 60 + "@example.org> "
    text = "References: " + entry * 2000
    data = text.encode()[:65_536]
    p = parse_summary_headers(data)
    assert len(p.references) == 1  # duplicates collapse; the cut entry is dropped
    data = ("References: " + " ".join(f"<r{i}@example.org>" for i in range(5000))).encode()[:65_536]
    p = parse_summary_headers(data)
    assert p.root == "r0@example.org"
    assert len(p.references) == 10
    assert not p.references[-1].endswith("@example.or")


def test_nil_header_value() -> None:
    assert parse_summary_headers(None) == ParsedHeaders(None, None, (), (), None, None, None, (), None)
    assert parse_summary_headers(b"").root is None


@pytest.mark.parametrize(
    ("subject", "expected"),
    [
        ("Re: AW: Fwd: FW: WG: Re[2]: Hello  World", "hello world"),
        ("[list] Re: AW: X", "x"),
        ("Re : X", "x"),
        ("RE^2: X", "x"),
        ("Antw: X", "x"),
        ("Antwort: X", "x"),
        ("SV: X", "x"),
        ("Re[3]: [EXTERNAL] X", "x"),
        ("Re: X", "x"),
        ("Re\uff1a X", "x"),
        ("  Straße ", "strasse"),
        ("Re:", ""),
        ("Recap: X", "recap: x"),
        (None, ""),
    ],
)
def test_normalise_subject(subject: str | None, expected: str) -> None:
    assert normalise_subject(subject) == expected


def test_subject_round_cap_keeps_the_rest() -> None:
    assert normalise_subject("Re: " * 25 + "X") == "re: " * 5 + "x"


def test_one_removal_per_round() -> None:
    assert normalise_subject("[a] " * 25 + "X") == "[a] " * 5 + "x"
    assert normalise_subject("Re: [a] " * 11 + "X") == "re: [a] x"


def test_subject_precut_before_sanitising() -> None:
    # the 4,000-character pre-cut happens before clean() collapses the blank run
    assert normalise_subject("Re: " + " " * 5000 + "X") == ""


def test_long_subject_digest_ignores_truncation_marker() -> None:
    assert subject_digest("X" * 400) == subject_digest("Re: " + "X" * 400)
    assert len(normalise_subject("X" * 5000)) == 1000


def test_subject_digest() -> None:
    import hashlib

    assert subject_digest("Re: Hello") == hashlib.sha256(b"hello").hexdigest()[:32]
    assert subject_digest("Re:") is None
    assert subject_digest(None) is None


def test_imap_thread_key_sources() -> None:
    p = parse_summary_headers(raw("Message-ID: <m@example.org>", "References: <r@example.org>", "Subject: S"))
    assert imap_thread_key("icloud", p) == ThreadKey("icloud", "headers", "r@example.org")
    p = parse_summary_headers(raw("Subject: Re: Hello"))
    assert imap_thread_key("icloud", p) == ThreadKey("icloud", "subject", subject_digest("Hello"))
    p = parse_summary_headers(raw("Subject: Re:"))
    assert imap_thread_key("icloud", p) is None
    assert imap_thread_key("icloud", parse_summary_headers(None)) is None


@pytest.mark.parametrize(
    ("address", "own", "expected"),
    [
        ("Me@Example.org", {"me@example.org"}, True),
        ("Me+News@Example.org", {"me@example.org"}, True),
        ("me@example.org.evil", {"me@example.org"}, False),
        ("m.e@example.org", {"me@example.org"}, False),
        ("m.e+x@example.org", {"me@example.org"}, False),
        ("me@example.org", {"Me@Example.org"}, True),
        ("ME+tag@example.org", {"Me@Example.org"}, True),
        ("me+x@example.org", {"me+x@example.org"}, True),
        ("me+y@example.org", {"me+x@example.org"}, False),
        ("+x@example.org", {"@example.org"}, False),
        (None, {"me@example.org"}, False),
        ("", {"me@example.org"}, False),
    ],
)
def test_is_own(address: str | None, own: set[str], expected: bool) -> None:
    assert is_own(address, frozenset(own)) is expected


@pytest.mark.parametrize(
    "data",
    [
        b"\xff\xfe\x00From: \x80\x81 <a@b>\r\nSubject: =?bogus-charset?q?x?=\r\n\r\n",
        b'To: <<<>>>, "unterminated, (((\r\nCc: a@@@b;;;,\r\nMessage-ID: <\x00\x01@x>\r\n\r\n',
        b"From:\r\nFrom: \r\nSubject:\r\n \r\n \r\nReferences: <<<<<<\r\n",
        b"Subject: =?utf-8?b?!!!?=\r\nFrom: =?utf-8?q?=FF?= <a@b.c>\r\n",
        b"garbage without colon\r\n\r\n",
    ],
)
def test_hostile_header_bytes_do_not_raise(data: bytes) -> None:
    parsed = parse_summary_headers(data)
    subject_digest(parsed.subject)
    imap_thread_key("icloud", parsed)
    is_own(parsed.from_address, frozenset({"me@example.org"}))


def test_folded_message_id_fallback_has_no_line_break() -> None:
    p = parse_summary_headers(raw("Message-ID: abc", " def"))
    assert p.message_id is not None
    assert "\n" not in p.message_id
    assert "\r" not in p.message_id


def test_cpu_bounds() -> None:
    data = b"To: " + b"(" * 65_000 + b"\r\n\r\n"
    start = time.perf_counter()
    parse_summary_headers(data)
    assert time.perf_counter() - start < 0.5
    start = time.perf_counter()
    normalise_subject("re:" * 2000)
    assert time.perf_counter() - start < 0.5
    for payload in (b"\\", b".", b",", b"a\\", b'"', b"<", b"(a"):
        data = b"From: " + payload * 8192 + b"\r\nTo: " + payload * 8192 + b"\r\nCc: " + payload * 8192 + b"\r\n\r\n"
        start = time.perf_counter()
        parse_summary_headers(data)
        assert time.perf_counter() - start < 0.5, payload
    data = b"Subject: " + b"=?utf-8?q?" * 6000 + b"\r\n\r\n"
    start = time.perf_counter()
    parse_summary_headers(data)
    assert time.perf_counter() - start < 0.5
    data = b"References: " + b"<(" * 30_000 + b"\r\n\r\n"
    start = time.perf_counter()
    parse_summary_headers(data)
    assert time.perf_counter() - start < 0.5


def test_to_cc_cut_before_parsing() -> None:
    addrs = ", ".join(f"u{i}@example.org" for i in range(2000))
    p = parse_summary_headers(raw("To: " + addrs))
    assert 0 < len(p.to) < 2000
    assert len(p.to) <= 8192 // 15 + 1
