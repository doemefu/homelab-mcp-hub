import pytest

from mcp_hub.sanitize import (
    FIELD_LIMITS,
    TRUNCATION_MARKER,
    clean,
    clean_flagged,
    html_to_text,
    validate_content_type,
    validate_timezone,
)


def test_removes_zero_width_bidi_and_control_characters() -> None:
    dirty = "a\u200bb\u200fc\u2060d\u2064e\ufefff\u202ag\u202eh\u2066i\u2069j\x00k\x07l\x1bm\x7fn"
    assert clean(dirty, 300) == "abcdefghijklmn"


def test_rules_apply_to_addresses_and_names() -> None:
    assert clean("sen\u200bder@exa\u202emple.test", FIELD_LIMITS["address"]) == "sender@example.test"
    assert clean("Evil\u2066 Name\u2069", FIELD_LIMITS["from_name"]) == "Evil Name"


def test_keeps_newline_and_tab_in_multiline_text() -> None:
    assert clean("a\tb\r\nc", 100, multiline=True) == "a\tb\nc"


def test_removes_tag_characters_and_other_format_characters() -> None:
    smuggled = "ok" + "".join(chr(0xE0000 + ord(c)) for c in "ignore") + "\u00ad\u061c\u180e!"
    assert clean(smuggled, 100) == "ok!"
    assert clean("a\u2028b\u2029c", 100, multiline=True) == "a\nb\nc"


def test_nfc_normalisation() -> None:
    assert clean("e\u0301", 10) == "é"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("see https://track.example.test/p?id=1&u=2#x now", "see [link: track.example.test] now"),
        ("HTTP://Upper.Example.TEST/path", "[link: upper.example.test]"),
        ("www.example.test/a?b=c", "[link: www.example.test]"),
        ("ftp://files.example.test/x", "[link: files.example.test]"),
        ("mailto:someone@example.test?subject=x", "[mail link]"),
        ("https://user:pw@host.example.test:8443/x", "[link: host.example.test]"),
        ("https://", "[link]"),
    ],
)
def test_urls_are_reduced_to_their_host(raw: str, expected: str) -> None:
    assert clean(raw, 300) == expected


def test_truncation_marker_counts_towards_the_limit() -> None:
    text = clean("x" * 500, 300)
    assert len(text) == 300
    assert text.endswith(TRUNCATION_MARKER)
    assert clean_flagged("x" * 500, 300)[1] is True
    assert clean_flagged("short", 300) == ("short", False)


def test_blank_line_runs_collapse_in_multiline_text() -> None:
    assert clean("a\n\n\n\n  \n\nb", 100, multiline=True) == "a\n\nb"


def test_single_line_fields_collapse_all_whitespace() -> None:
    assert clean("  Re:\n  hello\t world  ", 300) == "Re: hello world"


def test_empty_and_none_give_empty_string() -> None:
    assert clean(None, 10) == ""
    assert clean("", 10) == ""


def test_html_drops_script_style_head_comments_and_images() -> None:
    html = (
        "<html><head><title>T</title><style>p{}</style></head><body>"
        "<script>alert(1)</script><!-- hidden comment --><p>Hello</p>"
        "<img src='https://x.example.test/a.png' alt='alt'>"
        "<p>World</p></body></html>"
    )
    assert clean(html_to_text(html), 300, multiline=True) == "Hello\n\nWorld"


@pytest.mark.parametrize(
    "hidden",
    [
        "<div hidden>SECRET</div>",
        "<span style='display:none'>SECRET</span>",
        "<span style='DISPLAY : NONE ; color:red'>SECRET</span>",
        '<div style="visibility: hidden">SECRET</div>',
        "<div style='display:none'><p>SECRET<b>more</b></p></div>",
    ],
)
def test_html_drops_hidden_elements(hidden: str) -> None:
    text = html_to_text(f"<p>before</p>{hidden}<p>after</p>")
    assert "SECRET" not in text
    assert "before" in text
    assert "after" in text


def test_html_unclosed_hidden_element_hides_the_rest() -> None:
    assert "SECRET" not in html_to_text("<p>shown</p><div hidden><p>SECRET")


def test_html_links_keep_text_not_target() -> None:
    text = clean(html_to_text("<a href='https://t.example.test/?u=1'>Click here</a>"), 100)
    assert text == "Click here"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("text/plain", "text/plain"),
        ("Application/PDF", "application/pdf"),
        ("image/svg+xml", "image/svg+xml"),
        ("text/plain; charset=utf-8", None),
        ("../../etc/passwd", None),
        ("text", None),
        ("x" * 200 + "/y", None),
        (None, None),
    ],
)
def test_content_type_validation(value: str | None, expected: str | None) -> None:
    assert validate_content_type(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Europe/Zurich", "Europe/Zurich"),
        ("UTC", "UTC"),
        ("America/New_York", "America/New_York"),
        ("Mars/Olympus_Mons", None),
        ("/etc/localtime", None),
        ("../zoneinfo/UTC", None),
        ("Europe/" + "x" * 60, None),
        ("", None),
        ("localtime", None),
        ("posixrules", None),
        (None, None),
    ],
)
def test_timezone_validation(value: str | None, expected: str | None) -> None:
    assert validate_timezone(value) == expected
