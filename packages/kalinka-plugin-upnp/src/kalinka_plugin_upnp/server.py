"""UPnP HTTP control, descriptions, discovery, and receiver lifecycle."""

import asyncio
import logging
from xml.etree import ElementTree as ET

from aiohttp import ClientSession, ClientTimeout, web
from defusedxml.common import DefusedXmlException
from defusedxml.ElementTree import fromstring

from .discovery import SERVER, Discovery
from .events import Eventing
from .media import UpnpError
from .playback import Playback
from .services import (
    CM,
    SERVICES,
    SOAP,
    Services,
    description,
    soap_fault,
    soap_response,
)

logger = logging.getLogger(__name__)


class Receiver:
    """Own all sockets and tasks for a UPnP renderer hosted by this input plugin."""

    def __init__(self, direct, name, host, port, udn):
        self.name, self.host, self.port, self.udn = name, host, port, udn
        self.playback = Playback(direct, self.changed)
        self.services = Services(self.playback)
        self.client = self.eventing = self.discovery = self.runner = self.publisher = (
            None
        )
        self.dirty = set()
        self.last_events = {}
        self.closed = False
        self.app = web.Application(client_max_size=128 * 1024)
        self.app.router.add_get("/description.xml", self.device_description)
        self.app.router.add_get("/{service}/scpd.xml", self.service_description)
        self.app.router.add_post("/{service}/control", self.control)
        self.app.router.add_route("SUBSCRIBE", "/{service}/event", self.subscribe)
        self.app.router.add_route("UNSUBSCRIBE", "/{service}/event", self.subscribe)

    def changed(self, service):
        self.dirty.add(service)

    async def start(self, *, advertise=True):
        try:
            self.client = ClientSession(timeout=ClientTimeout(total=3), trust_env=False)
            self.eventing = Eventing(self.client, self.services.event)
            self.playback.start()
            self.runner = web.AppRunner(self.app, access_log=None, shutdown_timeout=2)
            await self.runner.setup()
            site = web.TCPSite(self.runner, self.host, self.port)
            await site.start()
            self.port = self.runner.addresses[0][1]
            self.publisher = asyncio.create_task(self._publish())
            if advertise:
                self.discovery = Discovery(
                    self.host,
                    f"http://{self.host}:{self.port}/description.xml",
                    self.udn,
                )
                await self.discovery.start()
        except BaseException:
            await self.close()
            raise

    async def device_description(self, request):
        return self.xml_response(description(self.name, self.udn))

    def service(self, request):
        service = SERVICES.get(request.match_info["service"])
        if service is None:
            raise web.HTTPNotFound()
        return service

    async def service_description(self, request):
        return self.xml_response(self.service(request).scpd())

    async def subscribe(self, request):
        if self.service(request) is CM and request.method == "SUBSCRIBE":
            await self.services.refresh_dsd()
        return await self.eventing.handle(request)

    async def control(self, request):
        service = self.service(request)
        try:
            root = fromstring(await request.read())
            if root.tag != f"{{{SOAP}}}Envelope":
                raise UpnpError(402, "Invalid Args")
            body = root.find(f"{{{SOAP}}}Body")
            if body is None or len(body) != 1:
                raise UpnpError(402, "Invalid Args")
            action = body[0]
            namespace, separator, name = action.tag.removeprefix("{").partition("}")
            soap_action = request.headers.get("SOAPACTION", "").strip('"')
            if (
                not separator
                or namespace != service.type
                or soap_action != f"{service.type}#{name}"
            ):
                raise UpnpError(401, "Invalid Action")
            arguments = {}
            for argument in action:
                key = argument.tag.rsplit("}", 1)[-1]
                if key in arguments or len(argument):
                    raise UpnpError(402, "Invalid Args")
                arguments[key] = argument.text or ""
            async with asyncio.timeout(10):
                values = await self.services.dispatch(service.name, name, arguments)
            if name in {
                "SetAVTransportURI",
                "SetNextAVTransportURI",
                "Play",
                "Stop",
                "Next",
                "Previous",
            }:
                logger.info("UPnP %s accepted", name)
            return self.xml_response(soap_response(service, name, values))
        except (ET.ParseError, DefusedXmlException):
            error = UpnpError(402, "Invalid Args")
        except UpnpError as exc:
            error = exc
        except web.HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001 - never expose upstream exception text
            logger.error("UPnP action failed (%s)", type(exc).__name__)
            error = UpnpError(501, "Action Failed")
        return self.xml_response(soap_fault(error), status=500)

    @staticmethod
    def xml_response(body, status=200):
        return web.Response(
            body=body,
            status=status,
            content_type="text/xml",
            charset="utf-8",
            headers={"SERVER": SERVER},
        )

    async def _publish(self):
        while True:
            await asyncio.sleep(0.2)
            self.eventing.prune()
            dirty, self.dirty = self.dirty, set()
            for service in dirty:
                body = self.services.event(service)
                if self.last_events.get(service) != body:
                    self.last_events[service] = body
                    self.eventing.publish(service, body)

    async def close(self):
        if self.closed:
            return
        self.closed = True
        if self.publisher:
            self.publisher.cancel()
            await asyncio.gather(self.publisher, return_exceptions=True)
        resources = (
            (self.discovery, "close"),
            (self.runner, "cleanup"),
            (self.playback, "close"),
            (self.eventing, "close"),
            (self.client, "close"),
        )
        for resource, method in resources:
            if resource is not None:
                try:
                    await getattr(resource, method)()
                except Exception as exc:  # noqa: BLE001 - complete cleanup without logging URLs
                    logger.error("UPnP shutdown failed (%s)", type(exc).__name__)
