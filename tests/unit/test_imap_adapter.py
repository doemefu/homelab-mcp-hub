import json
import logging
import socket
import ssl
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage

import pytest
from imapclient.exceptions import IMAPClientAbortError, IMAPClientError, LoginError, ProtocolError

from mcp_hub.ids import MessageRef, encode_message_id
from mcp_hub.providers.base import SNIPPET_FETCH_BYTES, UNDECODABLE_NOTE, ListQuery, ProviderError, item_hash
from mcp_hub.providers.deadline import CallDeadline
from mcp_hub.providers.folders import FolderMapCache
from mcp_hub.providers.headers import ParsedHeaders
from mcp_hub.providers.imap import ImapMailbox
from mcp_hub.providers.mime import Part
from tests.support.imap_fake import COMMAND_ALLOWLIST, FakeFolder, FakeImapClient, FakeImapServer, FakeMessage
from tests.support.logcapture import Capture

T0 = datetime(2026, 9, 29, 7, 0, tzinfo=UTC)
PLAIN = (b"text", b"plain", (b"charset", b"utf-8"), None, None, b"7bit", 64, 2, None, None, None, None)


def headers(subject: str, sender: str = "Sender <sender@example.test>") -> bytes:
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = sender, "owner@example.test", subject
    return bytes(msg).split(b"\n\n")[0] + b"\r\n\r\n"


def message(
    uid: int, minutes_ago: int, subject: str, *, seen: bool = False, text: bytes = b"Hello there"
) -> FakeMessage:
    return FakeMessage(
        uid,
        T0 - timedelta(minutes=minutes_ago),
        flags=(b"\\Seen",) if seen else (),
        headers=headers(subject),
        text=text,
        bodystructure=PLAIN,
    )


def server(*messages: FakeMessage, validity: int = 7) -> FakeImapServer:
    return FakeImapServer([FakeFolder("INBOX", uidvalidity=validity, messages={m.uid: m for m in messages})])


def mailbox(srv: FakeImapServer, client: type[FakeImapClient] = FakeImapClient) -> ImapMailbox:
    return ImapMailbox(
        "icloud",
        host="imap.example.test",
        port=993,
        folder="INBOX",
        username="u",
        password="p",
        client_factory=lambda host, port, context, timeout: client(srv),
        folder_maps=FolderMapCache(),
    )


def test_list_unread_filters_on_exact_internaldate_and_sorts_newest_first() -> None:
    srv = server(
        message(1, 60 * 30, "old"), message(2, 30, "second"), message(3, 5, "first"), message(4, 1, "read", seen=True)
    )
    clients: list[FakeImapClient] = []

    def recording(s: FakeImapServer) -> FakeImapClient:
        clients.append(FakeImapClient(s))
        return clients[-1]

    page = mailbox(srv, recording).list_messages(
        ListQuery("inbox", T0 - timedelta(hours=24), True, None, 20), CallDeadline.start()
    )  # type: ignore[arg-type]
    assert [m.subject for m in page.items] == ["first", "second"]
    assert page.more is False
    assert page.items[0].ref == MessageRef("icloud", "INBOX", 7, 3)
    assert (page.items[0].from_address, page.items[0].from_name) == ("sender@example.test", "Sender")
    assert page.items[0].snippet_text == "Hello there"
    [client] = clients
    assert (client.normalise_times, client.folder_encode, client.use_uid) == (False, False, True)


def test_one_malformed_message_does_not_fail_the_listing() -> None:
    odd = message(2, 3, "x")
    odd.headers = b"From: =?rot13?q?Fraqre?= <a@xn--.example.test>\r\nSubject: =?idna?q?x?=\r\n\r\n"
    odd.bodystructure = (
        b"text", b"plain", (b"charset", b"base64"), None, None, b"7bit", 10, 1, None, None, None, None
    )  # fmt: skip
    broken = message(3, 2, "y")
    broken.bodystructure = ("not", "a", "structure")
    empty = message(4, 4, "z")
    empty.bodystructure = ()
    missing = message(5, 6, "w")
    missing.bodystructure = None
    page = mailbox(server(message(1, 5, "good"), odd, broken, empty, missing)).list_messages(
        ListQuery("inbox", T0 - timedelta(hours=1), True, None, 20), CallDeadline.start()
    )
    assert len(page.items) == 5
    assert "good" in [m.subject for m in page.items]
    assert all(isinstance(m.snippet_text, str) for m in page.items)


def test_get_message_of_a_malformed_message_still_answers() -> None:
    odd = message(4, 3, "x", text=b"\xff\xfe")
    odd.bodystructure = (
        b"text", b"plain", (b"charset", b"rot13"), None, None, b"8bit", 2, 1, None, None, None, None
    )  # fmt: skip
    detail = mailbox(server(odd)).get_message(MessageRef("icloud", "INBOX", 7, 4), CallDeadline.start())
    assert isinstance(detail.body_text, str)
    assert detail.body_source == "text/plain"


def test_list_unread_limit_sets_more() -> None:
    srv = server(*(message(i, i, f"m{i}") for i in range(1, 6)))
    page = mailbox(srv).list_messages(ListQuery("inbox", T0 - timedelta(hours=1), True, None, 2), CallDeadline.start())
    assert [m.subject for m in page.items] == ["m1", "m2"]
    assert page.more is True


def test_adapter_only_uses_read_only_commands() -> None:
    srv = server(message(1, 5, "a"))
    box = mailbox(srv)
    box.list_messages(ListQuery("inbox", T0 - timedelta(hours=1), True, None, 20), CallDeadline.start())
    box.get_message(MessageRef("icloud", "INBOX", 7, 1), CallDeadline.start())
    box.check(CallDeadline.start())
    names = [name for name, _ in srv.commands]
    assert set(names) <= COMMAND_ALLOWLIST
    assert {argument for name, argument in srv.commands if name == "EXAMINE"} == {"INBOX"}
    for name, argument in srv.commands:
        if name == "UID FETCH":
            for item in argument[1]:  # type: ignore[index]
                assert item in {"FLAGS", "INTERNALDATE", "BODYSTRUCTURE"} or item.startswith("BODY.PEEK["), item
                assert "RFC822" not in item
                assert item != "BODY.PEEK[]"
    assert names.count("LOGOUT") == 3


def test_get_message_fetches_the_text_part_partially() -> None:
    big = message(9, 5, "big", text=b"x" * 300_000)
    big.bodystructure = (b"text", b"plain", (b"charset", b"utf-8"), None, None, b"7bit", 300_000, 1, None, None, None)
    srv = server(big)
    detail = mailbox(srv).get_message(MessageRef("icloud", "INBOX", 7, 9), CallDeadline.start())
    part_fetch = [
        list(argument[1])  # type: ignore[index]
        for name, argument in srv.commands
        if name == "UID FETCH" and any("BODY.PEEK[1]" in i for i in argument[1])  # type: ignore[index]
    ]
    assert part_fetch == [["BODY.PEEK[1]<0.262144>"]]
    assert len(detail.body_text) == 262_144
    assert detail.body_cut is True
    assert detail.body_source == "text/plain"
    assert detail.unread is True


@pytest.mark.parametrize(("validity", "uid"), [(8, 9), (7, 99)])
def test_changed_uidvalidity_or_missing_uid_is_not_found(validity: int, uid: int) -> None:
    with pytest.raises(ProviderError) as caught:
        mailbox(server(message(9, 5, "x"), validity=validity)).get_message(
            MessageRef("icloud", "INBOX", 7, uid), CallDeadline.start()
        )
    assert caught.value.code == "not_found"


def test_get_message_outside_the_registry_inbox_is_not_found_without_examine() -> None:
    srv = server(message(9, 5, "x"))
    with pytest.raises(ProviderError) as caught:
        mailbox(srv).get_message(MessageRef("icloud", "Archive", 7, 9), CallDeadline.start())
    assert caught.value.code == "not_found"
    assert "EXAMINE" not in [name for name, _ in srv.commands]


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (LoginError("[AUTHENTICATIONFAILED] secret-detail"), "auth_expired"),
        (LoginError("LOGIN failed"), "auth_expired"),
        (LoginError("[UNAVAILABLE] try later"), "unreachable"),
        (LoginError("[LIMIT] too many"), "upstream_error"),
        (TimeoutError("timed out"), "upstream_timeout"),
        (ssl.SSLError("bad cert"), "unreachable"),
        (socket.gaierror("no such host"), "unreachable"),
        (ConnectionRefusedError("refused"), "unreachable"),
        (IMAPClientError("NO something"), "upstream_error"),
        (RuntimeError("anything"), "upstream_error"),
    ],
)
def test_failures_map_to_error_codes_without_provider_text(error: Exception, code: str) -> None:
    srv = server()
    srv.fail["LOGIN"] = error
    with pytest.raises(ProviderError) as caught:
        mailbox(srv).check(CallDeadline.start())
    assert caught.value.code == code
    assert caught.value.cause == type(error).__name__
    assert "secret-detail" not in str(caught.value)


def test_connect_failure_is_unreachable() -> None:
    def refuse(host: str, port: int, context: ssl.SSLContext, timeout: float) -> FakeImapClient:
        raise ConnectionRefusedError("refused")

    box = ImapMailbox("icloud", host="h", port=1, folder="INBOX", username="u", password="p", client_factory=refuse)
    with pytest.raises(ProviderError) as caught:
        box.check(CallDeadline.start())
    assert caught.value.code == "unreachable"


def test_production_context_verifies_hosts() -> None:
    box = ImapMailbox("icloud", host="h", port=993, folder="INBOX", username="u", password="p")
    assert box._context.verify_mode == ssl.CERT_REQUIRED
    assert box._context.check_hostname is True


@pytest.fixture
def provider_log() -> Iterator[Capture]:
    handler = Capture()
    logger = logging.getLogger("mcp_hub.providers")
    logger.addHandler(handler)
    yield handler
    logger.removeHandler(handler)


def test_undecodable_message_becomes_the_placeholder_with_one_log_line(provider_log: Capture) -> None:
    broken = message(2, 3, "broken")
    broken.bodystructure = 5  # not a structure: walk() raises inside the per-message guards
    page = mailbox(server(message(1, 5, "good"), broken)).list_messages(
        ListQuery("inbox", T0 - timedelta(hours=1), True, None, 20), CallDeadline.start()
    )
    by_uid = {m.ref.uid: m for m in page.items}
    assert by_uid[1].subject == "good"
    assert by_uid[1].snippet_text == "Hello there"
    placeholder = by_uid[2]
    assert (placeholder.snippet_text, placeholder.subject, placeholder.from_address) == (UNDECODABLE_NOTE, None, None)
    [line] = [json.loads(line) for line in provider_log.lines]
    assert line["event"] == "item_degraded"
    assert (line["account"], line["capability"], line["exception"]) == ("icloud", "mail", "TypeError")
    assert line["item"] == item_hash(encode_message_id(MessageRef("icloud", "INBOX", 7, 2)))


def test_item_degraded_logs_the_exception_class_name_only(
    provider_log: Capture, monkeypatch: pytest.MonkeyPatch
) -> None:
    import mcp_hub.providers.imap as imap

    original = imap.parse_summary_headers

    def exploding(raw: bytes | None) -> ParsedHeaders:
        if raw is not None and b"secret" in raw:
            raise ValueError("SENTINEL-provider-text secret@example.test")
        return original(raw)

    monkeypatch.setattr(imap, "parse_summary_headers", exploding)
    page = mailbox(server(message(1, 3, "secret"), message(2, 4, "fine"))).list_messages(
        ListQuery("inbox", T0 - timedelta(hours=1), True, None, 20), CallDeadline.start()
    )
    assert [m.snippet_text for m in page.items] == [UNDECODABLE_NOTE, "Hello there"]
    text = "\n".join(provider_log.lines) + "\n".join(m for _, _, m in provider_log.raw)
    assert "SENTINEL" not in text
    assert "secret" not in text
    assert [json.loads(line)["exception"] for line in provider_log.lines] == ["ValueError"]


def test_a_part_that_fails_to_decode_only_empties_its_snippet(monkeypatch: pytest.MonkeyPatch) -> None:
    import mcp_hub.providers.imap as imap

    original = imap.decode_part

    def exploding(data: bytes, part: Part) -> str:
        if data == b"boom":
            raise ValueError("boom")
        return original(data, part)

    monkeypatch.setattr(imap, "decode_part", exploding)
    srv = server(message(1, 3, "a", text=b"boom"), message(2, 4, "b"))
    page = mailbox(srv).list_messages(ListQuery("inbox", T0 - timedelta(hours=1), True, None, 20), CallDeadline.start())
    assert [(m.subject, m.snippet_text) for m in page.items] == [("a", ""), ("b", "Hello there")]


def snippet_failing(error: Exception) -> type[FakeImapClient]:
    class SnippetFailingClient(FakeImapClient):
        def fetch(self, messages: object, items: object) -> dict[int, dict[bytes, object]]:
            if any(item.endswith(f"<0.{SNIPPET_FETCH_BYTES}>") for item in items):  # type: ignore[attr-defined]
                raise error
            return super().fetch(messages, items)  # type: ignore[arg-type]

    return SnippetFailingClient


def test_failing_snippet_fetch_degrades_to_no_snippet(provider_log: Capture) -> None:
    srv = server(message(1, 3, "a"), message(2, 4, "b"))
    page = mailbox(srv, snippet_failing(ProtocolError("SENTINEL odd literal"))).list_messages(
        ListQuery("inbox", T0 - timedelta(hours=1), True, None, 20), CallDeadline.start()
    )
    assert [(m.subject, m.snippet_text) for m in page.items] == [("a", ""), ("b", "")]
    lines = [json.loads(line) for line in provider_log.lines]
    assert [(e["event"], e["exception"]) for e in lines] == [("item_degraded", "ProtocolError")] * 2
    assert "SENTINEL" not in "\n".join(provider_log.lines)


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (OSError("reset"), "unreachable"),
        (IMAPClientAbortError("eof"), "upstream_error"),
        (IMAPClientError("FETCH command error: BAD [x]"), "upstream_error"),
    ],
)
def test_connection_failure_or_bad_during_the_snippet_fetch_still_fails_the_account(
    error: Exception, code: str
) -> None:
    with pytest.raises(ProviderError) as caught:
        mailbox(server(message(1, 3, "a")), snippet_failing(error)).list_messages(
            ListQuery("inbox", T0 - timedelta(hours=1), True, None, 20), CallDeadline.start()
        )
    assert caught.value.code == code


class DeepStructureClient(FakeImapClient):
    """IMAPClient's recursive response parser raises RecursionError on a BODYSTRUCTURE nested ~1000 deep."""

    deep: frozenset[int] = frozenset({1})

    def fetch(self, messages: object, items: object) -> dict[int, dict[bytes, object]]:
        if "BODYSTRUCTURE" in items and any(uid in self.deep for uid in messages):  # type: ignore[operator,attr-defined]
            raise RecursionError("maximum recursion depth exceeded")
        return super().fetch(messages, items)  # type: ignore[arg-type]


def test_parser_recursion_on_one_message_degrades_only_that_message(provider_log: Capture) -> None:
    page = mailbox(server(message(1, 3, "deep"), message(2, 4, "fine")), DeepStructureClient).list_messages(
        ListQuery("inbox", T0 - timedelta(hours=1), True, None, 20), CallDeadline.start()
    )
    by_uid = {m.ref.uid: m for m in page.items}
    assert by_uid[1].snippet_text == UNDECODABLE_NOTE
    assert (by_uid[2].subject, by_uid[2].snippet_text) == ("fine", "Hello there")
    assert [json.loads(line)["exception"] for line in provider_log.lines] == ["RecursionError"]


def test_parser_recursion_in_get_message_still_answers_with_headers() -> None:
    detail = mailbox(server(message(1, 3, "deep")), DeepStructureClient).get_message(
        MessageRef("icloud", "INBOX", 7, 1), CallDeadline.start()
    )
    assert (detail.subject, detail.body_source, detail.attachments) == ("deep", "none", [])


@pytest.mark.parametrize(("size", "cut"), [(262_144, False), (262_145, True)])
def test_body_cut_only_when_the_part_is_longer_than_the_limit(size: int, cut: bool) -> None:
    row = message(9, 5, "edge", text=b"x" * size)
    row.bodystructure = (b"text", b"plain", (b"charset", b"utf-8"), None, None, b"7bit", size, 1, None, None, None)
    detail = mailbox(server(row)).get_message(MessageRef("icloud", "INBOX", 7, 9), CallDeadline.start())
    assert len(detail.body_text) == 262_144
    assert detail.body_cut is cut
