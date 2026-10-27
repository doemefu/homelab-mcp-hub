import base64
import codecs
import encodings
import encodings.aliases
import pkgutil
import quopri
import random
import time
from collections.abc import Callable

import pytest

from mcp_hub.providers.mime import (
    DENIED_CODECS,
    Part,
    attachment_parts,
    choose_text_part,
    decode_part,
    safe_decode,
    walk,
)

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


@pytest.mark.parametrize(
    "charset",
    ["utf-8\x00", "\x00", "utf-\ud800", "\udcff", "x" * 10_000, "x-unknown-7", "", None],
    ids=["nul", "only-nul", "surrogate", "lone-surrogate", "overlong", "unknown", "empty", "none"],
)
def test_safe_decode_never_raises_for_a_hostile_charset(charset: str | None) -> None:
    """Security SC1: a charset name from the wire (an RFC 2231 `%00`, a lone surrogate) never makes decoding raise;
    it falls back to UTF-8 with replacement."""
    assert safe_decode("Grüezi".encode(), charset) == "Grüezi"
    assert "\ufffd" in safe_decode(b"\xff\xfe bad", charset)


def test_unknown_charset_falls_back_to_utf8_with_replacement() -> None:
    part = Part("1", "text/plain", {"charset": "x-unknown-7"}, "8bit", 10, None, None)
    assert decode_part("ok ✓".encode(), part) == "ok ✓"
    assert "�" in decode_part(b"\xff\xfe bad", Part("1", "text/plain", {}, "8bit", 5, None, None))


def test_denied_codecs_are_canonical_names() -> None:
    for name in DENIED_CODECS:
        assert name == codecs.lookup(name).name


@pytest.mark.parametrize(
    "charset",
    ["punycode", "PUNYCODE", "idna", "unicode-escape", "unicode_escape", "raw_unicode_escape", "unicode_escape "],
)
def test_denied_codecs_fall_back_to_utf8(charset: str) -> None:
    raw = "Grüezi \\u00e9 xn--a-ecp.ru -".encode() + b"\xff"
    assert safe_decode(raw, charset) == raw.decode("utf-8", "replace")


def test_escape_codecs_do_not_interpret_backslashes() -> None:
    raw = b"a\\u0041\\x41\\N{BULLET}\\n"
    for charset in ("unicode-escape", "raw-unicode-escape"):
        assert safe_decode(raw, charset) == raw.decode("ascii")


_SIZE = 64 * 1024
_RATIO_BOUND = 2.5  # a 2x input may take at most 2.5x the time (Q-f)
_HOSTILE: dict[str, Callable[[int], bytes]] = {
    "ascii": lambda n: b"a" * n,
    "hyphens": lambda n: b"-" * n,
    "high_bytes": lambda n: b"\xff" * n,
    "c1_bytes": lambda n: b"\x80\x81" * (n // 2),
    "backslashes": lambda n: b"\\" * n,
    "unicode_escapes": lambda n: b"\\u00e9" * (n // 6),
    "punycode_like": lambda n: b"xn--" + b"a-" * (n // 2),
    "escape_prefixes": lambda n: b"\\U\\x\\N{" * (n // 6),
    "random": lambda n: random.Random(0).randbytes(n),  # noqa: S311 - deterministic test data
    "punycode_digits": lambda n: b"-" + b"9" * (n - 1),
    "utf7_shift": lambda n: b"+" + b"A" * (n - 1),
    "iso2022_escapes": lambda n: b"\x1b$B" * (n // 3),
    "named_escapes": lambda n: b"\\N{LATIN SMALL LETTER A}" * (n // 24),
}


def _best_of(fn: Callable[[], object], runs: int = 5) -> float:
    best = float("inf")
    for _ in range(runs):
        start = time.perf_counter()
        fn()
        best = min(best, time.perf_counter() - start)
    return best


def _assert_linear(charset: str, payload: str) -> None:
    make = _HOSTILE[payload]
    small, large = make(_SIZE), make(2 * _SIZE)
    t_small = _best_of(lambda: safe_decode(small, charset))
    t_large = _best_of(lambda: safe_decode(large, charset))
    # 5 ms floor against timer noise on trivial inputs
    assert t_large <= max(t_small * _RATIO_BOUND, 0.005), f"{charset}/{payload}: {t_small:.4f}s -> {t_large:.4f}s"


def _text_codecs() -> list[str]:
    candidates = {m.name for m in pkgutil.iter_modules(encodings.__path__)} | set(encodings.aliases.aliases.values())
    candidates |= DENIED_CODECS
    names = []
    for name in sorted(candidates - {"aliases", "charmap", "undefined"}):  # the last two cannot decode on their own
        try:
            info = codecs.lookup(name)
        except LookupError:
            continue
        if getattr(info, "_is_text_encoding", True):
            names.append(name)
    return names


def test_decode_time_bound_over_text_codecs() -> None:
    names = _text_codecs()
    assert len(names) >= 110
    for name in names:
        for payload in _HOSTILE:
            _assert_linear(name, payload)


@pytest.mark.parametrize("charset", sorted(DENIED_CODECS))
def test_denied_codec_timing_each(charset: str) -> None:
    for payload in _HOSTILE:
        _assert_linear(charset, payload)
    punycode_like = _HOSTILE["punycode_like"](_SIZE)
    assert safe_decode(punycode_like, charset) == punycode_like.decode("utf-8", "replace")


def test_malformed_encoded_word_in_a_part_parameter_keeps_the_raw_value() -> None:
    odd = (b"application", b"pdf", (b"name", b"=?utf-8?b?abcde?="), None, None, b"base64", 10, None, None, None, None)
    [part] = walk(odd)
    assert part.params["name"] == "=?utf-8?b?abcde?="
