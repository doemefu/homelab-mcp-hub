import pytest

from mcp_hub.providers.base import ProviderError, read_capped


def test_cap_stops_after_first_chunk_beyond_limit() -> None:
    served: list[int] = []

    def chunks():  # type: ignore[no-untyped-def]
        for n in range(10):
            served.append(n)
            yield b"x" * 16_384

    with pytest.raises(ProviderError) as info:
        read_capped(chunks(), 65_536)
    assert (info.value.code, info.value.cause) == ("too_large", "ResponseTooLarge")
    assert len(served) == 5


def test_deadline_checked_between_chunks() -> None:
    now = [0.0]

    def chunks():  # type: ignore[no-untyped-def]
        for _ in range(5):
            now[0] += 3.0
            yield b"x"

    with pytest.raises(ProviderError) as info:
        read_capped(chunks(), 1_000, deadline=7.0, clock=lambda: now[0])
    assert (info.value.code, info.value.cause) == ("upstream_timeout", "CallDeadline")
