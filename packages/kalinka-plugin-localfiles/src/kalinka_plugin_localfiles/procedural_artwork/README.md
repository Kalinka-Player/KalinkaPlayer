# Procedural album artwork generator

Standalone, deterministic generator of abstract album covers from album
metadata. Pure Python + Pillow + NumPy — no ML runtimes, no remote services,
no model files. Designed to run comfortably on a Raspberry Pi 4 (4 GB).

```python
from kalinka_plugin_localfiles.procedural_artwork import (
    ProceduralArtworkGenerator, AlbumArtworkInput,
)

gen = ProceduralArtworkGenerator()          # default_size=512, version=1
album = AlbumArtworkInput(artist="Aurora Fields", title="Night Currents",
                          genre="ambient")
image = await gen.generate(album)           # PIL.Image, 512x512 RGB
data = await gen.generate_bytes(album, size=600, image_format="PNG")
params = await gen.generate_to_file(album, Path("cover.png"))
```

## Family vs edition identity

Two identity layers keep releases of the same album visually related:

* **Family identity** — *which album* this is. Taken from the MusicBrainz
  release-group MBID when present, otherwise from a fallback key built from
  the normalized artist + normalized base title (controlled edition markers
  such as "Deluxe Edition", "2019 Remaster", "Mono" stripped from the end;
  arbitrary parenthesized content is never removed) + a canonical track-list
  signature over the first 12 normalized tracks (so appended bonus tracks do
  not break the family). The family selects the template, composition,
  palette structure and broad style.
* **Edition identity** — *which release* of the album. It only contributes
  restrained variation: small hue shifts, slight offsets and line-width
  changes, a different grain pattern. Every scalar is a bounded interpolation
  `final = family * (1 - strength) + edition * strength`, with strengths of
  roughly: remaster 5 %, mono/stereo 7 %, unknown alternate release 8 %,
  anniversary/special 10 %, bonus-track 12 %, deluxe/expanded 14 %
  (`identity.EDITION_STRENGTHS`).

## Deterministic seeds

All seeds are 64-bit BLAKE2b hashes (never Python `hash()`), with separate
personalization strings for the family, edition and semantic domains:

* `family_seed = H_fam(generator_version, family_key)`
* `edition_seed = H_edn(generator_version, family_seed,
  normalized_full_title, release_id, track_signature)`

Randomness is consumed only through `numpy.random.Generator(PCG64(...))`
streams derived from `(seed, domain)` pairs — no global RNG state. The output
size participates in neither seed, and all geometry is computed in unit
coordinates, so the same album renders the same composition at every size.

## Semantic style

An optional `album_embedding` (any 1-D finite numeric vector) is projected
onto the style scalars (hue, saturation, softness, complexity, contrast,
grain, angularity, texture density) through fixed constant-seeded projection
vectors, salted by `embedding_version`. Similar embeddings therefore land on
nearby visual parameters. Embeddings influence style only — never family
membership. Without an embedding, style is drawn from the genre profile using
the family seed. Genre profiles exist for ambient, electronic, techno, jazz,
classical, rock, hip hop, metal, folk and a default; multi-value genre
strings ("ambient electronic", "jazz fusion") are matched by token/alias and
blended.

## Templates

Five abstract templates (each its own module under `templates/`): `waves`
(flowing wave lines), `orbits` (circles/orbital geometry), `horizon`
(layered landscape bands), `blocks` (geometric grid), `slashes` (diagonal
collage). No text, faces or figurative imagery.

## Sizes

Default 512×512; any integer size from 32 to 2048 is accepted (64, 128, 256,
512, 600, 1024 all practical). Everything visual scales proportionally; small
covers stay recognisably related to large ones. Rendering happens directly at
the requested size with 2× supersampling for antialiasing up to 1280 px
output (larger sizes render 1:1 to bound memory).

## Async behaviour

`generate()`, `generate_bytes()` and `generate_to_file()` offload the
CPU-bound render/encode with `asyncio.to_thread()`, so the event loop stays
responsive. Exceptions propagate; cancellation of the awaiting task is safe
(the worker thread finishes its render and the result is dropped). The class
never creates its own event loop and never calls `asyncio.run()`.
`resolve_parameters()` and `render()` remain synchronous and pure for callers
that need them. `generate_to_file()` writes atomically (temp file in the
target directory, `os.replace` on success, temp cleanup on failure).

## Raspberry Pi resource usage

A 512×512 render takes on the order of tens of milliseconds on desktop and a
few hundred milliseconds on a Pi 4; peak transient memory is dominated by the
supersampled canvas plus one float32 copy (≈20–30 MB at 512, ≈100 MB at
1024). **Recommended concurrency on a Pi 4 is 1–2 simultaneous renders** —
the class deliberately does not limit concurrency itself; wrap calls in an
`asyncio.Semaphore(1)` or `(2)`. No rendered images are retained internally.

## Generator versioning

`GENERATOR_VERSION` (currently 1) is hashed into every seed. Bumping the
version (per instance via `ProceduralArtworkGenerator(generator_version=N)`)
deliberately regenerates all artwork; keeping it fixed keeps artwork stable
across releases of this code as long as the algorithm is unchanged.

## Dependencies

Pillow and NumPy only, both already provided by the host application
(works with NumPy 1.26+ and 2.x). Python >= 3.11.
