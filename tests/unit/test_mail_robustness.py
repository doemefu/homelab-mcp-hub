"""Hostile mail through the real adapter and the tool layer (review report 18, F1)."""

import json
import random
from pathlib import Path

import pytest

from mcp_hub.config import load_settings
from mcp_hub.health import StatusStore
from mcp_hub.ids import MessageRef, encode_message_id
from mcp_hub.providers import Adapters
from mcp_hub.providers.base import UNDECODABLE_NOTE
from mcp_hub.providers.imap import ImapMailbox
from mcp_hub.registry import Account, load_registry
from mcp_hub.tools import HubContext
from mcp_hub.tools.common import structured_result
from mcp_hub.tools.mail import run_get_message, run_list_unread
from tests.unit.test_imap_adapter import HEADER_KEY, T0, FakeClient, message

pytestmark = pytest.mark.anyio
ODD_WORDS = [
    b"=?utf-16?b?/w==?=",
    b"=?utf-8?b?7aCA?=",  # an encoded UTF-16 surrogate
    b"=?utf-7?q?+2D3eAA-?=",
    b"=?rot13?q?Fraqre?=",
    b"=?x-unknown?q?abc?=",
    b"=?utf-8?q?=FF=FE?=",
]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def raw_headers(sender: bytes, subject: bytes, to: bytes = b"owner@example.test") -> bytes:
    return b"From: " + sender + b"\r\nTo: " + to + b"\r\nCc: " + to + b"\r\nSubject: " + subject + b"\r\n\r\n"


def hostile_message(header: bytes, uid_minutes: int = 3) -> dict[bytes, object]:
    row = message(uid_minutes, "unused")
    row[HEADER_KEY] = header
    return row


def context(secrets_dir: Path, clients: dict[str, FakeClient]) -> HubContext:
    for ref in ("gmail-username", "gmail-app-password"):
        (secrets_dir / ref).write_text("placeholder")

    def factory(account: Account, _dir: Path) -> ImapMailbox:
        client = clients[account.id]
        return ImapMailbox(
            account.id, host="imap.example.test", port=993, folder="INBOX", username="u", password="p",
            client_factory=lambda host, port, ctx, timeout: client,
        )  # fmt: skip

    settings = load_settings({"HUB_SECRETS_DIR": str(secrets_dir)})
    return HubContext(settings, load_registry(secrets_dir / "accounts.json"), StatusStore(), Adapters(mailbox=factory))


def assert_strict_json(model: object) -> None:
    result = structured_result(model)  # type: ignore[arg-type]
    text = result.content[0].text  # type: ignore[union-attr]
    text.encode("utf-8")  # strict: raises on any lone surrogate
    assert json.loads(text) == result.structured_content


async def test_raw_latin1_from_header_does_not_fail_the_listing(secrets_dir: Path) -> None:
    hostile = hostile_message(raw_headers(b"M\xfcller <mueller@example.test>", b"Gr\xfcezi"))
    clients = {
        "icloud": FakeClient({1: hostile}),
        "gmail": FakeClient({5: message(10, "fine")}),
    }
    ctx = context(secrets_dir, clients)
    result, _, outcome = await run_list_unread(ctx, account=None, since=None, limit=None, now=T0)
    assert outcome == "ok"
    assert [i.account for i in result.items] == ["icloud", "gmail"]
    assert result.items[0].untrusted.from_name == "M�ller"
    assert_strict_json(result)
    detail, _ = await run_get_message(
        ctx, message_id=encode_message_id(MessageRef("icloud", "INBOX", 7, 1)), max_chars=None
    )
    assert_strict_json(detail)


async def test_hostile_headers_always_give_strict_json(secrets_dir: Path) -> None:
    rng = random.Random(20261001)  # deterministic property-style sweep  # noqa: S311 - test data only
    for case in range(150):
        noise = bytes(rng.randrange(0x80, 0x100) for _ in range(rng.randrange(1, 6)))
        word = rng.choice(ODD_WORDS)
        sender = rng.choice([noise, word, noise + b" " + word]) + b" <s" + noise[:1] + b"@example.test>"
        subject = rng.choice([noise, word, b"x" + noise + word])
        header = raw_headers(sender, subject, to=rng.choice([word, noise]) + b" <t@example.test>")
        clients = {"icloud": FakeClient({1: hostile_message(header)}), "gmail": FakeClient({})}
        ctx = context(secrets_dir, clients)
        listed, _, outcome = await run_list_unread(ctx, account="icloud", since=None, limit=None, now=T0)
        assert outcome == "ok", case
        assert len(listed.items) == 1, case
        assert_strict_json(listed)
        detail, _ = await run_get_message(ctx, message_id=listed.items[0].id, max_chars=None)
        assert_strict_json(detail)


async def test_one_item_that_fails_in_the_tool_layer_degrades_only_that_item(
    secrets_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import mcp_hub.tools.mail as mail

    original = mail.clean

    def exploding(value: str | None, limit: int, *, multiline: bool = False) -> str:
        if value == "BOOM":
            raise ValueError("BOOM")
        return original(value, limit, multiline=multiline)

    monkeypatch.setattr(mail, "clean", exploding)
    clients = {"icloud": FakeClient({1: message(3, "BOOM"), 2: message(5, "fine")}), "gmail": FakeClient({})}
    result, _, outcome = await run_list_unread(
        context(secrets_dir, clients), account=None, since=None, limit=None, now=T0
    )
    assert outcome == "ok"
    assert [i.untrusted.subject for i in result.items] == ["", "fine"]
    assert result.items[0].untrusted.snippet == UNDECODABLE_NOTE
    assert_strict_json(result)
