import pytest

from mcp_hub.providers.graph_thread import (
    LIST_FLOOR,
    MAX_UNIQUE_BODY_READS,
    THREAD_LIST_TOP,
    UNIQUE_BODY_BYTES,
    body_text,
    thread_list_params,
)


def test_constants() -> None:
    assert (THREAD_LIST_TOP, MAX_UNIQUE_BODY_READS, UNIQUE_BODY_BYTES) == (51, 20, 65_536)
    assert LIST_FLOOR == "1900-01-01T00:00:00Z"


def test_thread_list_params_exact() -> None:
    params = thread_list_params("AAQk-_=", select="id,subject", expand="x", unique_body=False)
    assert params == {
        "$filter": "receivedDateTime ge 1900-01-01T00:00:00Z and conversationId eq 'AAQk-_='",
        "$orderby": "receivedDateTime desc",
        "$top": "51",
        "$select": "id,subject",
        "$expand": "x",
    }
    with_unique = thread_list_params("abc", select="id,subject", expand="x", unique_body=True)
    assert with_unique["$select"] == "id,subject,uniqueBody"


@pytest.mark.parametrize("value", ["a'b", "a b", "a" * 301, "", "a/b"])
def test_conversation_id_refused(value: str) -> None:
    with pytest.raises(ValueError, match="conversation id"):
        thread_list_params(value, select="id", expand="x", unique_body=False)


def test_body_text_shapes() -> None:
    assert body_text({"uniqueBody": {"contentType": "text", "content": "hi"}}, "uniqueBody") == (
        "hi",
        "text/plain",
        False,
    )
    html = body_text({"body": {"contentType": "HTML", "content": "<p>hi</p>"}}, "body")
    assert html is not None
    assert html[1:] == ("text/html-converted", False)
    assert "hi" in html[0]
    assert body_text({"body": {"contentType": "weird", "content": "x"}}, "body") == ("x", "text/plain", False)
    assert body_text({"body": {"content": "x"}}, "body") == ("x", "text/plain", False)
    for raw in ({}, {"body": None}, {"body": "x"}, {"body": []}, {"body": {"content": 5}}, {"body": {}}):
        assert body_text(raw, "body") is None
    assert body_text({"body": {"content": "x"}}, "uniqueBody") is None


def test_body_text_cut_at_64_kib_utf8() -> None:
    exact = "a" * UNIQUE_BODY_BYTES
    assert body_text({"body": {"content": exact}}, "body") == (exact, "text/plain", False)
    # a 2-byte character straddles the limit: the partial tail is dropped
    straddle = "a" * (UNIQUE_BODY_BYTES - 1) + "é"
    result = body_text({"body": {"content": straddle}}, "body")
    assert result == ("a" * (UNIQUE_BODY_BYTES - 1), "text/plain", True)
    multi = "€" * 30_000  # 3 bytes each
    result = body_text({"body": {"content": multi}}, "body")
    assert result is not None
    assert result[2] is True
    assert result[0] == "€" * (UNIQUE_BODY_BYTES // 3)
    # lone surrogates encode with surrogatepass and never raise
    surrogates = "\ud800" * 30_000
    result = body_text({"body": {"content": surrogates}}, "body")
    assert result is not None
    assert result[2] is True
    assert len(result[0].encode("utf-8", "surrogatepass")) <= UNIQUE_BODY_BYTES
