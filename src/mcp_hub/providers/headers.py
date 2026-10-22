"""IMAP header parsing for the mail history tools (spec 080 §5.5): summary headers, Message-ID tokens, thread roots.

Everything here is bounded: the header block is at most 64 KiB, To/Cc text is cut to 8 KiB before the address
parser sees it, and the subject loop runs at most 20 rounds. Third-party text is returned as decoded; the tool
layer sanitises it into `untrusted` (§5.3). Nothing is logged here.
"""

import email.policy
import hashlib
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from email.header import decode_header, make_header
from email.parser import BytesHeaderParser
from email.utils import getaddresses
from typing import Final, Literal

from mcp_hub.ids import MESSAGE_ID_PATTERN, ThreadKey
from mcp_hub.sanitize import clean

SUMMARY_HEADER_FIELDS: Final = "FROM TO CC SUBJECT MESSAGE-ID IN-REPLY-TO REFERENCES"
SUMMARY_HEADER_ITEM: Final = f"BODY.PEEK[HEADER.FIELDS ({SUMMARY_HEADER_FIELDS})]<0.65536>"
CANDIDATE_HEADER_BYTES: Final = 2048
MAX_ADDRESS_HEADER_BYTES: Final = 8192
MAX_REFERENCES: Final = 10
MAX_IN_REPLY_TO_TOKENS: Final = 100  # the phrase-form scan looks at no more (security M1: no quadratic scan)
MAX_SUBJECT_ROUNDS: Final = 20
_SUBJECT_INPUT_CHARS: Final = 4000
_SUBJECT_KEPT_CHARS: Final = 1000
_PARSER: Final = BytesHeaderParser(policy=email.policy.default)
_FOLD: Final = re.compile(r"\r?\n(?=[ \t])")
_SPLIT: Final = re.compile(r"[\s,]+")
_WHITESPACE: Final = re.compile(r"\s+")
_PREFIX: Final = re.compile(r"\s*(?:re|aw|antw|antwort|fwd?|wg|wtr|sv)\s*(?:\[\d+\]|\(\d+\)|\^\d+)?\s*:\s*", re.I)
_TAG: Final = re.compile(r"\[[^\[\]]{1,40}\]\s*")


def candidate_header_item(fields: Sequence[Literal["MESSAGE-ID", "FROM"]]) -> str:
    return f"BODY.PEEK[HEADER.FIELDS ({' '.join(fields)})]<0.{CANDIDATE_HEADER_BYTES}>"


@dataclass(frozen=True, slots=True)
class ParsedHeaders:
    from_name: str | None
    from_address: str | None
    to: tuple[str, ...]
    cc: tuple[str, ...]
    subject: str | None
    message_id: str | None
    in_reply_to: str | None
    references: tuple[str, ...]
    root: str | None


def header_bytes(row: Mapping[bytes, object]) -> bytes:
    for key, value in row.items():
        if key.upper().startswith(b"BODY[HEADER.FIELDS") and isinstance(value, bytes):
            return value
    return b""


def valid_message_id(value: str | None) -> str | None:
    if value is not None and MESSAGE_ID_PATTERN.fullmatch(value) is not None:
        return value
    return None


def _unbracketed(value: str) -> list[str]:
    return [t for t in _SPLIT.split(value) if "@" in t and MESSAGE_ID_PATTERN.fullmatch(t) is not None]


def _skip_quoted(text: str, i: int) -> int:
    """Index after the closing quote of the string opened at `i` (len(text) when unterminated)."""
    n = len(text)
    i += 1
    while i < n:
        if text[i] == "\\":
            i += 2
        elif text[i] == '"':
            return i + 1
        else:
            i += 1
    return n


def _skip_comment(text: str, i: int) -> int:
    """Index after the nested comment opened at `i` (len(text) when unterminated)."""
    n = len(text)
    depth = 0
    while i < n:
        c = text[i]
        if c == "\\":
            i += 2
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return n


def msg_id_tokens(value: str) -> list[str]:
    """Message-IDs of an `In-Reply-To` or `References` value (M2); unique, in order, no RFC 2047 decoding."""
    text = _FOLD.sub("", value)
    if "<" not in text:
        found = _unbracketed(text)
    else:
        found = []
        i, n = 0, len(text)
        while i < n:
            c = text[i]
            if c == '"':
                i = _skip_quoted(text, i)
            elif c == "(":
                i = _skip_comment(text, i)
            elif c == "<":
                end = text.find(">", i + 1)
                if end < 0:
                    break  # an unterminated id is dropped (it may be the cut at the 64 KiB cap)
                token = text[i + 1 : end]
                i = end + 1
                if token.startswith("<"):  # one extra bracket level
                    token = token[1:]
                    if text.startswith(">", i):
                        i += 1
                if token:
                    found.append(token)
            else:
                i += 1
    return list(dict.fromkeys(found))


def bare_message_id(value: str | None) -> str | None:
    """The first token of a Message-ID header, else the stripped value when it holds no bracket (M1)."""
    if value is None:
        return None
    tokens = msg_id_tokens(value)
    if tokens:
        return tokens[0]
    stripped = value.strip()
    return stripped if stripped and "<" not in stripped else None


def _decoded(name: str, text: str) -> str:
    try:
        return str(email.policy.default.header_factory(name, text))
    except Exception:  # odd charset or malformed encoded word: keep the raw text
        return text


def _display_name(name: str) -> str:
    try:
        return str(make_header(decode_header(name)))
    except Exception:  # odd charset or malformed encoded word: keep the raw name
        return name


def _addresses(text: str) -> list[tuple[str, str]]:
    """(display name, address) pairs; the linear stdlib parser on text cut to 8 KiB (the policy parser is quadratic)."""
    try:
        return [(_display_name(n), a) for n, a in getaddresses([text[:MAX_ADDRESS_HEADER_BYTES]]) if a or n]
    except Exception:
        return []


def _first(items: Mapping[str, list[str]], name: str) -> str | None:
    values = items.get(name)
    return values[0] if values else None


def _in_reply_to(value: str | None, references: Sequence[str], people: set[str]) -> str | None:
    if value is None:
        return None
    tokens = msg_id_tokens(value)[:MAX_IN_REPLY_TO_TOKENS]
    if not tokens:
        return bare_message_id(value)
    referenced = frozenset(references)
    for token in tokens:  # phrase form: a token that also occurs in References is the id
        if token in referenced:
            return token
    for token in tokens:
        if token.lower() not in people:
            return token
    return None


def parse_summary_headers(raw: bytes | None) -> ParsedHeaders:
    if not raw:
        return ParsedHeaders(None, None, (), (), None, None, None, (), None)
    items: dict[str, list[str]] = {}
    for name, value in _PARSER.parsebytes(raw).raw_items():
        items.setdefault(name.lower(), []).append(_FOLD.sub("", str(value)).strip())
    sender = (_addresses(_first(items, "from") or "") or [("", "")])[0]
    to = tuple(a for _, a in _addresses(_first(items, "to") or "") if a)
    cc = tuple(a for _, a in _addresses(_first(items, "cc") or "") if a)
    subject_raw = _first(items, "subject")
    message_id = bare_message_id(_first(items, "message-id"))
    refs_raw = _first(items, "references")
    all_refs = [t for t in msg_id_tokens(refs_raw) if t != message_id] if refs_raw else []
    # spec 4.9 follow-up: pattern-invalid tokens are skipped
    valid_refs = [t for t in all_refs if valid_message_id(t) is not None]
    people = {a.lower() for a in (sender[1], *to, *cc) if a}
    in_reply_to = _in_reply_to(_first(items, "in-reply-to"), all_refs, people)
    root = (valid_refs[0] if valid_refs else None) or valid_message_id(in_reply_to) or valid_message_id(message_id)
    return ParsedHeaders(
        from_name=sender[0] or None,
        from_address=sender[1] or None,
        to=to,
        cc=cc,
        subject=_decoded("Subject", subject_raw) if subject_raw is not None else None,
        message_id=message_id,
        in_reply_to=in_reply_to,
        references=tuple(valid_refs[-MAX_REFERENCES:]),
        root=root,
    )


def normalise_subject(subject: str | None) -> str:
    """Subject key text (spec 080 §5.5, S-k); empty when nothing is left."""
    if not subject:
        return ""
    text = unicodedata.normalize(
        "NFKC", clean(subject[:_SUBJECT_INPUT_CHARS], _SUBJECT_INPUT_CHARS)[:_SUBJECT_KEPT_CHARS]
    )
    for _ in range(MAX_SUBJECT_ROUNDS):
        if not text:
            break
        match = _PREFIX.match(text) or _TAG.match(text)  # one removal per round (S-k)
        if match is None:
            break
        text = text[match.end() :]
    text = unicodedata.normalize("NFKC", text.casefold())
    return _WHITESPACE.sub(" ", text).strip()


def subject_digest(subject: str | None) -> str | None:
    normalised = normalise_subject(subject)
    if not normalised:
        return None
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()[:32]


def imap_thread_key(account: str, parsed: ParsedHeaders) -> ThreadKey | None:
    if parsed.root is not None:
        return ThreadKey(account, "headers", parsed.root)
    digest = subject_digest(parsed.subject)
    return ThreadKey(account, "subject", digest) if digest is not None else None


def is_own(address: str | None, own: frozenset[str]) -> bool:
    """Case-insensitive match; a `+tag` is ignored when the configured entry has none; no dot folding (S-j)."""
    if not address:
        return False
    own = frozenset(entry.lower() for entry in own)
    lowered = address.lower()
    if lowered in own:
        return True
    local, at, domain = lowered.rpartition("@")
    if not at or "+" not in local:
        return False
    base = local.split("+", 1)[0]
    return bool(base) and f"{base}@{domain}" in own
