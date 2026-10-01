"""HTML-to-text hiding as a region rule (spec 080 §5.3 rule 2): regression corpus from review report 18 and a
generator-based check. Comments give what a browser shows."""

import random

import pytest

from mcp_hub.sanitize import html_to_text

# (html, text that must not appear, text that must appear or None)
CORPUS = [
    # F3 shape: unclosed inline tags; all text is visible in a browser.
    ("<b>x" * 1000 + "<p>end", None, "end"),
    # R2: 150 unclosed rows, hidden preheader span; the browser shows the sentence and hides the preheader.
    (
        "<table>" + "<tr><td>cell" * 150 + "<p>Hello<span style='display:none'>PREHEADER</span><p>Your invoice.",
        "PREHEADER",
        "Your invoice.",
    ),
    # R4: unterminated comment, CDATA and declaration run to the end of the document in a browser.
    ("<p>Hi</p><!-- SECRET", "SECRET", "Hi"),
    ("<p>Hi</p><!-- a > SECRET", "SECRET", "Hi"),
    ("<p>Hi</p><![CDATA[ a > SECRET", "SECRET", "Hi"),
    ("<p>Hi</p><! SECRET", "SECRET", "Hi"),
    # N-R1: a nested same-name end tag closes the inner element only; SECRET stays in the hidden outer one.
    ("<td style='display:none'><table><tr><td>inner</td></tr></table>SECRET</td>", "SECRET", None),
    ("<li hidden><ul><li>x</li></ul>SECRET</li>", "SECRET", None),
    ("<tr hidden><td><table><tr><td>i</td></tr></table>SECRET</td></tr>", "SECRET", None),
    # N-R5: </li> and </td> close a and b in a browser; the later </a> and </b> are ignored, SECRET stays hidden.
    ("<ul><li><a>x</li><li><div hidden></a>SECRET</div></li></ul>", "SECRET", "x"),
    ("<table><tr><td><b>x</td><td><span style='display:none'></b>SECRET</span></td></tr></table>", "SECRET", "x"),
    # N-R6: browsers ignore the slash on non-void elements, so these open hidden elements.
    ("<div hidden/>SECRET</div>after", "SECRET", "after"),
    ("<span style='display:none'/>SECRET</span>after", "SECRET", "after"),
    ("<table><tr><td hidden/>SECRET</td><td>after</td></tr></table>", "SECRET", "after"),
    ("<p hidden/>SECRET</p>after", "SECRET", "after"),
    ("<script/>SECRET</script>after", "SECRET", "after"),
    # N-R7: HTML5 ignores </span> while a div is open above it; SECRET stays hidden.
    ("<span><div hidden></span>SECRET</div>after", "SECRET", "after"),
    ("<p><span>x<p><div hidden></span>SECRET</div>after", "SECRET", "after"),
    # Mixed case and nested same-name elements inside a region.
    ("<DIV HIDDEN><div>a</div>SECRET</Div>after", "SECRET", "after"),
    # Raw-text elements inside a region: a browser reads everything up to their end tag as text.
    ("<div hidden><textarea></div>SECRET</textarea>MORE</div>after", "MORE", "after"),
    ("<head><title></head>SECRET</title></head><p>after", "SECRET", "after"),
    ("<span hidden><title></span>SECRET", "SECRET", None),
    # Other parsing rules inside a region: the rest of the document is hidden.
    ("<div hidden><svg></div>SECRET", "SECRET", None),
    ("<div hidden><plaintext></div>SECRET", "SECRET", None),
    # An ordinary HTML mail: the title inside head stays hidden and the body is shown.
    ("<html><head><title>T</title><style>p{}</style></head><body><p>Hello</p></body></html>", "T", "Hello"),
]


@pytest.mark.parametrize(("html", "hidden", "shown"), CORPUS)
def test_regression_corpus(html: str, hidden: str | None, shown: str | None) -> None:
    text = html_to_text(html)
    if hidden is not None:
        assert hidden not in text
    if shown is not None:
        assert shown in text


def test_implied_end_tag_over_hides_by_design() -> None:
    # Accepted over-hiding: <p hidden> relies on an implied end tag; the region lasts to the balancing </p>.
    text = html_to_text("<p hidden>SECRET<p>visible in a browser</p>after")
    assert "SECRET" not in text
    assert "visible in a browser" not in text  # the first </p> balances the inner <p>
    assert "after" not in text


NAMES = ["div", "span", "p", "td", "tr", "li", "b", "a", "table", "ul"]
RAW_TEXT = ["textarea", "title", "xmp", "iframe", "noembed", "noframes", "noscript"]
HIDE_REST = ["plaintext", "select", "svg", "math"]
HIDING_ATTRS = [" hidden", " style='display:none'", " style='VISIBILITY : hidden'", " HIDDEN"]


def _case(rng: random.Random, name: str) -> str:
    return name.upper() if rng.random() < 0.2 else name


def _document(rng: random.Random) -> tuple[str, list[str], list[str]]:
    """Token-level document plus the markers that must stay hidden and those that must appear.

    The generator tracks the rule itself (state: visible, region (X, n) with an optional raw-text element, or
    hidden to the end) and places a SECRET marker only where the rule hides and a VISIBLE marker only before the
    first hiding start tag."""
    parts: list[str] = []
    secrets: list[str] = []
    visibles: list[str] = []
    region: tuple[str, int] | None = None
    raw: str | None = None
    to_end = seen_hiding = False
    for index in range(rng.randrange(5, 40)):
        roll = rng.random()
        name = rng.choice(NAMES)
        if roll < 0.3:  # text
            if to_end or region is not None:
                parts.append(f" SECRET{index} ")
                secrets.append(f"SECRET{index}")
            elif not seen_hiding:
                parts.append(f" VISIBLE{index} ")
                visibles.append(f"VISIBLE{index}")
            else:
                parts.append(" x ")
        elif raw is not None and roll < 0.6:  # tags are text inside a raw-text element; sometimes its end tag
            parts.append(f"</{_case(rng, raw)}>" if rng.random() < 0.5 else f"<{name} hidden></{name}>")
            if parts[-1].lower() == f"</{raw}>":
                raw = None
        elif roll < 0.5:  # hiding start tag, sometimes self-closing
            slash = "/" if rng.random() < 0.2 else ""
            parts.append(f"<{_case(rng, name)}{rng.choice(HIDING_ATTRS)}{slash}>")
            seen_hiding = True
            if raw is not None or to_end:
                continue
            if region is None:
                region = (name, 1)
            elif region[0] == name:
                region = (name, region[1] + 1)
        elif roll < 0.75:  # plain start tag, sometimes self-closing
            slash = "/" if rng.random() < 0.2 else ""
            parts.append(f"<{_case(rng, name)}{slash}>")
            if raw is None and region is not None and region[0] == name:
                region = (name, region[1] + 1)
        elif roll < 0.95:  # end tag: matching, stray or misnested
            parts.append(f"</{_case(rng, name)}>")
            if raw is None and region is not None and region[0] == name:
                region = (name, region[1] - 1) if region[1] > 1 else None
        elif region is not None and raw is None:  # element with other parsing rules inside a region
            element = rng.choice(RAW_TEXT + HIDE_REST)
            parts.append(f"<{_case(rng, element)}>")
            if element in RAW_TEXT:
                raw = element
            else:
                to_end = True
    return "".join(parts), secrets, visibles


def test_generated_documents_follow_the_region_rule() -> None:
    rng = random.Random(20261001)  # noqa: S311 - deterministic test data
    for _ in range(5000):
        html, secrets, visibles = _document(rng)
        text = html_to_text(html)
        for marker in secrets:
            assert marker not in text, html
        for marker in visibles:
            assert marker in text, html
