These fixtures contain generated stereo PCM encoded as FLAC in fragmented MP4:
1.25 seconds at 16-bit/44.1 kHz and 24-bit/96 kHz. No music or credentials are used.

Run python3 generate.py with ffmpeg and ffprobe installed to regenerate them.
The script normalizes the manifest duration and segment timeline to the actual
packet durations, removing empty final fragments emitted by some FFmpeg versions.
The decoder tests independently reconstruct the original PCM and compare every
sample, including after seeks. Tests use the checked-in fixtures; FFmpeg's command
line tools are not needed to run them.
