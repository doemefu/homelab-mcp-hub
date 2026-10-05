"""Characterisation tests (review 01 m6): the real GraphMailbox + GraphTokenSource + tool layer + status checker.
A failure is a real leak or a wiring defect: fix the code, never the test."""

import base64
import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx2
import pytest

from mcp_hub.checker import HealthChecker
from mcp_hub.config import load_settings
from mcp_hub.health import StatusStore, capability_status
from mcp_hub.ids import GraphMessageRef, encode_message_id
from mcp_hub.logging import configure_logging
from mcp_hub.providers import Adapters
from mcp_hub.providers.graph import Backoff, GraphMailbox
from mcp_hub.providers.graph_auth import AccessTokenCache, GraphTokenSource
from mcp_hub.providers.msidentity import GraphAccount
from mcp_hub.registry import Account, load_registry
from mcp_hub.tools import HubContext
from mcp_hub.tools.mail import run_get_message, run_list_unread
from tests.support.dav_transport import RecordingTransport
from tests.support.graph_fixtures import INBOX_ID, message
from tests.support.logcapture import Capture
from tests.support.logfields import allowed_fields
from tests.support.ms_transport import FAKE_CLIENT_ID, MsTransport, json_answer
from tests.support.token_fakes import KEY, FakeStore

pytestmark = pytest.mark.anyio
BASE = "https://graph.microsoft.com:443/v1.0"
NOW = datetime(2026, 9, 29, 7, 0, tzinfo=UTC)
TOKEN_PATH = "/consumers/oauth2/v2.0/token"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def js(
    body: object, status: int = 200, headers: dict[str, str] | None = None
) -> Callable[[httpx2.Request], httpx2.Response]:
    return lambda r: httpx2.Response(status, content=json.dumps(body).encode(), headers=headers or {})


class _NoProbe:
    """icloud stays offline in these tests."""

    def check(self) -> None: ...


def hub(secrets_dir: Path, store: FakeStore, ms: MsTransport, graph: RecordingTransport) -> HubContext:
    for ref in ("outlook-ms-client-id", "db-username", "db-password", "token-encryption-key"):
        (secrets_dir / ref).write_text("placeholder")
    settings = load_settings({"HUB_SECRETS_DIR": str(secrets_dir)})
    cache, backoff = AccessTokenCache(), Backoff()

    def opener(account: Account, _dir: Path) -> GraphMailbox:
        acct = GraphAccount(account.id, account.provider, "consumers", FAKE_CLIENT_ID, ("Mail.Read", "offline_access"))
        tokens = GraphTokenSource(
            acct,
            store,
            cache=cache,
            client_factory=lambda t: httpx2.Client(transport=ms.transport(), trust_env=False),
        )
        return GraphMailbox(
            account.id,
            tokens,
            backoff=backoff,
            inbox_ids={},
            client_factory=lambda t: httpx2.Client(transport=graph.transport(), trust_env=False),
        )

    def mailbox(account: Account, directory: Path) -> object:
        return opener(account, directory) if account.id == "outlook" else _NoProbe()

    return HubContext(
        settings,
        load_registry(secrets_dir / "accounts.json"),
        StatusStore(),
        Adapters(mailbox=mailbox, calendar=lambda a, d: _NoProbe()),  # type: ignore[arg-type]
    )


async def test_end_to_end_logs_are_clean(secrets_dir: Path) -> None:
    configure_logging("DEBUG")
    capture = Capture()
    logging.getLogger().addHandler(capture)
    try:
        received = (NOW - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        leaky = message(
            1,
            receivedDateTime=received,
            subject="SUBJECT-SENTINEL",
            bodyPreview="BODY-SENTINEL",
            **{"from": {"emailAddress": {"name": "N", "address": "leak@example.test"}}},
        )
        hostile = {"id": "AAMk/../SENTINEL-ID", "subject": "SUBJECT-SENTINEL"}  # degraded without an item hash
        graph = RecordingTransport(
            {
                ("GET", f"{BASE}/me/mailFolders/inbox/messages"): js({"value": [leaky, hostile]}),
                ("GET", f"{BASE}/me/mailFolders/inbox"): js({"id": INBOX_ID}),
                ("GET", f"{BASE}/me/messages/AAMkMSG0001="): js(
                    leaky
                    | {
                        "parentFolderId": INBOX_ID,
                        "hasAttachments": True,
                        "body": {"contentType": "text", "content": "BODY-SENTINEL"},
                    }
                ),
                ("GET", f"{BASE}/me/messages/AAMkMSG0001=/attachments"): js({"value": "ATTACHMENT-SENTINEL"}),
            }
        )
        ms = MsTransport(
            {
                TOKEN_PATH: [
                    json_answer(
                        200,
                        {
                            "access_token": "AT-SENTINEL",
                            "refresh_token": "RT-SENTINEL",
                            "expires_in": 3600,
                            "scope": "Mail.Read",
                        },
                    )
                ]
            }
        )
        ctx = hub(secrets_dir, FakeStore(), ms, graph)
        result, _, outcome = await run_list_unread(ctx, account="outlook", since=None, limit=20, now=NOW)
        assert outcome == "ok"
        assert [i.untrusted.subject for i in result.items] == ["SUBJECT-SENTINEL"]
        detail, _ = await run_get_message(
            ctx, message_id=encode_message_id(GraphMessageRef("outlook", "AAMkMSG0001=")), max_chars=None
        )
        assert detail.folder == "inbox"
        assert detail.attachments == []
    finally:
        logging.getLogger().removeHandler(capture)
    assert all(host == "graph.microsoft.com" and port == 443 for host, port, *_ in graph.seen)
    assert {(host, port) for host, port, *_ in ms.seen} == {("login.microsoftonline.com", 443)}
    events = [json.loads(line)["event"] for line in capture.lines]
    assert "token_refresh" in events
    assert events.count("item_degraded") == 2  # the id-less list entry and the malformed attachment list
    joined = "\n".join(capture.lines + [m for _, _, m in capture.raw])
    for secret in (
        "RT-SENTINEL",
        "AT-SENTINEL",
        "leak@example.test",
        "SUBJECT-SENTINEL",
        "BODY-SENTINEL",
        "SENTINEL-ID",
        "ATTACHMENT-SENTINEL",
        "graph.microsoft.com",
        "login.microsoftonline.com",
        "AAMkMSG",
        INBOX_ID,
        FAKE_CLIENT_ID,
        "Bearer",
        base64.b64encode(KEY).decode(),
    ):
        assert secret not in joined
    for line in capture.lines:
        event = json.loads(line)
        assert set(event) <= allowed_fields(event["event"])


async def test_duplicate_graph_ids_are_listed_once(secrets_dir: Path) -> None:
    received = (NOW - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    entries = [message(n, receivedDateTime=received) for n in (1, 2, 1, 1)]
    graph = RecordingTransport({("GET", f"{BASE}/me/mailFolders/inbox/messages"): js({"value": entries})})
    answer = {"access_token": "AT-SENTINEL", "refresh_token": "RT-SENTINEL", "expires_in": 3600, "scope": "Mail.Read"}
    ms = MsTransport({TOKEN_PATH: [json_answer(200, answer)]})
    result, _, outcome = await run_list_unread(
        hub(secrets_dir, FakeStore(), ms, graph), account="outlook", since=None, limit=20, now=NOW
    )
    assert outcome == "ok"
    ids = [i.id for i in result.items]
    assert len(ids) == 2  # result_count is len(result.items) and the budget is applied to the same list
    assert len(set(ids)) == 2


async def test_check_then_invalid_grant_shows_auth_expired_in_list_accounts(secrets_dir: Path) -> None:
    """Review 01 answer 8: check -> token source -> HTTP 400 invalid_grant -> list_accounts auth_expired."""
    ms = MsTransport({TOKEN_PATH: [json_answer(400, {"error": "invalid_grant", "error_codes": [70000]})]})
    graph = RecordingTransport({("GET", f"{BASE}/me/mailFolders/inbox"): js({"id": INBOX_ID})})
    store = FakeStore()
    ctx = hub(secrets_dir, store, ms, graph)
    await HealthChecker(ctx, interval=60, first_delay=0).check_once()
    outlook = ctx.registry.get("outlook")
    assert outlook is not None
    status = capability_status(outlook, "mail", ctx.settings.secrets_dir, ctx.status)
    assert status.status == "auth_expired"
    assert status.last_error_code == "auth_expired"
    assert store.invalid_grant
    assert graph.seen == []
    assert len(ms.seen) == 1
    await HealthChecker(ctx, interval=60, first_delay=0).check_once()
    assert len(ms.seen) == 1  # a recorded invalid grant short-circuits: Microsoft is not contacted again
