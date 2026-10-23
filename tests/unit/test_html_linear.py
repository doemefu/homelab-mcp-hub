"""SC2: html_to_text stays linear on hostile 64 KiB bodies, as get_thread converts up to 50 of them per call (Q-f).

T14 runs this test once more in the production image and records the image's Python patch version.
"""

from collections.abc import Callable

import pytest

from mcp_hub.sanitize import html_to_text
from tests.support.linear_time import scales_linearly, scaling

SIZE = 64 * 1024


def _scaled(count_at_64_kib: int, unit: str, pad: str = "") -> Callable[[int], str]:
    """`unit` repeated count_at_64_kib times for 64 KiB (proportionally more for larger sizes), padded to the size."""

    def build(size: int) -> str:
        text = unit * (count_at_64_kib * size // SIZE)
        return text + pad * max(0, (size - len(text)) // max(1, len(pad)))

    return build


SHAPES: dict[str, Callable[[int], str]] = {
    "unterminated comment": lambda size: "<p>Hi</p><!--" + "a" * (size - 13),
    "unterminated CDATA": lambda size: "<p>Hi</p><![CDATA[" + "a" * (size - 18),
    "10k <a": _scaled(10_000, "<a", " x"),
    "end-tag runs": lambda size: "</" * (size // 2),
    "a < without >": lambda size: "text <" + "a b " * ((size - 6) // 4),
    "2,000 nested hidden divs": _scaled(2_000, "<div hidden>", "x"),
    "attribute with 60k quotes": lambda size: '<a title="x" b=' + '"' * (size * 60_000 // SIZE) + ">text",
}


@pytest.mark.parametrize("shape", list(SHAPES))
def test_html_to_text_linear(shape: str) -> None:
    small, large = scaling(html_to_text, SHAPES[shape], SIZE)
    assert scales_linearly(small, large), (shape, small, large)
