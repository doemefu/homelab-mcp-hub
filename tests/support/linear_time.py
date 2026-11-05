"""Size-scaling check for CPU-bound helpers on hostile input (WP13c Q-f: best of N, ratio bound, no absolute bound)."""

import gc
import math
import time
from collections.abc import Callable

RUNS = 7
ATTEMPTS = 3  # a sustained slowdown of one measurement (noisy runner) is retried; a real quadratic fails every attempt
MAX_RATIO = 8.0  # a 4x input may take at most 8x the time (linear about 4, quadratic 16)
MIN_SAMPLE_SECONDS = 0.005  # each sample repeats the call until it lasts this long, so timer jitter cannot decide


def best_time(call: Callable[[str], object], text: str, repeat: int, runs: int = RUNS) -> float:
    """Fastest of `runs` samples of `repeat` calls each, with the garbage collector paused (as timeit does)."""
    best = math.inf
    enabled = gc.isenabled()
    gc.disable()
    try:
        for _ in range(runs):
            started = time.perf_counter()
            for _ in range(repeat):
                call(text)
            best = min(best, time.perf_counter() - started)
    finally:
        if enabled:
            gc.enable()
    return best


def scaling(call: Callable[[str], object], build: Callable[[int], str], size: int) -> tuple[float, float]:
    """Best times on `build(size)` and `build(4 * size)`, both with the same repeat count. The measurement is repeated
    (up to ATTEMPTS times) until it is within the bound; the one with the lowest ratio is returned."""
    small, large = build(size), build(4 * size)
    once = best_time(call, small, 1, runs=1)
    repeat = max(1, math.ceil(MIN_SAMPLE_SECONDS / max(once, 1e-6)))
    best: tuple[float, float] | None = None
    for _ in range(ATTEMPTS):
        measured = (best_time(call, small, repeat), best_time(call, large, repeat))
        if best is None or measured[1] * best[0] < best[1] * measured[0]:
            best = measured
        if scales_linearly(*best):
            break
    assert best is not None
    return best


def scales_linearly(small: float, large: float) -> bool:
    return large <= MAX_RATIO * small
