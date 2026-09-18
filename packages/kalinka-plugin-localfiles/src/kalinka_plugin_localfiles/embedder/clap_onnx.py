"""ONNX Runtime wrapper for the CLAP music checkpoint.

The shipped model is HTSAT-base + RoBERTa text encoder, trained on
the AudioSet music subset (``music_audioset_epoch_15_esc_90.14.pt``
from huggingface.co/lukewys/laion_clap). It replaces the original
general-audio HTSAT-tiny ``630k-audioset-best.pt`` that v1 used.

Replaces the PyTorch-based ``laion_clap`` library with a lightweight
ONNX Runtime backend, reducing the embedder process memory footprint
from ~1.7 GB to ~600-800 MB (HTSAT-base is ~2x the weights of tiny;
still well within the 4 GB Pi budget when paired with the multi-
fragment loader below).

Audio loading uses ``soundfile`` (header probe + seek/read fragment)
plus ``soxr`` for resampling, deliberately avoiding ``librosa.load``,
which decodes the whole file into memory and OOMs on a 4 GB Pi for
tracks longer than a few minutes. With fragment-level decoding the
peak waveform allocation is bounded by the 10 s fragment length
regardless of track duration.

Expected model files in *model_dir*:
  - clap_audio_encoder.onnx
  - clap_text_encoder.onnx
  - clap_tokenizer.json

Generate these with ``scripts/export_clap_onnx.py``.
"""

from __future__ import annotations

import gc
import logging
import os
import urllib.request
from typing import TYPE_CHECKING, BinaryIO, Optional

import numpy as np

if TYPE_CHECKING:  # imported lazily at the call site; heavy to load
    import soundfile

from ..embedding_utils import VA_HEAD_VERSION

logger = logging.getLogger(__name__.split(".")[-1])

# ---------------------------------------------------------------------------
# Model download URLs — update after hosting ONNX files
# ---------------------------------------------------------------------------

_RELEASE_BASE = (
    "https://github.com/madenvel/KalinkaPlayer/releases/download/clap-onnx-v2"
)

# HTSAT-base fits in a single ONNX protobuf (≈ 285 MB), so unlike v1
# there's no separate ``.onnx.data`` external-weights file.
_MODEL_URLS: dict[str, str] = {
    "clap_audio_encoder": f"{_RELEASE_BASE}/clap_audio_encoder.onnx",
    "clap_text_encoder": f"{_RELEASE_BASE}/clap_text_encoder.onnx",
    "clap_tokenizer": f"{_RELEASE_BASE}/clap_tokenizer.json",
    # Mood/VA artifacts (from kalinka-training). VA_HEAD_VERSION in the name
    # makes a head update download as a new file, not the cached same-name copy.
    "va_head": f"{_RELEASE_BASE}/va_head_v{VA_HEAD_VERSION}.onnx",
    "mood_index": f"{_RELEASE_BASE}/mood_index_v{VA_HEAD_VERSION}.npz",
}

_MODEL_FILENAMES: dict[str, str] = {
    "clap_audio_encoder": "clap_audio_encoder.onnx",
    "clap_text_encoder": "clap_text_encoder.onnx",
    "clap_tokenizer": "clap_tokenizer.json",
    "va_head": f"va_head_v{VA_HEAD_VERSION}.onnx",
    "mood_index": f"mood_index_v{VA_HEAD_VERSION}.npz",
}

# Audio constants matching laion_clap (non-fusion, HTSAT-base)
_SAMPLE_RATE = 48_000
_FRAGMENT_SECONDS = 10
_MAX_SAMPLES = _SAMPLE_RATE * _FRAGMENT_SECONDS  # 480_000 = 10 s at 48 kHz
_TOKEN_MAX_LEN = 77

# Multi-fragment sampling thresholds (in seconds of source audio):
#   duration <  _MULTI_FRAGMENT_MIN_S  → 1 fragment (repeat-padded)
#   <= duration <  _THREE_FRAGMENT_MIN_S → 2 fragments at 33% / 66%
#   duration >= _THREE_FRAGMENT_MIN_S   → 3 fragments at 25% / 50% / 75%
# Fragment starts are clamped to ``max(0, duration - fragment)`` so
# the read window always fits inside the file.
_MULTI_FRAGMENT_MIN_S = 15.0
_THREE_FRAGMENT_MIN_S = 30.0


def _ensure_model_file(name: str, model_dir: str) -> Optional[str]:
    """Return path to a model file, downloading if necessary.

    Writes to ``<dest>.part`` first and atomic-renames on success.
    Without that, an interrupted ``urlretrieve`` leaves a truncated
    file at ``dest``; on the next call ``os.path.isfile(dest)`` short-
    circuits and onnxruntime fails to parse the partial ONNX — and
    the download never retries because the loader's failure is not
    interpreted as "redownload". Stale ``.part`` from a previous
    interrupted run is removed before the new attempt starts.
    """
    filename = _MODEL_FILENAMES.get(name)
    if not filename:
        logger.error("Unknown model file: %s", name)
        return None

    # Create the model directory tree up front, before checking for
    # existing files. Without this, a fresh install on a host where
    # /var/lib/kalinka/models doesn't exist yet would race
    # `os.path.isfile(dest)` returning False (because the dir is
    # absent) into the download branch, which then has to create the
    # dir anyway — and if `url` is None for some other code path,
    # the directory never gets created and a manual file-copy is
    # impossible without sudo.
    try:
        os.makedirs(model_dir, exist_ok=True)
    except OSError as e:
        logger.error("Cannot create model dir %s: %s", model_dir, e)
        return None

    dest = os.path.join(model_dir, filename)
    if os.path.isfile(dest):
        return dest

    url = _MODEL_URLS.get(name)
    if not url:
        logger.error(
            "Model file '%s' not found at %s and no download URL configured. "
            "Run scripts/export_clap_onnx.py and copy files to %s.",
            filename,
            dest,
            model_dir,
        )
        return None

    tmp = dest + ".part"
    # Clear stale .part from a previous interrupted run (e.g. SIGKILL
    # mid-download). Cannot be a partial-but-resumable file because
    # urllib.request.urlretrieve doesn't support range requests.
    try:
        os.unlink(tmp)
    except FileNotFoundError:
        pass

    logger.info("Downloading '%s' from %s ...", name, url)
    try:
        urllib.request.urlretrieve(url, tmp)
        os.replace(tmp, dest)
    except Exception as e:
        logger.error("Failed to download '%s': %s", name, e)
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        return None

    logger.info("Saved '%s' to %s", name, dest)
    return dest


# ---------------------------------------------------------------------------
# Audio preprocessing (replaces laion_clap waveform handling)
# ---------------------------------------------------------------------------

def _quantize(waveform: np.ndarray) -> np.ndarray:
    """Replicate laion_clap's float32 -> int16 -> float32 quantisation."""
    clipped = np.clip(waveform, -1.0, 1.0)
    as_int16 = (clipped * 32767.0).astype(np.int16)
    return (as_int16 / 32767.0).astype(np.float32)


def _pad_or_crop(waveform: np.ndarray, max_len: int = _MAX_SAMPLES) -> np.ndarray:
    """Repeat-pad short audio, crop long audio (deterministic first-crop)."""
    length = len(waveform)
    if length >= max_len:
        return waveform[:max_len]
    # repeat-pad: tile then zero-pad remainder
    n_repeat = max_len // length
    padded = np.tile(waveform, n_repeat)
    remainder = max_len - len(padded)
    if remainder > 0:
        padded = np.concatenate([padded, np.zeros(remainder, dtype=np.float32)])
    return padded


def _fragment_starts_s(duration_s: float) -> list[float]:
    """Pick fragment start positions (seconds) for a track of *duration_s*.

    See _MULTI_FRAGMENT_MIN_S / _THREE_FRAGMENT_MIN_S for the schedule.
    Each start is clamped so the 10 s read window fits inside the file;
    on very short tracks the single returned start is 0.0 and the
    caller is expected to repeat-pad the result.
    """
    if duration_s < _MULTI_FRAGMENT_MIN_S:
        return [0.0]

    if duration_s < _THREE_FRAGMENT_MIN_S:
        ratios = (0.33, 0.66)
    else:
        ratios = (0.25, 0.50, 0.75)

    max_start = max(0.0, duration_s - _FRAGMENT_SECONDS)
    return [min(duration_s * r, max_start) for r in ratios]


def _read_fragment(
    f: "soundfile.SoundFile",
    start_s: float,
    src_sr: int,
) -> Optional[np.ndarray]:
    """Decode a single 10 s fragment from *f* starting at *start_s*.

    Reads only the bytes needed (header-aware seek + read), averages
    stereo to mono, resamples to 48 kHz with soxr if needed, then
    runs the int16 quantisation roundtrip to match laion_clap's
    training-time preprocessing. Returns ``None`` on read failure.

    Per-fragment failures log at DEBUG; the caller in
    ``_load_audio_fragments`` decides whether the whole file is
    unembeddable and emits a single WARNING in that case. This keeps
    one bad file from spamming three per-fragment warnings × N retry
    attempts × parent album/artist aggregate jobs.
    """
    import soxr

    n_src = int(_FRAGMENT_SECONDS * src_sr)
    try:
        f.seek(int(start_s * src_sr))
        chunk = f.read(n_src, dtype="float32", always_2d=False)
    except Exception as e:
        logger.debug("soundfile read failed at %.2fs: %s", start_s, e)
        return None

    if chunk.size == 0:
        logger.debug("soundfile read at %.2fs returned 0 frames", start_s)
        return None

    # mono: average channels for stereo+ input. soundfile returns
    # shape (frames,) for mono files when always_2d=False.
    if chunk.ndim == 2:
        chunk = chunk.mean(axis=1, dtype=np.float32)

    if src_sr != _SAMPLE_RATE:
        chunk = soxr.resample(chunk, src_sr, _SAMPLE_RATE).astype(
            np.float32, copy=False
        )

    chunk = _quantize(chunk)
    return _pad_or_crop(chunk)


def _describe(audio: BinaryIO) -> str:
    """What to call a stream in a log line. File-backed streams carry the
    name they were opened with; the rest have only their type."""
    return getattr(audio, "name", None) or type(audio).__name__


def _load_audio_fragments(audio: BinaryIO):
    """Yield 10 s mono/48 kHz fragments from an open audio stream.

    Generator — never holds more than one fragment in memory at once,
    so the embedder can run inference and discard each waveform
    before the next read. For streams we can't read the generator
    yields nothing and the caller treats that as a failed embedding.

    @param audio A seekable binary stream, which is what lets a track on a
        share cost the fragments sampled rather than the whole file.
    """
    try:
        import soundfile as sf
    except Exception as e:
        logger.warning("soundfile import failed: %s", e)
        return

    try:
        with sf.SoundFile(audio) as f:
            src_sr = f.samplerate
            n_frames = len(f)
            duration_s = n_frames / src_sr if src_sr else 0.0
            if duration_s <= 0.0:
                logger.warning("Audio %s has zero duration", _describe(audio))
                return

            starts = _fragment_starts_s(duration_s)
            n_yielded = 0
            for i, start_s in enumerate(starts):
                frag = _read_fragment(f, start_s, src_sr)
                if frag is None:
                    # If the first fragment fails, the file is most
                    # likely structurally unreadable by libsndfile
                    # (e.g. malformed MP3 framing, or a FLAC variant
                    # libsndfile can't seek). Bail out instead of
                    # retrying the same broken file at two more
                    # offsets — saves log noise and a few hundred ms
                    # per file × N retry attempts.
                    if i == 0:
                        logger.warning(
                            "Skipping %s: libsndfile cannot read first "
                            "fragment (file may be malformed or use a "
                            "FLAC/MP3 variant libsndfile doesn't support)",
                            _describe(audio),
                        )
                        return
                    continue
                yield frag
                n_yielded += 1
                # Free the waveform before the next seek/read so peak
                # RSS is bounded by one fragment, not N.
                del frag
                gc.collect()

            # Mid-file fragment failures (i > 0) are silent at WARNING;
            # partial embeddings still get produced from the fragments
            # that did work, so this isn't a track-level failure.
    except Exception as e:
        logger.warning("Failed to open audio %s: %s", _describe(audio), e)
        return


# ---------------------------------------------------------------------------
# ClapOnnxModel
# ---------------------------------------------------------------------------

class ClapOnnxModel:
    """ONNX Runtime-based CLAP model for audio and text embedding.

    Drop-in replacement for laion_clap with the same lifecycle:
    load() -> get_audio_embedding / get_text_embedding -> unload().
    """

    def __init__(self, model_dir: str, custom_model_dir: str = ""):
        # custom_model_dir overrides model_dir if it points to a directory with
        # ONNX files. Both come from user config and may contain a
        # leading ``~`` — expand here so every downstream consumer
        # (``_ensure_model_file``, ``os.makedirs``, ORT session loader)
        # sees an absolute path. Without this, a config value of
        # ``~/kalinka/models`` is treated literally and the loader
        # creates a directory named ``~`` under the server's CWD, which
        # then masks subsequent "delete cached models and re-download"
        # migrations because the real home directory is empty.
        expanded_custom = (
            os.path.expanduser(custom_model_dir) if custom_model_dir else ""
        )
        expanded_dir = os.path.expanduser(model_dir)
        self._model_dir = (
            expanded_custom
            if (expanded_custom and os.path.isdir(expanded_custom))
            else expanded_dir
        )
        self._audio_session = None
        self._text_session = None
        self._tokenizer = None
        self._va_head_session = None

    @property
    def is_audio_loaded(self) -> bool:
        return self._audio_session is not None

    @property
    def is_text_loaded(self) -> bool:
        return self._text_session is not None and self._tokenizer is not None

    @property
    def is_loaded(self) -> bool:
        # "Fully loaded" == both towers. Kept for offline callers
        # (assess-ai-search scripts) that load() then assert is_loaded.
        return self.is_audio_loaded and self.is_text_loaded

    @property
    def has_va_head(self) -> bool:
        return self._va_head_session is not None

    def _session_options(self):
        import onnxruntime as ort

        sess_opts = ort.SessionOptions()
        sess_opts.inter_op_num_threads = 1
        sess_opts.intra_op_num_threads = 2
        # ORT's CPU memory arena grows to the high-water mark of
        # intermediate tensors and never returns pages to the OS, which
        # on a 4 GB Pi accumulates into OOM territory over a few hundred
        # tracks. Disabling the arena costs ~5-10% inference latency
        # but keeps the RSS flat.
        sess_opts.enable_cpu_mem_arena = False
        sess_opts.enable_mem_pattern = False
        return sess_opts

    def load_text(self) -> None:
        """Load the text encoder + tokenizer (+ optional VA head).

        This is the only tower needed to encode search queries, so the
        embedder keeps it resident whenever AI search is enabled. The VA
        (mood) head is tiny (~0.25 MB) and loads here too, so mood backfill
        works without the audio tower resident.
        """
        if self.is_text_loaded:
            return

        import onnxruntime as ort

        text_path = _ensure_model_file("clap_text_encoder", self._model_dir)
        tok_path = _ensure_model_file("clap_tokenizer", self._model_dir)
        if not text_path or not tok_path:
            raise FileNotFoundError(
                f"CLAP text model files missing from {self._model_dir}. "
                "Run scripts/export_clap_onnx.py on a dev machine first."
            )

        sess_opts = self._session_options()
        providers = ["CPUExecutionProvider"]

        self._text_session = ort.InferenceSession(
            text_path, sess_options=sess_opts, providers=providers
        )
        logger.info("CLAP text ONNX session loaded")

        from tokenizers import Tokenizer

        self._tokenizer = Tokenizer.from_file(tok_path)
        self._tokenizer.enable_truncation(max_length=_TOKEN_MAX_LEN)
        self._tokenizer.enable_padding(
            length=_TOKEN_MAX_LEN, pad_id=1, pad_token="<pad>"
        )
        logger.info("CLAP tokenizer loaded")

        # VA (mood) head — best-effort/optional; on failure mood ranking stays
        # off (get_valence_arousal returns None) without breaking CLAP embedding.
        if self._va_head_session is None:
            try:
                va_path = _ensure_model_file("va_head", self._model_dir)
                if va_path:
                    self._va_head_session = ort.InferenceSession(
                        va_path, sess_options=sess_opts, providers=providers
                    )
                    logger.info("VA (mood) head ONNX session loaded")
            except Exception as e:
                logger.warning("VA head load failed (mood ranking disabled): %s", e)
                self._va_head_session = None

    def load_audio(self) -> None:
        """Load the audio encoder (~272 MB; needed only while indexing)."""
        if self.is_audio_loaded:
            return

        import onnxruntime as ort

        audio_path = _ensure_model_file("clap_audio_encoder", self._model_dir)
        if not audio_path:
            raise FileNotFoundError(
                f"CLAP audio model file missing from {self._model_dir}. "
                "Run scripts/export_clap_onnx.py on a dev machine first."
            )

        self._audio_session = ort.InferenceSession(
            audio_path,
            sess_options=self._session_options(),
            providers=["CPUExecutionProvider"],
        )
        logger.info("CLAP audio ONNX session loaded")

    def load(self) -> None:
        """Download (if needed) and create both ONNX towers (full lifecycle)."""
        self.load_text()
        self.load_audio()

    def unload_audio(self) -> None:
        """Release the audio encoder session, keeping the text tower resident."""
        if self._audio_session is None:
            return
        self._audio_session = None
        gc.collect()
        logger.info("CLAP audio ONNX session unloaded")

    def unload(self) -> None:
        """Release all ONNX sessions and the tokenizer."""
        self._audio_session = None
        self._text_session = None
        self._tokenizer = None
        self._va_head_session = None

    def get_audio_embedding(self, audio: BinaryIO) -> Optional[np.ndarray]:
        """Compute 512-dim audio embedding. Returns None on failure.

        Long enough tracks are sampled at multiple fragments and the
        per-fragment embeddings are averaged before returning. The
        caller is expected to L2-normalise the result.

        @param audio A seekable binary stream of the track, opened by
            whichever storage holds it.

        We only accumulate the 512-float embedding vectors (≈ 2 KB
        each), never the raw fragment waveforms — the audio loader
        is a generator and `_run_one` drops its input before returning.
        """
        if not self.is_audio_loaded:
            return None

        accum: Optional[np.ndarray] = None
        n_fragments = 0

        for waveform in _load_audio_fragments(audio):
            try:
                result = self._audio_session.run(
                    None, {"waveform": waveform[np.newaxis, :]}
                )
                vec = result[0][0].astype(np.float32)
                del result
            except Exception as e:
                logger.warning(
                    "ONNX audio inference failed for %s (fragment %d): %s",
                    _describe(audio), n_fragments, e,
                )
                continue
            finally:
                del waveform

            if accum is None:
                accum = vec
            else:
                accum += vec
                del vec
            n_fragments += 1
            gc.collect()

        if accum is None or n_fragments == 0:
            return None
        if n_fragments > 1:
            accum /= n_fragments
        return accum

    def get_text_embedding(self, text: str) -> Optional[np.ndarray]:
        """Compute 512-dim text embedding. Returns None on failure."""
        if not self.is_text_loaded:
            return None
        try:
            encoded = self._tokenizer.encode(text)
            input_ids = np.array([encoded.ids], dtype=np.int64)
            attention_mask = np.array([encoded.attention_mask], dtype=np.int64)
            result = self._text_session.run(
                None,
                {"input_ids": input_ids, "attention_mask": attention_mask},
            )
            return result[0][0].astype(np.float32)
        except Exception as e:
            logger.warning("ONNX text inference failed: %s", e)
            return None

    def get_valence_arousal(
        self, embedding: np.ndarray
    ) -> Optional[tuple[float, float]]:
        """Map a 512-d CLAP audio embedding -> (valence, arousal) in 1-9.

        L2-normalizes defensively (the head trains on unit-norm vectors). None
        if the head is absent or inference fails.
        """
        if self._va_head_session is None or embedding is None:
            return None
        try:
            x = np.asarray(embedding, dtype=np.float32).reshape(-1)
            norm = np.linalg.norm(x)
            if norm > 0:
                x = x / norm
            out = self._va_head_session.run(
                None, {"embedding": x[np.newaxis, :]}
            )[0][0]
            return (float(out[0]), float(out[1]))
        except Exception as e:
            logger.warning("VA head inference failed: %s", e)
            return None
