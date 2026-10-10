import re
from pathlib import Path

import pytest
from kalinka_plugin_upnp.media import (
    DSD_FORMATS,
    MIME_TYPES,
    Media,
    UpnpError,
    format_time,
    parse_time,
    sink_protocol_info,
)

NATIVE_PLAYER = Path(__file__).resolve().parents[2].joinpath(
    "kalinka-renderer", "src", "player", "NativePlayer.cpp"
)
DSD_TYPES = [
    "audio/x-dsf",
    "audio/dsf",
    "audio/x-dff",
    "audio/dff",
    "audio/dsd",
    "audio/x-dsd",
]
DIDL = """<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/"
 xmlns:dc="http://purl.org/dc/elements/1.1/"
 xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">
 <item id="42" parentID="0" restricted="1">
  <dc:title>Title &amp; more</dc:title><upnp:artist>Artist</upnp:artist>
  <upnp:album>Album</upnp:album><upnp:albumArtURI>http://media.test/art.jpg</upnp:albumArtURI>
  <res protocolInfo="http-get:*:audio/mpeg:*" duration="0:01:00">http://media.test/other.mp3</res>
  <res protocolInfo="http-get:*:audio/flac:*" duration="1:02:03.125">http://media.test/audio?key=secret&amp;id=42</res>
 </item>
</DIDL-Lite>"""


@pytest.fixture(scope="module")
def format_of():
    """The body of the renderer's formatOf, which picks a decoder per source."""
    source = NATIVE_PLAYER.read_text()
    start = source.index("AudioFormat formatOf(")
    return source[start : source.index("\n}\n", start)]


@pytest.fixture(scope="module")
def renderer_formats(format_of):
    formats = dict(re.findall(r'\{"([^"]+)",\s*AudioFormat::(\w+)\}', format_of))
    assert formats
    return formats


def test_didl_uses_the_matching_resource_and_maps_metadata():
    media = Media.parse("http://media.test/audio?key=secret&id=42", DIDL)
    assert media.source.format == "audio/flac"
    assert media.source.source.url == media.uri
    assert media.source.sequential is False
    assert media.track.title == "Title & more"
    assert media.track.performer.name == "Artist"
    assert media.track.album.title == "Album"
    assert media.track.album.image.large == "http://media.test/art.jpg"
    assert media.duration_ms == 3723125
    assert media.track.duration == 3723
    assert media.track.id.source == "upnp"
    assert "secret" not in str(media.track.id)
    assert media.metadata == DIDL


@pytest.mark.parametrize("declared", DSD_TYPES)
def test_declared_dsd_reaches_the_renderer_as_dsd(declared, renderer_formats):
    metadata = DIDL.replace("audio/flac", declared)
    media = Media.parse("http://media.test/audio?key=secret&id=42", metadata)
    assert renderer_formats[media.source.format] == "FormatDsd"


def test_dsd_is_advertised_only_while_the_renderer_outputs_it():
    advertised = {
        dsd: {entry.split(":")[2] for entry in sink_protocol_info(dsd).split(",")}
        for dsd in (False, True)
    }
    assert advertised[True] == MIME_TYPES.keys()
    assert advertised[True] - advertised[False] == set(DSD_TYPES)


def test_every_type_the_renderer_plays_as_dsd_waits_for_dsd_output(
    renderer_formats,
):
    sent_as_dsd = {
        sent for sent in MIME_TYPES.values() if renderer_formats[sent] == "FormatDsd"
    }
    assert sent_as_dsd == DSD_FORMATS


def test_every_type_sent_to_the_renderer_selects_the_declared_decoder(
    renderer_formats,
):
    for declared, sent in MIME_TYPES.items():
        assert sent in renderer_formats, sent
        if declared in renderer_formats:
            assert renderer_formats[sent] == renderer_formats[declared], declared


def test_every_type_the_renderer_decodes_is_accepted(renderer_formats):
    assert {name for name in renderer_formats if "/" in name} <= MIME_TYPES.keys()


def test_every_url_suffix_the_renderer_decodes_is_accepted(
    format_of, renderer_formats
):
    suffixes = re.findall(r'ends_with\("\.(\w+)"\)', format_of)
    assert suffixes
    for suffix in suffixes:
        media = Media.parse(f"http://media.test/a.{suffix}", "")
        assert media.source.format in renderer_formats, suffix


@pytest.mark.parametrize(
    "extension,format",
    [
        ("MP3", "audio/mpeg"),
        ("flac", "audio/flac"),
        ("WAV", "audio/wav"),
        ("wave", "audio/wav"),
        ("ogg", "audio/ogg"),
        ("oga", "audio/ogg"),
        ("dsf", "audio/x-dsf"),
        ("DFF", "audio/x-dff"),
    ],
)
def test_missing_metadata_uses_url_path(extension, format):
    media = Media.parse(f"https://media.test/A%20song.{extension}?x=.wav", "")
    assert media.track.title == f"A song.{extension}"
    assert media.source.format == format
    assert media.duration_ms == 0


@pytest.mark.parametrize(
    "uri",
    [
        "file:///tmp/a.flac",
        "ftp://media.test/a.flac",
        "http:///a.flac",
        "http://media.test:bad/a.flac",
        "http://media.test:/a.flac",
        "http://media.test/a.flac\n",
    ],
)
def test_invalid_sources_are_rejected(uri):
    with pytest.raises(UpnpError) as error:
        Media.parse(uri, "")
    assert error.value.code == 716


@pytest.mark.parametrize(
    "metadata",
    ["<broken", "<item/>", '<!DOCTYPE x [<!ENTITY x "expansion">]><x>&x;</x>'],
)
def test_invalid_or_unsafe_metadata_is_rejected(metadata):
    with pytest.raises(UpnpError) as error:
        Media.parse("http://media.test/a.flac", metadata)
    assert error.value.code == 402


def test_unsupported_declared_format_does_not_fall_back_to_extension():
    metadata = DIDL.replace(
        "http://media.test/audio?key=secret&amp;id=42", "http://media.test/a.flac"
    ).replace("audio/flac", "audio/aac")
    with pytest.raises(UpnpError) as error:
        Media.parse("http://media.test/a.flac", metadata)
    assert error.value.code == 714


def test_extensionless_url_needs_a_matching_resource_type():
    with pytest.raises(UpnpError):
        Media.parse("http://media.test/unknown", DIDL)


@pytest.mark.parametrize(
    "value", ["-1:00:00", "1:60:00", "0:00:61", "1", "nan", "0:00:01.1234"]
)
def test_invalid_time(value):
    with pytest.raises(ValueError):
        parse_time(value)


def test_time_conversion():
    assert parse_time("123:45:56.1") == 445556100
    assert format_time(445556100) == "123:45:56"
