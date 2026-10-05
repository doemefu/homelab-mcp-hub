import base64
import json

import pytest

from mcp_hub.errors import ToolError
from mcp_hub.ids import GraphMessageRef, MessageRef, decode_message_id, encode_message_id

GRAPH_ID = (
    "AAMkAGVmMDEzMTM4LTZmYWUtNDdkNC1hMDZiLTU1OGY5OTZhYmY4OABGAAAAAAAiQ8W967B7TKBjgx9rVEURBwAiIsqMbYjsT5e-"
    "T7KzowPTAAAAAAEMAAAiIsqMbYjsT5e-T7KzowPTAASoXUT3AAA="
)


def raw(parts: list[object]) -> str:
    return "v1." + base64.urlsafe_b64encode(json.dumps(parts).encode()).rstrip(b"=").decode()


def test_graph_ref_round_trip_and_folder() -> None:
    ref = GraphMessageRef("outlook", GRAPH_ID)
    value = encode_message_id(ref)
    assert decode_message_id(value) == ref
    assert ref.folder == "inbox"
    assert len(value) <= 512


def test_longest_allowed_graph_id_fits_512() -> None:
    assert len(encode_message_id(GraphMessageRef("a" * 32, "A" * 300))) <= 512


@pytest.mark.parametrize("graph_id", ["", "A" * 301, "abc/def", "../me", "a?b=c", "a%2Fb", "a b", "a+b", "ä", 7, None])
def test_graph_id_charset_enforced(graph_id: object) -> None:
    with pytest.raises(ToolError) as info:
        decode_message_id(raw(["g", "outlook", graph_id]))
    assert info.value.code == "invalid_argument"


def test_imap_ids_unchanged() -> None:
    ref = MessageRef("icloud", "INBOX", 7, 3)
    assert decode_message_id(encode_message_id(ref)) == ref


@pytest.mark.parametrize(
    "parts",
    [["g", "outlook"], ["g", "Outlook", GRAPH_ID], ["g", "outlook", GRAPH_ID, 1], ["x", "outlook", GRAPH_ID]],
)
def test_malformed_graph_ids(parts: list[object]) -> None:
    with pytest.raises(ToolError):
        decode_message_id(raw(parts))


def test_imap_mailbox_refuses_a_graph_ref_before_connecting() -> None:
    from mcp_hub.providers.base import ProviderError
    from mcp_hub.providers.imap import ImapMailbox

    opened: list[str] = []

    def refuse(*args: object, **kwargs: object) -> object:
        opened.append("connect")
        raise AssertionError("no connection expected")

    box = ImapMailbox(
        "outlook",
        host="h",
        port=993,
        folder="INBOX",
        username="u",
        password="p",
        client_factory=refuse,  # type: ignore[arg-type]
    )
    with pytest.raises(ProviderError) as info:
        box.get_message(GraphMessageRef("outlook", GRAPH_ID))
    assert (info.value.code, info.value.cause) == ("not_found", "ForeignRef")
    assert opened == []
