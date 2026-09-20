# SDD retrieval benchmark — runbook

How to rerun Kalinka's semantic search against the Song Describer Dataset, what every number means, and what a wrong number looks like. Written for an agent picking this up cold.

## What it does

Two suites over one index, asking different questions of the same search, and a probe for the machine that will run it.

**Captions** (the default): can search find *one* recording from a human's description of it? Ground truth is the caption→track pairing; scoring is strict rank plus a graded judge. This is CLAP at its strongest.

**Mood** (`--stages mood`): given a mood word, does the ranking fill with tracks a human tagged that way? Ground truth is the MTG-Jamendo mood/theme tags that ship inside the Song Describer record; many tracks are right per query, so scoring is P@10, mAP, nDCG and R-precision over a relevance set. This is the suite the valence/arousal fusion exists to win, and the one to run when the mood head or the blend changes.

**Device** (`device_probe.py`, run on the device): of the seconds one track costs to embed, how many are reading the file, how many are decoding it and how many are ONNX inference — on the hardware that will actually run the server. It answers a different question from the two suites: not what search finds, but what indexing a library costs the machine in front of you. Run it when the encoder, the fragment loader or the ONNX session options change, and before quoting anyone an indexing time.

It indexes 706 recordings through the shipped server — the real scan, the real CLAP embedder, the real vector index — then asks the public `/ai_search` endpoint all 1106 human captions that came with those recordings and checks where each caption's own recording landed. Two outputs come out of it: the quality tables and a timeline of where the time went. No human looks at anything.

The audio is prepared so that nothing but the audio can be matched on: every tag is deleted, every ID3 container is physically removed, and each file is renamed to the SHA-1 of its dataset id. Metadata enrichment is off, which also means the metadata-text embedding stage never runs — the index holds audio vectors and nothing else.

## Rerun from scratch

```
make bench-sdd ARGS="--out tmp/sdd_bench/run1"
```

That is the whole thing: download, prepare, index, query twice, judge, report. The reference run took 18 minutes on a 22-thread desktop with the downloads already cached, two thirds of it embedding; the first run also pulls about 4 GB (3.3 GB of audio, ~800 MB of CLAP models, ~420 MB of judge model).

`make bench-sdd` forwards `ARGS` to `benchmarks/sdd/run.py`, which can equally be run directly with the venv's interpreter. Useful variations:

```
# stages are resumable; earlier artifacts are reused
python benchmarks/sdd/run.py --out tmp/sdd_bench/run1 --stages retrieve score report

# one ranking configuration only
python benchmarks/sdd/run.py --out tmp/sdd_bench/run1 --runs mood_on

# re-extract and re-strip the audio (after changing the preparation)
python benchmarks/sdd/run.py --out tmp/sdd_bench/run1 --stages prep --force-prep

# the mood suite over an index that already exists (about three minutes)
python benchmarks/sdd/run.py --out tmp/sdd_bench/run1 --stages mood
```

`make test` runs this benchmark's unit tests along with the rest; they cover the scoring, the log parsing, the tag stripping and the device probe's accounting, and none of them need a server.

## Rerun the device probe

`device_probe.py` is one self-contained file with no imports from this benchmark, because it runs on the device rather than here. It needs the localfiles plugin importable — on a Kalinka install that is `/opt/kalinka/venv/bin/python` — a CLAP model directory (`/var/lib/kalinka/models` on a package install, once AI search has downloaded them), and a few tracks on the storage the library really lives on.

```
scp benchmarks/sdd/device_probe.py <device>:/tmp/
scp ~/.cache/kalinka-sdd-bench/audio/00a1196ae8bd*.mp3 <device>:/mnt/usb/Probe/

ssh <device> '/opt/kalinka/venv/bin/python /tmp/device_probe.py \
    --model-dir /var/lib/kalinka/models --csv /tmp/device.csv \
    usb=/mnt/usb/Probe'
```

Any track will do; the reference run used one of the benchmark's own. Several locations can be given at once (`usb=/mnt/usb/Probe sd=/home/pi/probe`) and are visited round-robin rather than one after another, which is the only way to tell a slow disk apart from a hot SoC. `--rounds` is the axis that matters — one round cannot show thermal drift — and `--tracks` caps how many files per location. A run of 8 rounds over 3 locations takes about 8 minutes on a Pi 4.

`--threads 1,2,3,4` adds a sweep of the audio session's `intra_op_num_threads` after the main measurement, which is the one tuning knob the split leaves open. It reloads the encoder once per value and reuses `--rounds`, so keep that small for a sweep. The values are visited in order on a board that is already warm, so one sweep confounds the setting with the heat: run it twice, ascending and descending, and take the mean of the two, which is what the reference measurement did. Start it on a cooled board — the probe prints the die temperature and the firmware's throttling word before and after the run, and on a Pi those two lines are what tell you whether you measured the code or the heatsink.

If the device has no model directory yet, fetch just the audio tower into a throwaway one, and delete it afterwards along with the tracks:

```
mkdir -p /tmp/probe-model
curl -sSL -o /tmp/probe-model/clap_audio_encoder.onnx \
  https://github.com/madenvel/KalinkaPlayer/releases/download/clap-onnx-v2/clap_audio_encoder.onnx
```

The probe runs in its own process against its own model directory and opens the audio read-only, so the server can keep running; it will compete with it for CPU, which is the one reason to stop playback first.

## Where everything lives

| Path | What |
|---|---|
| `~/.cache/kalinka-sdd-bench/dataset/` | `song_describer.csv`, `audio.zip` as downloaded, md5-verified against the Zenodo record |
| `~/.cache/kalinka-sdd-bench/audio/` | the prepared library: 706 tagless `<sha1>.mp3` files in one flat directory |
| `~/.cache/kalinka-sdd-bench/models/` | CLAP checkpoints, shared by every run (the server's model dir is a symlink to it) |
| `~/.cache/kalinka-sdd-bench/judge/` | the judge model, pinned to one repository revision |
| `<out>/fakeroot/` | the server's throwaway prefix: config, `localfiles.db`, `var/log/kalinka/server.log` |
| `<out>/manifest.csv` | track_id, opaque file, duration, size, caption count, and the track id the server will mint |
| `<out>/dataset_stats.json` | caption and audio statistics, plus the leakage check |
| `<out>/index_timings.csv` | one row per embedded track: total, decode, inference, other |
| `<out>/index_summary.json` | stage wall clocks, the server's own stage status, and what was indexed |
| `<out>/results_<run>.csv` | one row per caption: rank, ranked ids, CLAP distances, latencies |
| `<out>/caption_similarity.csv` | the full 1106 × 1106 judge similarity matrix |
| `<out>/metrics_<run>.json` | the strict and graded numbers |
| `<out>/timeline.csv`, `<out>/clock.json` | the timeline table and its raw spans |
| `<out>/fingerprint.json` | code commit, model checksums, settings, judge revision, instrumentation list |
| `<out>/mood_queries.csv` | the mood query set: query, family, support, relevant track ids |
| `<out>/mood_scores_<run>.csv` | per-query P@10, AP, nDCG, R-precision, prevalence, lift |
| `<out>/mood_metrics.json` | the mood suite's aggregates and per-query rows |
| `<out>/results.md`, `<out>/results_mood.md` | the two reports |
| `device.csv` on the device (`--csv`) | one row per embedding: round, location, cache state, I/O, decode, inference, other, die temperature, CPU clock |

Nothing under `~/.cache` is repo material and nothing under `tmp/` is committed. `benchmarks/sdd/results.md`, `results_mood.md` and `results_device.md` are the reference runs kept in the repository.

## How each metric is computed

**Strict.** For a caption, the correct answer is the one recording it was written about. `rank` is the 1-based position of that recording in the returned list, or a miss. `R@k` is the share of captions whose recording came back at rank ≤ k. `MRR` is the mean of 1/rank, counting a miss as 0. Median rank is over all captions, with misses ranking as a sentinel; the report also gives the median over found ones only.

**Graded.** An independent judge — `sentence-transformers/all-mpnet-base-v2`, pinned by commit, run under onnxruntime, mean-pooled and L2-normalised — embeds all 1106 captions and the full cosine matrix is cached to CSV. A returned track's relevance to a query is the **highest** cosine between the query caption and any caption that track carries. The target track therefore always scores 1.0, because the query is one of its own captions. `nDCG@10` uses linear gains and the ideal ranking over all 706 tracks. `soft-Recall@10 ≥ t` is the share of captions where at least one of the top ten cleared relevance t.

The judge is deliberately not CLAP's text tower: CLAP grading a CLAP ranking measures agreement with itself.

**Random baseline.** With 706 tracks, a shuffled library returns the right one in the top 10 about 1.4% of the time. It is printed in the strict table as the floor.

**The mood suite.** A query is a tag, and every track carrying that tag is relevant. `P@10` is how much of the top ten carries it; `mAP` averages precision at each relevant rank, divided by the number of relevant tracks in the corpus (so finding 3 of 40 cannot score like 3 of 3); `nDCG@10` uses binary gains; `R-precision` is precision at the rank equal to the number of relevant tracks. The baseline is each tag's own prevalence — 8% of the corpus tagged `happy` means a random ranking scores P@10 = 0.08 — and `lift` is precision over that. Ranked depth is the whole library rather than the shipped 50, because mAP over a truncated list measures the truncation. Scoring is over judged tracks only: recordings with no mood tag are removed from the ranking first, since an untagged track is an unlabelled one, not a wrong one.

**The device probe.** A track is embedded twice per round: once with its page cache dropped (`posix_fadvise(DONTNEED)`), which is what a first indexing pass pays, and once with it hot. Four buckets, measured inside the shipped code path: **I/O** is time inside the stream's own `read` and `seek`, taken by wrapping the handle the encoder is given; **decode** is time inside `clap_onnx._read_fragment` minus the I/O that happened within it, so it is decode and resample CPU only; **inference** is time inside the ONNX session's `run`; **other** is the remainder — opening the stream, the header probe, the int16 roundtrip, the per-fragment collection. `drift` compares the first round's median with the last one's, alongside the die temperature at each, because a passively cooled board is slower at the end of an indexing run than at the start and only the last figure is a rate.

## Reading the result

Good:

- strict R@1 in the low tens of percent, R@10 several times the random baseline. Retrieval is working.
- strict R@50 close to, but not at, 100%: the candidate pool is 50 deep, so R@50 is the recall of the retrieval step itself.
- graded nDCG@10 well above the strict recall — expected, since many unlabelled tracks genuinely match a caption.
- median latency in the tens of milliseconds; CLAP text encoding dominating a query's server time.

Suspicious, in the order worth checking:

- **strict R@10 at or below the random baseline (1.4%)** — a harness bug, not a model result. Check that `manifest.csv`'s `kalinka_track_id` column matches ids the endpoint returned (`results_<run>.csv` holds dataset ids, so a mismatch shows up as every rank being a miss), and that `index_summary.json` reports 706 embedded.
- **many empty answers** — queries returning nothing. Twelve are expected: captions with non-ASCII characters are skipped by the query encoder. Many more than that means the searcher lost its text tower or sqlite-vec; grep the server log for `sqlite-vec not available` and `CLAP encode skipped`.
- **soft-Recall@0.5 near 100%** — the threshold is too loose to separate anything; read the 0.6 and 0.7 columns instead, and treat 0.5 as a sanity floor rather than a score.
- **nDCG@10 near 1.0** — the judge is agreeing with itself. Check that the judge model is not a CLAP export and that `fingerprint.json` names the mpnet revision.
- **R@50 exactly 100%** — every target was in the pool; either the candidate limit was raised past the library size or the library is smaller than the manifest.
- **mood P@10 below prevalence (lift under 1.0)** — the ranking is worse than shuffling for that tag. One or two such tags among the abstract ones is a known CLAP weakness; most of them means the query phrasing or the tag join is broken, so check `mood_queries.csv` has plausible relevant sets.
- **fusion and CLAP-only scoring identically across every family** — the mood leg never engaged. Check the server log for `mood target V=… conf=…` lines during the `tags_fusion` run; a confidence of 0 everywhere means the mood index failed to load.
- **indexing wall clock far above the sum of its parts** — the embedder was idle, usually waiting for its poll interval rather than a nudge. The timeline shows this as a gap; it is a finding about the pipeline, not a measurement error.
- **device probe: I/O above a few percent, or a location that reads at over 1 GB/s** — check the `on <device> (<fstype>)` line the probe prints for that location. `/tmp` is tmpfs on Raspberry Pi OS, so a "disk" measurement taken there is a measurement of RAM; this is how the first attempt at the reference run was wrong.
- **device probe: drift above a few percent between the first round and the last** — the board is throttling, not the code regressing. The temperature and clock columns say which; quote the last round as the indexing rate and treat the first as a cold best case.
- **device probe: inference share far below 85% on a small board** — either the audio tower is not the shipped one, or the session options changed. Check `intra_op_num_threads` in `clap_onnx`: the shipped value is deliberately below the core count so indexing leaves the player room.

## Changing it

**A new dataset.** `sddbench/dataset.py` is the only part that knows about SDD. Provide: a caption list (`Caption(caption_id, track_id, text, is_valid_subset)`), audio files prepared into one flat directory with opaque names, and a manifest. Everything downstream works off `manifest.csv` and the caption list; `metrics.py` never sees a filename. Keep the checksum verification — a silently truncated download looks exactly like a worse model.

**A new judge.** `sddbench/judge.py` holds the repository, the pinned revision, the pooling and the token limit, and `fingerprint()` hashes the files it actually loaded. Swap those four constants for another sentence-embedding model with an ONNX export. Do not swap in an audio-text model: the judge must have no relationship to the system under test. Delete `caption_similarity.csv` afterwards or the cached matrix from the old judge will be reused.

**A new mood vocabulary or query phrasing.** `sddbench/tags.py` holds the support floors, the affective/contextual split and the two phrasings. The query set follows from the corpus and those floors — it is not a hand-picked list, so pointing the suite at more audio widens it automatically.

**A new ranking configuration.** Add a name to `RUNS` in `run.py` and a title in `report.RUN_TITLES`, and set whatever configuration it needs through `/server/config` the way `instance.set_mood` does. Anything reachable as a setting can be ablated without re-indexing; anything that changes the vectors needs a fresh `<out>` directory.

## Instrumentation

The server is not modified. `sddbench/timing/` holds a `sitecustomize.py` that an environment variable arms and a `bench_timing.py` that installs an import hook, wrapping about twenty functions to log their durations. `bench_timing.TOUCHES` lists them and the report prints that list, so a wrapper cannot be added without appearing in the next report. All are timing-only except the two KNN wrappers, which also log the distances the call returned — the endpoint publishes rank order and no score, so without them the results file would have ranks and nothing behind them.

If a future release moves one of those functions, the hook logs `BENCH could not patch …` and the run continues with coarser timings rather than failing.
