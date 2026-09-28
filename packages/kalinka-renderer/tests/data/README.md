# Decoder test fixtures

`ladder.flac` / `ladder.mp3` / `ladder.ogg` — 6 s, 22050 Hz, stereo, a 440 Hz
sine whose amplitude steps once a second: `0.10 + 0.15 * second`. Decode anywhere in the
stream and the peak amplitude says which second you landed in, which is how
`test_decoder_start_offset.cpp` checks where a decoder started. It survives
MP3 and Vorbis lossy coding, where exact sample values would not.

Regenerate with:

```sh
python - <<'PY'
import math, struct, wave
RATE, SECS = 22050, 6
w = wave.open("ladder.wav", "wb")
w.setnchannels(2); w.setsampwidth(2); w.setframerate(RATE)
frames = bytearray()
for i in range(RATE * SECS):
    level = 0.10 + 0.15 * (i // RATE)
    v = int(level * 32000 * math.sin(2 * math.pi * 440 * i / RATE))
    frames += struct.pack("<hh", v, v)
w.writeframes(bytes(frames)); w.close()
PY
ffmpeg -i ladder.wav -c:a flac ladder.flac
ffmpeg -i ladder.wav -c:a libmp3lame -b:a 64k ladder.mp3
ffmpeg -i ladder.wav -c:a libvorbis -q:a 4 ladder.ogg
ffmpeg -i ladder.wav -ac 1 -t 0.5 -c:a libvorbis -q:a 4 mono.ogg
```

`mono.ogg` is the first half second downmixed to mono, for channel-count tests.
