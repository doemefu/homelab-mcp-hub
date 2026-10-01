import threading
from pathlib import Path

import anyio
import pytest

from mcp_hub.providers.base import (
    ACCOUNT_ERROR_MESSAGES,
    AccountLimiters,
    ProviderError,
    read_capped,
    read_credential,
    run_blocking,
)

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def test_read_credential_strips_line_endings_only(tmp_path: Path) -> None:
    (tmp_path / "icloud-app-password").write_text("abcd-efgh-ijkl-mnop\n")
    assert read_credential(tmp_path, "icloud-app-password") == "abcd-efgh-ijkl-mnop"


@pytest.mark.parametrize("content", [None, "", "\n"])
def test_missing_or_empty_credential_is_a_provider_error(tmp_path: Path, content: str | None) -> None:
    if content is not None:
        (tmp_path / "k").write_text(content)
    with pytest.raises(ProviderError) as caught:
        read_credential(tmp_path, "k")
    assert caught.value.code == "upstream_error"


def test_read_capped_accepts_exactly_the_limit_and_rejects_more() -> None:
    assert read_capped([b"ab", b"cd"], limit=4) == b"abcd"
    with pytest.raises(ProviderError) as caught:
        read_capped([b"ab", b"cd", b"e"], limit=4)
    assert caught.value.code == "too_large"


async def test_run_blocking_deadline_gives_upstream_timeout() -> None:
    release = threading.Event()
    with pytest.raises(ProviderError) as caught:
        await run_blocking(lambda: release.wait(5), slot=AccountLimiters().get("icloud"), timeout=0.2)
    release.set()
    assert caught.value.code == "upstream_timeout"


async def test_abandoned_call_keeps_its_connection_slot() -> None:
    # Two calls time out while their threads still block; a third call for the same account must not run.
    limiters, release, started = AccountLimiters(), threading.Event(), []

    def block() -> None:
        started.append(1)
        release.wait(5)

    for _ in range(2):
        with pytest.raises(ProviderError):
            await run_blocking(block, slot=limiters.get("icloud"), timeout=0.2)
    with pytest.raises(ProviderError) as caught:
        await run_blocking(lambda: started.append(3), slot=limiters.get("icloud"), timeout=0.3)
    assert caught.value.code == "upstream_timeout"
    assert started == [1, 1]  # the third call never ran
    await run_blocking(lambda: None, slot=limiters.get("gmail"), timeout=0.3)  # other accounts are unaffected
    release.set()


async def test_thread_budget_bounds_live_threads() -> None:
    limiters, release = AccountLimiters(total_threads=2), threading.Event()
    for account in ("a1", "a2"):
        with pytest.raises(ProviderError):
            await run_blocking(lambda: release.wait(5), slot=limiters.get(account), timeout=0.1)
    with pytest.raises(ProviderError) as caught:
        await run_blocking(lambda: None, slot=limiters.get("a3"), timeout=0.1)
    assert (caught.value.code, caught.value.cause) == ("upstream_timeout", "ThreadLimit")
    release.set()


async def test_cancel_before_the_worker_starts_returns_the_thread_token() -> None:
    # Delta D1: 16 cancellations before the hand-over must not use up the thread budget.
    limiters = AccountLimiters(total_threads=16)
    for n in range(16):
        with anyio.CancelScope() as scope:
            scope.cancel()  # cancelled before run_sync hands the function to a worker
            await run_blocking(lambda: None, slot=limiters.get(f"a{n}"), timeout=1.0)
    results: list[int] = []

    async def one(n: int) -> None:
        await run_blocking(lambda: results.append(n), slot=limiters.get(f"b{n}"), timeout=2.0)

    async with anyio.create_task_group() as tg:
        for n in range(16):
            tg.start_soon(one, n)
    assert len(results) == 16


async def test_limiter_allows_two_concurrent_calls_per_account() -> None:
    limiters = AccountLimiters()
    active, peak = 0, 0
    lock = threading.Lock()

    def work() -> None:
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        threading.Event().wait(0.1)
        with lock:
            active -= 1

    async def one() -> None:
        await run_blocking(work, slot=limiters.get("icloud"))

    async with anyio.create_task_group() as tg:
        for _ in range(5):
            tg.start_soon(one)
    assert peak == 2
    assert limiters.get("icloud").connections is limiters.get("icloud").connections
    assert limiters.get("gmail").connections is not limiters.get("icloud").connections


def test_every_account_error_message_is_fixed_text() -> None:
    expected = {"auth_expired", "unreachable", "upstream_timeout", "upstream_error", "too_large", "not_found"}
    assert set(ACCOUNT_ERROR_MESSAGES) >= expected
