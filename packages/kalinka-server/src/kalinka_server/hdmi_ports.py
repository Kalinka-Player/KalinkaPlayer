"""The machine's HDMI ports as the kernel sees them, for a log export.

A Raspberry Pi refuses HDMI audio on a port whose sink does not declare HDMI
in its EDID, so a port's state tells an unplugged or sleeping receiver apart
from a fault in the player.
"""

import os
import re
from typing import Any, Dict, List, Optional

DRM_CLASS = "/sys/class/drm"

_CONNECTOR_RE = re.compile(r"card\d+-HDMI-[A-Z]-\d+")
_BLOCK = 128
_DESCRIPTORS = range(54, 126, 18)
_SINK_NAME = 0xFC
_CTA_EXTENSION = 0x02
_BASIC_AUDIO = 0x40
_AUDIO_DATA = 1
_VENDOR_DATA = 3
_HDMI_OUI = b"\x03\x0c\x00"


def _read(path: str) -> bytes:
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return b""


def _text(path: str) -> Optional[str]:
    return _read(path).decode("ascii", "replace").strip() or None


def _sink_name(edid: bytes) -> Optional[str]:
    for start in _DESCRIPTORS:
        descriptor = edid[start : start + 18]
        if descriptor[:3] == b"\x00\x00\x00" and descriptor[3] == _SINK_NAME:
            return descriptor[5:].split(b"\n")[0].decode("ascii", "replace").strip()
    return None


def describe_edid(edid: bytes) -> Dict[str, Any]:
    """The sink's name, and whether it takes HDMI (not DVI) and audio, decided
    as the kernel's drm_detect_hdmi_monitor() and drm_detect_monitor_audio()
    decide them."""
    if len(edid) < _BLOCK:
        return {"sink": None, "hdmi": False, "audio": False}
    hdmi = audio = False
    for start in range(_BLOCK, len(edid) - _BLOCK + 1, _BLOCK):
        block = edid[start : start + _BLOCK]
        if block[0] != _CTA_EXTENSION:
            continue
        audio = audio or bool(block[3] & _BASIC_AUDIO)
        end = block[2] if 4 <= block[2] < _BLOCK else 4
        at = 4
        while at < end:
            tag, size = block[at] >> 5, block[at] & 0x1F
            payload = block[at + 1 : at + 1 + size]
            audio = audio or (tag == _AUDIO_DATA and size > 0)
            hdmi = hdmi or (
                tag == _VENDOR_DATA and size >= 5 and payload[:3] == _HDMI_OUI
            )
            at += 1 + size
    return {"sink": _sink_name(edid), "hdmi": hdmi, "audio": audio}


def hdmi_ports(root: str = DRM_CLASS) -> List[Dict[str, Any]]:
    """Every HDMI connector: whether a sink is plugged in, whether the display
    drives it, and what the sink's EDID declares; empty without DRM."""
    try:
        names = sorted(
            name for name in os.listdir(root) if _CONNECTOR_RE.fullmatch(name)
        )
    except OSError:
        return []
    ports = []
    for name in names:
        path = os.path.join(root, name)
        edid = _read(os.path.join(path, "edid"))
        port: Dict[str, Any] = {
            "connector": name,
            "status": _text(os.path.join(path, "status")),
            "enabled": _text(os.path.join(path, "enabled")),
            "edid_bytes": len(edid),
        }
        if edid:
            port.update(describe_edid(edid))
        ports.append(port)
    return ports
