One AAC-LC encode of generated tones (440 Hz left, 660 Hz right, 1.25 s at
44.1 kHz) in every container the renderer reads: ADTS (tones.aac), progressive
MP4 with its index first (tones.m4a) or last (tones-index-last.m4a), and
fragmented MP4 without (tones-fragmented.m4a) and with (tones-fragmented-sidx.m4a)
a sidx box, and fragmented MP4 whose movie header lists the first units itself
(tones-fragmented-mixed.m4a); ../dash/aac segments the same packets. Since every container
carries the same units, every decoder must play the same samples.
ramp-flac.mp4 is FLAC in MP4 holding the ramp the DASH FLAC fixtures encode, and
alac.m4a a short ALAC file the renderer must refuse. No music is used.

Regenerate with python3 generate.py (needs ffmpeg), then rerun
../dash/generate.py so the DASH segments match.
