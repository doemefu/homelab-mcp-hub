import random

import pytest

from mcp_hub.sanitize import (
    FIELD_LIMITS,
    TRUNCATION_MARKER,
    clean,
    clean_flagged,
    html_to_text,
    truncate,
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


def test_timezone_names_do_not_depend_on_the_system_zone_database(monkeypatch: pytest.MonkeyPatch) -> None:
    # Linux system zoneinfo directories contain `localtime` and `posixrules`; the IANA list must not.
    import mcp_hub.sanitize as sanitize

    monkeypatch.setattr(sanitize, "available_timezones", lambda: {"localtime", "posixrules"}, raising=False)
    sanitize._iana_names.cache_clear()
    try:
        assert validate_timezone("localtime") is None
        assert validate_timezone("posixrules") is None
        assert validate_timezone("Factory") is None
        assert validate_timezone("Europe/Zurich") == "Europe/Zurich"
    finally:
        sanitize._iana_names.cache_clear()


def _has_surrogate(text: str) -> bool:
    return any(0xD800 <= ord(ch) <= 0xDFFF for ch in text)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("M\udcfcller", "M�ller"),  # raw Latin-1 byte, escaped by the email parser: unknown charset
        ("J\udcc3\udcbcrg", "Jürg"),  # raw UTF-8 bytes, escaped by the email parser: restored
        ("\udcff", "�"),
        ("a\ud800b", "a�b"),  # a lone surrogate outside the escape range
        ("ok", "ok"),
    ],
)
def test_lone_surrogates_never_leave_the_sanitiser(raw: str, expected: str) -> None:
    assert clean(raw, 100) == expected
    assert clean_flagged(raw, 100, multiline=True)[0] == expected
    assert clean(raw, 100).encode("utf-8")  # strict encoding works


def test_truncate_and_html_to_text_return_no_surrogates() -> None:
    assert not _has_surrogate(truncate("x\udcff" * 100, 50))
    assert not _has_surrogate(html_to_text("<p>a\udcffb</p>"))


def test_html_converter_keeps_constant_state_on_unclosed_tags() -> None:
    # The region rule needs O(1) state: 256 KiB of unclosed tags leaves no per-element bookkeeping behind.
    from html.parser import HTMLParser

    from mcp_hub.sanitize import _TextExtractor

    parser = _TextExtractor()
    parser.feed("<b>x" * 65_536)
    parser.close()
    own = {name: value for name, value in vars(parser).items() if name not in vars(HTMLParser()) and name != "parts"}
    assert all(isinstance(value, int | str | bool | type(None)) for value in own.values()), own
    assert "".join(parser.parts).count("x") == 65_536


def test_unclosed_tags_convert_quickly() -> None:
    import time

    started = time.perf_counter()
    html_to_text("<b>x" * 65_536 + "<div>y" * 1_000)
    assert time.perf_counter() - started < 5.0  # generous guard; was about 28 s before the depth cap


def test_hidden_element_beyond_the_depth_cap_still_hides() -> None:
    deep = "<div>" * 400
    text = html_to_text(f"{deep}visible<span hidden>SECRET</span><div style='display:none'>MORE</div>")
    assert "visible" in text
    assert "SECRET" not in text
    assert "MORE" not in text


def test_implicitly_closed_elements_do_not_fill_the_depth_cap() -> None:
    # Legacy mail: unclosed table rows and cells, which browsers close implicitly, then a hidden preheader.
    rows = "<table>" + "<tr><td>cell" * 150
    text = html_to_text(f"{rows}<p>Hello<span style='display:none'>PREHEADER</span><p>Your invoice is attached.")
    assert "Your invoice is attached." in text
    assert "PREHEADER" not in text


def test_hiding_implicitly_closed_element_still_hides() -> None:
    text = html_to_text("<table><tr><td hidden>SECRET</td><td>shown</td></tr></table>")
    assert "SECRET" not in text
    assert "shown" in text


@pytest.mark.parametrize(
    "raw",
    [
        "\u202e".encode(),  # RIGHT-TO-LEFT OVERRIDE
        "\u200b".encode(),  # ZERO WIDTH SPACE
    ],
)
def test_escaped_invisible_characters_are_restored_before_filtering(raw: bytes) -> None:
    # Raw UTF-8 header bytes arrive as surrogate escapes (email parser); the clean-up must run before the
    # control/format filter, not only in truncate().
    escaped = raw.decode("ascii", "surrogateescape")
    assert clean("a" + escaped + "b", 50) == "ab"


@pytest.mark.parametrize(
    "html",
    [
        "<td style='display:none'><table><tr><td>inner</td></tr></table>SECRET</td>shown",
        "<li hidden><ul><li>x</li></ul>SECRET</li>shown",
        "<tr hidden><td><table><tr><td>i</td></tr></table>SECRET</td></tr>shown",
    ],
)
def test_nested_same_name_end_tag_does_not_close_an_outer_hiding_element(html: str) -> None:
    assert "SECRET" not in html_to_text(html)


def _nestings() -> list[str]:
    cases = []
    for tag in ("td", "li", "tr", "p"):
        for depth in range(1, 6):
            nested = f"<{tag}>" * depth + "inner" + f"</{tag}>" * depth
            siblings = "".join(f"<{tag}>inner{n}</{tag}>" for n in range(depth))
            unclosed = f"<{tag}>inner" * depth
            for inside in (nested, siblings, unclosed):
                cases.append(f"<{tag} hidden>{inside}SECRET</{tag}>after")
                cases.append(f"<div><{tag} style='display:none'>{inside}SECRET</{tag}></div>after")
    return cases


@pytest.mark.parametrize("html", _nestings())
def test_hidden_implicitly_closed_elements_never_reveal_secret(html: str) -> None:
    assert "SECRET" not in html_to_text(html)


def test_hidden_sibling_paragraph_closes_without_hiding_the_rest() -> None:
    # Unclosed paragraphs before a hidden preheader paragraph must not keep the preheader open to the end.
    text = html_to_text("<p>Hello<p style='display:none'>PREHEADER</p><p>Your invoice is attached.")
    assert "PREHEADER" not in text
    assert "Your invoice is attached." in text


def _random_tree(rng: random.Random, depth: int, hidden: bool) -> str:
    parts = []
    for _ in range(rng.randrange(1, 4)):
        if depth > 0 and rng.random() < 0.7:
            tag = rng.choice(["td", "li", "tr", "p", "div", "span", "table", "ul", "dd", "option"])
            hides = rng.random() < 0.25
            inner = _random_tree(rng, depth - 1, hidden or hides)
            # Implicitly closed, non-hiding elements sometimes lose their end tag, as in legacy mail.
            omit = tag in {"td", "li", "tr", "p", "dd", "option"} and not hides and rng.random() < 0.5
            parts.append(f"<{tag}{' hidden' if hides else ''}>{inner}{'' if omit else f'</{tag}>'}")
        else:
            parts.append("SECRET" if hidden else "ok")
    return "".join(parts)


def test_text_inside_hidden_elements_never_appears_in_random_documents() -> None:
    rng = random.Random(7)  # noqa: S311 - deterministic test data
    for _ in range(3000):
        html = _random_tree(rng, rng.randrange(1, 7), False)
        assert "SECRET" not in html_to_text(html), html


def test_end_tag_closes_a_tracked_hiding_cell_while_deeper_elements_overflow() -> None:
    # A hiding cell below the cap, more than MAX_HTML_DEPTH open elements inside it, then its end tag: the cell's
    # content stays hidden and the text after the cell is shown, as in a browser.
    text = html_to_text("<table><tr><td hidden>SECRET" + "<div>" * 300 + "</td>after")
    assert "SECRET" not in text
    assert "after" in text
