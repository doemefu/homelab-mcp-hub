# ruff: noqa: RUF001  # U+2028/U+2029 are named in the rule
"""Search terms, value normaliser and Graph `$search` sanitiser (spec 080 §5.2 "Query handling", D82-D85).

Error texts are fixed; a refused value is never echoed."""

import unicodedata
from collections.abc import Sequence
from typing import Final

import httpx2

from mcp_hub.errors import ToolError
from mcp_hub.providers.base import (
    MAX_SEARCH_WORDS,
    IncompleteReason,
    SearchKey,
    SearchTerms,
)
from mcp_hub.providers.imap_guard import MAX_LITERAL_BYTES, MAX_VALUE_BYTES
from mcp_hub.sanitize import without_surrogates

__all__ = [
    "VALUE_TOO_LONG",
    "IncompleteReason",
    "SearchKey",
    "SearchTerms",
    "graph_search_text",
    "imap_value_keys",
    "match_fold",
    "normalise_value",
    "parse_search_terms",
]

MAX_VALUE_CHARS: Final = 200
# The IMAP guard's limits on the normalised values (D82): refused here so that user input is never an account error.
MAX_VALUE_BYTES_EACH: Final = MAX_LITERAL_BYTES
MAX_TOTAL_BYTES: Final = MAX_VALUE_BYTES
MAX_QUERY_WORDS: Final = MAX_SEARCH_WORDS
# Names of the search operators of Gmail, Graph and Outlook; a query word starting with one plus ":" is refused.
OPERATOR_PREFIXES: Final = (
    "from", "to", "cc", "bcc", "subject", "in", "is", "label", "has", "after", "before", "older_than",
    "newer_than", "filename", "category", "received", "sent", "participants", "hasattachments", "deliveredto",
    "list", "rfc822msgid", "larger", "smaller", "size", "older", "newer",
)  # fmt: skip
OPERATOR_WORDS: Final = frozenset({"AND", "OR", "NOT", "NEAR"})  # exact, case-sensitive
GRAPH_KEEP_PUNCTUATION: Final = frozenset("@._-'")
GRAPH_STRIP: Final = "-.'"
MAX_GRAPH_TOKENS: Final = 32
MAX_GRAPH_ENCODED_CHARS: Final = 1500
MATCH_SUBJECT_CHARS: Final = 4000
MATCH_NAME_CHARS: Final = 1000

VALUE_RULES: Final = "query, sender, recipient and subject must be 1 to 200 characters without control characters"
VALUE_TOO_LONG: Final = "A search value is too long once normalised"
NO_TERMS: Final = "Pass query or at least one of sender, recipient, subject"
TOO_MANY_WORDS: Final = "query takes at most 8 words"
OPERATOR_REFUSED: Final = "query takes plain words; use the sender, recipient and subject arguments"
NO_SEARCHABLE_TEXT: Final = "A search value has no searchable text"
RESERVED_TEXT: Final = "A search value must not contain X-GM-RAW"
EMPTY_GRAPH_QUERY: Final = "query has no letters or digits that Outlook can search"
GRAPH_QUERY_TOO_LONG: Final = "query is too long for Outlook search"


def _refuse(message: str) -> ToolError:
    return ToolError("invalid_argument", message)


def normalise_value(value: object, name: str) -> str:
    """The wire form of one value: no control characters, NFC, no `"` or `\\`, single spaces.

    `name` is the argument name; it is not used in the fixed error texts."""
    del name
    if not isinstance(value, str) or not 1 <= len(value) <= MAX_VALUE_CHARS:
        raise _refuse(VALUE_RULES)
    if any(unicodedata.category(ch) in ("Cc", "Cs") for ch in value):
        raise _refuse(VALUE_RULES)
    kept = "".join(" " if ch in "  " else ch for ch in value if unicodedata.category(ch) != "Cf")
    kept = unicodedata.normalize("NFC", kept).replace('"', "").replace("\\", "")
    kept = " ".join(kept.split())
    if not kept:
        raise _refuse(NO_SEARCHABLE_TEXT)
    if "X-GM-RAW" in kept.upper():
        raise _refuse(RESERVED_TEXT)
    return kept


def _check_operators(word: str) -> None:
    folded = unicodedata.normalize("NFKC", word).casefold()
    if any(folded.startswith(prefix + ":") for prefix in OPERATOR_PREFIXES):
        raise _refuse(OPERATOR_REFUSED)
    if word in OPERATOR_WORDS or (folded[0] in "-+" and len(folded) > 1 and folded[1].isalnum()):
        raise _refuse(OPERATOR_REFUSED)


def parse_search_terms(
    *, query: str | None, sender: str | None, recipient: str | None, subject: str | None
) -> SearchTerms:
    if query is None and sender is None and recipient is None and subject is None:
        raise _refuse(NO_TERMS)
    words: list[str] = []
    if query is not None:
        seen: set[str] = set()
        for word in normalise_value(query, "query").split(" "):
            if word.casefold() not in seen:
                seen.add(word.casefold())
                words.append(word)
        if len(words) > MAX_QUERY_WORDS:
            raise _refuse(TOO_MANY_WORDS)
        for word in words:
            _check_operators(word)
    terms = SearchTerms(
        words=tuple(words),
        sender=None if sender is None else normalise_value(sender, "sender"),
        recipient=None if recipient is None else normalise_value(recipient, "recipient"),
        subject=None if subject is None else normalise_value(subject, "subject"),
    )
    sizes = [
        len(value.encode("utf-8")) for value in (*terms.words, terms.sender, terms.recipient, terms.subject) if value
    ]
    if any(size > MAX_VALUE_BYTES_EACH for size in sizes) or sum(sizes) > MAX_TOTAL_BYTES:
        raise _refuse(VALUE_TOO_LONG)
    return terms


def _allowlist_tokens(word: str) -> list[str]:
    """The tokens of one word under the Graph allowlist: letters, marks, digits and `@ . _ - '` survive, leading and
    trailing `- . '` are stripped, a token without a letter or digit is dropped, the rest is lower-cased."""
    tokens: list[str] = []
    spaced = "".join(ch if unicodedata.category(ch)[0] in "LMN" or ch in GRAPH_KEEP_PUNCTUATION else " " for ch in word)
    for token in spaced.split():
        token = token.strip(GRAPH_STRIP)
        if any(unicodedata.category(ch)[0] in "LN" for ch in token):
            tokens.append(token.lower())
    return tokens


def _gmail_tokens(word: str) -> list[str]:
    """The allowlist tokens of `word`, then, inside each: a `-` survives only between two letters or digits (any
    other `-` splits the token) and each piece starts at its first letter or digit. No piece of a Gmail TEXT value
    can then begin with `-`, whatever precedes it (Q-a)."""
    pieces: list[str] = []
    for token in _allowlist_tokens(word):
        kept = "".join(
            " " if ch == "-" and not (_alnum(token[i - 1 : i]) and _alnum(token[i + 1 : i + 2])) else ch
            for i, ch in enumerate(token)
        )
        for piece in kept.split():
            start = next((i for i, ch in enumerate(piece) if _alnum(ch)), None)
            if start is not None:
                pieces.append(piece[start:])
    return pieces


def _alnum(ch: str) -> bool:
    return len(ch) == 1 and unicodedata.category(ch)[0] in "LN"


def imap_value_keys(terms: SearchTerms, *, gmail: bool) -> list[tuple[SearchKey, str]] | None:
    """The `key value` pairs of an IMAP search. On Gmail a `-` at the start of a quoted TEXT value negates it (Q-a),
    so each TEXT word goes through the allowlist above and its tokens form that word's one TEXT value; a word with no
    token is dropped. None when nothing is left to search for (no TEXT word and no filter): the folders are then
    refused. FROM, TO and SUBJECT stay as given."""
    keys: list[tuple[SearchKey, str]] = [("TEXT", word) for word in terms.words]
    if gmail:
        sanitised = (" ".join(_gmail_tokens(word)) for word in terms.words)
        keys = [("TEXT", value) for value in sanitised if value]
    filters: tuple[tuple[SearchKey, str | None], ...] = (
        ("FROM", terms.sender),
        ("TO", terms.recipient),
        ("SUBJECT", terms.subject),
    )
    for key, value in filters:
        if value is not None:
            keys.append((key, value))
    return keys or None


def _encoded_search_length(text: str) -> int:
    encoded = str(httpx2.QueryParams({"$search": f'"{text}"'}))
    return len(encoded.split("=", 1)[1])


def graph_search_text(words: Sequence[str]) -> str | None:
    """Allowlist sanitiser for Graph `$search` (D83): letters, marks, digits and `@ . _ - '` survive, lower-cased."""
    tokens = [token for word in words for token in _allowlist_tokens(word)]
    if len(tokens) > MAX_GRAPH_TOKENS:
        raise _refuse(GRAPH_QUERY_TOO_LONG)
    if not tokens:
        return None
    text = " ".join(tokens)
    if _encoded_search_length(text) > MAX_GRAPH_ENCODED_CHARS:
        raise _refuse(GRAPH_QUERY_TOO_LONG)
    return text


def _fold_pass(text: str) -> str:
    return text.replace('"', "").replace("\\", "")


def match_fold(text: str) -> str:
    """Comparison form for the hub's own substring filters (Graph); applied to needle and candidate alike."""
    kept: list[str] = []
    for ch in without_surrogates(text):
        category = unicodedata.category(ch)
        if category == "Cc":
            kept.append(" " if ch.isspace() else "")
        elif category != "Cf":
            kept.append(ch)
    folded = unicodedata.normalize("NFKC", _fold_pass("".join(kept)))
    folded = unicodedata.normalize("NFKC", folded.casefold())
    return " ".join(_fold_pass(folded).split())
