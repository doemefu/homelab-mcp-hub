import socket
import ssl
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage

import pytest
from imapclient.exceptions import IMAPClientError, LoginError

from mcp_hub.ids import MessageRef
from mcp_hub.providers.base import ProviderError
from mcp_hub.providers.imap import HEADER_ITEM, ImapMailbox

T0 = datetime(2026, 9, 29, 7, 0, tzinfo=UTC)
PLAIN = (b"text", b"plain", (b"charset", b"utf-8"), None, None, b"7bit", 64, 2, None, None, None, None)
HEADER_KEY = b"BODY[HEADER.FIELDS (FROM TO CC SUBJECT)]<0>"


def headers(subject: str, sender: str = "Sender <sender@example.test>") -> bytes:
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = sender, "owner@example.test", subject
    return bytes(msg).split(b"\n\n")[0] + b"\r\n\r\n"


class FakeClient:
    def __init__(
        self, messages: dict[int, dict[bytes, object]], *, validity: int = 7, login_error: Exception | None = None
    ) -> None:
        self.messages, self.validity, self.login_error = messages, validity, login_error
        self.calls: list[tuple[str, object]] = []
        self.normalise_times = True

    def login(self, username: str, password: str) -> None:
        self.calls.append(("login", None))
        if self.login_error:
            raise self.login_error

    def select_folder(self, folder: str, readonly: bool = False) -> dict[bytes, object]:
        self.calls.append(("select_folder", (folder, readonly)))
        return {b"UIDVALIDITY": self.validity, b"EXISTS": len(self.messages)}

    def search(self, criteria: list[object]) -> list[int]:
        self.calls.append(("search", criteria))
        return [uid for uid, m in self.messages.items() if b"\\Seen" not in m[b"FLAGS"]]  # type: ignore[operator]

    def fetch(self, messages: list[int], data: list[str]) -> dict[int, dict[bytes, object]]:
        self.calls.append(("fetch", list(data)))
        out: dict[int, dict[bytes, object]] = {}
        for uid in messages:
            if uid not in self.messages:
                continue
            row = self.messages[uid]
            out[uid] = {}
            for item in data:
                if item == HEADER_ITEM:
                    out[uid][HEADER_KEY] = row[HEADER_KEY]
                elif item.startswith("BODY.PEEK["):
                    section, rest = item[len("BODY.PEEK[") :].split("]")
                    length = int(rest.split(".")[1].rstrip(">"))
                    out[uid][f"BODY[{section}]<0>".encode()] = row[b"TEXT"][:length]  # type: ignore[index]
                elif item.encode() in row:
                    out[uid][item.encode()] = row[item.encode()]
        return out

    def noop(self) -> None:
        self.calls.append(("noop", None))

    def logout(self) -> None:
        self.calls.append(("logout", None))


def message(minutes_ago: int, subject: str, *, seen: bool = False, text: bytes = b"Hello there") -> dict[bytes, object]:
    return {
        b"FLAGS": (b"\\Seen",) if seen else (),
        b"INTERNALDATE": T0 - timedelta(minutes=minutes_ago),
        b"BODYSTRUCTURE": PLAIN,
        HEADER_KEY: headers(subject),
        b"TEXT": text,
    }


def mailbox(client: FakeClient) -> ImapMailbox:
    return ImapMailbox(
        "icloud",
        host="imap.example.test",
        port=993,
        folder="INBOX",
        username="u",
        password="p",
        client_factory=lambda host, port, context, timeout: client,
    )


def test_list_unread_filters_on_exact_internaldate_and_sorts_newest_first() -> None:
    client = FakeClient(
        {1: message(60 * 30, "old"), 2: message(30, "second"), 3: message(5, "first"), 4: message(1, "read", seen=True)}
    )
    page = mailbox(client).list_unread(T0 - timedelta(hours=24), 20)
    assert [m.subject for m in page.items] == ["first", "second"]
    assert page.more is False
    assert page.items[0].ref == MessageRef("icloud", "INBOX", 7, 3)
    assert (page.items[0].from_address, page.items[0].from_name) == ("sender@example.test", "Sender")
    assert page.items[0].snippet_text == "Hello there"
    assert client.normalise_times is False


def test_one_malformed_message_does_not_fail_the_listing() -> None:
    odd = message(3, "x")
    odd[HEADER_KEY] = b"From: =?rot13?q?Fraqre?= <a@xn--.example.test>\r\nSubject: =?idna?q?x?=\r\n\r\n"
    odd[b"BODYSTRUCTURE"] = (
        b"text", b"plain", (b"charset", b"base64"), None, None, b"7bit", 10, 1, None, None, None, None
    )  # fmt: skip
    broken = message(2, "y")
    broken[b"BODYSTRUCTURE"] = ("not", "a", "structure")
    empty = message(4, "z")
    empty[b"BODYSTRUCTURE"] = ()
    missing = message(6, "w")
    del missing[b"BODYSTRUCTURE"]
    client = FakeClient({1: message(5, "good"), 2: odd, 3: broken, 4: empty, 5: missing})
    page = mailbox(client).list_unread(T0 - timedelta(hours=1), 20)
    assert len(page.items) == 5
    assert "good" in [m.subject for m in page.items]
    assert all(isinstance(m.snippet_text, str) for m in page.items)


def test_get_message_of_a_malformed_message_still_answers() -> None:
    odd = message(3, "x", text=b"\xff\xfe")
    odd[b"BODYSTRUCTURE"] = (
        b"text", b"plain", (b"charset", b"rot13"), None, None, b"8bit", 2, 1, None, None, None, None
    )  # fmt: skip
    detail = mailbox(FakeClient({4: odd})).get_message(MessageRef("icloud", "INBOX", 7, 4))
    assert isinstance(detail.body_text, str)
    assert detail.body_source == "text/plain"


def test_list_unread_limit_sets_more() -> None:
    client = FakeClient({i: message(i, f"m{i}") for i in range(1, 6)})
    page = mailbox(client).list_unread(T0 - timedelta(hours=1), 2)
    assert [m.subject for m in page.items] == ["m1", "m2"]
    assert page.more is True


def test_adapter_only_uses_read_only_commands() -> None:
    client = FakeClient({1: message(5, "a")})
    box = mailbox(client)
    box.list_unread(T0 - timedelta(hours=1), 20)
    box.get_message(MessageRef("icloud", "INBOX", 7, 1))
    box.check()
    names = {name for name, _ in client.calls}
    assert names <= {"login", "select_folder", "search", "fetch", "noop", "logout"}
    assert all(args == ("INBOX", True) for name, args in client.calls if name == "select_folder")
    for name, items in client.calls:
        if name == "fetch":
            for item in items:  # type: ignore[attr-defined]
                assert item in {"FLAGS", "INTERNALDATE", "BODYSTRUCTURE"} or item.startswith("BODY.PEEK["), item
                assert "RFC822" not in item
                assert item != "BODY.PEEK[]"
    assert [n for n, _ in client.calls].count("logout") == 3


def test_get_message_fetches_the_text_part_partially() -> None:
    client = FakeClient({9: message(5, "big", text=b"x" * 300_000)})
    detail = mailbox(client).get_message(MessageRef("icloud", "INBOX", 7, 9))
    part_fetch = [
        items
        for name, items in client.calls
        if name == "fetch" and any("BODY.PEEK[1]" in i for i in items)  # type: ignore[attr-defined]
    ]
    assert part_fetch == [["BODY.PEEK[1]<0.262144>"]]
    assert len(detail.body_text) == 262_144
    assert detail.body_cut is True
    assert detail.body_source == "text/plain"
    assert detail.unread is True


@pytest.mark.parametrize(("validity", "uid"), [(8, 9), (7, 99)])
def test_changed_uidvalidity_or_missing_uid_is_not_found(validity: int, uid: int) -> None:
    client = FakeClient({9: message(5, "x")}, validity=validity)
    with pytest.raises(ProviderError) as caught:
        mailbox(client).get_message(MessageRef("icloud", "INBOX", 7, uid))
    assert caught.value.code == "not_found"


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
    client = FakeClient({}, login_error=error)
    with pytest.raises(ProviderError) as caught:
        mailbox(client).check()
    assert caught.value.code == code
    assert caught.value.cause == type(error).__name__
    assert "secret-detail" not in str(caught.value)


def test_connect_failure_is_unreachable() -> None:
    def refuse(host: str, port: int, context: ssl.SSLContext, timeout: float) -> FakeClient:
        raise ConnectionRefusedError("refused")

    box = ImapMailbox("icloud", host="h", port=1, folder="INBOX", username="u", password="p", client_factory=refuse)
    with pytest.raises(ProviderError) as caught:
        box.check()
    assert caught.value.code == "unreachable"


def test_production_context_verifies_hosts() -> None:
    box = ImapMailbox("icloud", host="h", port=993, folder="INBOX", username="u", password="p")
    assert box._context.verify_mode == ssl.CERT_REQUIRED
    assert box._context.check_hostname is True
