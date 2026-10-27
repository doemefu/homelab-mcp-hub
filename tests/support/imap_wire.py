"""A minimal real IMAP4rev1 server on loopback that records the bytes a client sends."""

import re
import select
import socketserver
import threading
import time
from types import TracebackType
from typing import Literal

import imapclient

from mcp_hub.providers.imap import HubImapClient

_LITERAL = re.compile(rb"\{(\d+)(\+?)\}")
Refuse = Literal[None, "before_continuation", "after_literal", "stall"]


class _Handler(socketserver.StreamRequestHandler):
    server: "_Server"
    rbufsize = 0  # unbuffered, so that "the client sent bytes before `+`" can be seen on the socket

    def _read_exact(self, count: int) -> bytes:
        data = b""
        while len(data) < count:
            chunk = self.rfile.read(count - len(data))
            if not chunk:
                break
            data += chunk
        return data

    def handle(self) -> None:
        self.wfile.write(b"* OK IMAP4rev1 ready\r\n")
        while True:
            line = self.rfile.readline()
            if not line:
                return
            line = line.rstrip(b"\r\n")
            full = line
            refuse = self.server.refuse
            while (literal := _LITERAL.search(line) if line.endswith(b"}") else None) is not None:
                if refuse in ("before_continuation", "stall"):
                    # The literal is never read: the next line is a new command (the client's LOGOUT, IR-2).
                    if refuse == "before_continuation":
                        self.wfile.write(line.split(b" ", 1)[0] + b" NO [BADCHARSET (US-ASCII)] no\r\n")
                    break
                if not literal.group(2):
                    if select.select([self.request], [], [], 0.1)[0]:
                        self.server.early_bytes = True  # the client did not wait for the continuation
                    self.wfile.write(b"+ go ahead\r\n")
                data = self._read_exact(int(literal.group(1)))
                self.server.literals.append(data)
                if refuse == "after_literal":
                    self.rfile.readline()
                    self.wfile.write(line.split(b" ", 1)[0] + b" NO [BADCHARSET (US-ASCII)] no\r\n")
                    full += data
                    break
                line = self.rfile.readline().rstrip(b"\r\n")
                full += data + line
            self.server.lines.append(full)
            self.server.stamps.append(time.monotonic())
            if refuse in ("before_continuation", "stall", "after_literal") and literal is not None:
                continue
            tag, _, rest = full.partition(b" ")
            command, _, args = rest.partition(b" ")
            sub = args.split(b" ", 1)[0].upper()
            if not self._answer(tag, command.upper(), sub):
                return

    def _answer(self, tag: bytes, command: bytes, sub: bytes) -> bool:
        out = self.wfile
        if command == b"CAPABILITY":
            out.write(b"* CAPABILITY " + self.server.capability + b"\r\n" + tag + b" OK done\r\n")
        elif command == b"EXAMINE":
            out.write(b"* 0 EXISTS\r\n* OK [UIDVALIDITY 7] ok\r\n" + tag + b" OK [READ-ONLY] done\r\n")
        elif command == b"UID" and sub == b"SEARCH":
            out.write(b"* SEARCH\r\n" + tag + b" OK done\r\n")
        elif command == b"LOGOUT":
            out.write(b"* BYE bye\r\n" + tag + b" OK done\r\n")
            return False
        elif command in (b"LOGIN", b"NOOP"):
            out.write(tag + b" OK done\r\n")
        else:
            out.write(tag + b" BAD unsupported\r\n")
        return True


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, capability: bytes, refuse: Refuse) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.capability = capability
        self.refuse = refuse
        self.lines: list[bytes] = []
        self.stamps: list[float] = []
        self.literals: list[bytes] = []
        self.early_bytes = False


class WireRecorder:
    """`refuse` makes the server reject a literal announcement: `before_continuation` answers a tagged NO instead of
    `+` and never reads the literal, `stall` answers nothing, `after_literal` reads the literal and then says NO."""

    def __init__(self, capability: bytes = b"IMAP4rev1 LITERAL+", refuse: Refuse = None) -> None:
        self._capability, self._refuse = capability, refuse

    def __enter__(self) -> "WireRecorder":
        self._server = _Server(self._capability, self._refuse)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    @property
    def lines(self) -> list[bytes]:
        return self._server.lines

    @property
    def stamps(self) -> list[float]:
        """`time.monotonic()` at which each entry of `lines` was complete."""
        return self._server.stamps

    @property
    def early_bytes(self) -> bool:
        """Whether a client sent bytes after a `{N}` announcement before the server answered `+`."""
        return self._server.early_bytes

    @property
    def literals(self) -> list[bytes]:
        return self._server.literals


def wire_client(recorder: WireRecorder) -> imapclient.IMAPClient:
    return imapclient.IMAPClient("127.0.0.1", port=recorder.port, ssl=False, timeout=5)


def hub_client(recorder: WireRecorder, *, timeout: float = 5) -> HubImapClient:
    """The production client class on the recorder's port."""
    return HubImapClient("127.0.0.1", port=recorder.port, ssl=False, timeout=timeout)
