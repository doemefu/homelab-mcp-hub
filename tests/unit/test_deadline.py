import pytest

from mcp_hub.providers import base, deadline
from mcp_hub.providers.base import ProviderError
from mcp_hub.providers.deadline import CallDeadline, SoftStop
from tests.support.clock import FakeClock


def test_no_stop_before_16_seconds() -> None:
    clock = FakeClock(100.0)
    call = CallDeadline.start(clock)
    clock.advance(15.999)
    call.before_command()
    assert not call.soft_expired()


def test_soft_stop_after_16_seconds() -> None:
    clock = FakeClock(100.0)
    call = CallDeadline.start(clock)
    clock.advance(16.001)
    assert call.soft_expired()
    with pytest.raises(SoftStop):
        call.before_command()


def test_hard_deadline_after_20_seconds() -> None:
    clock = FakeClock(100.0)
    call = CallDeadline.start(clock)
    clock.advance(20.001)
    assert call.remaining() < 0
    with pytest.raises(ProviderError) as info:
        call.before_command()
    assert info.value.code == "upstream_timeout"
    assert info.value.cause == "CallDeadline"


def test_soft_stop_is_not_a_provider_error() -> None:
    assert not issubclass(SoftStop, ProviderError)


def test_start_reads_patched_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(base, "PROVIDER_TIMEOUT_SECONDS", 2.0)
    monkeypatch.setattr(deadline, "SOFT_STOP_SECONDS", 1.6)
    clock = FakeClock(100.0)
    call = CallDeadline.start(clock)
    assert call.hard_at == 102.0
    clock.advance(1.7)
    with pytest.raises(SoftStop):
        call.before_command()


def test_elapsed() -> None:
    clock = FakeClock(100.0)
    call = CallDeadline.start(clock)
    assert call.elapsed() == 0.0
    clock.advance(12.5)
    assert call.elapsed() == 12.5
