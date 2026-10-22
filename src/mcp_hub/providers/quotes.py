"""Quote removal for `get_thread` bodies (spec 080 §5.2 "Quote removal"; WP13c Option 7a, E-mf7, MF-P4, Q2).

Works on decoded plain text whose line breaks and no-break spaces the caller has normalised (LF, U+0020), before any
sanitising: §5.3 rule 4 would turn `<mailto:…>` into a link label and take the `@` the attribution guard needs.

Linear in the input: every line is classified once; an attribution is a line joined with at most the next 2 lines;
the header-block look-ahead stops after MAX_LOOKAHEAD_LINES lines (blank lines included); the forward-marker
look-back keeps the last 3 non-blank lines in a deque. Patterns are anchored and run on single lines only. Pure:
nothing is logged.
"""

import re
import unicodedata
from collections import deque
from typing import Final, Literal

from mcp_hub.providers.headers import _PREFIX, _TAG, MAX_SUBJECT_ROUNDS
from mcp_hub.sanitize import clean

QuoteMode = Literal["heuristic", "none"]

FORWARD_MARKERS: Final = (
    "Forwarded message",
    "Weitergeleitete Nachricht",
    "Begin forwarded message:",
    "Anfang der weitergeleiteten Nachricht:",
)
MAX_LOOKAHEAD_LINES: Final = 64  # (d) and (a) scan at most this many lines (blank lines included)

_ATTRIBUTION_LINES: Final = 3
_BLOCK_LINES: Final = 6  # non-blank lines after the first line of a header block that may hold its header lines
_SENT_TO_FROM_LINES: Final = 2
_UNDERSCORE_TO_BLOCK_LINES: Final = 3
_MIN_UNDERSCORES: Final = 10
_FORWARD_LOOK_BACK: Final = 3
_SUBJECT_SCAN_CHARS: Final = 1000  # the prefix rounds look at no more (20 rounds of at most a 42-character tag)

_SEPARATOR: Final = re.compile(
    r"-{2,}\s*(?:Original Message|Ursprüngliche Nachricht|Original-Nachricht|Originalnachricht)\s*-{2,}", re.I
)
# Every header name may carry *…* emphasis, as in the plain rendering of an Outlook block (lead ruling, WP13c).
_FROM: Final = re.compile(r"\*?(?:From|Von)\*?:")
_SENT: Final = re.compile(r"\*?(?:Sent|Gesendet)\*?:")
_HEADER: Final = re.compile(r"\*?(?:Sent|Gesendet|Date|Datum|To|An)\*?:")
_SUBJECT: Final = re.compile(r"\*?(?:Subject|Betreff)\*?:")
_FORWARD_PREFIX: Final = re.compile(r"\s*(?:fwd?|wg|wtr)\s*(?:\[\d+\]|\(\d+\)|\^\d+)?\s*:", re.I)
_DIGIT: Final = re.compile(r"[0-9]")

# Line kinds, as bits: one line can be several at once.
_BLANK: Final = 1
_QUOTE: Final = 1 << 1  # first non-blank character is ">"
_FROM_LINE: Final = 1 << 2
_SENT_LINE: Final = 1 << 3
_HEADER_LINE: Final = 1 << 4  # Sent/Gesendet/Date/Datum/To/An
_SUBJECT_LINE: Final = 1 << 5
_UNDERSCORES: Final = 1 << 6
_SEPARATOR_LINE: Final = 1 << 7
_MARKER: Final = 1 << 8
_COLON_END: Final = 1 << 9
_HAS_DIGIT: Final = 1 << 10
_HAS_AT: Final = 1 << 11


def _forwarded(subject: str | None) -> bool:
    """Whether the first prefix of the subject is Fwd/Fw/WG/Wtr, after the tag stripping of normalise_subject (S-k)."""
    if not subject:
        return False
    text = unicodedata.normalize("NFKC", clean(subject[:_SUBJECT_SCAN_CHARS], _SUBJECT_SCAN_CHARS))
    for _ in range(MAX_SUBJECT_ROUNDS):
        if _FORWARD_PREFIX.match(text):
            return True
        if _PREFIX.match(text):
            return False
        tag = _TAG.match(text)
        if tag is None:
            return False
        text = text[tag.end() :]
    return False


def _kind(line: str) -> int:
    """Bits of one stripped line; leading whitespace (`^\\s*`) is already gone."""
    if not line:
        return _BLANK
    kind = 0
    if line.startswith(">"):
        kind |= _QUOTE
    if _FROM.match(line):
        kind |= _FROM_LINE
    if _SENT.match(line):
        kind |= _SENT_LINE
    if _HEADER.match(line):
        kind |= _HEADER_LINE
    if _SUBJECT.match(line):
        kind |= _SUBJECT_LINE
    if len(line) >= _MIN_UNDERSCORES and not line.strip("_"):
        kind |= _UNDERSCORES
    if line.startswith("--") and _SEPARATOR.fullmatch(line):
        kind |= _SEPARATOR_LINE
    if any(marker in line for marker in FORWARD_MARKERS):
        kind |= _MARKER
    if line.endswith(":"):
        kind |= _COLON_END
    if _DIGIT.search(line):
        kind |= _HAS_DIGIT
    if "@" in line:
        kind |= _HAS_AT
    return kind


class _Lines:
    """The classified lines of one body and the per-line facts the rules look up in constant time."""

    __slots__ = ("after_forward", "blocks_allowed", "kinds", "n", "next_non_blank", "original", "stripped")

    def __init__(self, text: str, subject: str | None) -> None:
        self.original = text.split("\n")
        self.stripped = [line.strip() for line in self.original]
        self.kinds = [_kind(line) for line in self.stripped]
        self.n = len(self.kinds)
        self.blocks_allowed = not _forwarded(subject)  # header blocks of a forward are its content
        self.next_non_blank = [self.n] * self.n
        following = self.n
        for index in range(self.n - 1, -1, -1):
            self.next_non_blank[index] = following
            if not self.kinds[index] & _BLANK:
                following = index
        self.after_forward = [False] * self.n
        recent: deque[bool] = deque(maxlen=_FORWARD_LOOK_BACK)
        for index, kind in enumerate(self.kinds):
            self.after_forward[index] = any(recent)
            if not kind & _BLANK:
                recent.append(bool(kind & _MARKER))

    def quote_follows(self, index: int) -> bool:
        following = self.next_non_blank[index]
        return following < self.n and bool(self.kinds[following] & _QUOTE)

    def cuts_to_end(self, index: int) -> bool:
        """Rules (a) and (d): this line starts a block that is removed with everything after it."""
        kind = self.kinds[index]
        if kind & _SEPARATOR_LINE:
            return True
        if kind & _UNDERSCORES:
            last = min(self.n - 1, index + _UNDERSCORE_TO_BLOCK_LINES)
            return any(self._header_block(start) for start in range(index + 1, last + 1))  # MF-P4
        return self._header_block(index)

    def _header_block(self, index: int) -> bool:
        """Rule (d) with E-mf7: a From/Von (or Sent/Gesendet) line followed by header lines, one of them Subject."""
        if not self.blocks_allowed or self.after_forward[index]:
            return False
        kind = self.kinds[index]
        if kind & _FROM_LINE:
            return self._headers_follow(index, found=0)
        if kind & _SENT_LINE:
            last = min(self.n - 1, index + _SENT_TO_FROM_LINES)
            if any(self.kinds[line] & _FROM_LINE for line in range(index + 1, last + 1)):
                return self._headers_follow(index, found=1)  # the Sent line is one of the block's header lines
        return False

    def _headers_follow(self, index: int, *, found: int) -> bool:
        seen, subject = 0, False
        for line in range(index + 1, min(self.n, index + 1 + MAX_LOOKAHEAD_LINES)):
            kind = self.kinds[line]
            if kind & _BLANK:
                continue
            if kind & (_HEADER_LINE | _SUBJECT_LINE):
                found += 1
                subject = subject or bool(kind & _SUBJECT_LINE)
                if found >= 2 and subject:
                    return True
            seen += 1
            if seen == _BLOCK_LINES:
                break
        return False

    def attribution_end(self, index: int) -> int | None:
        """Rules (b), (c), (e) with the guard: the last line of an attribution that starts here, else None.

        A start that matches rule (e) only gives way to a later start inside its join that ends on the same line,
        so a sign-off or reply line right before an attribution is never taken as part of it.
        """
        for last in range(index, min(self.n, index + _ATTRIBUTION_LINES)):
            anchored, other = self._attribution(index, last)
            if not (anchored or other):
                continue
            if anchored or not any(any(self._attribution(start, last)) for start in range(index + 1, last + 1)):
                return last
        return None

    def _attribution(self, first: int, last: int) -> tuple[bool, bool]:
        """(anchored by rule (b) or (c), matched by rule (e)) for lines first..last joined, guard included."""
        if not self.kinds[last] & _COLON_END:
            return False, False
        kinds = self.kinds[first : last + 1]
        if not any(kind & _HAS_DIGIT for kind in kinds):
            return False, False
        if not any(kind & _HAS_AT for kind in kinds) and not self.quote_follows(last):
            return False, False
        start = self.stripped[first]
        joined = " ".join(self.stripped[first : last + 1])
        wrote = joined.endswith("wrote:")
        anchored = (start.startswith("On ") and wrote) or (start.startswith("Am ") and " schrieb" in joined)
        other = wrote or joined.endswith("schrieb:") or "schrieb am " in joined
        return anchored, other


def remove_quotes(text: str, *, subject: str | None) -> tuple[str, QuoteMode]:
    """Text without the quoted history, and whether the heuristic removed anything (§5.2 steps 1 to 3).

    `subject` is the raw subject; a first prefix Fwd/Fw/WG/Wtr (after list tags) keeps header blocks.
    """
    lines = _Lines(text, subject)
    kept = [True] * lines.n
    end, removed = lines.n, False
    index = 0
    while index < lines.n:  # step 1
        if lines.kinds[index] & _BLANK:
            index += 1
            continue
        if lines.cuts_to_end(index):
            end, removed = index, True
            break
        last = lines.attribution_end(index)
        if last is None:
            index += 1
            continue
        removed = True
        if not lines.quote_follows(last):
            end = index
            break
        for line in range(index, last + 1):  # S-l: only the attribution goes; the scan continues (Q2)
            kept[line] = False
        index = last + 1
    result: list[str] = []
    for line in range(end):  # step 2
        if not kept[line]:
            continue
        if lines.kinds[line] & _QUOTE:
            removed = True
            continue
        result.append(lines.original[line])
    while result and not result[-1].strip():  # step 3
        result.pop()
    if not result:
        return text, "none"
    return "\n".join(result), "heuristic" if removed else "none"
