"""HTML-to-text hiding as a region rule (spec 080 §5.3 rule 2).

The named corpus checks the rule against browsers (each comment gives what a browser shows; cases from all review
rounds of report 18). The generator checks that the code follows the rule: its oracle is a model of the rule, so
it cannot find places where the rule itself differs from a browser."""

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
    # N-R8a: table, object, template and button bound the scope of an end tag; a browser ignores a </div> or
    # </span> that is not in scope, so SECRET stays inside the hidden element.
    ("<div hidden><table><tr><td></div>SECRET</td></tr></table></div>", "SECRET", None),
    ("<span hidden><table><tr><td></span>SECRET", "SECRET", None),
    ("<div hidden><object></div>SECRET", "SECRET", None),
    ("<div hidden><table><caption></div>SECRET", "SECRET", None),
    ("<div hidden><template></div>SECRET", "SECRET", None),
    ("<p hidden><button></p>SECRET</button>", "SECRET", None),
    # A table closed inside the region does not keep it open: the browser closes the div at its end tag.
    ("<div hidden><table><tr><td>x</td></tr></table></div>after", "x", "after"),
    # N-R8b: the end tag of a formatting element runs the adoption agency algorithm; the text after it lands in a
    # hidden clone of the element, so it stays hidden.
    ("<b hidden><div>x</b>SECRET</div>", "SECRET", None),
    ("<a style='display:none'><p>x</a>SECRET</p>", "SECRET", None),
    ("<font style='visibility:hidden'><table><tr><td><div>x</font>SECRET", "SECRET", None),
    # A formatting element without blocks inside closes normally.
    ("<b hidden>x</b>after", "x", "after"),
    # N-R8c: script data in the double-escaped state: the first </script> does not end the script.
    ("<script><!--<script></script>SECRET</script>-->after", "SECRET", None),
    # Old comment-wrapped scripts without a nested <script do not hide the rest.
    ("<script><!-- var a = 1; //--></script><p>after</p>", "var a", "after"),
    # N-R9: the first element that is not allowed in head starts the body, as in a browser.
    ("<head><title>t</title><p>Hello", "t", "Hello"),
    ("<head><style>p{}</style></head><body>Hi", "p{}", "Hi"),
    ("<head><meta charset='utf-8'><body>Body text", None, "Body text"),
    ("<head><title>t</title><div hidden>SECRET</div><p>shown", "SECRET", "shown"),
    # N-R10: foreign elements honour the slash: a self-closing svg or math is empty.
    ("<svg/><p>BODYTEXT</p>", None, "BODYTEXT"),
    ("<div hidden><math/></div>after", None, "after"),
    ("<div hidden><svg></div>SECRET<p>more", "more", None),
    # N-R11: a hidden void element (a tracking image) never opens a region.
    ("<p>a</p><img src=x style='display:none'><p>after</p>", None, "after"),
    ("<p>a</p><img src=x hidden><p>after</p>", None, "after"),
    ("<p>a</p><img src=x hidden/><p>after</p>", None, "after"),
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


NAMES = ["div", "span", "p", "td", "tr", "li", "b", "a", "font", "table", "ul", "button", "object"]
RAW_TEXT = ["textarea", "title", "xmp", "iframe", "noembed", "noframes", "noscript"]
HIDE_REST = ["plaintext", "select", "svg", "math"]
SCOPE = {"table", "object", "applet", "marquee", "template", "button"}
FORMATTING = {"a", "b", "big", "code", "em", "font", "i", "nobr", "s", "small", "strike", "strong", "tt", "u"}
BLOCKS = {"div", "p", "li", "ul", "ol", "table", "h1", "pre", "blockquote", "section", "center", "form", "dl"}
DROPPED = {"script", "style", "head", "template", "noscript", "svg", "iframe", "object"}
HIDING_ATTRS = [" hidden", " style='display:none'", " style='VISIBILITY : hidden'", " HIDDEN"]
SCRIPT_DATA = ["x", "<!-- y //-->", "<!--<script>", "<!--<SCRIPT>z", "<!-- a --><script"]


def _case(rng: random.Random, name: str) -> str:
    return name.upper() if rng.random() < 0.2 else name


class Rule:
    """Token-level model of the region rule, written independently of the parser callbacks."""

    def __init__(self) -> None:
        self.region: str | None = None
        self.depth = 0
        self.bounds: dict[str, int] = {}
        self.raw: str | None = None
        self.to_end = False

    def hidden(self) -> bool:
        return self.to_end or self.region is not None

    def start(self, name: str, *, hiding: bool, self_closing: bool = False) -> None:
        if self.to_end or (self_closing and name in {"svg", "math"}):
            return
        if self.region is None:
            if hiding or name in DROPPED:
                self.region, self.depth = name, 1
            return
        if self.raw is not None:
            return
        boundaries = SCOPE | BLOCKS if self.region in FORMATTING else SCOPE
        if name == self.region and not sum(self.bounds.values()):
            self.depth += 1
        elif name in boundaries:
            self.bounds[name] = self.bounds.get(name, 0) + 1
        elif name in RAW_TEXT:
            self.raw = name
        elif name in HIDE_REST:
            self.to_end = True

    def end(self, name: str) -> None:
        if self.region is None:
            return
        if self.raw is not None:
            if name == self.raw:
                self.raw = None
            return
        if self.bounds.get(name):
            self.bounds[name] -= 1
            return
        if name != self.region or sum(self.bounds.values()):
            return
        self.depth -= 1
        if not self.depth:
            self.region, self.bounds = None, {}

    def script(self, data: str) -> None:
        lowered = data.lower()
        opened = lowered.find("<!--")
        if opened != -1 and "<script" in lowered[opened + 4 :]:
            self.to_end = True


def _document(rng: random.Random) -> tuple[str, list[str], list[str]]:
    """Token-level document plus the markers that must stay hidden and those that must appear.

    The oracle is the Rule model: SECRET markers go where the rule hides, VISIBLE markers everywhere else. The
    generator emits stray, misnested and self-closing tags, mixed case, nested same-name elements, scope
    boundaries, formatting elements around blocks, raw-text and foreign elements and script blocks with and
    without double escapes."""
    parts: list[str] = []
    secrets: list[str] = []
    visibles: list[str] = []
    rule = Rule()
    for index in range(rng.randrange(5, 40)):
        roll = rng.random()
        name = rng.choice(NAMES)
        slash = "/" if rng.random() < 0.2 else ""
        if roll < 0.3:  # text
            if rule.hidden():
                parts.append(f" SECRET{index} ")
                secrets.append(f"SECRET{index}")
            else:
                parts.append(f" VISIBLE{index} ")
                visibles.append(f"VISIBLE{index}")
        elif rule.raw is not None and roll < 0.6:  # tags are text inside a raw-text element; sometimes its end tag
            closing = rng.random() < 0.5
            parts.append(f"</{_case(rng, rule.raw)}>" if closing else f"<{name} hidden></{name}>")
            if closing:
                rule.end(rule.raw)
        elif roll < 0.45:  # hiding start tag, sometimes self-closing
            parts.append(f"<{_case(rng, name)}{rng.choice(HIDING_ATTRS)}{slash}>")
            rule.start(name, hiding=True, self_closing=bool(slash))
        elif roll < 0.68:  # plain start tag, sometimes self-closing
            parts.append(f"<{_case(rng, name)}{slash}>")
            rule.start(name, hiding=False, self_closing=bool(slash))
        elif roll < 0.9:  # end tag: matching, stray or misnested
            parts.append(f"</{_case(rng, name)}>")
            rule.end(name)
        elif roll < 0.95 and rule.raw is None:  # script block
            data = rng.choice(SCRIPT_DATA)
            parts.append(f"<script>{data}</script>")
            rule.start("script", hiding=True)
            rule.script(data)
            rule.end("script")
        elif rule.region is not None and rule.raw is None:  # element with other parsing rules inside a region
            element = rng.choice(RAW_TEXT + HIDE_REST)
            foreign_slash = "/" if element in {"svg", "math"} and rng.random() < 0.3 else ""
            parts.append(f"<{_case(rng, element)}{foreign_slash}>")
            rule.start(element, hiding=False, self_closing=bool(foreign_slash))
    return "".join(parts), secrets, visibles


def test_generated_documents_follow_the_region_rule() -> None:
    rng = random.Random(20261001)  # noqa: S311 - deterministic test data
    for _ in range(6000):
        html, secrets, visibles = _document(rng)
        text = html_to_text(html)
        for marker in secrets:
            assert marker not in text, html
        for marker in visibles:
            assert marker in text, html
