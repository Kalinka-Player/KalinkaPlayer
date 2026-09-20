# Kalinka semantic search on the Song Describer Dataset

> The reference run, kept in the repository so a change can be compared against something. Regenerate it with `make bench-sdd ARGS="--out tmp/sdd_bench/run1"` and copy the result here; the runbook is [RUNBOOK.md](RUNBOOK.md), the mood suite's report is [results_mood.md](results_mood.md), and what an embedding costs on a Raspberry Pi is [results_device.md](results_device.md).

Every number below comes from one run of the shipped server over 706 recordings it had never seen, queried 1106 times through the same endpoint the apps call. Code kalinka-image-v1.1.1-17-g5195fe3-dirty, 22 CPU threads.

## Quality

Strict scoring counts one recording per caption: the one the caption was written about. Nothing else in the library can be right, however well it matches.

### Strict — the captioned recording only

| Configuration | Captions | R@1 | R@5 | R@10 | R@50 | MRR | Median rank |
|---|---:|---:|---:|---:|---:|---:|---:|
| CLAP + valence/arousal fusion (shipped default) | 1106 | 5.0% | 20.6% | 32.8% | 63.2% | 0.133 | 24 |
| CLAP + valence/arousal fusion (shipped default) — validated subset | 746 | 5.4% | 20.5% | 33.7% | 66.9% | 0.138 | 22 |
| CLAP only (mood ranking off) | 1106 | 4.7% | 20.6% | 33.1% | 66.1% | 0.132 | 25 |
| CLAP only (mood ranking off) — validated subset | 746 | 5.4% | 20.6% | 33.8% | 69.8% | 0.139 | 22 |
| Random ranking of 706 tracks | — | 0.1% | 0.7% | 1.4% | 7.1% | 0.006 | > 50 |

### Graded — judged by an independent sentence embedder

A returned track's relevance to a query is the highest cosine similarity between the query caption and any caption that track carries, measured by all-mpnet-base-v2 — a text model with no audio in its training and no relationship to CLAP. Soft recall@10 asks whether anything in the top ten cleared the threshold.

| Configuration | Captions | nDCG@10 | soft-R@10 ≥0.5 | ≥0.6 | ≥0.7 | Mean top-1 relevance |
|---|---:|---:|---:|---:|---:|---:|
| CLAP + valence/arousal fusion (shipped default) | 1106 | 0.703 | 94.8% | 78.0% | 51.6% | 0.541 |
| CLAP + valence/arousal fusion (shipped default) — validated subset | 746 | 0.711 | 96.4% | 81.4% | 55.5% | 0.553 |
| CLAP only (mood ranking off) | 1106 | 0.703 | 94.8% | 78.5% | 51.5% | 0.539 |
| CLAP only (mood ranking off) — validated subset | 746 | 0.711 | 96.5% | 81.4% | 55.0% | 0.552 |

### Ablation — what valence/arousal fusion costs or buys

Both configurations are the same index queried twice: the ablation is a settings change and a restart, not a re-embedding.

Turning fusion on moves R@1 4.7% → 5.0% (+0.3 pts), R@10 33.1% → 32.8% (-0.3 pts) and R@50 66.1% → 63.2% (-2.9 pts), with MRR 0.132 → 0.133 and nDCG@10 0.703 → 0.703.

So the top of the list is unchanged and the depth of it is worse: the mood leg unions its own candidates into the pool, and with the list capped at 50 those candidates displace CLAP hits that would otherwise have been in it. It is not free, either — median latency 118 ms → 124 ms and p95 128 ms → 252 ms.

What this does not say is that mood ranking is useless. These queries are descriptions of recordings, which is what CLAP is strongest at; the mood axis was added for queries CLAP is weakest at ("something melancholy"), and a caption benchmark contains almost none of those. The other kind is measured separately, in [results_mood.md](results_mood.md), over the same index and the MTG-Jamendo mood tags — and there fusion wins: on affective tags CLAP alone ranks barely better than shuffling, and fusion raises P@10 from 0.050 to 0.087. Read the two together: this is what the blend costs where CLAP is strong, that is what it buys where CLAP is weak.

## Five queries, as illustration only

Drawn with a fixed seed before their outcomes were known, and part of no metric above or below. Each returned track is named by its dataset id and by one of its own captions, which is how a reader can see what the ranking heard. `d` is the searcher's own KNN distance over the stored int8 vectors — lower is closer — and the order can differ from it, because the mood blend is applied after the KNN leg.

```
query   Instrumental piece with some baroque-era musical arrangements that could well be used in the opening credits of a period film.
target  402055 (rank 5)
  1. 1089085  d=476.233124  "Possibly a movie soundtrack, this instrumental piece has a dramatic tone, creates a slig"
  2. 1119403  d=489.010223  "A strings orchestra and piano combine in this waltz to give a fantasy feeling."
  3. 344323  d=495.492676  "Relaxing music played mostly on the piano that can be used while studying, meditating or"
  4. 1350313  d=494.741333  "Instrumental mainly on flute ideal for a situation where someone is starting on a nature"
  5. 402055  d=494.714050  "Instrumental piece with some baroque-era musical arrangements that could well be used in" <- target
```

```
query   Happy swinging cabaret jazz track with female vocals, piano, brass and bass.
target  457119 (rank 2)
  1. 457080  d=494.189240  "A whimsy and unserious piano melody, underpinned by consistent snares with occasional of"
  2. 457119  d=502.959229  "A piano vocal track for a cosy night in a jazz bar." <- target
  3. 1014963  d=507.780457  "This song starts with a electric guitar riff and then gets rich with brass instruments a"
  4. 1051198  d=506.270691  "A record noise opens up this song, followed quickly by a filtered female voice; piano ch"
  5. 260841  d=505.283081  "A playful and jazzy song about cats, featuring a funny meow at the beginning."
```

```
query   This is a experimental piece or sound effect with quiet noises of running water and someone whispering.
target  1070256 (rank 1)
  1. 1070256  d=564.381958  "This is a experimental piece or sound effect with quiet noises of running water and some" <- target
  2. 1067070  d=611.464661  "This is an experimental electronic songs, with noisy synths and a male voice speaking a "
  3. 272434  d=639.828857  "Haunting expansive sound as if you are in space"
  4. 727904  d=650.229187  "A dark and slow electronic track makes one think of scary dungeons filled with slimy sku"
  5. 464545  d=653.447021  "An ambient track with a pitched down male voice, mellow pads and choir. Towards the end "
```

```
query   It has a ballet dance or circus music feel mostly played on the xylophone
target  17170 (rank 18)
  1. 1350951  d=549.087402  "1950s New York police caper movie. Or music for a commercial about cat food."
  2. 1402647  d=554.053223  "This song makes you feel like entering to a haunted mansion or magical village."
  3. 632094  d=554.884644  "Guttural nu-metal with a match-day pub visit hymn foundation."
  4. 25249  d=552.192017  "Synthetic orchestral music with hopeful, yearning harmonies, fast pizzicato motifs and w"
  5. 26432  d=566.879150  "A song that leads one to a fantastic land where some supernatural creatures are right ar"
```

```
query   Energetic guitar and cajon duo accompanied by a portuguese singing woman.
target  83867 (rank 14)
  1. 83880  d=541.457275  "Percussive, Hispanic Latin American Sounding Folk Guitar song with female vocalist. Upbe"
  2. 305164  d=548.888855  "Spanish speaking ethnic folk song, with an interesting prominent percussion section. The"
  3. 305159  d=559.499756  "Fast tempo percussion with an energetic beat and a vocal melody"
  4. 1288067  d=558.696716  "This is very Arabic/Turkish kind of music, there is a single string instrument that is p"
  5. 1063331  d=566.603943  "An instrumental hopefully positive track where the foreground melody features a classic "
```

## Timeline

End to end: **18m 3s**. Bold rows are the phases and sum to it; the rows under each are that phase's own breakdown, measured inside it. A phase's parts can fall short of its wall clock — the difference is the pipeline waiting on its own queues, which is reported rather than hidden.

| Stage | Seconds | % of end-to-end |
|---|---:|---:|
| Dataset preparation (verify, extract, strip, manifest) | 5.1 | 0.5% |
| **Indexing — end to end** | 748.8 | 69.1% |
| — server start, plugin load, model provisioning | 2.0 | 0.2% |
| — library scan (walk, tags, database rows) | 64.3 | 5.9% |
| &nbsp;&nbsp;· directory listing | 0.0 | 0.0% |
| &nbsp;&nbsp;· per-file indexing | 63.8 | 5.9% |
| &nbsp;&nbsp;· stale-row cleanup | 0.0 | 0.0% |
| — CLAP audio embedding (wall clock) | 682.0 | 62.9% |
| &nbsp;&nbsp;· audio tower load | 10.7 | 1.0% |
| &nbsp;&nbsp;· fragment decode + resample | 44.1 | 4.1% |
| &nbsp;&nbsp;· ONNX inference | 442.3 | 40.8% |
| &nbsp;&nbsp;· stream open, quantise, collect | 93.4 | 8.6% |
| &nbsp;&nbsp;· vector + job writes | 24.4 | 2.3% |
| — mood (valence/arousal) backfill | 0.0 | 0.0% |
| &nbsp;&nbsp;· mood head inference | 0.0 | 0.0% |
| &nbsp;&nbsp;· mood writes | 0.1 | 0.0% |
| **Retrieval run — CLAP + valence/arousal fusion (shipped default)** | 163.7 | 15.1% |
| — query text encoding (CLAP text tower) | 84.5 | 7.8% |
| — vector KNN over the index | 5.3 | 0.5% |
| — mood mapping and blend | 0.3 | 0.0% |
| — HTTP, serialisation, database fetch of hits | 66.2 | 6.1% |
| **Retrieval run — CLAP only (mood ranking off)** | 129.9 | 12.0% |
| — query text encoding (CLAP text tower) | 84.8 | 7.8% |
| — vector KNN over the index | 5.3 | 0.5% |
| — mood mapping and blend | 0.0 | 0.0% |
| — HTTP, serialisation, database fetch of hits | 39.5 | 3.6% |
| Ablation settings change and restart | 3.0 | 0.3% |
| **Scoring** | 33.2 | 3.1% |
| — judge embedding of every caption + similarity matrix | 33.0 | 3.0% |
| — metric computation | 0.2 | 0.0% |

The mood backfill reads as no time at all because it does not have any of its own: the embedder drains its audio queue, backfills (V,A) from the vectors it just stored, and loops, so the mood work happened inside the embedding row — 0.13 s of it, for all 706 tracks, the whole of it a 512→2 head over a vector already in memory.

Per-query latency, measured at the client:

- CLAP + valence/arousal fusion (shipped default): median 124 ms, p95 252 ms, max 277 ms.
- CLAP only (mood ranking off): median 118 ms, p95 128 ms, max 149 ms.

## The dataset

Song Describer Dataset (Zenodo 10072001, DOI 10.5281/zenodo.10072001): **1106 captions** over **706 recordings**, every file two minutes of audio from MTG-Jamendo. Columns: caption_id, track_id, caption, is_valid_subset, familiarity, artist_id, album_id, path, duration. The caption_id → track_id pairing is the ground truth; nothing else about a track is used.

- Captions per track: 476 track(s) with 1, 127 track(s) with 2, 57 track(s) with 3, 25 track(s) with 4, 21 track(s) with 5.
- `is_valid_subset` marks 746 captions (547 tracks) as the authors' validated subset; the rest are unflagged or explicitly False.
- Caption length: 8–100 words, median 17.
- 12 captions contain non-ASCII characters (typographic apostrophes, accented words).
- Audio indexed: 706 files, 23.2 hours, median 120s each.

## Configuration fingerprint

- Code: `5195fe363f16` on `main` (working tree dirty)
- Host: Linux-7.2.4-200.fc44.x86_64-x86_64-with-glibc2.43, Python 3.14.7, 22 threads
- Packages: kalinka-server 5.0.1.dev23+g5195fe3, kalinka-plugin-localfiles 5.0.1.dev23+g5195fe3, kalinka-plugin-sdk 3.3.0, onnxruntime 1.27.0, numpy 2.5.1, soundfile 0.13.1, soxr 1.0.0, sqlite-vec 0.1.9, tokenizers 0.23.1, mutagen 1.48.1, httpx 0.28.1
- CLAP release: https://github.com/madenvel/KalinkaPlayer/releases/download/clap-onnx-v2, model version 4, stored format 2 (int8, cap 0.25), VA head v1
  - `clap_audio_encoder.onnx` — 285.0 MB, sha256 `1b0c8b624a8746e6…`
  - `clap_text_encoder.onnx` — 501.5 MB, sha256 `da42ffab77e0fa12…`
  - `clap_tokenizer.json` — 3.6 MB, sha256 `2bb1a22cfbe25b8e…`
  - `va_head_v1.onnx` — 0.3 MB, sha256 `ee35b2f180063d71…`
  - `mood_index_v1.npz` — 0.1 MB, sha256 `aeacea660fc4f14c…`
- Audio sampling: 48 kHz, 10s fragments; query text truncated at 77 tokens
- Settings (schema `e1071cf7839bb2e6`), shipped defaults except where the benchmark set them:
  - `localfiles.ai_search.enabled` = `True`
  - `localfiles.ai_search.max_results` = `50`
  - `localfiles.ai_search.knn_candidate_limit` = `50`
  - `localfiles.ai_search.mood.enabled` = `True`
  - `localfiles.ai_search.mood.weight` = `0.6`
  - `localfiles.ai_search.mood.candidates` = `200`
  - `localfiles.ai_search.mood.nn_threshold` = `0.3`
  - `localfiles.ai_search.audio_batch_size` = `4`
  - `localfiles.enricher.enabled` = `False`
  - `search.ai_suggestions_limit` = `50`
- Judge: sentence-transformers/all-mpnet-base-v2 at revision `e8c3b32edf54`, onnxruntime, mean over the attention mask, then L2 normalise, max 384 tokens
- Indexing outcome: 706 of 706 indexed, 706 embedded, 0 embedding failures

## Leakage check

The library the server indexed carries no text that a caption could have reached. Each file was copied out of the archive under a name that is the SHA-1 of its dataset id and nothing else, into one flat directory, and every tag was deleted before indexing. Enrichment (MusicBrainz, AcoustID, Deezer) was off, so no name was fetched back from outside either, and with it off the metadata-text embedding stage never runs at all — the only vectors in the index are audio.

Verified after preparation, on all 706 files: 0 files with any tag frame, ID3v2 header or ID3v1 trailer; 0 filenames sharing a word with any caption; 0 files in the library that the manifest does not list.

The endpoint's own answer is kept beside the results as `sample_response_<run>.json`: every hit it names carries a hash for a title and "Unknown Artist" for an artist, which is the same check seen from the outside.

## Integrity checks

Each returned track should carry the CLAP distance the searcher logged for that same query. With the mood leg off every returned track came from the KNN leg, so anything under 100% there would mean a query was answered with another query's hits.

| Run | Queries | Tracks returned | With a distance from their own query | Empty answers | Refused (HTTP) |
|---|---:|---:|---:|---:|---:|
| CLAP + valence/arousal fusion (shipped default) | 1106 | 54700 | 86.7% | 12 | 0 |
| CLAP only (mood ranking off) | 1106 | 54700 | 100.0% | 12 | 0 |

## Instrumentation

The server's own logs already time a track's embedding and a query's KNN leg. Everything finer was taken with wrappers installed from outside the server, by an import hook on `PYTHONPATH` that the benchmark sets and nothing else does — no file under `packages/` was edited for this run. Each wrapper calls what it wrapped, returns what it returned, and logs one line.

| Wrapped symbol | What it measures | Kind |
|---|---|---|
| `clap_onnx._read_fragment` | fragment decode/resample time | timing |
| `clap_onnx.ClapOnnxModel.get_audio_embedding` | per-track embedding split | timing |
| `clap_onnx.ClapOnnxModel.load_audio` | audio tower load time | timing |
| `clap_onnx.ClapOnnxModel.load_text` | text tower load time | timing |
| `clap_onnx.ClapOnnxModel.get_text_embedding` | text encode time | timing |
| `clap_onnx.ClapOnnxModel.get_valence_arousal` | mood head time | timing |
| `onnxruntime session.run (audio, text)` | inference time inside an embedding | timing |
| `embedder_db.AsyncEmbedderDb.complete_clap_job` | vector write time | timing |
| `embedder_db.AsyncEmbedderDb.store_mood_va` | mood write time | timing |
| `embedder_db.AsyncEmbedderDb.claim_batch` | job claim time | timing |
| `embedder_db.AsyncEmbedderDb.schedule_new_jobs` | job scheduling time | timing |
| `embedder.EmbeddingWorker._process_clap_batch` | batch time | timing |
| `embedder.EmbeddingWorker._process_va_backfill` | mood backfill time | timing |
| `indexer.FileIndexer._audio_files_by_folder` | scan listing time | timing |
| `indexer.FileIndexer._index_files` | scan per-file time | timing |
| `indexer.FileIndexer.cleanup_stale_tracks` | scan cleanup time | timing |
| `searcher.SearchWorker._encode_query_blob` | query encode time | timing |
| `searcher.SearchWorker._knn_leg` | KNN leg time | timing |
| `searcher.SearchWorker._query_to_va` | mood mapping time | timing |
| `searcher.SearchWorker._do_search` | whole-query time | timing |
| `searcher_db.AsyncSearcherDb.knn_search_audio` | KNN time AND the distances it returned | timing + observation |
| `searcher_db.AsyncSearcherDb.knn_search_mood` | mood KNN time AND the distances it returned | timing + observation |

The two `knn_search_*` wrappers are the one exception to timing-only: they also log the distances the call returned. The search endpoint publishes rank order and no score, so without them `results.csv` would have a rank column and nothing behind it.

## Caveats

- **Strict scoring is pessimistic by construction.** One track is correct per caption. A library of 706 recordings holds many that match "acoustic guitar solo with a relaxed feel" equally well, and every one of them counts against the score.
- **Graded scoring is lenient, and biased toward the judge's habits.** It rewards captions that sound alike, which is not the same as music that sounds alike: two tracks described with the same vocabulary score as relevant whether or not a listener would agree. A soft recall near 100% means the threshold is loose, not that retrieval is solved.
- **12 captions are non-ASCII and return nothing.** The query encoder skips a query that is not ASCII, so a caption with a typographic apostrophe is answered with an empty list (12 empty answers in this run). They were left exactly as the dataset wrote them, and they count as misses.
- **Long captions are truncated** to 77 tokens by CLAP's tokenizer, which is a fifth of the longest caption in the set.
- **Top-50 is the whole candidate pool.** The KNN leg's candidate limit is 50, so R@50 is the recall of the retrieval step itself and ranks below 50 are the whole list, not a prefix of a deeper one.
- **One host, one run.** Timings are this machine's (22 threads, Linux-7.2.4-200.fc44.x86_64-x86_64-with-glibc2.43) and a single run at that: they say where the time goes, not what the variance is. What the same split looks like on the hardware the server is built for is measured separately, in [results_device.md](results_device.md), and it is not this table scaled down.

## What the run turned up

None of this was changed for the benchmark.

1. **A query that is not ASCII is answered with nothing.** `SearchWorker._encode_query_blob` returns `None` for a non-ASCII query, so no vector is encoded and no candidate is retrieved — the caller gets an empty list, not a worse one. 12 of 1106 captions here trip it, and all they contain is a typographic apostrophe. Normalising the query (NFKD, curly quotes folded to ASCII) before the gate would keep the tokenizer's English assumption and lose nothing.
2. **Two settings control the same depth, and the server's is inert.** `/ai_search` passes the server's `ai_suggestions_limit` into `module.ai_search(query, offset, limit)`, and the local library ignores that argument in favour of its own `ai_search.max_results`. Raising the server's setting alone changes nothing for the local library.
3. **The ranked list cannot be deeper than the candidate pool.** `knn_candidate_limit` (50) bounds what the KNN leg retrieves, so a `max_results` above it silently returns fewer tracks than asked for. The two settings want to move together, or the second wants to be derived from the first.
4. **The per-call budget can outlive the call it cancelled.** A query that overruns the server's 3 s budget is cancelled at the `await`, but the executor thread stays blocked on the searcher's response queue and the searcher keeps working. Two consumers can then be waiting on one queue, and nothing guarantees which one receives the next response — a later query can be answered with an earlier query's tracks. This run checks for exactly that (the integrity table above). Per run — CLAP + valence/arousal fusion (shipped default): 0 refused, 12 empty, CLAP only (mood ranking off): 0 refused, 12 empty.
5. **Embedding is inference-bound, one fragment at a time.** 76% of a track's embedding is ONNX inference and only 8% is decoding. The encoder is run once per 10 s fragment, so a track costs three separate single-sample sessions; batching a track's fragments into one call is the obvious thing to measure next. On a Raspberry Pi 4 the same split is 90% inference and 3% decode ([results_device.md](results_device.md)), so on the machine this is for, the win has to come out of the tower rather than out of the call count.
6. **Mood fusion, as configured, only costs on this kind of query.** It leaves the top ten where it was, takes several points off R@50 by unioning its own candidates into a list capped at 50, and doubles p95 latency. The ablation section says why that is not the same as "turn it off": these captions describe recordings, and the mood axis exists for the queries that do not. Measuring it needs mood queries with mood ground truth, which this dataset does not have.
