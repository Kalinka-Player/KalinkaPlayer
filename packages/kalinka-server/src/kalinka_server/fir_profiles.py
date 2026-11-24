"""Validate filter impulse WAVs before publishing a multi-rate convolution preset."""

import struct

import numpy as np
from google.protobuf.message import DecodeError

from .renderer_proto import renderer_pb2 as pb

MAX_PROFILE_BYTES = 32 * 1024 * 1024


def impulse_info(wav: bytes) -> tuple[int, int, int]:
    """Return sample rate, channels and tap count for renderer-compatible WAVs."""
    if len(wav) < 12 or wav[:4] != b"RIFF" or wav[8:12] != b"WAVE":
        raise ValueError("Choose a RIFF/WAVE filter impulse")
    end = int.from_bytes(wav[4:8], "little") + 8
    if end > len(wav):
        raise ValueError("Incomplete impulse WAV")
    pos, fmt = 12, None
    while pos + 8 <= end:
        tag, size = struct.unpack_from("<4sI", wav, pos)
        pos += 8
        if pos + size > end:
            raise ValueError("Incomplete WAV chunk")
        if tag == b"fmt ":
            if fmt is not None or size < 16:
                raise ValueError("Invalid WAV format")
            codec, channels, rate, byte_rate, stride, width = struct.unpack_from(
                "<HHIIHH", wav, pos
            )
            bits = width
            if codec == 0xFFFE:
                if size < 40:
                    raise ValueError("Incomplete extensible WAV format")
                extra, bits, mask = struct.unpack_from("<HHI", wav, pos + 16)
                guid = wav[pos + 24 : pos + 40]
                if (
                    not 22 <= extra <= size - 18
                    or guid[1:] != bytes.fromhex("00000000001000800000aa00389b71")
                    or mask not in (0, 4 if channels == 1 else 3)
                ):
                    raise ValueError("Unsupported extensible WAV format")
                codec = guid[0]
            if not (
                (
                    codec == 1
                    and bits in (16, 24, 32)
                    and width in (16, 24, 32)
                    and bits <= width
                )
                or (codec == 3 and width == bits and bits in (32, 64))
            ):
                raise ValueError("Use 16/24/32-bit PCM or 32/64-bit float WAV")
            if (
                channels not in (1, 2)
                or not rate
                or stride != channels * width // 8
                or byte_rate != rate * stride
            ):
                raise ValueError(
                    "Use a mono or stereo impulse with a valid sample rate"
                )
            fmt = (rate, channels, stride, codec, width)
        elif tag == b"data":
            if fmt is None:
                raise ValueError("WAV data precedes its format")
            rate, channels, stride, codec, width = fmt
            frames, remainder = divmod(size, stride)
            if remainder or not 1 <= frames <= 262144:
                raise ValueError("Each WAV must contain 1 to 262,144 complete frames")
            if codec == 3:
                values = np.frombuffer(
                    wav, dtype=f"<f{width // 8}", count=frames * channels, offset=pos
                )
                if not np.isfinite(values).all():
                    raise ValueError("The impulse contains non-finite coefficients")
            return rate, channels, frames
        pos += size + (size & 1)
        if pos > 1024 * 1024:
            raise ValueError("WAV header is too large")
    raise ValueError("Missing WAV audio data")


def read_profile(data):
    profile = pb.FirProfile()
    try:
        profile.ParseFromString(bytes(data))
    except DecodeError as exc:
        raise ValueError("Invalid FIR profile upload") from exc
    name = profile.name.strip()
    if not name or not name.isprintable() or len(name.encode()) > 120:
        raise ValueError("Use a printable filter name of at most 120 UTF-8 bytes")
    if not 1 <= len(profile.impulse_wav) <= 8:
        raise ValueError("Choose one to eight impulse WAVs, one per sample rate")
    variants = {}
    for wav in profile.impulse_wav:
        rate, _, _ = impulse_info(wav)
        if rate in variants:
            raise ValueError(f"Two impulses use {rate} Hz. Choose one per sample rate")
        variants[rate] = wav
    ordered = sorted(variants)
    payload = pb.FirProfile(name=name, impulse_wav=[variants[r] for r in ordered])
    rates = ", ".join(f"{r / 1000:g}" for r in ordered) + " kHz"
    return name, rates, payload.SerializeToString(deterministic=True)
