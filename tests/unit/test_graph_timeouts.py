import threading
import time
from datetime import UTC, datetime

import httpx2
import pytest

from mcp_hub.providers import base
from mcp_hub.providers.base import AccountLimiters, ProviderError, run_blocking
from mcp_hub.providers.graph import Backoff, GraphMailbox
from tests.support.dav_transport import RecordingTransport
from tests.support.graph_fixtures import FakeTokens
from tests.unit.test_graph_adapter import LIST

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def test_hanging_graph_call_times_out_within_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(base, "PROVIDER_TIMEOUT_SECONDS", 0.5)
    release = threading.Event()

    def hang(request: httpx2.Request) -> httpx2.Response:
        release.wait(5)
        return httpx2.Response(200, content=b'{"value": []}')

    t = RecordingTransport({LIST: hang})
    mailbox = GraphMailbox(
        "outlook",
        FakeTokens(),
        client_factory=lambda timeout: httpx2.Client(transport=t.transport()),
        backoff=Backoff(),
        inbox_ids={},
    )
    started = time.monotonic()
    try:
        with pytest.raises(ProviderError) as info:
            await run_blocking(
                lambda: mailbox.list_unread(datetime(2026, 9, 29, tzinfo=UTC), 20),
                slot=AccountLimiters().get("outlook"),
            )
    finally:
        release.set()
    assert (info.value.code, info.value.cause) == ("upstream_timeout", "TimeoutError")
    assert time.monotonic() - started < 3.0
