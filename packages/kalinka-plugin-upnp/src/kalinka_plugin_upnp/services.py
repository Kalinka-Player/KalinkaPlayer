"""UPnP AV service contracts, descriptions, actions, and event snapshots."""

import logging
from dataclasses import dataclass, field
from xml.etree import ElementTree as ET

from .media import (
    DSD_FORMATS,
    Media,
    UpnpError,
    format_time,
    parse_time,
    sink_protocol_info,
)
from .playback import Playback

logger = logging.getLogger(__name__)

DEVICE_TYPE = "urn:schemas-upnp-org:device:MediaRenderer:1"
SOAP = "http://schemas.xmlsoap.org/soap/envelope/"
EVENT = "urn:schemas-upnp-org:event-1-0"


def xml(element):
    return ET.tostring(element, encoding="utf-8", xml_declaration=True)


def elements(parent, values):
    for key, value in values.items():
        ET.SubElement(parent, key).text = str(value)


@dataclass(frozen=True)
class Variable:
    """A service state variable and the validation shared by its arguments."""

    type: str = "string"
    allowed: tuple[str, ...] = ()
    maximum: int | None = None
    evented: bool = False

    def validate(self, value):
        if self.type in ("ui2", "ui4", "i4"):
            try:
                number = int(value)
            except ValueError:
                raise UpnpError(402, "Invalid Args") from None
            low = -(2**31) if self.type == "i4" else 0
            high = (
                self.maximum
                if self.maximum is not None
                else {"ui2": 65535, "ui4": 2**32 - 1, "i4": 2**31 - 1}[self.type]
            )
            if not low <= number <= high:
                raise UpnpError(402, "Invalid Args")
        if self.type == "boolean" and value not in (
            "0",
            "1",
            "true",
            "false",
            "yes",
            "no",
        ):
            raise UpnpError(402, "Invalid Args")


@dataclass(frozen=True)
class Action:
    """Argument names in their wire order, mapped to state variables."""

    inputs: dict[str, str] = field(default_factory=dict)
    outputs: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Service:
    """One advertised service with an executable action contract."""

    name: str
    variables: dict[str, Variable]
    actions: dict[str, Action]

    @property
    def type(self):
        return f"urn:schemas-upnp-org:service:{self.name}:1"

    def scpd(self):
        root = ET.Element("scpd", xmlns="urn:schemas-upnp-org:service-1-0")
        elements(ET.SubElement(root, "specVersion"), {"major": 1, "minor": 0})
        actions = ET.SubElement(root, "actionList")
        for name, action in self.actions.items():
            node = ET.SubElement(actions, "action")
            elements(node, {"name": name})
            arguments = ET.SubElement(node, "argumentList")
            for direction, mapping in (("in", action.inputs), ("out", action.outputs)):
                for argument, variable in mapping.items():
                    elements(
                        ET.SubElement(arguments, "argument"),
                        {
                            "name": argument,
                            "direction": direction,
                            "relatedStateVariable": variable,
                        },
                    )
        table = ET.SubElement(root, "serviceStateTable")
        for name, variable in self.variables.items():
            node = ET.SubElement(
                table, "stateVariable", sendEvents="yes" if variable.evented else "no"
            )
            elements(node, {"name": name, "dataType": variable.type})
            if variable.allowed:
                allowed = ET.SubElement(node, "allowedValueList")
                for value in variable.allowed:
                    elements(allowed, {"allowedValue": value})
            if variable.maximum is not None:
                elements(
                    ET.SubElement(node, "allowedValueRange"),
                    {"minimum": 0, "maximum": variable.maximum, "step": 1},
                )
        return xml(root)


INSTANCE = {"InstanceID": "A_ARG_TYPE_InstanceID"}
CHANNEL = {**INSTANCE, "Channel": "A_ARG_TYPE_Channel"}
AVT = Service(
    "AVTransport",
    {
        "A_ARG_TYPE_InstanceID": Variable("ui4"),
        "A_ARG_TYPE_SeekMode": Variable(allowed=("TRACK_NR", "REL_TIME", "ABS_TIME")),
        "A_ARG_TYPE_SeekTarget": Variable(),
        "TransportState": Variable(
            allowed=(
                "STOPPED",
                "PLAYING",
                "TRANSITIONING",
                "PAUSED_PLAYBACK",
                "NO_MEDIA_PRESENT",
            )
        ),
        "TransportStatus": Variable(allowed=("OK", "ERROR_OCCURRED")),
        "TransportPlaySpeed": Variable(allowed=("1",)),
        "PlaybackStorageMedium": Variable(allowed=("NONE", "NETWORK", "UNKNOWN")),
        "RecordStorageMedium": Variable(allowed=("NOT_IMPLEMENTED",)),
        "PossiblePlaybackStorageMedia": Variable(),
        "PossibleRecordStorageMedia": Variable(),
        "CurrentPlayMode": Variable(allowed=("NORMAL",)),
        "RecordMediumWriteStatus": Variable(allowed=("NOT_IMPLEMENTED",)),
        "CurrentRecordQualityMode": Variable(allowed=("NOT_IMPLEMENTED",)),
        "PossibleRecordQualityModes": Variable(),
        "NumberOfTracks": Variable("ui4", maximum=1),
        "CurrentTrack": Variable("ui4", maximum=1),
        **{
            name: Variable()
            for name in (
                "CurrentTrackDuration",
                "CurrentMediaDuration",
                "CurrentTrackMetaData",
                "CurrentTrackURI",
                "AVTransportURI",
                "AVTransportURIMetaData",
                "NextAVTransportURI",
                "NextAVTransportURIMetaData",
                "RelativeTimePosition",
                "AbsoluteTimePosition",
                "CurrentTransportActions",
            )
        },
        "RelativeCounterPosition": Variable("i4"),
        "AbsoluteCounterPosition": Variable("i4"),
        "LastChange": Variable(evented=True),
    },
    {
        "SetAVTransportURI": Action(
            {
                **INSTANCE,
                "CurrentURI": "AVTransportURI",
                "CurrentURIMetaData": "AVTransportURIMetaData",
            }
        ),
        "SetNextAVTransportURI": Action(
            {
                **INSTANCE,
                "NextURI": "NextAVTransportURI",
                "NextURIMetaData": "NextAVTransportURIMetaData",
            }
        ),
        "GetMediaInfo": Action(
            INSTANCE,
            {
                "NrTracks": "NumberOfTracks",
                "MediaDuration": "CurrentMediaDuration",
                "CurrentURI": "AVTransportURI",
                "CurrentURIMetaData": "AVTransportURIMetaData",
                "NextURI": "NextAVTransportURI",
                "NextURIMetaData": "NextAVTransportURIMetaData",
                "PlayMedium": "PlaybackStorageMedium",
                "RecordMedium": "RecordStorageMedium",
                "WriteStatus": "RecordMediumWriteStatus",
            },
        ),
        "GetTransportInfo": Action(
            INSTANCE,
            {
                "CurrentTransportState": "TransportState",
                "CurrentTransportStatus": "TransportStatus",
                "CurrentSpeed": "TransportPlaySpeed",
            },
        ),
        "GetPositionInfo": Action(
            INSTANCE,
            {
                "Track": "CurrentTrack",
                "TrackDuration": "CurrentTrackDuration",
                "TrackMetaData": "CurrentTrackMetaData",
                "TrackURI": "CurrentTrackURI",
                "RelTime": "RelativeTimePosition",
                "AbsTime": "AbsoluteTimePosition",
                "RelCount": "RelativeCounterPosition",
                "AbsCount": "AbsoluteCounterPosition",
            },
        ),
        "GetDeviceCapabilities": Action(
            INSTANCE,
            {
                "PlayMedia": "PossiblePlaybackStorageMedia",
                "RecMedia": "PossibleRecordStorageMedia",
                "RecQualityModes": "PossibleRecordQualityModes",
            },
        ),
        "GetTransportSettings": Action(
            INSTANCE,
            {
                "PlayMode": "CurrentPlayMode",
                "RecQualityMode": "CurrentRecordQualityMode",
            },
        ),
        "GetCurrentTransportActions": Action(
            INSTANCE, {"Actions": "CurrentTransportActions"}
        ),
        "Play": Action({**INSTANCE, "Speed": "TransportPlaySpeed"}),
        "Pause": Action(INSTANCE),
        "Stop": Action(INSTANCE),
        "Next": Action(INSTANCE),
        "Previous": Action(INSTANCE),
        "Seek": Action(
            {
                **INSTANCE,
                "Unit": "A_ARG_TYPE_SeekMode",
                "Target": "A_ARG_TYPE_SeekTarget",
            }
        ),
        "SetPlayMode": Action({**INSTANCE, "NewPlayMode": "CurrentPlayMode"}),
    },
)
RCS = Service(
    "RenderingControl",
    {
        "A_ARG_TYPE_InstanceID": Variable("ui4"),
        "A_ARG_TYPE_Channel": Variable(allowed=("Master",)),
        "A_ARG_TYPE_PresetName": Variable(allowed=("FactoryDefaults",)),
        "PresetNameList": Variable(),
        "Volume": Variable("ui2", maximum=100),
        "Mute": Variable("boolean"),
        "LastChange": Variable(evented=True),
    },
    {
        "ListPresets": Action(INSTANCE, {"CurrentPresetNameList": "PresetNameList"}),
        "SelectPreset": Action({**INSTANCE, "PresetName": "A_ARG_TYPE_PresetName"}),
        "GetVolume": Action(CHANNEL, {"CurrentVolume": "Volume"}),
        "SetVolume": Action({**CHANNEL, "DesiredVolume": "Volume"}),
        "GetMute": Action(CHANNEL, {"CurrentMute": "Mute"}),
        "SetMute": Action({**CHANNEL, "DesiredMute": "Mute"}),
    },
)
CM = Service(
    "ConnectionManager",
    {
        "SourceProtocolInfo": Variable(evented=True),
        "SinkProtocolInfo": Variable(evented=True),
        "CurrentConnectionIDs": Variable(evented=True),
        "A_ARG_TYPE_ConnectionID": Variable("i4"),
        "A_ARG_TYPE_RcsID": Variable("i4"),
        "A_ARG_TYPE_AVTransportID": Variable("i4"),
        "A_ARG_TYPE_ProtocolInfo": Variable(),
        "A_ARG_TYPE_ConnectionManager": Variable(),
        "A_ARG_TYPE_Direction": Variable(allowed=("Input",)),
        "A_ARG_TYPE_ConnectionStatus": Variable(allowed=("OK", "Unknown")),
    },
    {
        "GetProtocolInfo": Action(
            outputs={"Source": "SourceProtocolInfo", "Sink": "SinkProtocolInfo"}
        ),
        "GetCurrentConnectionIDs": Action(
            outputs={"ConnectionIDs": "CurrentConnectionIDs"}
        ),
        "GetCurrentConnectionInfo": Action(
            {"ConnectionID": "A_ARG_TYPE_ConnectionID"},
            {
                "RcsID": "A_ARG_TYPE_RcsID",
                "AVTransportID": "A_ARG_TYPE_AVTransportID",
                "ProtocolInfo": "A_ARG_TYPE_ProtocolInfo",
                "PeerConnectionManager": "A_ARG_TYPE_ConnectionManager",
                "PeerConnectionID": "A_ARG_TYPE_ConnectionID",
                "Direction": "A_ARG_TYPE_Direction",
                "Status": "A_ARG_TYPE_ConnectionStatus",
            },
        ),
    },
)
SERVICES = {service.name: service for service in (AVT, RCS, CM)}


def description(name, udn):
    root = ET.Element("root", xmlns="urn:schemas-upnp-org:device-1-0")
    elements(ET.SubElement(root, "specVersion"), {"major": 1, "minor": 0})
    device = ET.SubElement(root, "device")
    elements(
        device,
        {
            "deviceType": DEVICE_TYPE,
            "friendlyName": name,
            "manufacturer": "Kalinka",
            "modelName": "Kalinka UPnP Renderer",
            "UDN": udn,
        },
    )
    services = ET.SubElement(device, "serviceList")
    for service in SERVICES.values():
        elements(
            ET.SubElement(services, "service"),
            {
                "serviceType": service.type,
                "serviceId": f"urn:upnp-org:serviceId:{service.name}",
                "SCPDURL": f"/{service.name}/scpd.xml",
                "controlURL": f"/{service.name}/control",
                "eventSubURL": f"/{service.name}/event",
            },
        )
    return xml(root)


class Services:
    """Apply validated wire actions to the playback bridge and expose its state."""

    def __init__(self, playback: Playback):
        self.playback = playback
        self.dsd = False
        self._dsd_asked = 0

    async def refresh_dsd(self):
        """Ask whether the renderer outputs DSD; a change is evented to controllers."""
        self._dsd_asked += 1
        asked = self._dsd_asked
        try:
            dsd = (await self.playback.direct.output_capabilities()).dsd is True
        except Exception as exc:  # noqa: BLE001 - an older server lacks the call
            logger.warning("UPnP could not ask about DSD (%s)", type(exc).__name__)
            dsd = False
        # Answers can arrive out of order; one asked before a later question
        # may predate a change of DSD mode.
        if asked == self._dsd_asked and dsd != self.dsd:
            self.dsd = dsd
            self.playback.changed(CM.name)
        return dsd

    def snapshot(self, service):
        p = self.playback
        if service == RCS.name:
            return {
                "Volume": p.volume,
                "Mute": int(p.muted),
                "PresetNameList": "FactoryDefaults",
            }
        if service == CM.name:
            return {
                "SourceProtocolInfo": "",
                "SinkProtocolInfo": sink_protocol_info(self.dsd),
                "CurrentConnectionIDs": "0",
            }
        current, next_media = p.current, p.next
        duration = format_time(p.duration_ms) if current else "0:00:00"
        return {
            "TransportState": p.state,
            "TransportStatus": p.status,
            "TransportPlaySpeed": "1",
            "PlaybackStorageMedium": "NETWORK" if current else "NONE",
            "RecordStorageMedium": "NOT_IMPLEMENTED",
            "RecordMediumWriteStatus": "NOT_IMPLEMENTED",
            "PossiblePlaybackStorageMedia": "NETWORK",
            "PossibleRecordStorageMedia": "NOT_IMPLEMENTED",
            "CurrentPlayMode": "NORMAL",
            "CurrentRecordQualityMode": "NOT_IMPLEMENTED",
            "PossibleRecordQualityModes": "NOT_IMPLEMENTED",
            "NumberOfTracks": int(current is not None),
            "CurrentTrack": int(current is not None),
            "CurrentTrackDuration": duration,
            "CurrentMediaDuration": duration,
            "CurrentTrackMetaData": current.metadata if current else "",
            "AVTransportURIMetaData": current.metadata if current else "",
            "CurrentTrackURI": current.uri if current else "",
            "AVTransportURI": current.uri if current else "",
            "NextAVTransportURI": next_media.uri if next_media else "",
            "NextAVTransportURIMetaData": next_media.metadata if next_media else "",
            "RelativeTimePosition": format_time(p.position_now_ms()),
            "AbsoluteTimePosition": format_time(p.position_now_ms()),
            "RelativeCounterPosition": 2147483647,
            "AbsoluteCounterPosition": 2147483647,
            "CurrentTransportActions": p.actions,
        }

    def event(self, service):
        values = self.snapshot(service)
        root = ET.Element("e:propertyset", {"xmlns:e": EVENT})
        if service != CM.name:
            namespace = "AVT" if service == AVT.name else "RCS"
            last = ET.Element(
                "Event", xmlns=f"urn:schemas-upnp-org:metadata-1-0/{namespace}/"
            )
            instance = ET.SubElement(last, "InstanceID", val="0")
            for name, value in values.items():
                if name in (
                    "RelativeTimePosition",
                    "AbsoluteTimePosition",
                    "RelativeCounterPosition",
                    "AbsoluteCounterPosition",
                ):
                    continue
                attributes = {"val": str(value)}
                if name in ("Volume", "Mute"):
                    attributes["channel"] = "Master"
                ET.SubElement(instance, name, attributes)
            values = {"LastChange": ET.tostring(last, encoding="unicode")}
        for name, value in values.items():
            elements(ET.SubElement(root, "e:property"), {name: value})
        return xml(root)

    async def dispatch(self, service_name, name, arguments):
        service = SERVICES[service_name]
        action = service.actions.get(name)
        if action is None:
            raise UpnpError(401, "Invalid Action")
        if arguments.keys() != action.inputs.keys():
            raise UpnpError(402, "Invalid Args")
        for key, variable in action.inputs.items():
            service.variables[variable].validate(arguments[key])
        if "InstanceID" in arguments and int(arguments["InstanceID"]) != 0:
            raise UpnpError(
                718 if service_name == AVT.name else 702, "Invalid InstanceID"
            )
        if "Channel" in arguments and arguments["Channel"] != "Master":
            raise UpnpError(402, "Invalid Channel")

        p = self.playback
        if service_name == AVT.name:
            if name in ("SetAVTransportURI", "SetNextAVTransportURI"):
                prefix = "Current" if name == "SetAVTransportURI" else "Next"
                uri, metadata = (
                    arguments[f"{prefix}URI"],
                    arguments[f"{prefix}URIMetaData"],
                )
                media = Media.parse(uri, metadata) if uri else None
                if (
                    media
                    and media.source.format in DSD_FORMATS
                    and not await self.refresh_dsd()
                ):
                    logger.info("UPnP refused DSD: renderer DSD output is not on")
                    raise UpnpError(714, "DSD output is not on at the renderer")
                await p.command("set_uri" if prefix == "Current" else "set_next", media)
            elif name == "Play":
                if arguments["Speed"] != "1":
                    raise UpnpError(717, "Play speed not supported")
                await p.command("play")
            elif name in ("Pause", "Stop", "Next", "Previous"):
                await p.command(name.lower())
            elif name == "Seek":
                if arguments["Unit"] not in ("TRACK_NR", "REL_TIME", "ABS_TIME"):
                    raise UpnpError(710, "Seek mode not supported")
                try:
                    if arguments["Unit"] == "TRACK_NR":
                        if int(arguments["Target"]) != 1:
                            raise ValueError
                        target = 0
                    else:
                        target = parse_time(arguments["Target"])
                except ValueError:
                    raise UpnpError(711, "Illegal seek target") from None
                await p.command("seek", target)
            elif name == "SetPlayMode" and arguments["NewPlayMode"] != "NORMAL":
                raise UpnpError(712, "Play mode not supported")
        elif service_name == RCS.name:
            if name == "SetVolume":
                await p.command("set_volume", int(arguments["DesiredVolume"]))
            elif name == "SetMute":
                await p.command(
                    "set_mute", arguments["DesiredMute"] in ("1", "true", "yes")
                )
            elif (
                name == "SelectPreset" and arguments["PresetName"] != "FactoryDefaults"
            ):
                raise UpnpError(701, "Invalid Name")
        elif name == "GetProtocolInfo":
            await self.refresh_dsd()
        elif name == "GetCurrentConnectionInfo":
            if int(arguments["ConnectionID"]) != 0:
                raise UpnpError(706, "Invalid connection reference")
            return {
                "RcsID": 0,
                "AVTransportID": 0,
                "ProtocolInfo": f"http-get:*:{p.current.source.format}:*"
                if p.current
                else "",
                "PeerConnectionManager": "",
                "PeerConnectionID": -1,
                "Direction": "Input",
                "Status": "OK",
            }
        values = self.snapshot(service_name)
        return {
            argument: values[variable] for argument, variable in action.outputs.items()
        }


def soap_response(service, action, values):
    root = ET.Element(
        "s:Envelope",
        {
            "xmlns:s": SOAP,
            "s:encodingStyle": "http://schemas.xmlsoap.org/soap/encoding/",
        },
    )
    body = ET.SubElement(root, "s:Body")
    elements(
        ET.SubElement(body, f"u:{action}Response", {"xmlns:u": service.type}), values
    )
    return xml(root)


def soap_fault(error):
    root = ET.Element("s:Envelope", {"xmlns:s": SOAP})
    fault = ET.SubElement(ET.SubElement(root, "s:Body"), "s:Fault")
    elements(fault, {"faultcode": "s:Client", "faultstring": "UPnPError"})
    detail = ET.SubElement(
        ET.SubElement(fault, "detail"),
        "UPnPError",
        xmlns="urn:schemas-upnp-org:control-1-0",
    )
    elements(detail, {"errorCode": error.code, "errorDescription": error.description})
    return xml(root)
