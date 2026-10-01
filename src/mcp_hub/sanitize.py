"""Untrusted-content rules (spec 080 §5.3) and per-field limits (§5.4). Applied to every third-party string."""

import functools
import re
import unicodedata
from html.parser import HTMLParser
from importlib import resources
from typing import Final
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

TRUNCATION_MARKER: Final = " [truncated]"
FIELD_LIMITS: Final[dict[str, int]] = {
    "address": 254,
    "from_name": 200,
    "organizer_name": 200,
    "filename": 200,
    "subject": 300,
    "title": 300,
    "location": 300,
    "snippet": 200,
    "calendar_name": 100,
    "content_type": 100,
    "description": 500,
    "original_timezone": 64,
}
# Zero-width (U+200B-U+200F, U+2060-U+2064, U+FEFF) and bidi controls (U+202A-U+202E, U+2066-U+2069).
_INVISIBLE: Final = re.compile("[\u200b-\u200f\u2060-\u2064\ufeff\u202a-\u202e\u2066-\u2069]")
_URL: Final = re.compile(r"(?i)(?:\bmailto:[^\s<>\"']*|\b(?:https?|ftp)://[^\s<>\"']*|\bwww\.[^\s<>\"']+)")
_SURROGATE: Final = re.compile(r"[\ud800-\udfff]")
# Raw patterns: the regex engine reads the \u escapes, so no lone surrogate appears in the source string.
_NON_ESCAPE_SURROGATE: Final = re.compile(r"[\ud800-\udc7f\udd00-\udfff]")
_BLANK_RUNS: Final = re.compile(r"\n{3,}")
_SPACES: Final = re.compile(r" +")
_CONTENT_TYPE: Final = re.compile(r"^[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*$")
_DROPPED_ELEMENTS: Final = frozenset({"script", "style", "head", "template", "noscript", "svg", "iframe", "object"})
_VOID_ELEMENTS: Final = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
)
# An unterminated comment, CDATA section or declaration at the end hides the rest, as in a browser.
_UNTERMINATED: Final = (("<!--", "-->"), ("<![CDATA[", "]]>"), ("<!", ">"), ("<?", ">"))
# Inside a hidden region a browser may tokenise these differently from the stdlib parser. Raw-text elements end
# at their own end tag in a browser, so everything up to it is ignored; the others hide the rest of the document.
_RAW_TEXT: Final = frozenset({"textarea", "title", "xmp", "iframe", "noembed", "noframes", "noscript"})
_HIDE_REST: Final = frozenset({"plaintext", "select", "svg", "math"})
_FOREIGN: Final = frozenset({"svg", "math"})  # these honour a self-closing slash: <svg/> is empty
# Scope boundaries: a browser ignores an end tag of the region's element while one of these is open inside it.
_SCOPE_BOUNDARIES: Final = frozenset({"table", "object", "applet", "marquee", "template", "button"})
# Formatting elements: their end tag runs the adoption agency algorithm across these blocks, so the text after it
# stays in a (hidden) clone; inside such a region the blocks count as boundaries too.
_FORMATTING: Final = frozenset(
    {"a", "b", "big", "code", "em", "font", "i", "nobr", "s", "small", "strike", "strong", "tt", "u"}
)
_FORMATTING_BOUNDARIES: Final = _SCOPE_BOUNDARIES | frozenset(
    {
        "address", "article", "aside", "blockquote", "center", "div", "dl", "fieldset", "footer", "form", "h1", "h2",
        "h3", "h4", "h5", "h6", "header", "li", "main", "nav", "ol", "p", "pre", "section", "ul",
    }
)  # fmt: skip
# Elements allowed in head; any other start tag ends a head region and starts the body, as in a browser.
_HEAD_CONTENT: Final = frozenset({"title", "meta", "link", "style", "script", "base", "noscript", "template"})
_BLOCK_ELEMENTS: Final = frozenset(
    {
        "p", "div", "br", "li", "tr", "table", "ul", "ol", "blockquote", "section", "article", "header", "footer",
        "pre", "hr", "h1", "h2", "h3", "h4", "h5", "h6",
    }
)  # fmt: skip


def without_surrogates(text: str) -> str:
    """No lone surrogate (category Cs) survives: it would make the JSON result unserialisable.

    The stdlib email parser keeps undecodable header bytes as surrogate escapes (U+DC80-U+DCFF). They are turned
    back into bytes and decoded as UTF-8, so raw UTF-8 headers keep their text; bytes of any other charset become
    U+FFFD (the charset is unknown, guessing one could invent text). Any other lone surrogate becomes U+FFFD."""
    if not _SURROGATE.search(text):
        return text
    text = _NON_ESCAPE_SURROGATE.sub("\ufffd", text)
    return text.encode("utf-8", "surrogateescape").decode("utf-8", "replace")


def truncate(text: str, limit: int) -> str:
    """Cut to `limit` characters including the visible marker (limits are all longer than the marker)."""
    text = without_surrogates(text)
    if len(text) <= limit:
        return text
    return text[: max(limit - len(TRUNCATION_MARKER), 0)].rstrip() + TRUNCATION_MARKER


def _link(match: re.Match[str]) -> str:
    raw = match.group(0)
    if raw.lower().startswith("mailto:"):
        return "[mail link]"
    try:
        host = urlsplit(raw if "://" in raw else "http://" + raw).hostname
    except ValueError:
        host = None
    return f"[link: {host[:253]}]" if host else "[link]"


def _normalise(value: str, *, multiline: bool) -> str:
    text = unicodedata.normalize("NFC", without_surrogates(value)).replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\u2028", "\n").replace("\u2029", "\n")
    text = _INVISIBLE.sub("", text)  # the spec's explicit list (§5.3 rule 3)
    # Superset (spec 080 rev. 4.4 §5.3, S10): every other control (Cc) and format (Cf) character, which includes the
    # Unicode tag characters U+E0000-U+E007F, soft hyphen U+00AD, U+061C and U+180E.
    text = "".join(ch for ch in text if ch in "\n\t" or unicodedata.category(ch) not in ("Cc", "Cf"))
    text = _URL.sub(_link, text)
    if not multiline:
        return " ".join(text.split())
    lines = [_SPACES.sub(" ", line).strip(" ") for line in text.split("\n")]  # tabs stay (§5.3 rule 3)
    return _BLANK_RUNS.sub("\n\n", "\n".join(lines)).strip()


def clean_flagged(value: str | None, limit: int, *, multiline: bool = False) -> tuple[str, bool]:
    """Sanitised text cut to `limit`, plus whether it was cut."""
    if not value:
        return "", False
    text = _normalise(value, multiline=multiline)
    return truncate(text, limit), len(text) > limit


def clean(value: str | None, limit: int, *, multiline: bool = False) -> str:
    return clean_flagged(value, limit, multiline=multiline)[0]


def _hidden(attrs: list[tuple[str, str | None]]) -> bool:
    for name, value in attrs:
        if name == "hidden":
            return True
        if name == "style" and value:
            style = "".join(value.lower().split())
            if "display:none" in style or "visibility:hidden" in style:
                return True
    return False


class _TextExtractor(HTMLParser):
    """Visible text only, by a region rule with O(1) state that fails closed (spec 080 §5.3 rule 2).

    A start tag that hides (`hidden`, inline display:none or visibility:hidden, or an always-dropped element such
    as script, style or head) opens a hidden region named after its tag. Inside the region nothing is emitted, and
    only tags of that name count: a start tag adds one, an end tag removes one, and the region ends at zero. All
    other tags are ignored for hiding, so stray, misnested or implicitly closed tags can never end a region early.
    A self-closing non-void element (`<div hidden/>`) opens like a start tag, as browsers ignore the slash. Inside
    a region, a raw-text element (title, textarea, iframe, ...) suspends counting up to its own end tag, as a
    browser reads it as text, and an element with other parsing rules (plaintext, select, svg, math) hides the rest
    of the document; a self-closing svg or math is empty. While a scope boundary (table, object, applet, marquee,
    template, button; for a formatting element such as b or a also block elements such as div or p) is open inside
    the region, the region's end tag is not in scope and is ignored, as in a browser. A head region ends at body or
    at the first element not allowed in head. Script data with "<!--" and a later "<script" hides the rest.

    Known limits: hidden-content removal is best effort and errs towards hiding. It recognises the hidden attribute
    and inline display:none / visibility:hidden only, not CSS classes or style sheets, zero-size or same-colour text
    or off-screen positioning, and it does not reproduce every HTML5 tree-construction rule. Mail content reaches
    the model marked as untrusted regardless.

    Accepted over-hiding: a hiding element that relies on an implied end tag (`<p hidden>` followed by another
    `<p>` without `</p>`, likewise li, td, tr) hides everything up to its balancing explicit end tag or the end
    of the document."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._region: str | None = None  # tag name of the hidden region
        self._depth = 0  # open elements of that name inside the region
        self._bounds: dict[str, int] = {}  # open scope boundaries inside the region, per tag (fixed tag set)
        self._bound_total = 0
        self._raw: str | None = None  # raw-text element inside the region: everything up to its end tag is ignored
        self._in_script = False  # the parser delivers script data
        self._script_comment = False  # that script data has opened "<!--"
        self._hide_rest = False

    def _visible(self) -> bool:
        return self._region is None and not self._hide_rest

    def _close_region(self) -> None:
        self._region, self._depth, self._bounds, self._bound_total = None, 0, {}, 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._start(tag, attrs, self_closing=False)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _VOID_ELEMENTS:
            if tag in _BLOCK_ELEMENTS and self._visible():
                self.parts.append("\n")
            return
        self._start(tag, attrs, self_closing=True)  # browsers ignore the slash on non-void HTML elements

    def _start(self, tag: str, attrs: list[tuple[str, str | None]], *, self_closing: bool) -> None:
        if tag == "script" and not self_closing:
            self._in_script, self._script_comment = True, False
        if self._hide_rest or (self_closing and tag in _FOREIGN):
            return
        if self._region is not None:
            if self._raw is not None:
                return
            if self._region == "head" and not self._bound_total and (tag == "body" or tag not in _HEAD_CONTENT):
                self._close_region()  # the body starts here; handle the tag as visible content
            else:
                self._start_in_region(tag)
                return
        if tag in _BLOCK_ELEMENTS:
            self.parts.append("\n")
        if tag not in _VOID_ELEMENTS and (tag in _DROPPED_ELEMENTS or _hidden(attrs)):
            self._region, self._depth = tag, 1

    def _start_in_region(self, tag: str) -> None:
        boundaries = _FORMATTING_BOUNDARIES if self._region in _FORMATTING else _SCOPE_BOUNDARIES
        if tag == self._region and not self._bound_total:
            self._depth += 1
        elif tag in boundaries:
            self._bounds[tag] = self._bounds.get(tag, 0) + 1
            self._bound_total += 1
        elif tag in _RAW_TEXT:
            self._raw = tag
        elif tag in _HIDE_REST:
            self._hide_rest = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self._in_script = False
        if self._region is not None:
            if self._raw is not None:
                if tag == self._raw:
                    self._raw = None
                return
            if self._bounds.get(tag):
                self._bounds[tag] -= 1
                self._bound_total -= 1
                return
            if tag != self._region or self._bound_total:  # not in scope while a boundary is open
                return
            self._depth -= 1
            if self._depth:
                return
            self._close_region()
        if tag in _BLOCK_ELEMENTS and self._visible():
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_script and not self._hide_rest:
            # Script data with "<!--" and a later "<script" is double-escaped in a browser: its first </script>
            # does not end the script, so the rest is hidden.
            lowered = data.lower()
            start = 0
            if not self._script_comment and (opened := lowered.find("<!--")) != -1:
                self._script_comment, start = True, opened + 4
            if self._script_comment and "<script" in lowered[start:]:
                self._hide_rest = True
        if self._visible():
            self.parts.append(data)


def _cut_unterminated(html: str) -> str:
    """Drop a trailing comment, CDATA section or declaration that never ends; older CPython releases return it as
    visible text on close()."""
    cut = len(html)
    for opener, closer in _UNTERMINATED:
        start = html.find(opener, html.rfind(closer) + 1)
        if start != -1:
            cut = min(cut, start)
    return html[:cut]


def html_to_text(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(_cut_unterminated(html))
    parser.close()
    return without_surrogates("".join(parser.parts))


def validate_content_type(value: str | None) -> str | None:
    if not value:
        return None
    lowered = value.strip().lower()
    return lowered if len(lowered) <= FIELD_LIMITS["content_type"] and _CONTENT_TYPE.fullmatch(lowered) else None


@functools.cache
def _iana_names() -> frozenset[str]:
    """IANA zone names from the pinned tzdata package, not from the system directory (Linux zoneinfo trees also
    hold `localtime` and `posixrules`); `Factory` is a placeholder, not a place."""
    zones = resources.files("tzdata").joinpath("zones").read_text(encoding="utf-8").split()
    return frozenset(zones) - {"Factory"}


def validate_timezone(value: str | None) -> str | None:
    if not value or len(value) > FIELD_LIMITS["original_timezone"] or value not in _iana_names():
        return None
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        return None
    return value
