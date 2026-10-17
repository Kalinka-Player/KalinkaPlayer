# My Library

The local library indexes MP3, FLAC, WAV (`.wav`, `.wave`), Ogg Vorbis (`.ogg`,
`.oga`), MP4 audio (`.m4a`), raw AAC (`.aac`), DSF (`.dsf`) and DSDIFF (`.dff`)
files from local folders and configured network shares.

Loose files in a configured library root or a generic container such as
`Music` or `Downloads` are listed individually in Recently Added. Those
directory names do not become albums; album tags and cue sheets still name
albums there. Numbered filenames such as `track-02.flac` do not supply an
artist. AcoustID can correct a guessed artist even after a text search matched
that guess, while artists read from tags are preserved. Its recording matches
do not assign album membership.

Recently Added contains Albums, Tracks and Artists sections, with name, genre
and entity-type filters. Tracks includes every track, including album members.
Albums and artists are dated by their newest track's first arrival. Adding a
track brings its album and artist forward; editing metadata or moving an
existing file preserves its arrival date.

The `recent` catalog declares `q`, `genre` and `type` filters and carries
`recent-albums`, `recent-tracks` and `recent-artists` sections with the matching
preview content types. Each section declares `q` and `genre`; selecting its
type on the parent returns the same filtered listing and total.

Files whose size or timestamp changes during copying are retried after they
settle, without requiring another filesystem event or a manual scan.

Ogg Vorbis tags and `METADATA_BLOCK_PICTURE` covers are read like FLAC's, with
the older `COVERART` comment as a fallback cover. A chained file, such as a
radio rip, is timed over all its links as the renderer plays them, each from
where its recording begins. An Ogg file holding Opus or FLAC instead is left
out with a warning in the log, because the renderer plays only Vorbis from an
Ogg container.

MP4 audio is indexed when its first audio track is AAC-LC or HE-AAC (v1 or
v2, any channel count), or FLAC in mono or stereo at 16, 24 or 32 bits, in a
progressive or a fragmented file. Its tags and first cover picture are read;
a FLAC track's sample rate, channels and precision come from its STREAMINFO,
and a fragmented file is timed over all its fragments. ALAC, Opus, AC-3, other
codecs and files with no audio track are left out with a warning in the log.
`.mp4` files are not indexed, since most are video.

A raw AAC file is indexed when it holds AAC-LC or HE-AAC in ADTS frames from
its start, or from the end of the ID3v2 tag it may begin with, which supplies
its tags and artwork. Anything else named `.aac`, such as an MP3, is left out
with a warning. Neither MP4 nor raw AAC audio is analysed for audio
embeddings, since libsndfile cannot decode it; those jobs fail once, without
retrying, and metadata/text embeddings remain available.

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
