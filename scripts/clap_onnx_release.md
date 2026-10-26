# CLAP ONNX release process

This is the recipe for cutting a new `clap-onnx-vN` release of the audio
encoder, text encoder, and tokenizer that the embedder downloads on first
boot.

Update this file when the recipe changes — it's the authoritative source.

## Currently shipping

| Tag | Backbone | Checkpoint | Audio .onnx | Text .onnx | Tokenizer | Notes |
|---|---|---|---|---|---|---|
| `clap-onnx-v1` | HTSAT-tiny | `630k-audioset-best.pt` (general audio) | ~85 MB + ~225 MB `.onnx.data` | ~500 MB | ~3.5 MB | Original. Used the `.onnx.data` external-weights file for the audio encoder. |
| `clap-onnx-v2` | HTSAT-base | `music_audioset_epoch_15_esc_90.14.pt` (music) | **285 MB single file** | 501 MB | 3.5 MB | Current. Self-contained audio encoder — no `.onnx.data`. |

## Steps to cut a new release

### 1. Export on a dev machine

Needs the kalinka-server `.venv` (which has `laion-clap`, `torch`,
`onnx`, `huggingface_hub`). Don't run this on the Pi.

```bash
# Default = HTSAT-base music checkpoint, auto-downloaded from HF
python scripts/export_clap_onnx.py --output-dir /tmp/clap_onnx_vN
```

The script validates the ONNX output against the PyTorch reference path
(cosine similarity must be ≥ 0.999 — currently observed at 1.000000 on
both audio and text). It will exit non-zero if validation fails.

Other recipes:

```bash
# Explicit checkpoint
python scripts/export_clap_onnx.py --output-dir /tmp/clap_onnx_vN \
    --ckpt /path/to/checkpoint.pt

# Reproduce v1 (HTSAT-tiny general audio)
python scripts/export_clap_onnx.py --output-dir /tmp/clap_onnx_v1 \
    --amodel HTSAT-tiny
```

### 2. Publish the GitHub release

1. Go to <https://github.com/Kalinka-Player/KalinkaPlayer/releases/new>
2. Tag: `clap-onnx-vN` (matches the value of `_RELEASE_BASE` in
   [`clap_onnx.py`](../packages/kalinka-plugin-localfiles/src/kalinka_plugin_localfiles/embedder/clap_onnx.py))
3. Title: `CLAP ONNX vN — <one-line description>`
4. Body: paste the row from the table above plus any model-swap notes
5. Upload exactly the files listed in `_MODEL_URLS` in `clap_onnx.py`:
   - `clap_audio_encoder.onnx`
   - `clap_audio_encoder.onnx.data` (only if the export produced one)
   - `clap_text_encoder.onnx`
   - `clap_tokenizer.json`
6. Publish.

**Common mistake**: renaming the files. The loader's
[`_MODEL_FILENAMES`](../packages/kalinka-plugin-localfiles/src/kalinka_plugin_localfiles/embedder/clap_onnx.py)
expects exact-match filenames; renaming breaks first-boot download.

### 3. Bump the code

In [`clap_onnx.py`](../packages/kalinka-plugin-localfiles/src/kalinka_plugin_localfiles/embedder/clap_onnx.py):

```python
_RELEASE_BASE = "https://github.com/Kalinka-Player/KalinkaPlayer/releases/download/clap-onnx-vN"
```

If the new export does *or doesn't* produce a `.onnx.data` sibling for
the audio encoder, sync `_MODEL_URLS` / `_MODEL_FILENAMES` to match —
extra entries cause spurious 404 download attempts on first boot.

In [`embedding_utils.py`](../packages/kalinka-plugin-localfiles/src/kalinka_plugin_localfiles/embedding_utils.py):

```python
CLAP_MODEL_VERSION = N     # bump — drives re-embedding
```

Bumping `CLAP_MODEL_VERSION` is what causes the embedder to invalidate the existing `embedding_clap_audio` blobs and re-schedule embedding jobs for every track. **If you forget this, the new model is loaded but never used** — the searcher will keep KNN-ing against old vectors.

It is a code constant, not a config field: a version the user could edit is a version that silently disables search when set wrong, and the value has to match what the shipped model actually is.

### 4. Deploy & migrate

On each player:

```bash
# Stop the server (the embedder process is a child; this stops both)
systemctl --user stop kalinka

# Remove the cached model files so the loader re-downloads from v(N)
rm /var/lib/kalinka/models/clap_audio_encoder.onnx{,.data}
rm /var/lib/kalinka/models/clap_text_encoder.onnx
rm /var/lib/kalinka/models/clap_tokenizer.json

# (Optional) start the server; the embedder will:
#   - download new ONNX from the GitHub release
#   - notice CLAP_MODEL_VERSION > stored versions
#   - schedule embedding jobs for every track
#   - work through them in the background
systemctl --user start kalinka
```

Re-embedding takes minutes-to-hours depending on library size and the
new backbone's per-track latency. HTSAT-base ≈ 2-3× HTSAT-tiny per
fragment; with 3 fragments per track at ~3-5s per fragment on a Pi,
budget ~10-15s per track. 500 tracks ≈ 1.5-2 hours.

Progress is observable via:

```bash
sqlite3 ~/kalinka/localfiles.db \
  "SELECT stage, status, COUNT(*) FROM embedding_jobs GROUP BY 1,2 ORDER BY 1,2"
```

`clap_audio` `pending` going to 0 means re-embedding is complete.

### 5. Measure

Re-run [`/assess-ai-search`](../.claude/skills/assess-ai-search/SKILL.md)
to quantify the change. Archive the previous report as
`REPORT.pr{N-1}-baseline.md` before overwriting so the delta is
recoverable.

## Rollback

To revert without redeploying old code:

1. Stop the server.
2. Restore the old model files from `/var/lib/kalinka/models/` (or
   manually download from the previous release tag).
3. Revert the `CLAP_MODEL_VERSION` bump in code and redeploy.
4. Restart. The embedder will not re-schedule jobs (because the constant is now ≤ the stored version) and the searcher will continue using the older embeddings.

There is no config-only rollback: the version lives in code so that the shipped model and the version that keys its embeddings can never disagree.
