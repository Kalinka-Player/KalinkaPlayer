"""TinyMU captions for every production fragment window, cached as JSONL.

TinyMU (arXiv:2604.15849): MATPAC++ audio encoder -> SmolLM2-135M decoder,
229M parameters. Each 10 s window from the production fragment rule
(clap_embed.fragment_starts_s) is resampled to 16 kHz and captioned with a
KV-cached greedy decoder that is token-identical to the authors' m2t.generate
(which re-runs the full sequence per token and is pinned to CUDA).

Needs PyTorch and the authors' code: see requirements-tinymu.txt.
"""
from __future__ import annotations

import functools
import hashlib
import json
import sys
import time
from pathlib import Path

import numpy as np

import clap_embed as ce

TINYMU_SR = 16_000           # MATPAC++ input rate (AudioEncoder.sr for "matpac")
TINYMU_MAX_NEW = 200          # caption length cap (tokens); captions end well before
TINYMU_EOS = 0                # SmolLM2 <|endoftext|>


def windows(path: str) -> list[float]:
    """Start times (s) of the production CLAP fragments for this file."""
    import soundfile as sf
    info = sf.info(path)
    return ce.fragment_starts_s(info.frames / float(info.samplerate))


def read_window_16k(path: str, start_s: float):
    """One <=10 s window, mono, resampled to 16 kHz the way TinyMU's loader does."""
    import soundfile as sf
    import torch
    import torchaudio.functional as AF
    with sf.SoundFile(path) as f:
        sr = f.samplerate
        f.seek(int(start_s * sr))
        x = f.read(int(ce.FRAGMENT_SECONDS * sr), dtype="float32", always_2d=False)
    x = ce.to_mono(x)
    return AF.resample(torch.from_numpy(np.ascontiguousarray(x)), sr, TINYMU_SR)


class TinyMU:
    """TinyMU m2t model on CPU with a KV-cached greedy decoder.

    m2t.generate re-runs the whole sequence per token and is pinned to CUDA;
    this loop feeds the same prefix ([audio, sep, prompt padded to 129]) and
    the same absolute positions, so its greedy output is token-identical.
    """

    def __init__(self, ckpt_dir: Path, src_dir: Path, threads: int):
        import torch
        import yaml
        sys.path.insert(0, str(src_dir / "src"))
        # MATPAC's loader calls torch.load without map_location on a CUDA checkpoint.
        torch_load = torch.load
        torch.load = functools.partial(torch_load, map_location="cpu")
        try:
            from models.m2t import m2t
            from transformers import AutoTokenizer
            cfg = yaml.safe_load(open(ckpt_dir / "tinymu.yaml"))
            cfg["model"]["encoder"]["matpac_ckpt_path"] = str(ckpt_dir / "matpac_plus_as_48_1_map_enconly.pt")
            self.model = m2t(cfg["model"])
            state = torch_load(ckpt_dir / "tinymu.pt", map_location="cpu", weights_only=False)
            self.model.load_state_dict(state["model"])
        finally:
            torch.load = torch_load
        self.model.eval()
        self.tok = AutoTokenizer.from_pretrained(cfg["model"]["decoder"]["textdec_name"])
        self.tok.add_special_tokens({"pad_token": "!"})
        self.max_prompt = cfg["model"]["decoder"]["max_text_token_len"]
        torch.set_num_threads(threads)
        self.torch = torch

    def caption(self, waves: list, prompt: str) -> list[str]:
        torch = self.torch
        lm = self.model.text_decoder.lm
        embed = self.model.text_decoder.embed_fn
        with torch.no_grad():
            x = torch.nn.utils.rnn.pad_sequence(waves, batch_first=True)
            enc = self.tok(text=[prompt] * len(waves), add_special_tokens=True, truncation=True,
                           max_length=self.max_prompt, padding="max_length", return_tensors="pt")
            prefix, mask = self.model(waveforms=x, waveform_lengths=[w.shape[0] for w in waves],
                                      input_ids=enc["input_ids"], input_mask=enc["attention_mask"],
                                      inference_mode=True)
            B, L = mask.shape
            out = lm(inputs_embeds=prefix, attention_mask=mask, use_cache=True)
            nxt = out.logits[:, -1].argmax(-1, keepdim=True)
            toks, done = [nxt], nxt.squeeze(1) == TINYMU_EOS
            for i in range(TINYMU_MAX_NEW - 1):
                if done.all():
                    break
                mask = torch.cat([mask, torch.ones(B, 1, dtype=mask.dtype)], 1)
                out = lm(inputs_embeds=embed(nxt), attention_mask=mask, past_key_values=out.past_key_values,
                         use_cache=True, position_ids=torch.full((B, 1), L + i, dtype=torch.long))
                nxt = out.logits[:, -1].argmax(-1, keepdim=True)
                toks.append(nxt)
                done |= nxt.squeeze(1) == TINYMU_EOS
            ids = torch.cat(toks, 1)
        return [self.tok.decode(r).split("<|endoftext|>")[0].strip() for r in ids]


def prompt_slug(prompt: str) -> str:
    return hashlib.sha1(prompt.encode()).hexdigest()[:8]


def tinymu_captions(corpus: Corpus, out: Path, prompt: str, get_model, batch: int) -> tuple[dict, float]:
    """{(track_id, window_idx): caption}; appends to a JSONL cache as it goes."""
    cache = out / f"captions_{prompt_slug(prompt)}.jsonl"
    caps, secs = {}, []
    if cache.exists():
        for line in open(cache):
            r = json.loads(line)
            caps[(r["id"], r["w"])] = r["text"]
            secs.append(r["sec"])
    todo = [(tid, w, p, s) for tid, p in zip(corpus.ids, corpus.paths)
            for w, s in enumerate(windows(p)) if (tid, w) not in caps]
    if todo:
        print(f"  [{corpus.name}] TinyMU '{prompt}': {len(todo)} windows to caption", flush=True)
        model = get_model()
        with open(cache, "a") as fh:
            for b in range(0, len(todo), batch):
                chunk = todo[b:b + batch]
                waves = [read_window_16k(p, s) for _, _, p, s in chunk]
                # zero-padding shorter windows changes MATPAC's input; caption those alone
                groups: dict[int, list] = {}
                for j, w in enumerate(waves):
                    groups.setdefault(w.shape[0], []).append(j)
                for idx in groups.values():
                    t0 = time.time()
                    texts = model.caption([waves[j] for j in idx], prompt)
                    sec = (time.time() - t0) / len(idx)
                    for j, text in zip(idx, texts):
                        tid, w, _, _ = chunk[j]
                        caps[(tid, w)] = text
                        secs.append(sec)
                        fh.write(json.dumps({"id": tid, "w": w, "text": text, "sec": sec}) + "\n")
                fh.flush()
                print(f"  [{corpus.name}] captioned {min(b + batch, len(todo))}/{len(todo)} "
                      f"({np.mean(secs[-len(chunk):]):.2f} s/window)", flush=True)
    return caps, float(np.mean(secs)) if secs else 0.0
