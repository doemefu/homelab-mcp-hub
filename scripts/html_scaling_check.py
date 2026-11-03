"""Size-scaling check of the HTML-to-text converter on malformed input (re-review R1, R4).

Runs html_to_text on 64 KiB and 256 KiB of each shape, and the text sanitiser (`clean`, URL and address-host rules,
#59) on its own shapes, and fails when the time grows clearly faster than the size (linear: about 4x, quadratic:
about 16x). Used by tests/unit/test_html_scaling.py and, on the production
interpreter, by scripts/smoke_image.sh (piped into the image: python - < scripts/html_scaling_check.py).
"""

import platform
import sys
import time
from collections.abc import Callable

from mcp_hub.sanitize import clean, html_to_text

SMALL, LARGE = 64 * 1024, 256 * 1024
MAX_RATIO = 8.0  # linear growth gives about 4; quadratic about 16
NOISE_FLOOR_SECONDS = 0.05  # below this the large input is fast enough whatever the ratio
SHAPES = {
    "open-tag flood <x": lambda n: "<x" * (n // 2),
    "end-tag flood </": lambda n: "</" * (n // 2),
    "unclosed <!--": lambda n: "<!--" + "a" * (n - 4),
    "unclosed <![CDATA[": lambda n: "<![CDATA[" + "a" * (n - 9),
    "<! flood": lambda n: "<!" * (n // 2),
    "<? flood": lambda n: "<?" * (n // 2),
    "attribute run": lambda n: "<a " + 'b="c" ' * ((n - 3) // 6),
    "unterminated attribute": lambda n: '<a href="' + "x" * (n - 9),
}
# Text shapes for `clean` (#59): a long `www.` token that fails the address-host check only at its end, many kept
# address hosts, and a long host prefix before each `www.` token (the look-back stops after 255 characters).
CLEAN_SHAPES = {
    "failing address-host token": lambda n: "x@www." + "." * (n - 7) + "%",
    "many address hosts": lambda n: "x@www.a " * (n // 8),
    "long host prefix": lambda n: ("x@" + "a." * 200 + "www.b ") * (n // 408),
}


def clean_all(text: str) -> str:
    return clean(text, len(text) + 1, multiline=True)


def elapsed(text: str, runs: int = 3, convert: Callable[[str], str] = html_to_text) -> float:
    """Fastest of `runs` timings, so one scheduler hiccup cannot fail the check."""
    best = float("inf")
    for _ in range(runs):
        started = time.perf_counter()
        convert(text)
        best = min(best, time.perf_counter() - started)
    return best


def measure() -> list[tuple[str, float, float]]:
    html = [(name, elapsed(build(SMALL)), elapsed(build(LARGE))) for name, build in SHAPES.items()]
    text = [
        (name, elapsed(build(SMALL), convert=clean_all), elapsed(build(LARGE), convert=clean_all))
        for name, build in CLEAN_SHAPES.items()
    ]
    return html + text


def scales_linearly(small: float, large: float) -> bool:
    return large < NOISE_FLOOR_SECONDS or large <= MAX_RATIO * max(small, 1e-4)


def hidden_text_leaks() -> list[str]:
    """Unterminated comment or CDATA at the end of a document must stay hidden (R4)."""
    cases = {
        "comment": "<p>Hi</p><!-- SECRET",
        "comment with >": "<p>Hi</p><!-- a > SECRET",
        "cdata": "<p>Hi</p><![CDATA[ SECRET",
        "cdata with >": "<p>Hi</p><![CDATA[ a > SECRET",
        "bogus": "<p>Hi</p><! SECRET",
        "nested end tag": "<td style='display:none'><table><tr><td>inner</td></tr></table>SECRET</td>",
        "deep nesting": "<div>" * 300 + "<span hidden>SECRET</span>",
        "script double escape": "<script><!--<script></script>SECRET</script>-->after",
        "end tag out of scope": "<div hidden><table><tr><td></div>SECRET</td></tr></table></div>",
    }
    return [name for name, html in cases.items() if "SECRET" in html_to_text(html)]


def main() -> int:
    print(f"python {platform.python_version()} ({sys.implementation.name})")
    failed = False
    for name, small, large in measure():
        ok = scales_linearly(small, large)
        failed |= not ok
        ratio = large / max(small, 1e-4)
        print(f"{'ok  ' if ok else 'FAIL'} {name:24} 64KiB {small:8.4f}s  256KiB {large:8.4f}s  x{ratio:6.1f}")
    leaks = hidden_text_leaks()
    print("hidden text leaks:", leaks or "none")
    return 1 if failed or leaks else 0


if __name__ == "__main__":
    sys.exit(main())
