"""Regenerate the AAC and MP4 fixtures: one AAC-LC encode in every container."""

from pathlib import Path
import math
import subprocess
import tempfile
import wave


ROOT = Path(__file__).resolve().parent
RATE = 44100
FRAMES = RATE * 5 // 4


def ffmpeg(*args):
    subprocess.run(["ffmpeg", "-v", "error", "-y", *map(str, args)], check=True)


def write_wav(path, pcm):
    with wave.open(str(path), "wb") as output:
        output.setparams((2, 2, RATE, 0, "NONE", "not compressed"))
        output.writeframes(pcm)


def tones():
    pcm = bytearray()
    for frame in range(FRAMES):
        for channel, frequency in enumerate((440, 660)):
            value = int(12000 * math.sin(2 * math.pi * frequency * frame / RATE))
            pcm.extend(value.to_bytes(2, "little", signed=True))
    return pcm


def ramp():
    """The PCM the FLAC-in-MP4 fixture must decode to, bit for bit; the tests
    compute the same ramp."""
    pcm = bytearray()
    for frame in range(FRAMES):
        for channel in range(2):
            value = ((frame % 503) - 251) * 57 * (-1 if channel else 1)
            pcm.extend(value.to_bytes(2, "little", signed=True))
    return pcm


def main():
    with tempfile.TemporaryDirectory() as temporary:
        source = Path(temporary) / "tones.wav"
        write_wav(source, tones())
        # One encode; every other AAC fixture copies its packets unchanged.
        ffmpeg("-i", source, "-c:a", "aac", "-b:a", "128k", "-movflags",
               "+faststart", ROOT / "tones.m4a")
        copy = ("-i", ROOT / "tones.m4a", "-c:a", "copy")
        ffmpeg(*copy, "-f", "adts", ROOT / "tones.aac")
        ffmpeg(*copy, ROOT / "tones-index-last.m4a")
        fragmented = ("-movflags", "+frag_keyframe+empty_moov+default_base_moof",
                      "-frag_duration", "250000")
        ffmpeg(*copy, *fragmented, ROOT / "tones-fragmented.m4a")
        ffmpeg(*copy, fragmented[0], fragmented[1] + "+global_sidx",
               *fragmented[2:], ROOT / "tones-fragmented-sidx.m4a")

        ramp_source = Path(temporary) / "ramp.wav"
        write_wav(ramp_source, ramp())
        ffmpeg("-i", ramp_source, "-c:a", "flac", "-strict", "-2", "-movflags",
               "+faststart", ROOT / "ramp-flac.mp4")
        ffmpeg("-i", ramp_source, "-t", "0.1", "-c:a", "alac", ROOT / "alac.m4a")


if __name__ == "__main__":
    main()
