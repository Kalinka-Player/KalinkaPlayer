"""Read WAV tags with the same PCM limits as the renderer.

Mutagen reports the container width for extensible WAV. Use its valid bit
count for library precision, and reject encodings the renderer cannot play.
"""

import struct
from typing import BinaryIO

from mutagen.wave import WAVE

_PCM_GUID = bytes.fromhex("0100000000001000800000aa00389b71")
_MAX_HEADER_BYTES = 64 << 20


class UnsupportedWave(ValueError):
    """A WAV the renderer cannot play: malformed, or an encoding it lacks."""


def open_pcm_wave(audio: BinaryIO) -> WAVE:
    audio.seek(0)
    header = audio.read(12)
    if len(header) != 12 or header[:4] != b"RIFF" or header[8:] != b"WAVE":
        raise UnsupportedWave("Expected RIFF/WAVE")
    end = struct.unpack_from("<I", header, 4)[0] + 8
    bits = None
    block_align = 0
    while audio.tell() + 8 <= end:
        chunk = audio.read(8)
        if len(chunk) != 8:
            raise UnsupportedWave("Truncated WAV header")
        name, size = struct.unpack("<4sI", chunk)
        start = audio.tell()
        if start + size > end:
            raise UnsupportedWave("Invalid WAV chunk size")
        if name == b"fmt ":
            if bits is not None or size < 16:
                raise UnsupportedWave("Invalid WAV format chunk")
            fmt = audio.read(min(size, 40))
            if len(fmt) != min(size, 40):
                raise UnsupportedWave("Truncated WAV format")
            encoding, channels, rate, byte_rate, block_align, width = struct.unpack(
                "<HHIIHH", fmt[:16]
            )
            bits = width
            if encoding == 0xFFFE:
                if size < 40:
                    raise UnsupportedWave("Invalid extensible WAV format")
                extra, bits, mask = struct.unpack_from("<HHI", fmt, 16)
                if not 22 <= extra <= size - 18 or fmt[25:40] != _PCM_GUID[1:]:
                    raise UnsupportedWave("Unsupported WAV encoding")
                encoding = fmt[24]
                if mask not in (0, 4 if channels == 1 else 3):
                    raise UnsupportedWave("Unsupported WAV channel layout")
            if encoding not in (1, 3) or channels not in (1, 2):
                raise UnsupportedWave("WAV supports mono/stereo PCM only")
            if encoding == 3:
                if bits not in (32, 64) or bits != width:
                    raise UnsupportedWave("WAV float samples must be 32 or 64 bits")
            elif bits not in (16, 24, 32) or width not in (16, 24, 32) or bits > width:
                raise UnsupportedWave("Unsupported WAV container width")
            if (
                rate == 0
                or block_align != channels * (width // 8)
                or byte_rate != rate * block_align
            ):
                raise UnsupportedWave("Invalid WAV sample rate or block alignment")
        elif name == b"data":
            if bits is None or size % block_align:
                raise UnsupportedWave("Invalid WAV audio data")
            data_end = start + size
            audio.seek(0, 2)
            if data_end > audio.tell():
                raise UnsupportedWave("Truncated WAV audio data")
            audio.seek(0)
            wav = WAVE(audio)
            wav.info.bits_per_sample = bits
            wav.info.audio_format = encoding
            return wav
        next_chunk = start + size + (size & 1)
        if next_chunk > end:
            raise UnsupportedWave("Invalid WAV chunk size")
        if next_chunk > _MAX_HEADER_BYTES:
            raise UnsupportedWave("WAV header is too large")
        audio.seek(next_chunk)
    raise UnsupportedWave("Missing WAV audio data")
