"""UPnP media descriptions mapped to renderer sources and Kalinka metadata."""

import hashlib
import re
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree as ET

from defusedxml.common import DefusedXmlException
from defusedxml.ElementTree import fromstring
from kalinka_plugin_sdk.datamodel import Album, Artist, CoverImage, EntityId, Track
from kalinka_plugin_sdk.inputmodule import DirectUrl, TrackSource

DIDL = "urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/"
DC = "http://purl.org/dc/elements/1.1/"
UPNP = "urn:schemas-upnp-org:metadata-1-0/upnp/"
MIME_TYPES = {
    "audio/mpeg": "audio/mpeg",
    "audio/mp3": "audio/mpeg",
    "audio/x-mp3": "audio/mpeg",
    "audio/x-mpeg": "audio/mpeg",
    "audio/mpeg3": "audio/mpeg",
    "audio/x-mpeg-3": "audio/mpeg",
    "audio/flac": "audio/flac",
    "audio/x-flac": "audio/flac",
    "application/x-flac": "audio/flac",
    "audio/ogg": "audio/ogg",
    "audio/x-ogg": "audio/ogg",
    "application/ogg": "audio/ogg",
    "application/x-ogg": "audio/ogg",
    "audio/vorbis": "audio/ogg",
    "audio/x-vorbis": "audio/ogg",
    "audio/x-vorbis+ogg": "audio/ogg",
    "audio/dsf": "audio/x-dsf",
    "audio/x-dsf": "audio/x-dsf",
    "audio/dff": "audio/x-dff",
    "audio/x-dff": "audio/x-dff",
    "audio/dsd": "audio/dsd",
    "audio/x-dsd": "audio/dsd",
}
DSD_FORMATS = frozenset({"audio/x-dsf", "audio/x-dff", "audio/dsd"})
EXTENSION_TYPES = {
    "mp3": "audio/mpeg",
    "flac": "audio/flac",
    "ogg": "audio/ogg",
    "oga": "audio/ogg",
    "dsf": "audio/x-dsf",
    "dff": "audio/x-dff",
}


def sink_protocol_info(dsd: bool) -> str:
    """The SinkProtocolInfo to advertise; DSD only while the renderer outputs it."""
    return ",".join(
        f"http-get:*:{mime}:*"
        for mime, sent in MIME_TYPES.items()
        if dsd or sent not in DSD_FORMATS
    )


class UpnpError(Exception):
    """A protocol fault whose description is safe to return to a controller."""

    def __init__(self, code: int, description: str):
        super().__init__(description)
        self.code = code
        self.description = description


def parse_time(value: str) -> int:
    """Parse UPnP hours:minutes:seconds, returning milliseconds."""
    match = re.fullmatch(r"(\d{1,6}):([0-5]\d):([0-5]\d)(?:\.(\d{1,3}))?", value)
    if not match:
        raise ValueError("Invalid time")
    hours, minutes, seconds, fraction = match.groups()
    return (int(hours) * 3600 + int(minutes) * 60 + int(seconds)) * 1000 + int(
        (fraction or "0").ljust(3, "0")
    )


def format_time(milliseconds: int) -> str:
    seconds = max(0, milliseconds // 1000)
    return f"{seconds // 3600}:{seconds // 60 % 60:02}:{seconds % 60:02}"


def entity(kind: str, value: str) -> EntityId:
    digest = hashlib.sha256(value.encode()).hexdigest()[:32]
    return EntityId(source="upnp", type=kind, id=digest)


@dataclass(frozen=True)
class Media:
    """One controller-owned HTTP resource, with its original DIDL metadata."""

    uri: str
    metadata: str
    source: TrackSource
    track: Track
    duration_ms: int

    @classmethod
    def parse(cls, uri: str, metadata: str) -> "Media":
        try:
            url = urlsplit(uri)
            if (
                url.scheme not in ("http", "https")
                or not url.hostname
                or url.port == 0
                or url.netloc.endswith(":")
            ):
                raise ValueError
            if any(ord(char) < 33 for char in uri):
                raise ValueError
        except ValueError:
            raise UpnpError(716, "Resource not found") from None

        item = resource = None
        if metadata:
            try:
                root = fromstring(metadata)
                if root.tag != f"{{{DIDL}}}DIDL-Lite":
                    raise ValueError
                items = root.findall(f"{{{DIDL}}}item")
                if len(items) != 1:
                    raise ValueError
                item = items[0]
                resource = next(
                    (
                        res
                        for res in item.findall(f"{{{DIDL}}}res")
                        if (res.text or "").strip() == uri
                    ),
                    None,
                )
            except (ET.ParseError, DefusedXmlException, ValueError):
                raise UpnpError(402, "Invalid metadata") from None

        mime = ""
        duration_ms = 0
        if resource is not None:
            protocol = resource.get("protocolInfo", "").split(":", 3)
            if len(protocol) == 4:
                if protocol[0] != "http-get":
                    raise UpnpError(714, "Illegal MIME-type")
                mime = protocol[2].split(";", 1)[0].strip().lower()
            try:
                duration_ms = parse_time(resource.get("duration", ""))
            except ValueError:
                pass
        if mime in ("", "*", "application/octet-stream"):
            extension = unquote(url.path).rsplit(".", 1)[-1].lower()
            mime = EXTENSION_TYPES.get(extension, "")
        if mime not in MIME_TYPES:
            raise UpnpError(714, "Illegal MIME-type")

        def value(namespace, name):
            return (
                (item.findtext(f"{{{namespace}}}{name}") or "").strip()
                if item is not None
                else ""
            )

        title = (
            value(DC, "title") or unquote(url.path.rsplit("/", 1)[-1]) or "UPnP stream"
        )
        artist_name = value(UPNP, "artist") or value(DC, "creator")
        artist = (
            Artist(id=entity("artist", artist_name), name=artist_name)
            if artist_name
            else None
        )
        album_title = value(UPNP, "album") or "UPnP"
        art = value(UPNP, "albumArtURI")
        try:
            art_url = urlsplit(art)
            if art_url.scheme not in ("http", "https") or not art_url.hostname:
                art = ""
        except ValueError:
            art = ""
        album = Album(
            id=entity("album", f"{artist_name}\n{album_title}"),
            title=album_title,
            artist=artist,
            image=CoverImage(small=art, thumbnail=art, large=art) if art else None,
        )
        track = Track(
            id=entity("track", uri),
            title=title,
            duration=duration_ms // 1000,
            performer=artist,
            album=album,
        )
        return cls(
            uri,
            metadata,
            TrackSource(source=DirectUrl(url=uri), format=MIME_TYPES[mime]),
            track,
            duration_ms,
        )
