# System test

A full-stack check of the localfiles module: a Kalinka server of its own in a throwaway fakeroot, a Samba server in a rootless podman container, and ten public-domain recordings split between a local folder and the share. It waits for the real indexer, enricher and CLAP embedder to finish, then checks name search, AI search, the content route, the URL a renderer is handed, and that removing and restoring a track is seen through inotify (local folder) or the startup scan after an in-app restart (share).

It is opt-in because it is slow and needs the outside world: podman, network access to archive.org, MusicBrainz and the model release, a few minutes of CPU for embedding, and on the first run roughly 800 MB of models.

```
make system-test
```

Equivalent to `KALINKA_SYSTEM_TEST=1 .venv/bin/python -m pytest tests/system -o log_cli=true --log-cli-level=INFO`. Without the variable nothing here is collected — not even imported — so `pytest tests/` stays cheap.

Downloads land in `~/.cache/kalinka-system-test` (override with `KALINKA_SYSTEM_TEST_CACHE`). Point that directory's `models/` at, or copy in, an existing set of CLAP models to skip the model download; the server's model directory is a symlink to it.

On failure the server log tail is attached to the report. The full log stays in the pytest temp directory (`.../kalinka0/prefix/var/log/kalinka/server.log`).
