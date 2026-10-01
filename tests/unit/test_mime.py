import base64
import quopri

import pytest

from mcp_hub.providers.mime import Part, attachment_parts, choose_text_part, decode_part, walk

PLAIN = (b"text", b"plain", (b"charset", b"utf-8"), None, None, b"7bit", 120, 4, None, None, None, None)
HTML = (b"text", b"html", (b"charset", b"iso-8859-1"), None, None, b"quoted-printable", 300, 9, None, None, None, None)
PDF = (
    b"application",
    b"pdf",
    (b"name", b"=?utf-8?q?R=C3=A9sum=C3=A9.pdf?="),
    None,
    None,
    b"base64",
    4000,
    None,
    (b"attachment", (b"filename", b"=?utf-8?q?R=C3=A9sum=C3=A9.pdf?=")),
    None,
    None,
)
ALTERNATIVE = ([PLAIN, HTML], b"alternative", (b"boundary", b"b1"), None, None, None)
MIXED = ([ALTERNATIVE, PDF], b"mixed", (b"boundary", b"b0"), None, None, None)


def test_single_part_is_section_1() -> None:
    [part] = walk(PLAIN)
    assert (part.section, part.mime_type, part.encoding, part.size) == ("1", "text/plain", "7bit", 120)


def test_nested_multipart_section_numbers() -> None:
    assert [(p.section, p.mime_type) for p in walk(MIXED)] == [
        ("1.1", "text/plain"),
        ("1.2", "text/html"),
        ("2", "application/pdf"),
    ]


def test_plain_is_preferred_over_html_and_attachments_listed() -> None:
    parts = walk(MIXED)
    chosen = choose_text_part(parts)
    assert chosen is not None
    assert chosen.section == "1.1"
    [attachment] = attachment_parts(parts, chosen)
    assert (attachment.filename, attachment.disposition, attachment.size) == ("Résumé.pdf", "attachment", 4000)


def test_html_is_used_when_no_plain_part_exists() -> None:
    chosen = choose_text_part(walk(([HTML, PDF], b"mixed", None, None, None, None)))
    assert chosen is not None
    assert (chosen.mime_type, chosen.section) == ("text/html", "1")


def test_text_attachment_is_not_chosen_as_body() -> None:
    note = (
        b"text",
        b"plain",
        None,
        None,
        None,
        b"7bit",
        10,
        1,
        None,
        (b"attachment", (b"filename", b"n.txt")),
        None,
        None,
    )
    assert choose_text_part(walk(([note], b"mixed", None, None, None, None))) is None


def test_message_rfc822_is_a_single_attachment_leaf() -> None:
    inner = (b"message", b"rfc822", None, None, None, b"7bit", 900, (), PLAIN, 20, None, (b"attachment", None), None)
    parts = walk(([PLAIN, inner], b"mixed", None, None, None, None))
    assert [p.mime_type for p in parts] == ["text/plain", "message/rfc822"]
    assert [p.section for p in attachment_parts(parts, parts[0])] == ["2"]


def test_decode_truncated_base64_and_quoted_printable() -> None:
    encoded = base64.encodebytes("Grüezi mitenand".encode())[:-3]  # cut mid-quantum like a partial fetch
    b64 = Part("1", "text/plain", {"charset": "utf-8"}, "base64", 100, None, None)
    assert decode_part(encoded, b64).startswith("Grüezi")
    qp = Part("1", "text/plain", {"charset": "iso-8859-1"}, "quoted-printable", 100, None, None)
    assert decode_part(quopri.encodestring("Café".encode("iso-8859-1")), qp) == "Café"


@pytest.mark.parametrize(
    ("encoded", "text"),
    [(b"YWI=", "ab"), (b"YWJjZA==", "abcd"), (b"YWJj", "abc"), (base64.b64encode("Grüezi".encode()), "Grüezi")],
)
def test_complete_base64_parts_round_trip_exactly(encoded: bytes, text: str) -> None:
    assert decode_part(encoded, Part("1", "text/plain", {"charset": "utf-8"}, "base64", 10, None, None)) == text


@pytest.mark.parametrize("charset", ["rot13", "base64", "hex", "zlib", "idna", "x-unknown-7", ""])
def test_non_text_charsets_fall_back_to_utf8(charset: str) -> None:
    part = Part("1", "text/plain", {"charset": charset}, "8bit", 10, None, None)
    assert decode_part("Grüezi".encode(), part) == "Grüezi"
    assert isinstance(decode_part(b"\xff\xfe\x00bad", part), str)  # never raises


def test_unknown_charset_falls_back_to_utf8_with_replacement() -> None:
    part = Part("1", "text/plain", {"charset": "x-unknown-7"}, "8bit", 10, None, None)
    assert decode_part("ok ✓".encode(), part) == "ok ✓"
    assert "�" in decode_part(b"\xff\xfe bad", Part("1", "text/plain", {}, "8bit", 5, None, None))
