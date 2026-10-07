import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from xml.etree import ElementTree as ET

import pytest
from aiohttp import ClientSession, web
from kalinka_plugin_sdk.datamodel import DeviceVolume, PlaybackState, PlayerStateEnum
from kalinka_plugin_upnp.media import Media
from kalinka_plugin_upnp.server import Receiver
from kalinka_plugin_upnp.services import AVT, CM, DEVICE_TYPE, RCS, SERVICES, SOAP
from test_media import DIDL, DSD_TYPES


@pytest.fixture
async def receiver(direct):
    receiver = Receiver(
        direct, "Kalinka & Living Room", "127.0.0.1", 0, "uuid:test-device"
    )
    await receiver.start(advertise=False)
    yield receiver
    await receiver.close()


@pytest.fixture
async def client():
    async with ClientSession() as client:
        yield client


def endpoint(receiver, path):
    return f"http://127.0.0.1:{receiver.port}{path}"


async def action(client, receiver, service, name, arguments, *, status=200):
    envelope = ET.Element("s:Envelope", {"xmlns:s": SOAP})
    node = ET.SubElement(
        ET.SubElement(envelope, "s:Body"), f"u:{name}", {"xmlns:u": service.type}
    )
    for key, value in arguments.items():
        ET.SubElement(node, key).text = str(value)
    async with client.post(
        endpoint(receiver, f"/{service.name}/control"),
        data=ET.tostring(envelope),
        headers={
            "SOAPACTION": f'"{service.type}#{name}"',
            "Content-Type": 'text/xml; charset="utf-8"',
        },
    ) as response:
        body = await response.read()
        assert response.status == status, body
    root = ET.fromstring(body)
    if status != 200:
        return root.findtext(".//{urn:schemas-upnp-org:control-1-0}errorCode")
    return {child.tag: child.text or "" for child in root.find(f"{{{SOAP}}}Body")[0]}


async def set_uri(client, receiver):
    return await action(
        client,
        receiver,
        AVT,
        "SetAVTransportURI",
        {
            "InstanceID": 0,
            "CurrentURI": "http://media.test/audio?key=secret&id=42",
            "CurrentURIMetaData": DIDL,
        },
    )


async def test_descriptions_and_all_scpd_argument_references(client, receiver):
    async with client.get(endpoint(receiver, "/description.xml")) as response:
        root = ET.fromstring(await response.read())
        assert response.status == 200
    ns = {"d": "urn:schemas-upnp-org:device-1-0"}
    assert root.findtext("d:device/d:deviceType", namespaces=ns) == DEVICE_TYPE
    assert (
        root.findtext("d:device/d:friendlyName", namespaces=ns)
        == "Kalinka & Living Room"
    )
    assert root.findtext("d:device/d:UDN", namespaces=ns) == "uuid:test-device"
    assert len(root.findall("d:device/d:serviceList/d:service", ns)) == 3
    for service in SERVICES.values():
        async with client.get(
            endpoint(receiver, f"/{service.name}/scpd.xml")
        ) as response:
            scpd = ET.fromstring(await response.read())
            assert response.status == 200
        s = {"s": "urn:schemas-upnp-org:service-1-0"}
        variables = {
            node.text
            for node in scpd.findall("s:serviceStateTable/s:stateVariable/s:name", s)
        }
        references = {
            node.text
            for node in scpd.findall(
                "s:actionList/s:action/s:argumentList/s:argument/s:relatedStateVariable",
                s,
            )
        }
        assert references <= variables
    async with client.get(endpoint(receiver, "/Unknown/scpd.xml")) as response:
        assert response.status == 404


async def test_soap_to_direct_playback_and_queries(client, receiver, direct):
    assert await set_uri(client, receiver) == {}
    direct.acquire.assert_not_called()
    media = await action(client, receiver, AVT, "GetMediaInfo", {"InstanceID": 0})
    assert media["CurrentURIMetaData"] == DIDL
    assert media["MediaDuration"] == "1:02:03"
    await action(client, receiver, AVT, "Play", {"InstanceID": 0, "Speed": 1})
    hold = direct.sessions[0]
    assert hold.play.call_args.args[0].source.url == media["CurrentURI"]
    hold.listener.on_state(PlaybackState(state=PlayerStateEnum.PLAYING, position=27000))
    position = await action(client, receiver, AVT, "GetPositionInfo", {"InstanceID": 0})
    assert position["RelTime"] == "0:00:27"
    assert position["TrackMetaData"] == DIDL
    await action(
        client,
        receiver,
        AVT,
        "Seek",
        {"InstanceID": 0, "Unit": "REL_TIME", "Target": "0:00:40.125"},
    )
    hold.seek.assert_awaited_once_with(40125)
    await action(client, receiver, AVT, "Pause", {"InstanceID": 0})
    hold.pause.assert_awaited_once()
    state = await action(client, receiver, AVT, "GetTransportInfo", {"InstanceID": 0})
    assert state["CurrentTransportState"] == "PAUSED_PLAYBACK"
    await action(client, receiver, AVT, "Stop", {"InstanceID": 0})
    hold.stop.assert_awaited_once()
    hold.release.assert_not_awaited()


@pytest.mark.parametrize(
    "service,name,arguments,code",
    [
        (AVT, "GetTransportInfo", {"InstanceID": 1}, "718"),
        (AVT, "GetTransportInfo", {}, "402"),
        (AVT, "GetTransportInfo", {"InstanceID": -1}, "402"),
        (AVT, "GetTransportInfo", {"InstanceID": "bad"}, "402"),
        (AVT, "Play", {"InstanceID": 0, "Speed": 2}, "717"),
        (AVT, "Seek", {"InstanceID": 0, "Unit": "ABS_COUNT", "Target": "1"}, "710"),
        (AVT, "Seek", {"InstanceID": 0, "Unit": "REL_TIME", "Target": "bad"}, "711"),
        (AVT, "SetPlayMode", {"InstanceID": 0, "NewPlayMode": "SHUFFLE"}, "712"),
        (AVT, "Record", {"InstanceID": 0}, "401"),
        (
            RCS,
            "SetVolume",
            {"InstanceID": 0, "Channel": "Master", "DesiredVolume": 101},
            "402",
        ),
        (RCS, "GetVolume", {"InstanceID": 0, "Channel": "LF"}, "402"),
        (
            RCS,
            "SetMute",
            {"InstanceID": 0, "Channel": "Master", "DesiredMute": "maybe"},
            "402",
        ),
        (CM, "GetCurrentConnectionInfo", {"ConnectionID": 1}, "706"),
    ],
)
async def test_protocol_faults(client, receiver, service, name, arguments, code):
    assert await action(client, receiver, service, name, arguments, status=500) == code


async def test_bad_xml_and_request_limits(client, receiver):
    for body in (b"<bad", b'<!DOCTYPE x [<!ENTITY x "expansion">]><x>&x;</x>'):
        async with client.post(
            endpoint(receiver, "/AVTransport/control"), data=body
        ) as response:
            assert response.status == 500
            assert b"402" in await response.read()
    async with client.post(
        endpoint(receiver, "/AVTransport/control"), data=b"x" * (129 * 1024)
    ) as response:
        assert response.status == 413


async def test_unsupported_media_does_not_interrupt_current_playback(
    client, receiver, direct
):
    await set_uri(client, receiver)
    await action(client, receiver, AVT, "Play", {"InstanceID": 0, "Speed": 1})
    assert (
        await action(
            client,
            receiver,
            AVT,
            "SetAVTransportURI",
            {
                "InstanceID": 0,
                "CurrentURI": "http://media.test/file.wav",
                "CurrentURIMetaData": "",
            },
            status=500,
        )
        == "714"
    )
    assert receiver.playback.active
    direct.sessions[0].release.assert_not_called()


@pytest.mark.parametrize("dsd", [None, False, True])
async def test_dsd_is_advertised_only_while_the_renderer_outputs_it(
    client, receiver, direct, dsd
):
    direct.set_dsd(dsd)
    protocols = await action(client, receiver, CM, "GetProtocolInfo", {})
    advertised = {entry.split(":")[2] for entry in protocols["Sink"].split(",")}
    assert "audio/flac" in advertised
    assert advertised & set(DSD_TYPES) == (set(DSD_TYPES) if dsd else set())


async def test_a_server_that_cannot_tell_leaves_dsd_unadvertised(client):
    older = SimpleNamespace(acquire=AsyncMock())
    receiver = Receiver(older, "Kalinka", "127.0.0.1", 0, "uuid:test-device")
    await receiver.start(advertise=False)
    try:
        protocols = await action(client, receiver, CM, "GetProtocolInfo", {})
        assert "audio/flac" in protocols["Sink"]
        assert "audio/x-dsf" not in protocols["Sink"]
    finally:
        await receiver.close()


async def test_a_closed_receiver_stops_watching_the_output(direct):
    receiver = Receiver(direct, "Kalinka", "127.0.0.1", 0, "uuid:test-device")
    await receiver.start(advertise=False)
    assert direct.watchers == [receiver.services]
    await receiver.close()
    assert direct.watchers == []


async def test_dsd_is_refused_while_the_renderer_does_not_output_it(
    client, receiver, direct
):
    await set_uri(client, receiver)
    await action(client, receiver, AVT, "Play", {"InstanceID": 0, "Speed": 1})
    for name, prefix in (
        ("SetAVTransportURI", "Current"),
        ("SetNextAVTransportURI", "Next"),
    ):
        arguments = {
            "InstanceID": 0,
            f"{prefix}URI": "http://media.test/a.dsf",
            f"{prefix}URIMetaData": "",
        }
        assert await action(client, receiver, AVT, name, arguments, status=500) == "714"
    assert receiver.playback.active
    assert receiver.playback.current.uri == "http://media.test/audio?key=secret&id=42"
    assert receiver.playback.next is None
    direct.sessions[0].play.assert_awaited_once()
    direct.sessions[0].release.assert_not_called()


async def test_controllers_are_told_when_dsd_output_changes(
    client, receiver, direct, callback
):
    url, notifications = callback
    direct.set_dsd(True)
    async with client.request(
        "SUBSCRIBE",
        endpoint(receiver, "/ConnectionManager/event"),
        headers={"NT": "upnp:event", "CALLBACK": f"<{url}>"},
    ) as response:
        assert response.status == 200
    _, body = await notification(notifications)
    assert "audio/x-dsf" in ET.fromstring(body).findtext(".//SinkProtocolInfo")
    direct.set_dsd(False)
    _, body = await notification(notifications)
    sink = ET.fromstring(body).findtext(".//SinkProtocolInfo")
    assert "audio/flac" in sink
    assert "audio/x-dsf" not in sink


async def test_dsd_url_without_metadata_is_played(client, receiver, direct):
    direct.set_dsd(True)
    uri = "http://media.test/Track%2001.dsf"
    assert (
        await action(
            client,
            receiver,
            AVT,
            "SetAVTransportURI",
            {"InstanceID": 0, "CurrentURI": uri, "CurrentURIMetaData": ""},
        )
        == {}
    )
    await action(client, receiver, AVT, "Play", {"InstanceID": 0, "Speed": 1})
    source = direct.sessions[0].play.call_args.args[0]
    assert source.source.url == uri
    assert source.format == "audio/x-dsf"


async def test_connection_and_rendering_controls(client, receiver, direct):
    protocols = await action(client, receiver, CM, "GetProtocolInfo", {})
    assert "http-get:*:audio/flac:*" in protocols["Sink"]
    assert "audio/aac" not in protocols["Sink"]
    assert await action(client, receiver, CM, "GetCurrentConnectionIDs", {}) == {
        "ConnectionIDs": "0"
    }
    connection = await action(
        client, receiver, CM, "GetCurrentConnectionInfo", {"ConnectionID": 0}
    )
    assert connection["Direction"] == "Input"
    assert connection["AVTransportID"] == "0"
    await set_uri(client, receiver)
    await action(client, receiver, AVT, "Play", {"InstanceID": 0, "Speed": 1})
    args = {"InstanceID": 0, "Channel": "Master"}
    assert await action(client, receiver, RCS, "GetVolume", args) == {
        "CurrentVolume": "50"
    }
    await action(client, receiver, RCS, "SetVolume", {**args, "DesiredVolume": 33})
    direct.sessions[0].set_volume.assert_awaited_with(33)
    await action(client, receiver, RCS, "SetMute", {**args, "DesiredMute": "true"})
    assert await action(client, receiver, RCS, "GetMute", args) == {"CurrentMute": "1"}
    await action(client, receiver, RCS, "SetMute", {**args, "DesiredMute": "false"})
    direct.sessions[0].set_volume.assert_awaited_with(33)


@pytest.fixture
async def callback():
    notifications = asyncio.Queue()

    async def notify(request):
        notifications.put_nowait((dict(request.headers), await request.read()))
        return web.Response()

    app = web.Application()
    app.router.add_route("NOTIFY", "/notify", notify)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0).start()
    yield f"http://127.0.0.1:{runner.addresses[0][1]}/notify", notifications
    await runner.cleanup()


def last_change(body):
    properties = ET.fromstring(body)
    last = ET.fromstring(properties.findtext(".//LastChange"))
    return {node.tag.rsplit("}", 1)[-1]: node.attrib for node in last[0]}


async def notification(queue):
    return await asyncio.wait_for(queue.get(), 2)


async def test_gena_initial_state_playback_events_renew_and_unsubscribe(
    client, receiver, direct, callback
):
    url, notifications = callback
    event_url = endpoint(receiver, "/AVTransport/event")
    async with client.request(
        "SUBSCRIBE",
        event_url,
        headers={
            "NT": "upnp:event",
            "CALLBACK": f"<{url}>",
            "TIMEOUT": "Second-infinite",
        },
    ) as response:
        assert response.status == 200
        sid = response.headers["SID"]
        assert sid.startswith("uuid:")
        assert response.headers["TIMEOUT"] == "Second-1800"
    headers, body = await notification(notifications)
    assert headers["SEQ"] == "0"
    assert headers["SID"] == sid
    assert last_change(body)["TransportState"]["val"] == "NO_MEDIA_PRESENT"
    await set_uri(client, receiver)
    await action(client, receiver, AVT, "Play", {"InstanceID": 0, "Speed": 1})
    direct.sessions[0].listener.on_state(
        PlaybackState(state=PlayerStateEnum.PLAYING, position=3000)
    )
    headers, body = await notification(notifications)
    assert headers["SEQ"] == "1"
    changes = last_change(body)
    assert changes["TransportState"]["val"] == "PLAYING"
    assert changes["CurrentTrackMetaData"]["val"] == DIDL
    assert "RelativeTimePosition" not in changes
    direct.sessions[0].revoke()
    headers, body = await notification(notifications)
    assert headers["SEQ"] == "2"
    assert last_change(body)["TransportState"]["val"] == "STOPPED"
    async with client.request(
        "SUBSCRIBE", event_url, headers={"SID": sid, "TIMEOUT": "Second-600"}
    ) as response:
        assert response.status == 200
        assert response.headers["TIMEOUT"] == "Second-600"
    assert notifications.empty()
    async with client.request(
        "UNSUBSCRIBE", event_url, headers={"SID": sid}
    ) as response:
        assert response.status == 200
    assert sid not in receiver.eventing.subscriptions
    async with client.request("SUBSCRIBE", event_url, headers={"SID": sid}) as response:
        assert response.status == 412


async def test_volume_changes_are_evented_with_master_channel(
    client, receiver, direct, callback
):
    url, notifications = callback
    await set_uri(client, receiver)
    await action(client, receiver, AVT, "Play", {"InstanceID": 0, "Speed": 1})
    async with client.request(
        "SUBSCRIBE",
        endpoint(receiver, "/RenderingControl/event"),
        headers={"NT": "upnp:event", "CALLBACK": f"<{url}>"},
    ) as response:
        assert response.status == 200
    _, body = await notification(notifications)
    assert last_change(body)["Volume"] == {"val": "50", "channel": "Master"}
    direct.sessions[0].listener.on_volume(
        DeviceVolume(current_volume=12, max_volume=80)
    )
    _, body = await notification(notifications)
    assert last_change(body)["Volume"] == {"val": "15", "channel": "Master"}


@pytest.mark.parametrize(
    "headers",
    [
        {"NT": "upnp:event", "CALLBACK": "<http://192.0.2.1/notify>"},
        {"NT": "upnp:event", "CALLBACK": "<file:///etc/passwd>"},
        {"NT": "upnp:event", "CALLBACK": "<http://user:password@127.0.0.1/notify>"},
        {"NT": "upnp:event", "CALLBACK": "bad"},
        {"SID": "uuid:unknown"},
    ],
)
async def test_invalid_subscriptions(client, receiver, headers):
    async with client.request(
        "SUBSCRIBE", endpoint(receiver, "/AVTransport/event"), headers=headers
    ) as response:
        assert response.status == 412


async def test_expired_subscription_is_removed(client, receiver, callback):
    url, notifications = callback
    event_url = endpoint(receiver, "/ConnectionManager/event")
    async with client.request(
        "SUBSCRIBE", event_url, headers={"NT": "upnp:event", "CALLBACK": f"<{url}>"}
    ) as response:
        sid = response.headers["SID"]
    _, body = await notification(notifications)
    assert ET.fromstring(body).findtext(".//CurrentConnectionIDs") == "0"
    receiver.eventing.subscriptions[sid].expires = 0
    async with client.request("SUBSCRIBE", event_url, headers={"SID": sid}) as response:
        assert response.status == 412


async def test_shutdown_closes_listener_and_releases_output(receiver, direct):
    await receiver.playback.command(
        "set_uri", Media.parse("http://media.test/a.flac", "")
    )
    await receiver.playback.command("play")
    await receiver.close()
    direct.sessions[0].release.assert_awaited_once()
    assert receiver.client.closed
    assert receiver.publisher.done()
    assert receiver.playback.worker.done()


async def test_shutdown_continues_if_discovery_cannot_send_byebye(receiver, direct):
    await receiver.playback.command(
        "set_uri", Media.parse("http://media.test/a.flac", "")
    )
    await receiver.playback.command("play")
    receiver.discovery = SimpleNamespace(
        close=AsyncMock(side_effect=OSError("Network down"))
    )
    await receiver.close()
    assert receiver.client.closed
    assert not receiver.runner.addresses
    direct.sessions[0].release.assert_awaited_once()


async def test_failed_start_cleans_up_tasks_and_http_socket(direct, monkeypatch):
    monkeypatch.setattr(
        "kalinka_plugin_upnp.server.Discovery.start",
        AsyncMock(side_effect=OSError("No interface")),
    )
    receiver = Receiver(direct, "Kalinka", "127.0.0.1", 0, "uuid:test")
    with pytest.raises(OSError):
        await receiver.start()
    assert receiver.client.closed
    assert receiver.playback.worker.done()
    assert receiver.publisher.done()
    assert not receiver.runner.addresses
