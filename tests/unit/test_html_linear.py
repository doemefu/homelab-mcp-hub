"""SC2: html_to_text stays linear on hostile 64 KiB bodies, as get_thread converts up to 50 of them per call (Q-f).

T14 runs this test once more in the production image and records the image's Python patch version.
"""

import sys
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

# "a < without >" runs html.parser's start-tag regex over the whole body, and its backtracking stack takes about
# 180 bytes per input byte: about 11 MiB at 64 KiB still fits a CPU cache, about 44 MiB at 256 KiB does not, so on
# shared CI runners that one step measured 8-15x although the parser is linear. From 256 KiB on, both samples miss
# the cache and the step is about 4x again. A quadratic still gives 16x, and more clearly at the larger size.
BASE_SIZE = {"a < without >": 256 * 1024}


# Same guard as test_html_scaling.py: html.parser is quadratic before 3.13.15 (minutes per shape on 3.13.5); CI and
# the production image run 3.13.15 or newer, where this test runs.
@pytest.mark.skipif(sys.version_info < (3, 13, 15), reason="stdlib html.parser is quadratic before 3.13.15")
@pytest.mark.parametrize("shape", list(SHAPES))
def test_html_to_text_linear(shape: str) -> None:
    small, large = scaling(html_to_text, SHAPES[shape], BASE_SIZE.get(shape, SIZE))
    assert scales_linearly(small, large), (shape, small, large)
