"""GENA subscriptions with ordered delivery and bounded notification storage."""

import asyncio
import ipaddress
import time
import uuid
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from aiohttp import ClientError, ClientSession, web


@dataclass
class Subscription:
    """At most one pending snapshot per subscriber, delivered in sequence."""

    service: str
    callback: str
    peer: str
    expires: float
    sid: str = field(default_factory=lambda: f"uuid:{uuid.uuid4()}")
    sequence: int = 0
    pending: bytes | None = None
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task | None = None


class Eventing:
    """Manage subscriptions owned by the requesting control point."""

    def __init__(self, client: ClientSession, snapshot):
        self.client, self.snapshot = client, snapshot
        self.subscriptions = {}
        self.tasks = set()

    def prune(self):
        now = time.monotonic()
        for sid, sub in list(self.subscriptions.items()):
            if sub.expires <= now:
                self.remove(sid)

    def remove(self, sid):
        sub = self.subscriptions.pop(sid, None)
        if sub and sub.task:
            sub.task.cancel()
        return sub

    async def handle(self, request):
        self.prune()
        service = request.match_info["service"]
        sid = request.headers.get("SID")
        sub = self.subscriptions.get(sid)
        if request.method == "UNSUBSCRIBE":
            if "CALLBACK" in request.headers or "NT" in request.headers:
                raise web.HTTPBadRequest()
            if not sub or sub.service != service or sub.peer != request.remote:
                raise web.HTTPPreconditionFailed()
            self.remove(sid)
            return web.Response()
        timeout = request.headers.get("TIMEOUT", "Second-1800")
        try:
            seconds = (
                1800
                if timeout.lower() == "second-infinite"
                else int(timeout.removeprefix("Second-"))
            )
            if not timeout.startswith("Second-") or seconds <= 0:
                raise ValueError
            seconds = min(seconds, 1800)
        except ValueError:
            raise web.HTTPBadRequest() from None
        if sid:
            if "CALLBACK" in request.headers or "NT" in request.headers:
                raise web.HTTPBadRequest()
            if not sub or sub.service != service or sub.peer != request.remote:
                raise web.HTTPPreconditionFailed()
            sub.expires = time.monotonic() + seconds
        else:
            if request.headers.get("NT") != "upnp:event":
                raise web.HTTPPreconditionFailed()
            if len(self.subscriptions) >= 64:
                raise web.HTTPServiceUnavailable()
            callback = request.headers.get("CALLBACK", "")
            try:
                if not callback.startswith("<") or not callback.endswith(">"):
                    raise ValueError
                callback = callback[1:-1]
                url = urlsplit(callback)
                if (
                    url.scheme != "http"
                    or url.username is not None
                    or url.password is not None
                    or url.fragment
                ):
                    raise ValueError
                if url.port == 0 or any(ord(char) < 33 for char in callback):
                    raise ValueError
                # A controller may only subscribe its own address, never a relay.
                if ipaddress.ip_address(url.hostname) != ipaddress.ip_address(
                    request.remote
                ):
                    raise ValueError
            except (ValueError, TypeError):
                raise web.HTTPPreconditionFailed() from None
            sub = Subscription(
                service, callback, request.remote, time.monotonic() + seconds
            )
            self.subscriptions[sub.sid] = sub
        response = web.Response(
            headers={"SID": sub.sid, "TIMEOUT": f"Second-{seconds}"}
        )
        try:
            await response.prepare(request)
            await response.write_eof()
        except BaseException:
            self.remove(sub.sid)
            raise
        if sub.task is None:
            sub.pending = self.snapshot(service)
            sub.ready.set()
            sub.task = asyncio.create_task(self._deliver(sub))
            self.tasks.add(sub.task)
            sub.task.add_done_callback(self.tasks.discard)
        return response

    def publish(self, service, body):
        self.prune()
        for sub in self.subscriptions.values():
            if sub.service == service:
                sub.pending = body
                sub.ready.set()

    async def _deliver(self, sub):
        try:
            while True:
                await sub.ready.wait()
                sub.ready.clear()
                body, sub.pending = sub.pending, None
                async with self.client.request(
                    "NOTIFY",
                    sub.callback,
                    data=body,
                    allow_redirects=False,
                    headers={
                        "CONTENT-TYPE": 'text/xml; charset="utf-8"',
                        "NT": "upnp:event",
                        "NTS": "upnp:propchange",
                        "SID": sub.sid,
                        "SEQ": str(sub.sequence),
                    },
                ) as response:
                    if not 200 <= response.status < 300:
                        return
                sub.sequence = sub.sequence + 1 if sub.sequence < 0xFFFFFFFF else 1
        except (ClientError, TimeoutError):
            pass
        finally:
            self.subscriptions.pop(sub.sid, None)

    async def close(self):
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.subscriptions.clear()
