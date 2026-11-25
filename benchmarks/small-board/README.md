# Kalinka on a small board

Measures what Kalinka costs on a Raspberry Pi 1, an original Zero or any other board at the bottom of the range: memory, start-up time, API latency, how fast the library indexes, and the CPU that playback takes. The numbers decide whether the indexer stays in Python or moves to a native process.

`board_probe.py` is a single file that needs only the standard library. Copy it to the board and run it there as root: the library database and the per-process memory figures belong to the service user. Each command prints a summary and appends JSON lines to `kalinka-probe.jsonl`. Send that file back.

## Setting up the board

- Raspberry Pi OS Lite (32-bit), trixie, on a fresh card. Turn AI search off in My Library: the board cannot install it.
- Point My Library at the music. A USB disk is the realistic case; leave the first scan for the `index` step below.
- Leave the board idle for a minute before each measurement.

## The run

| Step | Command | What it measures |
|---|---|---|
| 1 | `sudo systemctl restart kalinka && sudo python3 board_probe.py ready` | Seconds until the server answers |
| 2 | `sudo python3 board_probe.py index` | The first scan: tracks per minute, and the indexer's CPU seconds per track. It ends after five minutes without a new track. |
| 3 | `sudo python3 board_probe.py memory` | RSS, PSS and swap per process, and the board's free memory |
| 4 | `sudo python3 board_probe.py rescan` | A full rescan of an indexed library: what the nightly rescan costs |
| 5 | `sudo python3 board_probe.py api --query "<a word in your track titles>"` | Status, queue and name-search latency, 10 requests each |
| 6 | `sudo python3 board_probe.py cpu --label idle` | CPU of each process over a minute with nothing playing |
| 7 | `sudo python3 board_probe.py cpu --label flac-16-44` | The same while a 16-bit 44.1 kHz FLAC plays. Start the track first. |
| 8 | `sudo python3 board_probe.py cpu --label flac-24-192` | The same with a 24-bit 192 kHz FLAC |
| 9 | `sudo python3 board_probe.py memory` | Memory again, after the library has settled |

Note the library's size (tracks and albums) and the storage it is on alongside the results.
