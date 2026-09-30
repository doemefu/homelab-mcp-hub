"""Token verification (spec 080 §4.3 rows 1-8) and the subject allowlist (kill switch, §4.6 L2)."""

import logging
import time
from collections.abc import Callable
from pathlib import Path

from mcp_hub.logging import log_event

_log = logging.getLogger("mcp_hub.auth")


class SubjectAllowlist:
    """One subject per line, exact case-sensitive match after trimming; missing or empty file = nobody."""

    def __init__(
        self, path: Path, *, reload_interval: float = 60.0, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._path = path
        self._reload_interval = reload_interval
        self._clock = clock
        self._subjects: frozenset[str] = frozenset()
        self._loaded_at: float | None = None

    def contains(self, subject: str) -> bool:
        if self._loaded_at is None or self._clock() - self._loaded_at >= self._reload_interval:
            self._subjects = self._load()
            self._loaded_at = self._clock()
        return bool(subject) and subject in self._subjects

    def _load(self) -> frozenset[str]:
        try:
            text = self._path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError) as exc:  # non-UTF-8 file = nobody, re-read after the interval
            log_event(_log, logging.WARNING, "allowlist_unavailable", exception=type(exc).__name__)
            return frozenset()
        subjects = frozenset(line.strip() for line in text.splitlines() if line.strip())
        if not subjects:
            log_event(_log, logging.WARNING, "allowlist_empty")
        return subjects
