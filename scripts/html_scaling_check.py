"""Size-scaling check of the HTML-to-text converter on malformed input (re-review R1, R4).

Runs html_to_text on 64 KiB and 256 KiB of each shape and fails when the time grows clearly faster than the size
(linear: about 4x, quadratic: about 16x). Used by tests/unit/test_html_scaling.py and, on the production
interpreter, by scripts/smoke_image.sh (piped into the image: python - < scripts/html_scaling_check.py).
"""

import platform
import sys
import time

from mcp_hub.sanitize import html_to_text

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


def elapsed(text: str) -> float:
    started = time.perf_counter()
    html_to_text(text)
    return time.perf_counter() - started


def measure() -> list[tuple[str, float, float]]:
    return [(name, elapsed(build(SMALL)), elapsed(build(LARGE))) for name, build in SHAPES.items()]


def scales_linearly(small: float, large: float) -> bool:
    return large < NOISE_FLOOR_SECONDS or large <= MAX_RATIO * max(small, 1e-4)


def hidden_text_leaks() -> list[str]:
    """Unterminated comment or CDATA at the end of a document must stay hidden (R4)."""
    cases = {"comment": "<p>Hi</p><!-- SECRET", "cdata": "<p>Hi</p><![CDATA[ SECRET", "bogus": "<p>Hi</p><! SECRET"}
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
