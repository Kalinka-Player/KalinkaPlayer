"""Regenerate mono/HE-AAC radio tones (ffmpeg with full libfdk_aac needed)."""

from pathlib import Path
import tempfile

from generate import ROOT, ffmpeg, tones, write_wav


def main():
    with tempfile.TemporaryDirectory() as temporary:
        source = Path(temporary) / "tones.wav"
        write_wav(source, tones())
        ffmpeg("-i", source, "-ac", "1", "-c:a", "aac", "-b:a", "64k",
               ROOT / "tones-mono.aac")
        encoded = Path(temporary) / "stereo.aac"
        ffmpeg("-i", source, "-c:a", "libfdk_aac", "-profile:a", "aac_he_v2",
               "-b:a", "64k", encoded)
        data = encoded.read_bytes()
        # Join between periodic parametric-stereo headers, like live radio.
        # faad2 emits mono until the next PS header, then stereo at the same
        # rate and with the same number of frames per access unit.
        at = 0
        for _ in range(3):
            at += ((data[at + 3] & 3) << 11) | (data[at + 4] << 3) | (data[at + 5] >> 5)
        (ROOT / "tones-ps-joined.aac").write_bytes(data[at:])


if __name__ == "__main__":
    main()
