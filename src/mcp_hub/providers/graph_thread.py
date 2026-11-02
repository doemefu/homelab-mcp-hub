"""Pure helpers for the Graph `get_thread` path (spec 080 §5.2, Option 5a)."""

from collections.abc import Mapping
from typing import Final, Literal

from mcp_hub.ids import GRAPH_ID
from mcp_hub.providers.base import BodySource
from mcp_hub.sanitize import html_to_text

THREAD_LIST_TOP: Final = 51
MAX_UNIQUE_BODY_READS: Final = 20
UNIQUE_BODY_BYTES: Final = 65_536
LIST_FLOOR: Final = "1900-01-01T00:00:00Z"


def thread_list_params(conversation_id: str, *, select: str, expand: str, unique_body: bool) -> dict[str, str]:
    """Query parameters of the conversation list; the id is re-checked before it enters the `$filter` literal."""
    if not GRAPH_ID.fullmatch(conversation_id):
        raise ValueError("conversation id")
    return {
        "$filter": f"receivedDateTime ge {LIST_FLOOR} and conversationId eq '{conversation_id}'",
        "$orderby": "receivedDateTime desc",
        "$top": str(THREAD_LIST_TOP),
        "$select": f"{select},uniqueBody" if unique_body else select,
        "$expand": expand,
    }


def body_text(raw: Mapping[str, object], field: Literal["uniqueBody", "body"]) -> tuple[str, BodySource, bool] | None:
    """Text of a Graph body object as (text, source, cut); None unless the field is an object with str content."""
    value = raw.get(field)
    content = value.get("content") if isinstance(value, dict) else None
    if not isinstance(content, str):
        return None
    kind = value.get("contentType") if isinstance(value, dict) else None
    is_html = isinstance(kind, str) and kind.lower() == "html"
    text = html_to_text(content) if is_html else content
    source: BodySource = "text/html-converted" if is_html else "text/plain"
    # Every character takes at least one byte, so the first 64 KiB of the encoding lie within the first 65,536
    # characters: only that prefix is encoded (#56), with the same result as encoding the whole text.
    head = text[:UNIQUE_BODY_BYTES].encode("utf-8", "surrogatepass")
    if len(text) <= UNIQUE_BODY_BYTES and len(head) <= UNIQUE_BODY_BYTES:
        return text, source, False
    return head[:UNIQUE_BODY_BYTES].decode("utf-8", "ignore"), source, True
