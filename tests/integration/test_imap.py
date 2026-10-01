"""IMAP adapter against GreenMail (spec 080 §10.2). Every seeded subject carries the run token (greenmail.RUN)."""

import json
import logging
import socket
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path

import imapclient
import pytest

from mcp_hub.config import load_settings
from mcp_hub.health import StatusStore
from mcp_hub.logging import configure_logging
from mcp_hub.providers import Adapters
from mcp_hub.providers.base import MAX_TEXT_PART_BYTES, ProviderError
from mcp_hub.registry import load_registry
from mcp_hub.tools import HubContext
from mcp_hub.tools.mail import run_get_message, run_list_unread
from tests.support import greenmail
from tests.support.logcapture import Capture
from tests.support.logfields import allowed_fields

pytestmark = [pytest.mark.provider, pytest.mark.anyio]  # "integration" means "starts the hub"
S = greenmail.subject
seeded_since = greenmail.since  # now - 10 min; assertions filter on the run token


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(scope="module", autouse=True)
def services() -> None:
    greenmail.require()


@pytest.fixture(autouse=True)
def greenmail_literals(monkeypatch: pytest.MonkeyPatch) -> None:
    greenmail.tolerate_greenmail_partial_literals(monkeypatch)


def text_mail(subject: str, body: str) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg.set_content(body)
    return msg


def test_list_unread_returns_seeded_messages_newest_first() -> None:
    since = seeded_since()
    plain = text_mail(S("First plain"), "Plain body line")
    html = EmailMessage()
    html["Subject"] = S("Second html")
    html.set_content("fallback")
    html.add_alternative("<p>Visible</p><div style='display:none'>HIDDEN</div>", subtype="html")
    attached = text_mail(S("Third attached"), "See attachment")
    attached.add_attachment(b"%PDF-1.4 test", maintype="application", subtype="pdf", filename="report.pdf")
    for msg in (plain, html, attached):
        greenmail.send("hub-list", msg)
    items = greenmail.ours(greenmail.mailbox("hub-list").list_unread(since, 20).items)
    assert [m.subject for m in items] == [S("Third attached"), S("Second html"), S("First plain")]
    assert [m.has_attachments for m in items] == [True, False, False]
    assert items[2].snippet_text.strip() == "Plain body line"
    assert all(m.unread and m.from_address == "sender@example.test" for m in items)
    assert all(m.ref.folder == "INBOX" and m.ref.uid > 0 for m in items)


def test_since_filter_and_limit() -> None:
    now = datetime.now(UTC)
    greenmail.append("hub-list", text_mail(S("Old one"), "old"), now - timedelta(days=3))
    since = seeded_since()
    for n in range(3):
        greenmail.send("hub-list", text_mail(S(f"Recent {n}"), "x"))
    page = greenmail.mailbox("hub-list").list_unread(since, 2)
    assert len(page.items) == 2
    assert page.more is True
    recent = greenmail.ours(greenmail.mailbox("hub-list").list_unread(now - timedelta(days=1), 50).items)
    assert S("Old one") not in [m.subject for m in recent]
    assert S("Recent 0") in [m.subject for m in recent]


def test_reading_never_sets_seen() -> None:
    since = seeded_since()
    greenmail.send("hub-get", text_mail(S("Keep unread"), "body"))
    box = greenmail.mailbox("hub-get")
    [item] = [m for m in box.list_unread(since, 20).items if m.subject == S("Keep unread")]
    detail = box.get_message(item.ref)
    assert detail.unread is True
    assert detail.body_text.strip() == "body"
    assert b"\\Seen" not in greenmail.flags("hub-get")[item.ref.uid]
    assert [m.subject for m in box.list_unread(since, 20).items].count(S("Keep unread")) == 1


def test_get_message_html_and_attachment_metadata() -> None:
    since = seeded_since()
    msg = EmailMessage()
    msg["Subject"] = S("Html only")
    msg["Cc"] = "Copy <copy@example.test>"
    msg.set_content(
        "<p>Hello <a href='https://t.example.test/?u=1'>link</a></p><span hidden>HIDDEN</span>", subtype="html"
    )
    msg.add_attachment(b"x" * 5000, maintype="image", subtype="png", filename="pic.png")
    greenmail.send("hub-get", msg)
    box = greenmail.mailbox("hub-get")
    [item] = [m for m in box.list_unread(since, 20).items if m.subject == S("Html only")]
    detail = box.get_message(item.ref)
    assert detail.body_source == "text/html-converted"
    assert "Hello" in detail.body_text
    assert "HIDDEN" not in detail.body_text
    assert detail.cc_addresses == ["copy@example.test"]
    assert [(a.filename, a.content_type) for a in detail.attachments] == [("pic.png", "image/png")]
    assert detail.attachments[0].size_bytes > 5000  # encoded size from BODYSTRUCTURE


def test_oversized_text_part_is_fetched_partially(monkeypatch: pytest.MonkeyPatch) -> None:
    since = seeded_since()
    line = "0123456789" * 7 + "\n"
    greenmail.send("hub-big", text_mail(S("Huge"), line * (600 * 1024 // len(line))))
    requested: list[str] = []
    original = imapclient.IMAPClient.fetch

    def spy(self: imapclient.IMAPClient, messages: object, data: list[str], modifiers: object = None) -> object:
        requested.extend(data)
        return original(self, messages, data, modifiers)

    monkeypatch.setattr(imapclient.IMAPClient, "fetch", spy)
    box = greenmail.mailbox("hub-big")
    [item] = [m for m in box.list_unread(since, 20).items if m.subject == S("Huge")]
    detail = box.get_message(item.ref)
    assert detail.body_cut is True
    assert len(detail.body_text.encode()) <= MAX_TEXT_PART_BYTES
    assert f"BODY.PEEK[1]<0.{MAX_TEXT_PART_BYTES}>" in requested
    assert all(i.startswith(("FLAGS", "INTERNALDATE", "BODYSTRUCTURE", "BODY.PEEK[")) for i in requested)
    assert not [i for i in requested if i in ("BODY.PEEK[]", "BODY[]", "RFC822")]


async def test_oversized_message_through_the_tool(secrets_dir: Path) -> None:
    since = seeded_since()
    greenmail.send("hub-big", text_mail(S("Huge tool"), "y" * 400_000))
    ctx = HubContext(
        load_settings({"HUB_SECRETS_DIR": str(secrets_dir)}),
        load_registry(secrets_dir / "accounts.json"),
        StatusStore(),
        Adapters(mailbox=lambda a, d: greenmail.mailbox("hub-big")),
    )
    listed, _, _ = await run_list_unread(ctx, account="icloud", since=since.isoformat(), limit=50)
    [item] = [i for i in listed.items if i.untrusted.subject == S("Huge tool")]
    result, _ = await run_get_message(ctx, message_id=item.id, max_chars=20000)
    assert result.body_truncated is True
    assert len(result.untrusted.body) <= 20000


def test_wrong_password_is_auth_expired() -> None:
    with pytest.raises(ProviderError) as caught:
        greenmail.mailbox("hub-get", password="not-the-password").check()
    assert caught.value.code == "auth_expired"


def test_closed_port_is_unreachable() -> None:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    with pytest.raises(ProviderError) as caught:
        greenmail.mailbox("hub-get", port=port).check()
    assert caught.value.code == "unreachable"


@pytest.fixture
def captured() -> Iterator[Capture]:
    configure_logging("DEBUG")
    handler = Capture()
    logging.getLogger().addHandler(handler)
    yield handler
    logging.getLogger().removeHandler(handler)


async def test_imap_logs_contain_no_credentials_or_content(secrets_dir: Path, captured: Capture) -> None:
    since = seeded_since()
    greenmail.send(
        "hub-logs", text_mail(S("Sentinel-Subject-7q"), "sentinel-body-7q https://sentinel-host-7q.example.test/p?q=1")
    )
    ctx = HubContext(
        load_settings({"HUB_SECRETS_DIR": str(secrets_dir), "LOG_LEVEL": "DEBUG"}),
        load_registry(secrets_dir / "accounts.json"),
        StatusStore(),
        Adapters(mailbox=lambda a, d: greenmail.mailbox("hub-logs")),
    )
    listed, _, _ = await run_list_unread(ctx, account="icloud", since=since.isoformat(), limit=50)
    [item] = [i for i in listed.items if i.untrusted.subject == S("Sentinel-Subject-7q")]
    await run_get_message(ctx, message_id=item.id, max_chars=None)
    failing = HubContext(
        ctx.settings,
        ctx.registry,
        StatusStore(),
        Adapters(mailbox=lambda a, d: greenmail.mailbox("hub-logs", password="not-the-password")),
    )
    await run_list_unread(failing, account=None, since=since.isoformat(), limit=5)
    text = "\n".join(captured.lines) + "\n".join(m for _, _, m in captured.raw)
    forbidden = (
        "hub-logs",
        greenmail.passwords()["hub-logs"],
        "not-the-password",
        "Sentinel-Subject-7q",
        "sentinel-body-7q",
        "sentinel-host-7q",
        greenmail.RUN,
        "@",
        "3993",
        "127.0.0.1",
        "INBOX",
    )
    for value in forbidden:
        assert value not in text, value
    assert not [r for r in captured.raw if r[0].startswith("imapclient") and r[1] < logging.WARNING]
    events = [json.loads(line) for line in captured.lines]
    assert all(set(e) <= allowed_fields(e["event"]) for e in events)
    assert any(e["event"] == "provider_call_failed" and e["outcome"] == "auth_expired" for e in events)
