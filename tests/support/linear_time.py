"""Size-scaling check for CPU-bound helpers on hostile input (WP13c Q-f: best of N, ratio bound, no absolute bound)."""

import gc
import math
import time
from collections.abc import Callable

RUNS = 7
MAX_RATIO = 2.5  # a 2x input may take at most 2.5x the time; quadratic growth gives about 4x
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
    """Best times on `build(size)` and `build(2 * size)`, both with the same repeat count."""
    small, large = build(size), build(2 * size)
    once = best_time(call, small, 1, runs=1)
    repeat = max(1, math.ceil(MIN_SAMPLE_SECONDS / max(once, 1e-6)))
    return best_time(call, small, repeat), best_time(call, large, repeat)


def scales_linearly(small: float, large: float) -> bool:
    return large <= MAX_RATIO * small
