"""Per-account, per-capability status kept in memory only (spec 080 §7.4, §5.2 HealthStatus)."""

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from mcp_hub.errors import ErrorCode
from mcp_hub.registry import Account, Capability

HealthStatus = Literal["ok", "auth_expired", "unreachable", "error", "unknown", "disabled"]
_FAILURE_STATUS: dict[ErrorCode, HealthStatus] = {
    "auth_expired": "auth_expired",
    "unreachable": "unreachable",
    "upstream_timeout": "unreachable",
}


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class CapabilityStatus:
    status: HealthStatus
    last_success_at: datetime | None = None
    last_error_at: datetime | None = None
    last_error_code: ErrorCode | None = None


class StatusStore:
    def __init__(self, clock: Callable[[], datetime] = utc_now) -> None:
        self._clock = clock
        self._entries: dict[tuple[str, Capability], CapabilityStatus] = {}

    def get(self, account_id: str, capability: Capability) -> CapabilityStatus:
        return self._entries.get((account_id, capability), CapabilityStatus(status="unknown"))

    def record_success(self, account_id: str, capability: Capability) -> None:
        current = self.get(account_id, capability)
        self._entries[(account_id, capability)] = replace(current, status="ok", last_success_at=self._clock())

    def record_failure(self, account_id: str, capability: Capability, code: ErrorCode) -> None:
        current = self.get(account_id, capability)
        self._entries[(account_id, capability)] = replace(
            current, status=_FAILURE_STATUS.get(code, "error"), last_error_at=self._clock(), last_error_code=code
        )


def _readable(path: Path) -> bool:
    # Proves readability by opening the file (spec 080 §9.6 case c); never reads or logs its content.
    try:
        with path.open("rb"):
            return True
    except OSError:  # missing, unreadable (permissions) or a directory
        return False


def missing_credentials(account: Account, capability: Capability, secrets_dir: Path) -> list[str]:
    """Key names of credential files that are missing or cannot be read; the caller logs names only."""
    return [ref for ref in account.credential_refs(capability) if not _readable(secrets_dir / ref)]


def capability_status(
    account: Account, capability: Capability, secrets_dir: Path, store: StatusStore
) -> CapabilityStatus:
    if not account.enabled or missing_credentials(account, capability, secrets_dir):
        return CapabilityStatus(status="disabled")
    return store.get(account.id, capability)
