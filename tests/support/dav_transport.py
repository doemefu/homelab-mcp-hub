"""Canned CalDAV answers for unit tests: multistatus builders and an in-process httpx2 transport that records hosts."""

from collections.abc import Callable
from dataclasses import dataclass, field
from xml.sax.saxutils import escape

import httpx2

_NS = 'xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav"'


def multistatus(*responses: str) -> bytes:
    return f'<?xml version="1.0" encoding="utf-8"?><D:multistatus {_NS}>{"".join(responses)}</D:multistatus>'.encode()


def prop_response(href: str, prop: str, status: str = "200 OK") -> str:
    return (
        f"<D:response><D:href>{escape(href)}</D:href><D:propstat><D:prop>{prop}</D:prop>"
        f"<D:status>HTTP/1.1 {status}</D:status></D:propstat></D:response>"
    )


def principal(href: str) -> bytes:
    return multistatus(
        prop_response("/", f"<D:current-user-principal><D:href>{escape(href)}</D:href></D:current-user-principal>")
    )


def home_set(href: str) -> bytes:
    return multistatus(
        prop_response("/p/", f"<C:calendar-home-set><D:href>{escape(href)}</D:href></C:calendar-home-set>")
    )


def collection(href: str, name: str | None, *, calendar: bool = True, components: tuple[str, ...] | None = None) -> str:
    kind = "<D:collection/><C:calendar/>" if calendar else "<D:collection/>"
    prop = f"<D:resourcetype>{kind}</D:resourcetype>"
    if name is not None:
        prop += f"<D:displayname>{escape(name)}</D:displayname>"
    if components is not None:
        comps = "".join(f'<C:comp name="{c}"/>' for c in components)
        prop += f"<C:supported-calendar-component-set>{comps}</C:supported-calendar-component-set>"
    return prop_response(href, prop)


def report(*objects: bytes) -> bytes:
    return multistatus(
        *(
            prop_response(
                f"/obj{n}.ics", f'<D:getetag>"{n}"</D:getetag><C:calendar-data>{escape(o.decode())}</C:calendar-data>'
            )
            for n, o in enumerate(objects)
        )
    )


@dataclass
class RecordingTransport:
    """Answers by (method, url) and records (host, port, method, path, has_authorization) of every request sent."""

    answers: dict[tuple[str, str], Callable[[httpx2.Request], httpx2.Response]]
    seen: list[tuple[str, int, str, str, bool]] = field(default_factory=list)

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        url = request.url
        port = url.port or (443 if url.scheme == "https" else 80)
        self.seen.append((url.host, port, request.method, url.path, "authorization" in request.headers))
        answer = self.answers.get((request.method, f"{url.scheme}://{url.host}:{port}{url.path}"))
        return answer(request) if answer is not None else httpx2.Response(404)

    def transport(self) -> httpx2.MockTransport:
        return httpx2.MockTransport(self.handle)


def dav(body: bytes) -> Callable[[httpx2.Request], httpx2.Response]:
    return lambda request: httpx2.Response(207, content=body, headers={"Content-Type": "application/xml"})
