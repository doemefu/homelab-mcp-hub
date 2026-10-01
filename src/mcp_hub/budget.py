"""Output budget (spec 080 §5.4): fewer items and truncated: true instead of an oversized result.

Measured on the compact JSON text the hub returns as content[0].text; the identical structured copy is not counted
(spec 080 rev. 4.4, D59).
"""

from collections.abc import Callable
from typing import Final

from pydantic import BaseModel

from mcp_hub.sanitize import truncate

HARD_MAX_CHARS: Final = 100_000
_MIN_FIELD_CHARS: Final = 50


def serialized_length(model: BaseModel) -> int:
    return len(model.model_dump_json())


def shrink_to_fit[T: BaseModel](model: T, fits: Callable[[T], bool]) -> T | None:
    """Halve the longest string field of model.untrusted until the model fits; None if that is impossible."""
    current = model
    while not fits(current):
        untrusted = getattr(current, "untrusted", None)
        if not isinstance(untrusted, BaseModel):
            return None
        fields = {k: v for k, v in untrusted.__dict__.items() if isinstance(v, str)}
        if not fields:
            return None
        name, value = max(fields.items(), key=lambda kv: len(kv[1]))
        if len(value) <= _MIN_FIELD_CHARS:
            return None
        shorter = untrusted.model_copy(update={name: truncate(value, max(len(value) // 2, _MIN_FIELD_CHARS))})
        current = current.model_copy(update={"untrusted": shorter})
    return current


def shrink_or_drop[T: BaseModel](model: T, fits: Callable[[T], bool], drop: Callable[[T], T | None]) -> T | None:
    """shrink_to_fit; when shortening the untrusted text is not enough, `drop` removes one list entry (None when
    nothing is left to drop) and the text is shortened again from its full length, so text is kept before list
    entries. Terminates because every round removes an entry; None if even the emptied model does not fit."""
    candidate: T | None = model
    while candidate is not None:
        fitted = shrink_to_fit(candidate, fits)
        if fitted is not None:
            return fitted
        candidate = drop(candidate)
    return None


def fit_items[T: BaseModel, R: BaseModel](items: list[T], build: Callable[[list[T], bool], R], budget: int) -> R:
    """Largest prefix of `items` whose result fits; `build(items, cut)` must OR `cut` into its truncated flag."""
    limit = min(budget, HARD_MAX_CHARS)
    full = build(items, False)
    if serialized_length(full) <= limit:
        return full
    low, high = 0, len(items) - 1  # the full list does not fit
    while low < high:  # largest prefix that fits; lengths grow monotonically with the prefix
        middle = (low + high + 1) // 2
        if serialized_length(build(items[:middle], True)) <= limit:
            low = middle
        else:
            high = middle - 1
    if low == 0 and items:
        first = shrink_to_fit(items[0], lambda candidate: serialized_length(build([candidate], True)) <= limit)
        if first is not None:
            return build([first], True)
    return build(items[:low], True)
