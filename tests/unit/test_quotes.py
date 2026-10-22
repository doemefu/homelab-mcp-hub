"""Quote removal for get_thread bodies (spec 080 §5.2 "Quote removal"; WP13c T3: E-mf7, MF-P4, Q2, Q-f)."""

from collections.abc import Callable
from pathlib import Path

import pytest

from mcp_hub.providers.quotes import FORWARD_MARKERS, MAX_LOOKAHEAD_LINES, remove_quotes
from mcp_hub.sanitize import html_to_text
from tests.support.linear_time import scales_linearly, scaling

FIXTURES = Path(__file__).parents[1] / "fixtures" / "quotes"
ATTRIBUTION = "On Mon, 5 Oct 2026 at 10:00, Alex Example <alex@example.org> wrote:"
OUTLOOK_BLOCK = (
    "From: Alex Example <alex@example.org>\n"
    "Sent: Monday, October 5, 2026 10:00\n"
    "To: Sam Example <sam@example.org>\n"
    "Subject: Meeting\n"
)


def _normalised(text: str) -> str:
    """What the caller hands over (mf-MT10): LF line breaks, no-break spaces as plain spaces."""
    return text.replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " ").replace("\u202f", " ")


def _fixture_input(name: str) -> str:
    html = FIXTURES / f"{name}.in.html"
    if html.exists():
        return _normalised(html_to_text(html.read_text(encoding="utf-8")))
    return (FIXTURES / f"{name}.in.txt").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "name",
    [
        "gmail-en",
        "apple-en",
        "ios-de",
        "gmail-de",
        "wrapped-three-lines",
        "schrieb-am",
        "outlook-star-from",
        "outlook-von-de",
        "outlook-underscores-en",
        "original-message-en",
        "original-message-de",
        "original-message-case",
        "gmail",
        "apple",
        "outlook",
        "interleaved",
        "owner-quote-line",
    ],
)
def test_quote_rules_table(name: str) -> None:
    expected = (FIXTURES / f"{name}.out.txt").read_text(encoding="utf-8").removesuffix("\n")
    assert remove_quotes(_fixture_input(name), subject="Re: Meeting") == (expected, "heuristic")


def test_every_fixture_is_in_the_table() -> None:
    inputs = {path.name.split(".")[0] for path in FIXTURES.glob("*.in.*")}
    outputs = {path.name.split(".")[0] for path in FIXTURES.glob("*.out.txt")}
    assert inputs == outputs
    assert len(inputs) == 17


@pytest.mark.parametrize(
    "separator",
    [
        "-----Original Message-----",
        "----- original message -----",
        "-- ORIGINAL MESSAGE --",
        "-----Ursprüngliche Nachricht-----",
        "-----URSPRÜNGLICHE NACHRICHT-----",
        "-------- Original-Nachricht --------",
        "--originalnachricht--",
        "  \t-----Original Message-----  ",
    ],
)
def test_dashed_separator_variants(separator: str) -> None:
    assert remove_quotes(f"Reply\n{separator}\nold text", subject=None) == ("Reply", "heuristic")


@pytest.mark.parametrize(
    "separator",
    ["-Original Message-", "-----Original Message", "-----Original Message----- old text", "-----Forwarded-----"],
)
def test_dashed_separator_needs_two_dashes_on_both_sides(separator: str) -> None:
    text = f"Reply\n{separator}\nold text"
    assert remove_quotes(text, subject=None) == (text, "none")


@pytest.mark.parametrize(
    "text",
    [
        "Text\n\nOn Monday I wrote:\nmore text",  # no digit, no @
        "Text\n\nAm Montag schrieb ich:\nmore text",
        "Text\n\nOn Monday alex@example.org wrote:\nmore text",  # an @ without a digit
        "Text\n\nOn 5 Oct I wrote:\nmore text",  # a digit without an @ and without a following quote
        "Text\n\nAm 5. Okt schrieb ich:\nmore text",
        "Our totals for 2026:\nmore text",  # ends with a colon, not an attribution
    ],
)
def test_guard_negatives_do_not_cut(text: str) -> None:
    assert remove_quotes(text, subject=None) == (text, "none")


def test_guard_needs_a_digit_even_before_a_quote() -> None:
    # The attribution stays; step 2 still removes the ">" line.
    text = "Text\n\nOn Monday I wrote:\n> quoted"
    assert remove_quotes(text, subject=None) == ("Text\n\nOn Monday I wrote:", "heuristic")


def test_guard_accepts_a_digit_with_a_following_quote() -> None:
    assert remove_quotes("Yes.\n\nOn 5 Oct Alex wrote:\n\n> quoted", subject=None) == ("Yes.", "heuristic")


@pytest.mark.parametrize("prefix", ["", "__________________________\n"])
def test_itinerary_block_without_subject_stays(prefix: str) -> None:
    text = f"Your trip:\n\n{prefix}From: Zürich\nTo: Berlin\nDate: 5 Oct 2026\n\nHave a good trip."
    assert remove_quotes(text, subject="Booking") == (text, "none")


def test_header_block_needs_two_header_lines() -> None:
    text = "Reply\n\nFrom: Alex Example\nSubject: Meeting\n\nold text"
    assert remove_quotes(text, subject=None) == (text, "none")


def test_header_names_are_case_sensitive_and_only_from_may_be_bold() -> None:
    # As written in §5.2 (d): only From/Von carry the optional asterisks (Option 7c: no case-insensitive match).
    bold = "Reply\n\n*From:* Alex\n*Sent:* Monday\n*To:* Sam\n*Subject:* Meeting\n\nold text"
    lower = "Reply\n\nfrom: Alex\nsent: Monday\nto: Sam\nsubject: Meeting\n\nold text"
    assert remove_quotes(bold, subject=None) == (bold, "none")
    assert remove_quotes(lower, subject=None) == (lower, "none")


def test_block_may_start_at_a_sent_line() -> None:
    text = "Ok.\n\nGesendet: Montag, 5. Oktober 2026 10:00\nVon: Alex Example\nBetreff: Termin\n\nold text"
    assert remove_quotes(text, subject=None) == ("Ok.", "heuristic")


def test_sent_line_needs_a_from_line_within_two_lines_and_a_subject() -> None:
    far = "Ok.\n\nSent: Monday\nTo: Sam\nDate: today\nFrom: Alex\nX: y\nZ: w\n\nold text"
    no_subject = "Ok.\n\nSent: Monday\nFrom: Zürich\nTo: Berlin\n\nold text"
    assert remove_quotes(far, subject=None) == (far, "none")
    assert remove_quotes(no_subject, subject=None) == (no_subject, "none")


def test_header_look_ahead_counts_six_non_blank_lines() -> None:
    within = "Reply\n\nFrom: Alex\na\nb\nc\nd\nTo: Sam\nSubject: Meeting\n\nold text"
    beyond = "Reply\n\nFrom: Alex\na\nb\nc\nd\ne\nTo: Sam\nSubject: Meeting\n\nold text"
    assert remove_quotes(within, subject=None) == ("Reply", "heuristic")
    assert remove_quotes(beyond, subject=None) == (beyond, "none")


def test_header_look_ahead_stops_after_the_line_limit() -> None:
    gap = "\n" * (MAX_LOOKAHEAD_LINES - 2)
    within = f"Reply\n\nFrom: Alex\nTo: Sam{gap}\nSubject: Meeting\n\nold text"
    beyond = f"Reply\n\nFrom: Alex\nTo: Sam\n{gap}\nSubject: Meeting\n\nold text"
    assert remove_quotes(within, subject=None) == ("Reply", "heuristic")
    assert remove_quotes(beyond, subject=None) == (beyond, "none")


def test_underscores_need_ten_and_a_block_within_three_lines() -> None:
    nine = "Reply\n\n_________\nFrom: Alex\nSent: Monday\nSubject: Meeting\n\nold text"
    far = "Reply\n\n__________\n\n\n\nFrom: Alex\nSent: Monday\nSubject: Meeting\n\nold text"
    # Without the separator the block itself still cuts, so the underscore line stays.
    assert remove_quotes(nine, subject=None) == ("Reply\n\n_________", "heuristic")
    assert remove_quotes(far, subject=None) == ("Reply\n\n__________", "heuristic")


@pytest.mark.parametrize("marker", FORWARD_MARKERS)
def test_forward_markers_and_prefixes_keep_content(marker: str) -> None:
    text = f"FYI, see below.\n\n---------- {marker} ---------\n\n{OUTLOOK_BLOCK}\nforwarded text"
    assert remove_quotes(text, subject="Meeting") == (text, "none")


def test_forward_marker_counts_three_preceding_non_blank_lines() -> None:
    within = f"FYI\n\nBegin forwarded message:\n\nnote 1\n\nnote 2\n{OUTLOOK_BLOCK}\nforwarded text"
    beyond = f"FYI\n\nBegin forwarded message:\nnote 1\nnote 2\nnote 3\n{OUTLOOK_BLOCK}\nforwarded text"
    assert remove_quotes(within, subject=None) == (within, "none")
    assert remove_quotes(beyond, subject=None) == (
        "FYI\n\nBegin forwarded message:\nnote 1\nnote 2\nnote 3",
        "heuristic",
    )


@pytest.mark.parametrize(
    "subject", ["Fwd: Meeting", "FW: Meeting", "Fw: Meeting", "WG: Termin", "Wtr: Termin", " fwd[2]: x"]
)
def test_forward_subject_prefix_keeps_header_blocks(subject: str) -> None:
    for text in (
        f"FYI\n\n{OUTLOOK_BLOCK}\nforwarded text",
        f"FYI\n\n________________\n{OUTLOOK_BLOCK}\nforwarded text",
    ):
        assert remove_quotes(text, subject=subject) == (text, "none")


@pytest.mark.parametrize("subject", ["Re: Fwd: Meeting", "AW: WG: Termin", "Meeting", "Fwdx: Meeting", "", None])
def test_other_subjects_still_cut(subject: str | None) -> None:
    assert remove_quotes(f"Reply\n\n{OUTLOOK_BLOCK}\nold text", subject=subject) == ("Reply", "heuristic")


def test_forward_does_not_keep_an_attribution() -> None:
    # Forward markers only exclude (d); the forwarded mail's own quoted history is still removed.
    text = f"FYI\n\nBegin forwarded message:\n\n{ATTRIBUTION}\nold text"
    assert remove_quotes(text, subject="Fwd: Meeting") == ("FYI\n\nBegin forwarded message:", "heuristic")


def test_attribution_then_quotes_continues_scan() -> None:
    text = f"Reply\n\n{ATTRIBUTION}\n> quoted\n\nMore reply\n\n{OUTLOOK_BLOCK}\nold text\n{ATTRIBUTION}\n> older"
    assert remove_quotes(text, subject=None) == ("Reply\n\n\nMore reply", "heuristic")


def test_attribution_followed_by_text_cuts_to_the_end() -> None:
    text = f"Reply\n\n{ATTRIBUTION}\nold text\n> older\nmore old text"
    assert remove_quotes(text, subject=None) == ("Reply", "heuristic")


def test_sign_off_before_an_attribution_stays() -> None:
    # A start that matches only rule (e) gives way to a later start that ends on the same line (lead question 1),
    # so the line before an attribution is never taken as its first line.
    text = f"Thanks\nSam\n{ATTRIBUTION}\nold text"
    two = "Thanks\n\nSam Example <sam1@example.org>\nwrote:\n> quoted"
    assert remove_quotes(text, subject=None) == ("Thanks\nSam", "heuristic")
    assert remove_quotes(two, subject=None) == ("Thanks", "heuristic")


def test_anchored_attribution_keeps_its_first_line() -> None:
    text = "Ok.\n\nOn Mon, 5 Oct 2026 at 10:00, Alex Example\n<alex1@example.org> wrote:\n> quoted"
    assert remove_quotes(text, subject=None) == ("Ok.", "heuristic")


def test_attribution_with_leading_whitespace() -> None:
    assert remove_quotes(f"Reply\n   {ATTRIBUTION}\n> quoted", subject=None) == ("Reply", "heuristic")


def test_fully_quoted_body_returns_original_none() -> None:
    for text in ("> a\n> b\n", f"{ATTRIBUTION}\nold text\n", f"\n\n{OUTLOOK_BLOCK}", "-----Original Message-----\n"):
        assert remove_quotes(text, subject=None) == (text, "none")


def test_trailing_blank_lines_trimmed() -> None:
    assert remove_quotes(f"Reply\n\n \n\n{ATTRIBUTION}\n> q\n\n", subject=None) == ("Reply", "heuristic")
    assert remove_quotes("Hello\n\n \n", subject=None) == ("Hello", "none")


def test_no_change_is_none() -> None:
    text = "Hello\n  indented\nWorld: done"
    assert remove_quotes(text, subject=None) == (text, "none")
    assert remove_quotes("", subject=None) == ("", "none")


def _lines(unit: str) -> Callable[[int], str]:
    return lambda size: unit * (size // len(unit))


HOSTILE: dict[str, Callable[[int], str]] = {
    "one dash line": lambda size: "-" * size,
    "underscores then blank lines": lambda size: "_" * 20 + "\n" * (size - 20),
    "attribution lines": _lines("On 1 wrote:\n"),
    "attribution lines with @": _lines("On 1 a@b wrote:\n"),
    "from lines": _lines("From:\n"),
    "from lines with blank runs": _lines("From:\n" + "\n" * 62),
    "sent and from lines": _lines("Sent:\nFrom:\n"),
    "underscores and from lines": _lines("__________\nFrom:\nTo:\n"),
    "alternating quote lines": _lines("> a\nb\n"),
    "one line of dashes, quotes and spaces": lambda size: "- > " * (size // 4),
    "colon lines": _lines("a:\n"),
    "sign-off and attribution": _lines("Sam\n\nOn 1 a@b wrote:\nx\n"),
    "schrieb am lines": _lines("schrieb am 1:\n"),
    "zero-width runs": lambda size: ("\u200b" * 63 + "\n") * (size // 64),
    "one zero-width line": lambda size: "\u200b" * size,
    "forward markers and blocks": _lines("Forwarded message\nFrom:\nTo:\nSubject:\n"),
}


@pytest.mark.parametrize("shape", list(HOSTILE))
def test_hostile_inputs_linear(shape: str) -> None:
    small, large = scaling(lambda text: remove_quotes(text, subject="Re: x"), HOSTILE[shape], 64 * 1024)
    assert scales_linearly(small, large), (shape, small, large)
