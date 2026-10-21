"""R2a/R2b: the tool layer creates the CallDeadline right before run_blocking; adapters never start their own."""

import threading
import time
from datetime import datetime
from pathlib import Path

import pytest

from mcp_hub.checker import HealthChecker
from mcp_hub.config import load_settings
from mcp_hub.health import StatusStore
from mcp_hub.ids import MessageRef, encode_message_id
from mcp_hub.providers import Adapters, Mailbox, base
from mcp_hub.providers.base import MailDetail, ProviderError, UnreadPage
from mcp_hub.providers.deadline import CallDeadline, SoftStop
from mcp_hub.providers.graph import Backoff, GraphMailbox
from mcp_hub.providers.imap import ImapMailbox
from mcp_hub.registry import load_registry
from mcp_hub.tools import HubContext
from mcp_hub.tools import mail as mail_tools
from mcp_hub.tools.mail import run_get_message, run_list_unread
from tests.support.graph_fixtures import FakeTokens
from tests.unit.test_mail_tools import detail

pytestmark = pytest.mark.anyio
SRC = Path(__file__).resolve().parents[2] / "src" / "mcp_hub"
ICLOUD_ID = encode_message_id(MessageRef("icloud", "INBOX", 7, 3))


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class SpyMailbox:
    def __init__(self, seen: list[CallDeadline]) -> None:
        self.seen = seen

    def check(self, deadline: CallDeadline) -> None:
        self.seen.append(deadline)

    def list_unread(self, since: datetime, limit: int, deadline: CallDeadline) -> UnreadPage:
        self.seen.append(deadline)
        return UnreadPage(items=[], more=False)

    def get_message(self, ref: object, deadline: CallDeadline) -> MailDetail:
        self.seen.append(deadline)
        return detail()


def context(secrets_dir: Path, seen: list[CallDeadline]) -> HubContext:
    for ref in ("gmail-username", "gmail-app-password"):
        (secrets_dir / ref).write_text("placeholder")
    settings = load_settings({"HUB_SECRETS_DIR": str(secrets_dir)})
    adapters = Adapters(mailbox=lambda account, _dir: SpyMailbox(seen))  # type: ignore[arg-type,return-value]
    return HubContext(settings, load_registry(secrets_dir / "accounts.json"), StatusStore(), adapters)


def _timed_run_blocking(entered: list[float]) -> object:
    real = base.run_blocking

    async def run_blocking(func: object, **kwargs: object) -> object:
        entered.append(time.monotonic())
        return await real(func, **kwargs)  # type: ignore[arg-type]

    return run_blocking


async def test_tool_passes_one_deadline_started_before_run_blocking(
    secrets_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[CallDeadline] = []
    entered: list[float] = []
    monkeypatch.setattr(mail_tools, "run_blocking", _timed_run_blocking(entered))
    ctx = context(secrets_dir, seen)

    await run_list_unread(ctx, account="icloud", since=None, limit=None)
    await run_get_message(ctx, message_id=ICLOUD_ID, max_chars=None)

    assert len(seen) == len(entered) == 2
    for deadline, at in zip(seen, entered, strict=True):
        assert deadline.started <= at
        assert at - deadline.started < 1.0  # created immediately before run_blocking, not earlier
    assert not [p for p in (SRC / "providers").rglob("*.py") if "CallDeadline.start(" in p.read_text()]


async def test_slow_slot_wait_is_charged_to_the_tool_deadline(secrets_dir: Path) -> None:
    """The deadline starts before the connection-slot wait, so the soft and hard limits include that wait."""
    seen: list[CallDeadline] = []
    ctx = context(secrets_dir, seen)
    slot = ctx.adapters.limiters.get("icloud")
    held = 0
    while slot.connections.acquire(blocking=False):
        held += 1
    timer = threading.Timer(0.3, lambda: [slot.connections.release() for _ in range(held)])
    timer.start()
    try:
        await run_list_unread(ctx, account="icloud", since=None, limit=None)
    finally:
        timer.join()
    [deadline] = seen
    assert deadline.hard_seconds - deadline.remaining() >= 0.25
    assert deadline.soft_seconds < deadline.hard_seconds


async def test_checker_passes_deadline(secrets_dir: Path) -> None:
    seen: list[CallDeadline] = []
    ctx = context(secrets_dir, seen)
    before = time.monotonic()
    await HealthChecker(ctx, interval=60).check_once()
    assert ctx.status.get("icloud", "mail").status == "ok"
    assert seen  # one deadline per checked mail account, each started inside this call
    for deadline in seen:
        assert before <= deadline.started <= time.monotonic()
    assert len({id(d) for d in seen}) == len(seen)


def _imap_noop_soft_stop() -> ImapMailbox:
    class Client:
        normalise_times = True

        def login(self, username: str, password: str) -> None: ...

        def noop(self) -> None:
            raise SoftStop

        def logout(self) -> None: ...

    return ImapMailbox(
        "icloud",
        host="imap.example.test",
        port=993,
        folder="INBOX",
        username="u",
        password="p",
        client_factory=lambda host, port, context, timeout: Client(),  # type: ignore[arg-type,return-value]
    )


class SoftStopTokens(FakeTokens):
    def access_token(self, deadline: float) -> str:
        raise SoftStop


def _graph_soft_stop() -> GraphMailbox:
    return GraphMailbox("outlook", SoftStopTokens(), backoff=Backoff(), inbox_ids={})


@pytest.mark.parametrize("make", [_imap_noop_soft_stop, _graph_soft_stop], ids=["imap", "graph"])
def test_soft_stop_in_check_is_upstream_timeout(make: object) -> None:
    box: Mailbox = make()  # type: ignore[operator]
    with pytest.raises(ProviderError) as info:
        box.check(CallDeadline.start())
    assert (info.value.code, info.value.cause) == ("upstream_timeout", "SoftStop")
