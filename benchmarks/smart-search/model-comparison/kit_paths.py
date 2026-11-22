"""Where the scripts find their inputs. Downloads and caches default to work/ (not committed);
every path can be overridden by an environment variable."""
import os
from pathlib import Path

KIT = Path(__file__).resolve().parent


def _p(var: str, default: Path) -> Path:
    return Path(os.path.expanduser(os.environ.get(var, str(default))))


# MTG-Jamendo autotagging TSVs (autotagging_genre/instrument/moodtheme.tsv) from
# https://github.com/MTG/mtg-jamendo-dataset/tree/master/data and the 600 clips
# (clips/<jamendo_id>.wav, the leading ~12 s of each track).
MTG_BENCH = _p("MTG_BENCH_DIR", KIT / "work/mtg_jamendo_eval")
# raw_30s/audio-low shard 00 of MTG-Jamendo (<id>.low.mp3); ids in inputs/mtgshard_ids.txt.
MTG_SHARD = _p("MTG_SHARD_DIR", KIT / "work/mtg_jamendo/00")
# song_id,audio_path,valence_mean,arousal_mean, sorted by song id (deam_manifest.py).
DEAM_MANIFEST = _p("DEAM_MANIFEST", KIT / "work/deam_manifest.csv")
# The clap-onnx-v2 release Kalinka downloads: clap_audio_encoder.onnx,
# clap_text_encoder.onnx, clap_tokenizer.json, va_head_v1.onnx, mood_index_v1.npz,
# and minilm/ (int8 all-MiniLM-L6-v2: model.onnx + tokenizer.json).
MODELS = _p("KALINKA_MODELS", KIT / "work/models")
# TinyMU checkpoint dir (tinymu.pt, tinymu.yaml, matpac_plus_as_48_1_map_enconly.pt)
# with the authors' code cloned into TinyMU/ (tested at commit 385133d).
TINYMU = _p("TINYMU_DIR", KIT / "work/models/tinymu")
# Embeddings and captions: <cache>/<corpus>/{clap_audio,clap2s_frag10,clap2s_seg30}.npz
# and captions_e87ad2bb.jsonl.
CACHE = _p("EVAL_CACHE", KIT / "work/cache")
