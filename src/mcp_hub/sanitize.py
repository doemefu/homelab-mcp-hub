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
MAX_HTML_DEPTH: Final = 256  # open elements tracked by the HTML-to-text converter
_BLANK_RUNS: Final = re.compile(r"\n{3,}")
_SPACES: Final = re.compile(r" +")
_CONTENT_TYPE: Final = re.compile(r"^[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*$")
_DROPPED_ELEMENTS: Final = frozenset({"script", "style", "head", "template", "noscript", "svg", "iframe", "object"})
_VOID_ELEMENTS: Final = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
)
# An unterminated comment, CDATA section or declaration at the end hides the rest, as in a browser.
_UNTERMINATED: Final = (("<!--", "-->"), ("<![CDATA[", "]]>"), ("<!", ">"), ("<?", ">"))
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
    """Visible text only: dropped and hidden elements hide their whole subtree; images and comments vanish.

    Every step costs at most O(MAX_HTML_DEPTH): a counter tracks the hiding elements on the stack, and start tags
    beyond the depth cap are not pushed (their end tags are consumed by `_overflow`), so unclosed tags cannot make
    the conversion quadratic. A hiding element beyond the cap hides the rest of the document."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._stack: list[tuple[str, bool]] = []
        self._hiding = 0  # hiding elements on the stack
        self._overflow = 0  # open start tags beyond the depth cap
        self._hide_rest = False

    def _visible(self) -> bool:
        return self._hiding == 0 and not self._hide_rest

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _BLOCK_ELEMENTS and self._visible():
            self.parts.append("\n")
        if tag in _VOID_ELEMENTS:
            return
        hides = tag in _DROPPED_ELEMENTS or _hidden(attrs)
        if len(self._stack) >= MAX_HTML_DEPTH:
            self._overflow += 1
            self._hide_rest = self._hide_rest or hides
            return
        self._stack.append((tag, hides))
        self._hiding += hides

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _BLOCK_ELEMENTS and self._visible():
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if self._overflow:
            self._overflow -= 1
        else:
            for index in range(len(self._stack) - 1, -1, -1):  # unmatched end tags are ignored
                if self._stack[index][0] == tag:
                    self._hiding -= sum(hides for _, hides in self._stack[index:])
                    del self._stack[index:]
                    break
        if tag in _BLOCK_ELEMENTS and self._visible():
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
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
