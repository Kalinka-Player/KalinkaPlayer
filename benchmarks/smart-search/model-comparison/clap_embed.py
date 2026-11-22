"""Shared CLAP embedding utilities for the mood/VA pipeline.

This module mirrors the PRODUCTION ONNX inference path byte-for-byte. The
canonical implementation lives in
``packages/kalinka-plugin-localfiles/src/kalinka_plugin_localfiles/embedder/clap_onnx.py``
and we deliberately replicate its preprocessing here so that embeddings we
train on are identical to what the Raspberry Pi computes at ingest time.

Anything that touches preprocessing is a parity hazard. Every parameter is an
explicit named constant so divergence from production is auditable.

Dependencies: numpy, onnxruntime, soundfile, soxr, tokenizers. NO torch, NO
laion_clap.
"""

from __future__ import annotations

import os
from typing import Iterator, Optional

import numpy as np

# --- Constants matching laion_clap (non-fusion, HTSAT-base) and production ---
SAMPLE_RATE = 48_000                       # CLAP music checkpoint operates at 48 kHz
FRAGMENT_SECONDS = 10                       # one encoder window = 10 s
MAX_SAMPLES = SAMPLE_RATE * FRAGMENT_SECONDS  # 480_000 samples per window
TOKEN_MAX_LEN = 77                         # RoBERTa max_len
EMBED_DIM = 512                            # CLAP joint embedding dim

# Multi-fragment sampling thresholds (seconds of source audio), per production:
#   duration <  MULTI_FRAGMENT_MIN_S      -> 1 fragment (repeat-padded)
#   MULTI.. <= duration < THREE_FRAG_MIN  -> 2 fragments at 33% / 66%
#   duration >= THREE_FRAGMENT_MIN_S      -> 3 fragments at 25% / 50% / 75%
MULTI_FRAGMENT_MIN_S = 15.0
THREE_FRAGMENT_MIN_S = 30.0

# ONNX graph I/O names (verified against the exported artifacts).
AUDIO_INPUT = "waveform"
AUDIO_OUTPUT = "embedding"
TEXT_INPUTS = ("input_ids", "attention_mask")

# Checkpoint identity, recorded into artifact metadata.
CLAP_CHECKPOINT_ID = "music_audioset_epoch_15_esc_90.14.pt"
CLAP_HF_REPO = "lukewys/laion_clap"

# Tokenizer padding config (production values).
PAD_ID = 1
PAD_TOKEN = "<pad>"


# --------------------------------------------------------------------------- #
# Preprocessing primitives (exact copies of the production implementation)
# --------------------------------------------------------------------------- #
def quantize_int16_roundtrip(waveform: np.ndarray) -> np.ndarray:
    """Replicate laion_clap's float32 -> int16 -> float32 quantisation.

    laion_clap trained on audio that had passed through an int16 codec, so the
    encoder expects this quantisation. Skipping it is a silent parity bug.
    """
    clipped = np.clip(waveform, -1.0, 1.0)
    as_int16 = (clipped * 32767.0).astype(np.int16)
    return (as_int16 / 32767.0).astype(np.float32)


def pad_or_crop(waveform: np.ndarray, max_len: int = MAX_SAMPLES) -> np.ndarray:
    """Repeat-pad short audio, deterministic first-crop for long audio."""
    length = len(waveform)
    if length >= max_len:
        return waveform[:max_len]
    n_repeat = max_len // length
    padded = np.tile(waveform, n_repeat)
    remainder = max_len - len(padded)
    if remainder > 0:
        padded = np.concatenate([padded, np.zeros(remainder, dtype=np.float32)])
    return padded


def fragment_starts_s(duration_s: float) -> list[float]:
    """Pick fragment start positions (seconds) for a track of *duration_s*."""
    if duration_s < MULTI_FRAGMENT_MIN_S:
        return [0.0]
    if duration_s < THREE_FRAGMENT_MIN_S:
        ratios = (0.33, 0.66)
    else:
        ratios = (0.25, 0.50, 0.75)
    max_start = max(0.0, duration_s - FRAGMENT_SECONDS)
    return [min(duration_s * r, max_start) for r in ratios]


def to_mono(chunk: np.ndarray) -> np.ndarray:
    """Average channels to mono (production downmix)."""
    if chunk.ndim == 2:
        return chunk.mean(axis=1, dtype=np.float32)
    return chunk


def resample_to_48k(chunk: np.ndarray, src_sr: int) -> np.ndarray:
    """Resample to 48 kHz with soxr (production resampler) if needed."""
    if src_sr == SAMPLE_RATE:
        return chunk.astype(np.float32, copy=False)
    import soxr

    return soxr.resample(chunk, src_sr, SAMPLE_RATE).astype(np.float32, copy=False)


def load_fragments(
    file_path: str,
    start_s: Optional[float] = None,
    end_s: Optional[float] = None,
) -> Iterator[np.ndarray]:
    """Yield production-preprocessed 10 s fragments (each MAX_SAMPLES long).

    With ``start_s``/``end_s`` given, fragments are sampled *within that window
    only* (used to embed the exact DEAM-annotated excerpt). Otherwise the
    whole file is used, exactly as production does.
    """
    import soundfile as sf

    with sf.SoundFile(file_path) as f:
        src_sr = f.samplerate
        total_s = f.frames / float(src_sr)

        win_start = 0.0 if start_s is None else max(0.0, start_s)
        win_end = total_s if end_s is None else min(total_s, end_s)
        win_dur = max(0.0, win_end - win_start)
        if win_dur <= 0:
            return

        for rel_start in fragment_starts_s(win_dur):
            abs_start = win_start + rel_start
            n_src = int(FRAGMENT_SECONDS * src_sr)
            try:
                f.seek(int(abs_start * src_sr))
                chunk = f.read(n_src, dtype="float32", always_2d=False)
            except Exception:
                continue
            if chunk.size == 0:
                continue
            chunk = to_mono(chunk)
            chunk = resample_to_48k(chunk, src_sr)
            chunk = quantize_int16_roundtrip(chunk)
            yield pad_or_crop(chunk)


def l2_normalize(v: np.ndarray) -> np.ndarray:
    norm = np.linalg.norm(v)
    return v / norm if norm > 0 else v


# --------------------------------------------------------------------------- #
# ONNX encoders
# --------------------------------------------------------------------------- #
def make_session(onnx_path: str):
    import onnxruntime as ort

    opts = ort.SessionOptions()
    opts.inter_op_num_threads = 1
    opts.intra_op_num_threads = max(1, (os.cpu_count() or 2) - 1)
    return ort.InferenceSession(onnx_path, sess_options=opts,
                                providers=["CPUExecutionProvider"])


def embed_audio_file(
    session,
    file_path: str,
    start_s: Optional[float] = None,
    end_s: Optional[float] = None,
    normalize: bool = True,
) -> Optional[np.ndarray]:
    """Production-faithful audio embedding: per-fragment encode, mean-pool.

    Production does NOT normalize inside the graph; the caller normalizes
    before storage. We default ``normalize=True`` so the returned vector
    matches what gets stored/searched on the Pi.
    """
    accum: Optional[np.ndarray] = None
    n = 0
    for waveform in load_fragments(file_path, start_s, end_s):
        out = session.run(None, {AUDIO_INPUT: waveform[np.newaxis, :]})
        vec = out[0][0].astype(np.float32)
        accum = vec if accum is None else accum + vec
        n += 1
    if accum is None or n == 0:
        return None
    accum /= n
    return l2_normalize(accum) if normalize else accum


def embed_waveform(session, waveform_48k_mono: np.ndarray,
                   normalize: bool = False) -> np.ndarray:
    """Embed a single already-48k-mono waveform (quantize + pad + encode).

    Used by the parity check so both encoders see identical input. Single
    window only (no multi-fragment averaging).
    """
    wav = quantize_int16_roundtrip(to_mono(waveform_48k_mono))
    wav = pad_or_crop(wav)
    out = session.run(None, {AUDIO_INPUT: wav[np.newaxis, :]})
    vec = out[0][0].astype(np.float32)
    return l2_normalize(vec) if normalize else vec


def make_tokenizer(tokenizer_path: str):
    from tokenizers import Tokenizer

    tok = Tokenizer.from_file(tokenizer_path)
    tok.enable_truncation(max_length=TOKEN_MAX_LEN)
    tok.enable_padding(length=TOKEN_MAX_LEN, pad_id=PAD_ID, pad_token=PAD_TOKEN)
    return tok


def embed_text(session, tokenizer, text: str,
               normalize: bool = True) -> np.ndarray:
    """ONNX text embedding (the encoder the Pi runs at query time)."""
    enc = tokenizer.encode(text)
    input_ids = np.array([enc.ids], dtype=np.int64)
    attention_mask = np.array([enc.attention_mask], dtype=np.int64)
    out = session.run(None, {
        TEXT_INPUTS[0]: input_ids,
        TEXT_INPUTS[1]: attention_mask,
    })
    vec = out[0][0].astype(np.float32)
    return l2_normalize(vec) if normalize else vec
