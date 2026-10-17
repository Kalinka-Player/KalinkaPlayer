"""Regenerate deterministic segmented FLAC fixtures with FFmpeg and ffprobe."""

import json
from pathlib import Path
import subprocess
import tempfile
import wave
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parent
NS = "urn:mpeg:dash:schema:mpd:2011"
ET.register_namespace("", NS)


def segment(source, destination, codec, frames):
    """Splits `source` into a DASH representation under `destination`, with
    a timeline built from the packets actually written."""
    destination.mkdir(exist_ok=True)
    for old in destination.glob("*.m4s"):
        old.unlink()
    subprocess.run([
        "ffmpeg", "-v", "error", "-y", "-i", str(source), *codec,
        "-f", "dash", "-seg_duration", "0.25", str(destination / "manifest.mpd"),
    ], check=True)
    manifest = ET.parse(destination / "manifest.mpd")
    manifest.getroot().set("mediaPresentationDuration", "PT1.25S")
    timeline = manifest.find(f".//{{{NS}}}SegmentTimeline")
    timeline.clear()
    position = 0
    with tempfile.TemporaryDirectory() as temporary:
        for chunk in sorted(destination.glob("chunk-*.m4s")):
            fragment = Path(temporary) / "fragment.mp4"
            fragment.write_bytes(
                (destination / "init-stream0.m4s").read_bytes() + chunk.read_bytes()
            )
            probe = subprocess.run([
                "ffprobe", "-v", "error", "-ignore_editlist", "1",
                "-show_entries", "packet=duration",
                "-of", "json", str(fragment),
            ], check=True, capture_output=True, text=True)
            duration = sum(p["duration"] for p in json.loads(probe.stdout)["packets"])
            if not duration:
                chunk.unlink()
                continue
            ET.SubElement(timeline, f"{{{NS}}}S", t=str(position), d=str(duration))
            position += duration
    assert position == frames, (destination, position)
    ET.indent(manifest)
    manifest.write(destination / "manifest.mpd", encoding="utf-8", xml_declaration=True)


def generate(bits, rate):
    with tempfile.TemporaryDirectory() as temporary:
        wav = Path(temporary) / "samples.wav"
        pcm = bytearray()
        for frame in range(rate * 5 // 4):
            for channel in range(2):
                value = ((frame % 503) - 251) * 57 * (-1 if channel else 1)
                if bits == 24:
                    value = value * 256 + frame % 127 - 63
                pcm.extend(value.to_bytes(bits // 8, "little", signed=True))
        with wave.open(str(wav), "wb") as output:
            output.setparams((2, bits // 8, rate, 0, "NONE", "not compressed"))
            output.writeframes(pcm)
        segment(wav, ROOT / str(bits), ["-c:a", "flac", "-strict", "-2"],
                rate * 5 // 4)


if __name__ == "__main__":
    generate(16, 44100)
    generate(24, 96000)
    # The AAC fixture's own packets, so every container decodes alike; run
    # ../aac/generate.py first. 55 units of 1024 frames, priming included,
    # less the 171 frames of padding the last unit's duration trims.
    segment(ROOT.parent / "aac" / "tones.m4a", ROOT / "aac", ["-c:a", "copy"],
            55 * 1024 - 171)
