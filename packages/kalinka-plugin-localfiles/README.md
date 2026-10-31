# My Library

The local library indexes MP3, FLAC, WAV (`.wav`, `.wave`), Ogg Vorbis (`.ogg`, `.oga`), MP4 audio (`.m4a`, or `.alac` as some rippers name Apple Lossless), raw AAC (`.aac`), DSF (`.dsf`) and DSDIFF (`.dff`) files from local folders and configured network shares.

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

An album's cover comes from its own folder when an image there is named `cover`, `folder`, `front` or `album` (JPEG, PNG, GIF or WebP, in any case), preferred in that order. It may sit beside the audio, in an artwork folder under it, or in a disc folder. Otherwise the cover is the embedded front cover most of the album's tracks carry, ties going to the earliest track. Failing that it is the most common embedded picture with no type, then a sleeve scan chosen by how it looks.

A folder only counts when the album is the only one with tracks in it. Several albums sharing a directory, loose tracks beside an album, or a library root give their images to none of them. Local art replaces covers found online or generated, which only fill albums with none of their own.

A track whose embedded cover differs from its album's keeps its own: the queue, Now Playing and track lists show it, while album grids and headers keep the album's. Tracks sharing such a picture share one stored copy, and a track showing its album's cover stores nothing. A loose track with no album keeps the cover it embeds.

Covers follow changes to the files at the next scan: an image added, replaced or removed, a retag, a track moving to another album. In a watched local folder they follow at once. A changed cover gets a new link, so clients that cache covers show the new one. Cover files nothing uses any more are deleted after a full scan, and a link kept under a regrouped album's old id shows the cover of the album it became. Embedded covers are chosen by picture type, front cover first; a back cover, booklet page or artist photo is never the front.

Ogg Vorbis tags and `METADATA_BLOCK_PICTURE` covers are read like FLAC's, with
the older `COVERART` comment as a fallback cover. A chained file, such as a
radio rip, is timed over all its links as the renderer plays them, each from
where its recording begins. An Ogg file holding Opus or FLAC instead is left
out with a warning in the log, because the renderer plays only Vorbis from an
Ogg container.

MP4 audio is indexed when its first audio track is AAC-LC or HE-AAC (v1 or
v2, any channel count), or FLAC or ALAC in mono or stereo at 16, 24 or 32
bits, in a progressive or a fragmented file. Its tags and first cover picture
are read; a FLAC track's sample rate, channels and precision come from its
STREAMINFO and an ALAC track's from its configuration, and a fragmented file
is timed over all its fragments. Opus, AC-3, other codecs and files with no
audio track are left out with a warning in the log. `.mp4` files are not
indexed, since most are video.

Playback reports the indexed audio codec separately from the source MIME
type. MP4 audio can display AAC, ALAC or FLAC while it is still served and
routed to the renderer as `audio/mp4`. This also carries the indexed codec
for other library formats, such as Vorbis in Ogg. Older library entries
without codec evidence keep the MIME-label fallback until they are reindexed.

A file left out for its format is not read again while it stays unchanged.
When a new version learns to read a format it used to refuse, its first scan
reads every such file once more.

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
