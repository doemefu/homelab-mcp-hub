"""Per-call deadline with a soft stop (spec 080 §5.4): partial results after 16 s, hard failure after 20 s."""

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from mcp_hub.providers import base
from mcp_hub.providers.base import ProviderError

SOFT_STOP_SECONDS: Final = 16.0


class SoftStop(Exception):  # noqa: N818
    """The soft limit passed: stop issuing commands and return what has been gathered."""


@dataclass(frozen=True, slots=True)
class CallDeadline:
    started: float
    soft_seconds: float
    hard_seconds: float
    clock: Callable[[], float] = time.monotonic

    @classmethod
    def start(cls, clock: Callable[[], float] = time.monotonic) -> "CallDeadline":
        # Limits are read at call time (tests patch them).
        return cls(clock(), SOFT_STOP_SECONDS, base.PROVIDER_TIMEOUT_SECONDS, clock)

    @property
    def hard_at(self) -> float:
        return self.started + self.hard_seconds

    def remaining(self) -> float:
        return self.hard_at - self.clock()

    def soft_expired(self) -> bool:
        return self.clock() - self.started > self.soft_seconds

    def before_command(self) -> None:
        if self.remaining() < 0:
            raise ProviderError("upstream_timeout", "CallDeadline")
        if self.soft_expired():
            raise SoftStop
