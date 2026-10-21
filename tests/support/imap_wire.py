"""A minimal real IMAP4rev1 server on loopback that records the bytes a client sends."""

import re
import socketserver
import threading
from types import TracebackType

import imapclient

_LITERAL = re.compile(rb"\{(\d+)(\+?)\}")
_CAPABILITY = b"IMAP4rev1 LITERAL+"


class _Handler(socketserver.StreamRequestHandler):
    server: "_Server"

    def handle(self) -> None:
        self.wfile.write(b"* OK IMAP4rev1 ready\r\n")
        while True:
            line = self.rfile.readline()
            if not line:
                return
            line = line.rstrip(b"\r\n")
            self.server.lines.append(line)
            literal = _LITERAL.search(line) if line.endswith(b"}") else None
            if literal is not None:
                if not literal.group(2):
                    self.wfile.write(b"+ go ahead\r\n")
                self.server.literals.append(self.rfile.read(int(literal.group(1))))
                continue  # the rest of the command follows on the next line
            tag, _, rest = line.partition(b" ")
            if not self._answer(tag, rest.split(b" ", 1)[0].upper()):
                return

    def _answer(self, tag: bytes, command: bytes) -> bool:
        out = self.wfile
        if command == b"CAPABILITY":
            out.write(b"* CAPABILITY " + _CAPABILITY + b"\r\n" + tag + b" OK done\r\n")
        elif command == b"EXAMINE":
            out.write(b"* 0 EXISTS\r\n* OK [UIDVALIDITY 7] ok\r\n" + tag + b" OK [READ-ONLY] done\r\n")
        elif command == b"UID":
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

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.lines: list[bytes] = []
        self.literals: list[bytes] = []


class WireRecorder:
    def __enter__(self) -> "WireRecorder":
        self._server = _Server()
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
    def literals(self) -> list[bytes]:
        return self._server.literals


def wire_client(recorder: WireRecorder) -> imapclient.IMAPClient:
    return imapclient.IMAPClient("127.0.0.1", port=recorder.port, ssl=False, timeout=5)
