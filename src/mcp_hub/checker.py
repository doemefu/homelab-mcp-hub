"""Background status check (spec 080 rev. 4.4 §7.4, D57): cheap per-capability probes that keep list_accounts
current. Started only from the production entry point and only when HUB_STATUS_CHECK_ENABLED is true."""

import logging
from collections.abc import Awaitable, Callable
from typing import Final

import anyio

from mcp_hub.health import missing_credentials
from mcp_hub.logging import log_event
from mcp_hub.providers import SUPPORTED_PROTOCOLS
from mcp_hub.providers.base import PROVIDER_TIMEOUT_SECONDS, ProviderError, run_blocking
from mcp_hub.registry import CAPABILITIES, Account, Capability
from mcp_hub.tools import HubContext

FIRST_CHECK_DELAY_SECONDS: Final = 30.0
_log = logging.getLogger("mcp_hub.checker")


def _cycle_outcome(total: int, ok: int) -> str:
    if total == 0:
        return "skipped"  # nothing to check (e.g. every account disabled), not "ok"
    return "ok" if ok == total else ("partial" if ok else "error")


class HealthChecker:
    def __init__(
        self,
        ctx: HubContext,
        *,
        interval: float,
        first_delay: float | None = None,
        timeout: float = PROVIDER_TIMEOUT_SECONDS,
        sleep: Callable[[float], Awaitable[None]] = anyio.sleep,
    ) -> None:
        self._ctx, self._interval, self._timeout, self._sleep = ctx, interval, timeout, sleep
        self._first_delay = FIRST_CHECK_DELAY_SECONDS if first_delay is None else first_delay

    def _targets(self) -> list[tuple[Account, Capability]]:
        """Enabled capabilities with an adapter and readable credentials (spec 080 §7.4)."""
        return [
            (account, capability)
            for account in self._ctx.registry.accounts
            for capability in CAPABILITIES
            if account.enabled
            and account.has(capability)
            and account.protocol(capability) in SUPPORTED_PROTOCOLS[capability]
            and not missing_credentials(account, capability, self._ctx.settings.secrets_dir)
        ]

    async def _check(self, account: Account, capability: Capability) -> bool:
        adapters, secrets = self._ctx.adapters, self._ctx.settings.secrets_dir

        def probe() -> None:
            # IMAP: login + NOOP; CalDAV: one principal PROPFIND.
            if capability == "mail":
                adapters.mailbox(account, secrets).check()
            else:
                adapters.calendar(account, secrets).check()

        try:
            await run_blocking(probe, slot=adapters.limiters.get(account.id), timeout=self._timeout)
        except Exception as exc:
            error = exc if isinstance(exc, ProviderError) else ProviderError("upstream_error", type(exc).__name__)
            self._ctx.status.record_failure(account.id, capability, error.code)
            log_event(
                _log,
                logging.WARNING,
                "status_check_failed",
                account=account.id,
                capability=capability,
                outcome=error.code,
                exception=error.cause,
            )
            return False
        self._ctx.status.record_success(account.id, capability)
        return True

    async def check_once(self) -> tuple[int, int]:
        """Returns (checks run, checks succeeded)."""
        results: list[bool] = []

        async def one(account: Account, capability: Capability) -> None:
            results.append(await self._check(account, capability))

        async with anyio.create_task_group() as tg:
            for account, capability in self._targets():
                tg.start_soon(one, account, capability)
        return len(results), sum(results)

    async def run(self) -> None:
        """Never ends on an exception; one count-only status_check_cycle line per cycle."""
        await self._sleep(self._first_delay)
        while True:
            try:
                total, ok = await self.check_once()
                log_event(
                    _log, logging.INFO, "status_check_cycle", result_count=total, outcome=_cycle_outcome(total, ok)
                )
            except Exception as exc:  # e.g. an unexpected registry state; the server keeps running
                log_event(_log, logging.WARNING, "status_check_cycle", outcome="error", exception=type(exc).__name__)
            await self._sleep(self._interval)
