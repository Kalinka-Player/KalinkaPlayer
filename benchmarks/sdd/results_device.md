# What an embedding costs on the device

> The reference device measurement, kept in the repository beside the retrieval reports so the timeline in [results.md](results.md) can be read against the hardware the server is built for. Regenerate it with `device_probe.py` as described in [RUNBOOK.md](RUNBOOK.md); the mood suite's report is [results_mood.md](results_mood.md).

The captions benchmark times the pipeline on a 22-thread desktop, which is not what a Kalinka install runs on. This asks the same question of a Raspberry Pi 4 with the library on an external disk: of the seconds one track costs to embed, how many are reading the file, how many are decoding it, and how many are the model?

One two-minute recording, embedded 48 times through the shipped encoder — cold page cache and hot, from the USB disk, the SD card and a tmpfs, visited round-robin so that no medium gets charged for the board warming up.

## The split

| | first round | eighth round | share, median over the run |
|---|---:|---:|---:|
| **Total** | **8.14 s** | **10.79 s** | 100% |
| File I/O — read and seek | 0.36 s ¹ | 0.02 s | 0.2% |
| Decode and resample | 0.31 s | 0.36 s | 3.4% |
| ONNX inference | 6.96 s | 9.65 s | 90.2% |
| Stream open, header probe, quantise, collect | 0.52 s | 0.76 s | 6.2% |
| *(audio tower load, once per embedder start)* | *3.3 s* | | |

¹ The first read after the drive had been sitting idle. Every other cold read in the run cost 0.018–0.032 s.

Nine tenths of a track is the model. Reading the file is two tenths of one percent, and decoding plus resampling is three percent — together they are smaller than the run-to-run variation caused by the board's temperature. **Nothing outside the audio tower is worth optimising on this hardware.**

For comparison, the same script on the desktop that produced [results.md](results.md) reports about 0.8 s per track, 77–79% of it inference and 7–9% decode. The Pi is roughly ten times slower per track, and the split is not the desktop's scaled down: inference grows from three quarters of the time to nine tenths of it, because decoding is the part that got comparatively cheaper.

## Storage is not the variable

| Location | Device | Cold read of the whole 4.80 MB file | I/O inside an embedding, cold | hot |
|---|---|---:|---:|---:|
| USB disk | `/dev/sda1` ext4 — a spinning Seagate Expansion, on the USB **3** bus (5 Gbps link) | 0.019 s (257 MB/s) | 0.021 s | 0.017 s |
| SD card | `/dev/mmcblk0p2` ext4 | 0.115 s (42 MB/s) | 0.028 s | 0.017 s |
| tmpfs | `/tmp` — RAM | 0.003 s (1500 MB/s) | 0.017 s | 0.017 s |

The fragment loader seeks to 25%, 50% and 75% of the track and decodes ten seconds at each, so it touches 4.01 MB of the 4.80 MB file rather than streaming all of it. Off a spinning disk that costs about 20 ms. **The disk is faster than the SD card the operating system is on**, and both are close enough to RAM that the choice does not show up in the total.

Two things this measurement had to get right to be worth anything:

- **`/tmp` is a tmpfs on Raspberry Pi OS.** The first attempt at this ran from there and measured RAM while believing it was measuring storage. The probe now prints the device and filesystem of every location it was given, and a reader should check that line before believing the I/O column.
- **The page cache.** Each track is embedded once with `posix_fadvise(DONTNEED)` applied first, which is what a first indexing pass actually pays, and once with the cache hot. Without that, every number after the first is a RAM measurement too.

## Heat is the variable

Eight rounds of continuous embedding, the same track from the same disk:

| Round | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Seconds per track | 8.14 | 8.60 | 9.10 | 9.78 | 10.05 | 10.35 | 10.71 | 10.79 |
| Die temperature | 78 °C | 83 °C | 84 °C | 83 °C | 84 °C | 84 °C | 84 °C | 83 °C |
| ARM clock | 1800 | 1677 | 1580 | 1677 | 1531 | 1531 | 1580 | 1677 |

**A third slower after eight minutes of work, and it had not levelled off.** The board idles at 67–69 °C, began this run at 73 °C, was past 80 °C within half a minute of embedding, and spent the rest of the run against its limit: the firmware's throttling word goes from `0xe0000` before the run — capping has happened at some point since boot — to `0xe0006` after it, which is bits 1 and 2: *arm frequency capped now*, *throttled now*. The clock column above is the firmware's own answer. Linux's `scaling_cur_freq` reported a contented 1800 MHz throughout, which is what the governor asked for rather than what the silicon delivered, and an earlier version of this measurement believed it.

This is not a defect in the code and there is nothing in the pipeline to fix for it. It does change what the honest number is:

| Library | At the cold-start rate (8.2 s) | At the sustained rate (10.9 s) |
|---|---:|---:|
| 100 tracks | 14 min | 18 min |
| 1000 tracks | 2 h 17 m | 3 h 01 m |
| 5000 tracks | 11 h 22 m | 15 h 05 m |
| 10000 tracks | 22 h 44 m | 30 h 11 m |

Quote the right-hand column. A first indexing pass is exactly the sustained load that produces it, and the left-hand column only describes the first minute of one. This is also the number to revisit if a device ever gets a fan: on a board that is thermally limited rather than core limited, cooling is worth more than any amount of pipeline work.

## What the thread count is worth

The audio session runs with `intra_op_num_threads` 2 on a four-core board, deliberately: indexing is not supposed to take the whole machine. Sweeping it is the one tuning question the split leaves open, and it has to be swept in both directions — each value is measured on a hotter board than the one before it, so a single order confounds the setting with the heat.

| Threads | ascending order | descending order | mean | vs shipped |
|---|---:|---:|---:|---:|
| 1 | 12.89 s | 14.20 s | 13.55 s | 1.46x |
| **2 — shipped** | 8.96 s | 9.60 s | **9.28 s** | 1.00x |
| 3 | 8.70 s | 8.62 s | 8.66 s | 0.93x |
| 4 | 8.80 s | 8.04 s | 8.42 s | 0.91x |

The second thread is worth 1.46x. The third and fourth together are worth 9%, and the two orders disagree about four threads by 0.76 s, three times what the fourth thread appears to buy over the third. That is what a thermally limited board looks like: past two threads the extra core mostly makes extra heat, which the firmware then takes back. The shipped setting is not leaving much on the table, and what it buys instead is two cores that a first indexing pass never touches while something is playing.

(Absolute seconds in this table are higher than in the split above because the sweep runs after the main measurement, on a board already at its limit; only the ratios are meaningful. An earlier one-off measurement on a cool board put three threads 14% ahead — that figure came from a board that had not yet saturated, and it does not survive a sustained run.)

## How it was measured

`device_probe.py`, one file copied to the device and run by the server's own interpreter (`/opt/kalinka/venv/bin/python`), against the installed plugin. It wraps exactly three things and changes nothing else:

| Wrapped | What it measures |
|---|---|
| the stream handed to `get_audio_embedding` | time inside `read` and `seek` — the I/O column |
| `clap_onnx._read_fragment` | fragment decode and resample, minus the I/O that happened inside it |
| the audio session's `run` | ONNX inference |

Whatever those three do not account for is the *other* column, which is therefore a remainder and not a measurement: opening the stream, libsndfile's header probe, the int16 quantisation roundtrip, the per-fragment garbage collection.

The server was left running. The probe is its own process with its own model directory and opens the audio read-only, so nothing the server owns was touched; it competes for CPU while it runs, which is the one reason to stop playback first.

## The device

- Raspberry Pi 4 Model B Rev 1.5 — 4 cores, 3795 MB RAM, idling at 67–69 °C with the cooling it has
- Linux 6.18.39+rpt-rpi-v8 aarch64, glibc 2.41, Python 3.13.5 in `/opt/kalinka/venv`
- The installed release, not a checkout: kalinka-server 5.0.0, kalinka-plugin-localfiles 5.0.0, kalinka-plugin-sdk 3.1.0. The encoder has changed once since that release — the fragment loader now takes an open stream where it used to take a path — and it is that stream the probe wraps; libsndfile accepts either, so the probe measures the released code as faithfully as it would the current one. Session options are identical in both.
- onnxruntime 1.24.4, numpy 1.26.4, soundfile 0.13.1, soxr 1.1.0 — the desktop run in [results.md](results.md) had onnxruntime 1.27.0 and numpy 2.5.1, so the two are not the same build of the runtime
- Audio tower: `clap_audio_encoder.onnx` from the `clap-onnx-v2` release, 284,984,620 bytes, sha256 `1b0c8b624a8746e6…` — the same file the retrieval benchmark used
- Session options as shipped: `inter_op_num_threads` 1, `intra_op_num_threads` 2, CPU arena disabled
- Audio: one 4,801,306-byte 320 kbps MP3 from the Song Describer set, of which the loader read 4,009,418 bytes per embedding
- Storage: `/mnt/usb` is a Seagate Expansion USB disk (ext4, spinning, 5 Gbps link); the root filesystem is the SD card
- Measured 2026-09-20

## What this does not measure

- **Query time.** Only the audio tower was timed. A search on a Pi also runs the text tower, which on the desktop is the largest part of a query, and nobody has timed it on this hardware.
- **The mood head, the scan and the database.** The timeline in [results.md](results.md) covers those; they are excluded here by construction, so this is the cost of an embedding rather than the cost of indexing a track.
- **Playback at the same time.** The box was otherwise idle apart from its own server. Indexing while something is playing has fewer cores to work with and makes more heat per second of audio delivered.

## Caveats

- **One board, one enclosure, one room.** The thermal behaviour is this device's. A Pi with a fan, or a cooler room, moves the sustained rate towards the cold-start rate and possibly past it; a Pi in a sealed case moves the other way.
- **One shape of file.** A two-minute 320 kbps MP3 at 48 kHz. Decode cost varies with the source — a 44.1 kHz file pays for resampling, a long FLAC costs the same as a short one because the loader reads fragments rather than the file — but all of that is inside the 3% that decoding gets.
- **The medians are over a run that was getting slower.** Within a round the three locations are within a few percent of each other; across rounds the same configuration moves by a third. Compare locations inside a round, never between rounds.
- **Model load is per embedder start**, not per track: 3.3 s from the USB disk, paid once when the embedder process comes up.
- **One device, one run.** Everything here says where the time goes on this machine, not what the variance is across machines.
