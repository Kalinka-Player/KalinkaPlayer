# Local files

The local library indexes MP3, FLAC, WAV (`.wav`, `.wave`), Ogg Vorbis (`.ogg`,
`.oga`), DSF (`.dsf`) and DSDIFF (`.dff`) files from local folders and configured
network shares.
Ogg Vorbis tags and `METADATA_BLOCK_PICTURE` covers are read like FLAC's, with
the older `COVERART` comment as a fallback cover. A chained file, such as a
radio rip, is timed over all its links as the renderer plays them, each from
where its recording begins. An Ogg file holding Opus or FLAC instead is left
out with a warning in the log, because the renderer plays only Vorbis from an
Ogg container.

WAV indexing reads ID3 tags and artwork, duration, sample rate and PCM
precision, including the valid bit count in extensible headers. Integer
16/24/32-bit and IEEE float 32/64-bit mono/stereo WAVs are accepted. Unsupported
encodings are left out. Untagged WAVs use the existing filename, folder and
cue-sheet metadata fallback.

DSD indexing reads ID3 tags, embedded artwork, duration and stream information.
DSDIFF's native artist and title fields fill gaps in ID3 metadata. Untagged DSD
uses the existing filename and folder extraction, and normal clustering,
artwork and MusicBrainz processing continue unchanged.

DSD audio is never converted to PCM for analysis. AcoustID/Chromaprint skips
DSD before file staging or fingerprint generation. PCM-dependent audio
embeddings are marked unsupported without retrying; metadata/text embeddings
remain available. Existing PCM analysis is unchanged.

Playback requires the renderer's DSD output setting and a compatible DAC.
See the [renderer settings](../kalinka-renderer/README.md#settings). Native DSD
and DoP preserve the source rate; there is no PCM fallback or DSD rate
conversion. DST-compressed DFF can have its metadata indexed but is not
playable by the renderer.
