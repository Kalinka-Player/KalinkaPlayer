"""DSDIFF's native title/artist fields, which Mutagen's ID3 reader omits."""

import struct
from typing import BinaryIO


def native_dsdiff_tags(audio: BinaryIO) -> dict[str, str]:
    """Read DIIN text without loading the audio payload (also on shares)."""
    audio.seek(0, 2)
    file_size = audio.tell()
    audio.seek(0)
    header = audio.read(16)
    if len(header) != 16 or header[:4] != b"FRM8" or header[12:] != b"DSD ":
        return {}
    end = min(file_size, 12 + struct.unpack(">Q", header[4:12])[0])

    def chunks(start, end):
        at = start
        while at + 12 <= end:
            audio.seek(at)
            header = audio.read(12)
            if len(header) != 12:
                return
            size = struct.unpack(">Q", header[4:])[0]
            if size > end - at - 12:
                return
            yield header[:4], at + 12, size
            at += 12 + size + size % 2

    result = {}
    for name, start, size in chunks(16, end):
        if name != b"DIIN":
            continue
        for field, offset, length in chunks(start, start + size):
            if field not in (b"DIAR", b"DITI") or not 4 <= length <= 1024 * 1024:
                continue
            audio.seek(offset)
            data = audio.read(length)
            if len(data) != length:
                continue
            text_size = struct.unpack(">I", data[:4])[0]
            if text_size <= length - 4:
                # Native DSDIFF text is ASCII; replacement preserves the
                # indexer's normal repair path for nonconforming encoders.
                result[field.decode("ascii")] = (
                    data[4 : 4 + text_size].rstrip(b"\x00").decode("utf-8", "replace")
                )
    return result
