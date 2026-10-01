"""IMAP BODYSTRUCTURE walking, text-part choice and part decoding (spec 080 §5.2 get_message, §5.4)."""

import base64
import binascii
import codecs
import quopri
import re
from collections.abc import Sequence
from dataclasses import dataclass
from email.header import decode_header, make_header
from email.utils import collapse_rfc2231_value, decode_rfc2231
from typing import Final

_BASE64_NOISE: Final = re.compile(rb"[^A-Za-z0-9+/]")


@dataclass(frozen=True, slots=True)
class Part:
    section: str
    mime_type: str
    params: dict[str, str]
    encoding: str
    size: int
    disposition: str | None
    filename: str | None


def _text(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value if isinstance(value, str) else ""


def _decoded_param(value: str) -> str:
    if "=?" in value:
        try:
            return str(make_header(decode_header(value)))
        except (ValueError, LookupError, UnicodeError):
            return value
    return value


def _params(value: object) -> dict[str, str]:
    if not isinstance(value, Sequence) or isinstance(value, bytes | str):
        return {}
    items = [_text(v) for v in value]
    params: dict[str, str] = {}
    for key, raw in zip(items[0::2], items[1::2], strict=False):
        name = key.lower()
        value_text = raw
        if name.endswith("*"):  # RFC 2231 extended value, e.g. filename*=utf-8''R%C3%A9sum%C3%A9.pdf
            name = name[:-1]
            value_text = collapse_rfc2231_value(decode_rfc2231(raw))
        params[name] = _decoded_param(value_text)
    return params


def _leaf(node: Sequence[object], section: str) -> Part:
    if len(node) < 2:  # empty or truncated structure: a neutral part that is never chosen as text
        return Part(section, "application/octet-stream", {}, "", 0, None, None)
    maintype, subtype = _text(node[0]).lower(), _text(node[1]).lower()
    params = _params(node[2]) if len(node) > 2 else {}
    encoding = _text(node[5]).lower() if len(node) > 5 else ""
    size = node[6] if len(node) > 6 and type(node[6]) is int else 0
    md5_index = 8 if maintype == "text" else 10 if (maintype, subtype) == ("message", "rfc822") else 7
    disposition_field = node[md5_index + 1] if len(node) > md5_index + 1 else None
    disposition: str | None = None
    disposition_params: dict[str, str] = {}
    if isinstance(disposition_field, Sequence) and not isinstance(disposition_field, bytes | str) and disposition_field:
        disposition = _text(disposition_field[0]).lower() or None
        disposition_params = _params(disposition_field[1]) if len(disposition_field) > 1 else {}
    filename = disposition_params.get("filename") or params.get("name")
    return Part(section, f"{maintype}/{subtype}", params, encoding, size, disposition, filename or None)


def walk(structure: Sequence[object], section: str = "") -> list[Part]:
    """Leaf parts in IMAP section order; a message/rfc822 part is one leaf."""
    if structure and isinstance(structure[0], list):  # multipart: ([children], subtype, ...)
        parts: list[Part] = []
        for index, child in enumerate(structure[0], start=1):
            parts += walk(child, f"{section}.{index}" if section else str(index))
        return parts
    return [_leaf(structure, section or "1")]


def choose_text_part(parts: list[Part]) -> Part | None:
    inline = [p for p in parts if p.disposition != "attachment"]
    for wanted in ("text/plain", "text/html"):
        for part in inline:
            if part.mime_type == wanted:
                return part
    return None


def attachment_parts(parts: list[Part], chosen: Part | None) -> list[Part]:
    return [
        p
        for p in parts
        if p is not chosen and (p.disposition == "attachment" or p.filename or not p.mime_type.startswith("text/"))
    ]


def _base64(data: bytes) -> bytes:
    """Decode a complete or partially fetched base64 part: padding is recomputed, never cut."""
    compact = _BASE64_NOISE.sub(b"", data)  # also strips "=" and whitespace
    if len(compact) % 4 == 1:  # a cut stream can end with one lone character that carries no full byte
        compact = compact[:-1]
    compact += b"=" * (-len(compact) % 4)
    try:
        return base64.b64decode(compact)
    except binascii.Error:
        return b""


def safe_decode(raw: bytes, charset: str | None) -> str:
    """Never raises: only real text codecs are used; rot13, base64, zlib, hex and idna fall back to UTF-8, and
    latin-1 is the last resort."""
    name = (charset or "utf-8").strip().lower() or "utf-8"
    try:
        info = codecs.lookup(name)
        if not getattr(info, "_is_text_encoding", True) or info.name == "idna":
            name = "utf-8"
    except LookupError:
        name = "utf-8"
    for candidate in (name, "utf-8"):
        try:
            return raw.decode(candidate, errors="replace")
        except (LookupError, UnicodeError, ValueError):
            continue
    return raw.decode("latin-1")


def decode_part(data: bytes, part: Part) -> str:
    raw = data
    if part.encoding == "base64":
        raw = _base64(data)
    elif part.encoding == "quoted-printable":
        raw = quopri.decodestring(data)
    return safe_decode(raw, part.params.get("charset"))
