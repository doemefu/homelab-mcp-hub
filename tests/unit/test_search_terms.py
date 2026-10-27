# ruff: noqa: RUF001  # the cases are the ambiguous Unicode characters themselves
"""Search terms, normaliser, Graph sanitiser and match folding (spec 080 §5.2 "Query handling", D82-D85)."""

import unicodedata
from datetime import UTC, datetime

import httpx2
import pytest

from mcp_hub.errors import ToolError
from mcp_hub.providers.base import FolderPage, ListQuery
from mcp_hub.providers.folders import ListFolder
from mcp_hub.search import (
    GRAPH_QUERY_TOO_LONG,
    MAX_GRAPH_ENCODED_CHARS,
    OPERATOR_PREFIXES,
    RESERVED_TEXT,
    SearchTerms,
    graph_search_text,
    imap_value_keys,
    match_fold,
    normalise_value,
    parse_search_terms,
)


def terms(
    query: str | None = None, sender: str | None = None, recipient: str | None = None, subject: str | None = None
) -> SearchTerms:
    return parse_search_terms(query=query, sender=sender, recipient=recipient, subject=subject)


def encoded_search_value(text: str) -> str:
    return str(httpx2.QueryParams({"$search": f'"{text}"'})).split("=", 1)[1]


@pytest.mark.parametrize(
    ("query", "words"),
    [
        ("  Rechnung   Oktober ", ("Rechnung", "Oktober")),
        ("Müller", ("Müller",)),  # NFD input leaves as NFC
        ("Re­chnung", ("Rechnung",)),  # soft hyphen (Cf) removed
        ('"Projekt X"', ("Projekt", "X")),
        ("a\\b", ("ab",)),
        ("a a b", ("a", "b")),
        ("MÜLLER", ("MÜLLER",)),  # case kept
        ("a b", ("a", "b")),  # U+2028 becomes a space
    ],
)
def test_normalise_wire_form(query: str, words: tuple[str, ...]) -> None:
    assert terms(query=query).words == words


def test_normalise_wire_form_is_utf8_nfc() -> None:
    sender = terms(sender="Müller").sender
    assert sender == "Müller"
    assert sender.encode() == b"M\xc3\xbcller"
    assert unicodedata.is_normalized("NFC", sender)


def test_normalise_value_returns_the_wire_form() -> None:
    assert normalise_value('  Mu\u0308ller \u200b"x" ', "sender") == "M\u00fcller x"


def test_filters_are_values_not_words() -> None:
    parsed = terms(sender="  Jean   Muster ", recipient="a b", subject='say "hi"')
    assert (parsed.words, parsed.sender, parsed.recipient, parsed.subject) == ((), "Jean Muster", "a b", "say hi")


@pytest.mark.parametrize(
    "query",
    [
        "a\tb",
        "a\nb",
        "\x00",
        "\x85",
        "\ud800",
        "​",
        "   ",
        "x" * 201,
        "",
        5,
        "X-gm-raw x",
        "X­-GM-RAW x",
        "a b c d e f g h i",
        "from:alice x",
        "Subject:Offerte",
        "HASATTACHMENTS:yes",
        "from：x",
        "list:x",
        "rfc822msgid:x",
        "Rechnung OR invoice",
        "a AND b",
        "x NOT y",
        "x NEAR y",
        "invoice -spam",
        "+urgent",
        "-7",
        "\uff0dx",  # fullwidth hyphen-minus (T1-S1)
        "\uff0bx",
    ],
)
def test_refused_values(query: object) -> None:
    with pytest.raises(ToolError) as caught:
        parse_search_terms(query=query, sender=None, recipient=None, subject="s")  # type: ignore[arg-type]
    assert caught.value.code == "invalid_argument"


@pytest.mark.parametrize("field", ["sender", "recipient", "subject"])
@pytest.mark.parametrize("value", ["a\tb", "\ud800", "   ", "x" * 201, "", 5, "x-gm-raw"])
def test_refused_filter_values(field: str, value: object) -> None:
    arguments: dict[str, object] = {"query": "ok", "sender": None, "recipient": None, "subject": None}
    arguments[field] = value
    with pytest.raises(ToolError) as caught:
        parse_search_terms(**arguments)  # type: ignore[arg-type]
    assert caught.value.code == "invalid_argument"


def test_x_gm_raw_has_its_own_text() -> None:
    with pytest.raises(ToolError) as caught:
        terms(query="x-gm-raw invoice")
    assert caught.value.message == RESERVED_TEXT


def test_all_four_omitted() -> None:
    with pytest.raises(ToolError) as caught:
        terms()
    assert caught.value.code == "invalid_argument"
    assert caught.value.message == "Pass query or at least one of sender, recipient, subject"


def test_value_length_is_counted_as_given() -> None:
    assert terms(query="x" * 200).words == ("x" * 200,)
    with pytest.raises(ToolError):
        terms(query="x" * 200 + "​")  # 201 code points as given


def test_eight_words_pass() -> None:
    assert terms(query="a b c d e f g h").words == tuple("abcdefgh")


def test_operator_prefix_table_has_27_names() -> None:
    assert len(OPERATOR_PREFIXES) == 27
    assert len(set(OPERATOR_PREFIXES)) == 27


@pytest.mark.parametrize(
    "word", ["Re: Offerte", "12:30", "Rückfrage", "and or", "-", "10-20", "10\uff0d20", "C-3PO", "--", "Re:"]
)
def test_allowed_colon_words(word: str) -> None:
    assert terms(query=word).words == tuple(word.split())


def test_operator_words_only_in_query() -> None:
    parsed = terms(query="plain", sender="from:x OR y", subject="-x")
    assert (parsed.sender, parsed.subject) == ("from:x OR y", "-x")


def test_word_dedup_casefold() -> None:
    assert terms(query="Rechnung rechnung RECHNUNG").words == ("Rechnung",)


def test_dedup_before_word_cap() -> None:
    assert len(terms(query="a A b B c C d D e E f F g G h H a").words) == 8


@pytest.mark.parametrize(
    "query", ["from:secret-word", "secret\tword", "x-gm-raw secret", "secret OR word", "-secret", "x" * 201 + "secret"]
)
def test_message_has_no_echo(query: str) -> None:
    with pytest.raises(ToolError) as caught:
        terms(query=query)
    assert "secret" not in caught.value.message


def test_search_terms_invariant() -> None:
    with pytest.raises(ValueError, match="at least one"):
        SearchTerms(words=(), sender=None, recipient=None, subject=None)
    with pytest.raises(ValueError, match="at most 8"):
        SearchTerms(words=tuple("abcdefghi"), sender=None, recipient=None, subject=None)
    for words, sender in ((("",), "x"), (("a b",), None), (("a",), ""), (("a\t",), None)):
        with pytest.raises(ValueError, match="non-empty"):
            SearchTerms(words=words, sender=sender, recipient=None, subject=None)
    with pytest.raises(ValueError, match="distinct"):
        SearchTerms(words=("a", "a"), sender=None, recipient=None, subject=None)


def test_imap_value_keys_order() -> None:
    parsed = SearchTerms(words=("a", "b"), sender="x", recipient=None, subject="y")
    assert imap_value_keys(parsed) == [("TEXT", "a"), ("TEXT", "b"), ("FROM", "x"), ("SUBJECT", "y")]
    assert imap_value_keys(terms(sender="x", recipient="z")) == [("FROM", "x"), ("TO", "z")]


@pytest.mark.parametrize(
    ("words", "expected"),
    [
        (["a%3Ab"], "a 3ab"),
        (["x&y"], "x y"),
        (["-x"], "x"),
        (["+x"], "x"),
        (["--"], None),
        (["*"], None),
        (["report*"], "report"),
        (["a", "ONEAR", "b"], "a onear b"),
        (["AND"], "and"),
        (["XRANK"], "xrank"),
        (["from：x"], "from x"),
        (["（a）"], "a"),
        (["a=b"], "a b"),
        (["O'Brien"], "o'brien"),
        (["jm@example.org"], "jm@example.org"),
        (["Grüße"], "grüße"),
        ([".x."], "x"),
        (["1..5"], "1..5"),  # pinned: plain-word interpretation inside R37
        (["a..b"], "a..b"),
        (["\uff21\uff2e\uff24"], "\uff41\uff4e\uff44"),  # fullwidth letters stay letters
        (["\uff0dx"], "x"),
        (["x\u2212y"], "x y"),
        (["@"], None),
        (["__"], None),
        (["a_b"], "a_b"),
        (["@example.org"], "@example.org"),
        (["Müller"], "müller"),  # marks are kept, nothing is normalised on the wire
        ([], None),
    ],
)
def test_graph_search_text(words: list[str], expected: str | None) -> None:
    assert graph_search_text(words) == expected


def test_graph_search_text_bounds() -> None:
    # seven words of five "/"-separated letters: 35 tokens, no silent drop
    with pytest.raises(ToolError) as caught:
        graph_search_text(["a/b/c/d/e"] * 7)
    assert (caught.value.code, caught.value.message) == ("invalid_argument", GRAPH_QUERY_TOO_LONG)
    assert graph_search_text(["a/b/c/d/e"] * 6 + ["a/b"]) == " ".join(["a b c d e"] * 6 + ["a b"])  # 32 tokens

    at_bound = "x" * (MAX_GRAPH_ENCODED_CHARS - len(encoded_search_value("")))
    assert len(encoded_search_value(at_bound)) == MAX_GRAPH_ENCODED_CHARS
    assert graph_search_text([at_bound]) == at_bound
    with pytest.raises(ToolError) as caught:
        graph_search_text([at_bound + "x"])
    assert caught.value.message == GRAPH_QUERY_TOO_LONG


def test_graph_search_text_bound_counts_encoded_characters() -> None:
    # "@" encodes to three characters, so the encoded value passes 1,500 well before 1,500 text characters
    with pytest.raises(ToolError):
        graph_search_text(["a@" * 400])


@pytest.mark.parametrize(
    "words",
    [
        ["a%3Ab"],
        ["x&y"],
        ["-x"],
        ["+x"],
        ["report*"],
        ["a", "ONEAR", "b"],
        ["from：x"],
        ["（a）"],
        ["a=b"],
        ["O'Brien"],
        ["jm@example.org"],
        ["Grüße"],
        ["a%25b", "c%26d", "e%2Af", "g%2Bh", "(i)", "k:l"],
    ],
)
def test_graph_search_text_wire_chars(words: list[str]) -> None:
    text = graph_search_text(words)
    assert text is not None
    wire = encoded_search_value(text)
    for forbidden in ("%25", "%26", "%2B", "%2A", "%3A", "%28", "%29", "%3D"):
        assert forbidden not in wire
    assert wire.count("%22") == 2


def test_match_fold() -> None:
    assert match_fold("Straße") == match_fold("STRASSE")
    assert match_fold("ＩＮＶＯＩＣＥ") == "invoice"
    assert match_fold("In​voice") == "invoice"
    assert match_fold("In\x07voice") == "invoice"
    assert match_fold("a\tb c  d") == "a b c d"
    assert match_fold("\ud800x") == match_fold("�x")
    assert match_fold("Müller") == match_fold("Müller")
    assert match_fold('Re: "Budget" Q4') == match_fold("Re: Budget Q4")
    assert match_fold("a\\b") == "ab"
    assert match_fold("＂a＂") == "a"  # fullwidth quotation mark turns into one under NFKC
    assert match_fold(" ​ ") == ""


def test_match_fold_pinned_residuals() -> None:
    assert match_fold("istanbul") not in match_fold("İstanbul")
    assert match_fold("muller") not in match_fold("Müller")


def test_list_query_defaults() -> None:
    since = datetime(2026, 10, 1, tzinfo=UTC)
    folder: ListFolder = "inbox"
    query = ListQuery(folder=folder, since=since, unread_only=False, direction=None, limit=10)
    assert query.search is None
    page = FolderPage(items=[], more=False, truncated=False, complete=True)
    assert page.incomplete == frozenset()
    searching = ListQuery(
        folder=folder,
        since=since,
        unread_only=False,
        direction=None,
        limit=10,
        search=terms(query="x"),
    )
    assert searching.search is not None
