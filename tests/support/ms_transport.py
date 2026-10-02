"""In-process Microsoft endpoints for unit tests: production URLs stay unchanged, every request is recorded.
Responses that must prove streaming behaviour use ChunkStream, which is not pre-read (httpx2 pre-reads content=)."""

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from urllib.parse import parse_qsl

import httpx2

FAKE_CLIENT_ID = "00000000-0000-4000-8000-000000000001"


def json_answer(status: int, body: dict[str, object], headers: dict[str, str] | None = None) -> httpx2.Response:
    return httpx2.Response(
        status, content=json.dumps(body).encode(), headers={"Content-Type": "application/json"} | (headers or {})
    )


class ChunkStream(httpx2.SyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks, self.served = chunks, 0

    def __iter__(self) -> Iterator[bytes]:
        for chunk in self.chunks:
            self.served += 1
            yield chunk


def streamed(
    status: int, chunks: list[bytes], headers: dict[str, str] | None = None
) -> tuple[httpx2.Response, ChunkStream]:
    stream = ChunkStream(chunks)
    return httpx2.Response(
        status, stream=stream, headers={"Content-Type": "application/json"} | (headers or {})
    ), stream


@dataclass
class MsTransport:
    answers: dict[str, list[httpx2.Response]]
    seen: list[tuple[str, int, str, str, dict[str, str], dict[str, str]]] = field(default_factory=list)

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        url = request.url
        form = dict(parse_qsl(request.read().decode())) if request.method == "POST" else {}
        self.seen.append((url.host, url.port or 443, request.method, url.path, form, dict(request.headers)))
        queue = self.answers.get(url.path)
        if not queue:
            return httpx2.Response(404)
        return queue.pop(0) if len(queue) > 1 else queue[0]

    def transport(self) -> httpx2.MockTransport:
        return httpx2.MockTransport(self.handle)
