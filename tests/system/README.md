# System test

A full-stack check of the localfiles module: a Kalinka server of its own in a throwaway fakeroot, a Samba server in a rootless podman container, and ten public-domain recordings split between a local folder and the share. It waits for the real indexer, enricher and CLAP embedder to finish, then checks name search, AI search, the content route, the URL a renderer is handed, and that removing and restoring a track is seen through inotify (local folder) or the startup scan after an in-app restart (share).

It is opt-in because it is slow and needs the outside world: podman, network access to archive.org, MusicBrainz and the model release, a few minutes of CPU for embedding, and on the first run roughly 800 MB of models.

```
make system-test
```

Equivalent to `KALINKA_SYSTEM_TEST=1 .venv/bin/python -m pytest tests/system -o log_cli=true --log-cli-level=INFO`. Without the variable nothing here is collected — not even imported — so `pytest tests/` stays cheap.

Downloads land in `~/.cache/kalinka-system-test` (override with `KALINKA_SYSTEM_TEST_CACHE`). Point that directory's `models/` at, or copy in, an existing set of CLAP models to skip the model download; the server's model directory is a symlink to it.

On failure the server log tail is attached to the report. The full log stays in the pytest temp directory (`.../kalinka0/prefix/var/log/kalinka/server.log`).

## Large library

A second test, opt-in on its own, for what only shows on a library the size a long-time user has. The library's workers all write one SQLite file, and a writer that waits five seconds for the lock gives up, so a statement whose cost grows with the library ends up locking the others out: on a 19,000-track library the CLAP job scheduling once held the lock for minutes each cycle, and enrichment never finished.

```
make system-test-large
```

The real indexer builds a library of 19,182 synthetic tracks (`KALINKA_SYSTEM_TEST_LARGE_TRACKS` to change that), which is then marked enriched and embedded, as a settled library is. Two real albums arrive in it and one is then split into disc folders. Everything has to settle, no write may fail as locked, and a probe that polls for the write lock ten times a second must never be kept from it for more than 2.5 seconds. When it is, the failure names the database calls that were under way, taken from a log every worker keeps of its calls for the run (`call_log_hook/`).

It needs the network (archive.org, MusicBrainz, the model release) but not podman. Where pytest's temp directory is in memory (a tmpfs `/tmp`) it takes about four minutes. `make system-test-large ARGS=--basetemp=<folder on a disk>` puts the server's database on that disk instead, where every commit costs a sync as it does on a device: indexing then takes several minutes more, and the lock is held for as long as a device would hold it.
