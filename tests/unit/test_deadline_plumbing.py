"""R2a/R2b: the tool layer creates the CallDeadline right before run_blocking; adapters never start their own."""

import logging
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import httpx2
import pytest

from mcp_hub.checker import HealthChecker
from mcp_hub.config import load_settings
from mcp_hub.errors import ToolError
from mcp_hub.health import StatusStore
from mcp_hub.ids import MessageRef, encode_message_id
from mcp_hub.providers import Adapters, Mailbox, base
from mcp_hub.providers import deadline as deadline_module
from mcp_hub.providers.base import MailDetail, ProviderError, UnreadPage
from mcp_hub.providers.deadline import CallDeadline, SoftStop
from mcp_hub.providers.graph import Backoff, GraphMailbox
from mcp_hub.providers.imap import ImapMailbox
from mcp_hub.registry import load_registry
from mcp_hub.tools import HubContext
from mcp_hub.tools import mail as mail_tools
from mcp_hub.tools.mail import run_get_message, run_list_unread
from tests.support.dav_transport import RecordingTransport
from tests.support.graph_fixtures import FakeTokens
from tests.unit.test_graph_adapter import LIST
from tests.unit.test_mail_tools import detail

pytestmark = pytest.mark.anyio
SRC = Path(__file__).resolve().parents[2] / "src" / "mcp_hub"
SINCE = datetime(2026, 9, 29, tzinfo=UTC)
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


async def test_slow_slot_wait_leaves_the_soft_stop_inside_the_hard_limit(
    secrets_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The deadline starts before the connection-slot wait: a wait longer than the soft limit but shorter than the
    hard limit reaches the adapter with the soft stop due, the hard limit not yet over, and no command is sent."""
    monkeypatch.setattr(deadline_module, "SOFT_STOP_SECONDS", 0.2)
    monkeypatch.setattr(base, "PROVIDER_TIMEOUT_SECONDS", 2.0)  # the slot wait may last up to 1.0 s
    entry: dict[str, object] = {}

    class Spy(SpyMailbox):
        def get_message(self, ref: object, deadline: CallDeadline) -> MailDetail:
            entry["remaining"], entry["soft_expired"] = deadline.remaining(), deadline.soft_expired()
            try:
                deadline.before_command()
            except SoftStop as exc:
                entry["raised"] = exc
                raise
            return detail()

    ctx = context(secrets_dir, [])
    ctx = HubContext(ctx.settings, ctx.registry, ctx.status, Adapters(mailbox=lambda a, d: Spy([])))  # type: ignore[arg-type,return-value]
    slot = ctx.adapters.limiters.get("icloud")
    held = 0
    while slot.connections.acquire(blocking=False):
        held += 1
    timer = threading.Timer(0.5, lambda: [slot.connections.release() for _ in range(held)])
    timer.start()
    try:
        with pytest.raises(ToolError) as info:
            await run_get_message(ctx, message_id=ICLOUD_ID, max_chars=None)
    finally:
        timer.join()
    assert entry["soft_expired"] is True
    assert isinstance(entry["raised"], SoftStop)
    assert isinstance(entry["remaining"], float)
    assert entry["remaining"] > 0
    assert info.value.code == "upstream_timeout"


class RawSoftStopProbe:
    def check(self, deadline: CallDeadline | None = None) -> None:
        raise SoftStop


async def test_checker_maps_a_raw_soft_stop_to_upstream_timeout(
    secrets_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    ctx = context(secrets_dir, [])
    ctx = HubContext(
        ctx.settings,
        ctx.registry,
        ctx.status,
        Adapters(mailbox=lambda a, d: RawSoftStopProbe()),  # type: ignore[arg-type,return-value]
    )
    with caplog.at_level(logging.WARNING, logger="mcp_hub"):
        await HealthChecker(ctx, interval=60).check_once()
    failed = [
        r.fields for r in caplog.records if r.getMessage() == "status_check_failed" and r.fields["capability"] == "mail"
    ]  # type: ignore[attr-defined]
    assert {(f["outcome"], f["exception"]) for f in failed} == {("upstream_timeout", "SoftStop")}
    assert ctx.status.get("icloud", "mail").status == "unreachable"  # the status of an upstream_timeout


def _graph_with_own_clock(transport: RecordingTransport, mailbox_clock: list[float]) -> GraphMailbox:
    return GraphMailbox(
        "outlook",
        FakeTokens(),
        client_factory=lambda timeout: httpx2.Client(transport=transport.transport(), trust_env=False),
        clock=lambda: mailbox_clock[0],
        backoff=Backoff(),
        inbox_ids={},
    )


def test_graph_honours_a_deadline_on_another_clock_than_its_own() -> None:
    """The CallDeadline's clock and the mailbox's clock are unrelated; the remaining time is what counts."""
    tool_clock = [5000.0]
    deadline = CallDeadline.start(lambda: tool_clock[0])
    tool_clock[0] += 19.5  # 0.5 s of the hard limit remain
    mailbox_clock = [100.0]

    def body_advancing_the_mailbox_clock(request: httpx2.Request) -> httpx2.Response:
        def body() -> Iterator[bytes]:
            for _ in range(5):
                mailbox_clock[0] += 1.0
                yield b" "

        return httpx2.Response(200, content=body())

    t = RecordingTransport({LIST: body_advancing_the_mailbox_clock})
    with pytest.raises(ProviderError) as info:
        _graph_with_own_clock(t, mailbox_clock).list_unread(SINCE, 20, deadline)
    assert (info.value.code, info.value.cause) == ("upstream_timeout", "CallDeadline")


def test_graph_sends_nothing_when_the_other_clock_deadline_is_over() -> None:
    tool_clock = [5000.0]
    deadline = CallDeadline.start(lambda: tool_clock[0])
    tool_clock[0] += 21.0
    t = RecordingTransport({LIST: lambda request: httpx2.Response(200, content=b'{"value": []}')})
    with pytest.raises(ProviderError) as info:
        _graph_with_own_clock(t, [100.0]).list_unread(SINCE, 20, deadline)
    assert (info.value.code, info.value.cause) == ("upstream_timeout", "CallDeadline")
    assert t.seen == []


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

        def socket(self) -> "Client":  # the guard sets a socket timeout before every command
            return self

        def settimeout(self, value: float) -> None: ...

        def capabilities(self) -> tuple[bytes, ...]:
            return ()

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
