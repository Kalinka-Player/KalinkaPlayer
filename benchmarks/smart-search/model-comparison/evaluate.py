#!/usr/bin/env python
"""Text-to-music retrieval: LAION-CLAP music, CLAP2-S and TinyMU captions.

Question: which model finds music best from a typed description, with and
without a DEAM-trained valence/arousal (mood) model on top? LAION-CLAP music
(HTSAT-base, Kalinka's clap-onnx-v2 release) is the reference; CLAP2-S is
laion/clap2-small-audioxl-s1B; TinyMU captions are searched through MiniLM.

Every system is ranked by ONE reimplementation of production's searcher
(kalinka_plugin_localfiles/searcher/searcher.py + searcher_db.py, defaults from
config_model.py): normalised query -> int8 -> L2 KNN over int8 track vectors
(top 50, min-max scored); mood leg = keyword spotting, else text nearest
neighbour over the mood vocabulary (threshold 0.3, top 3); 200 nearest tracks
in (V,A) unioned in; final = (1 - 0.6*conf)*clap + 0.6*conf*mood.

Systems
  CLAP               production encoder, mood leg off
  CLAP + V/A         what ships: deployed va_head_v1.onnx + mood_index_v1.npz
  CLAP2-S            CLAP2-S on the same 10 s fragments, mood leg off
  CLAP2-S + V/A      head retrained on CLAP2-S DEAM embeddings with va_recipe.py's
                     exact recipe/split; mood words re-embedded with CLAP2 text
  TinyMU (+ V/A)     TinyMU window captions embedded with int8 MiniLM; its head
                     uses the same recipe on the pooled caption vectors
  CLAP + TinyMU      mean of per-query z-scored CLAP and TinyMU similarities
                     (fixed 0.5/0.5), with CLAP's V/A for "+ V/A"
  secondary: CLAP2-S without int8, CLAP2-S on 30 s windows, sentence-cased
             queries ("Sad piano.") for both encoders, keyword-only mood leg.

Corpora
  mtg600     600 MTG-Jamendo ~12 s clips, 75 queries (assess-ai-search benchmark)
  mtgshard   584 full-length MTG-Jamendo tracks, same 75 queries
  deam_test  DEAM's 271 held-out songs (the deployed head never saw them),
             15 mood queries; relevance = top quartile of sv*zV + sa*zA
  deam_cv    all 1802 DEAM songs; V/A from 5-fold out-of-fold heads trained
             with the same recipe for BOTH encoders (not the deployed head)

Metrics @10: P, nDCG, MRR, hit, lift over random; DEAM adds tgt = mean target
z-score of the top 10. Pairs compared on per-query nDCG@10 (paired bootstrap
95% CI + Wilcoxon).

Run after embed_clap.py, embed_clap2.py and (for TinyMU) caption_tinymu.py;
see README.md.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from pathlib import Path

# The V/A recipe trains in float32 numpy. At 8+ threads OpenBLAS splits the
# larger products differently, which moves the early-stopping epoch; one thread
# reproduces the deployed head. Set before numpy loads OpenBLAS.
os.environ["OPENBLAS_NUM_THREADS"] = "1"

import numpy as np  # noqa: E402

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import clap_embed as ce  # noqa: E402
import va_recipe as T  # noqa: E402
from corpora import LOADERS, Corpus, deam_split  # noqa: E402
from kit_paths import CACHE, MODELS  # noqa: E402

RELEASE = MODELS                                   # the clap-onnx-v2 release files
CLAP_CACHES = {n: CACHE / n / "clap_audio.npz" for n in ("mtg600", "mtgshard", "deam")}
SIZE = __import__("os").environ.get("CLAP2_SIZE", "small")   # xxs | xs | small | medium | large
HUB = f"hf-hub:laion/clap2-{SIZE}-audioxl-s1B"
TINYMU_CAPS = CACHE                                 # <corpus>/captions_<sha1(prompt)[:8]>.jsonl
TINYMU_PROMPT_SLUG = "e87ad2bb"                     # "Describe the music you hear."
MINILM_DIR = RELEASE / "minilm"                      # production int8 all-MiniLM-L6-v2 (Jamendo plugin)
TAG = {"xxs": "clap2xxs", "xs": "clap2xs", "small": "clap2s", "medium": "clap2m", "large": "clap2l"}[SIZE]
LABEL = {"xxs": "CLAP2-XXS", "xs": "CLAP2-XS", "small": "CLAP2-S", "medium": "CLAP2-M", "large": "CLAP2-L"}[SIZE]
K = 10

# production defaults (config_model.py)
MOOD_WEIGHT, MOOD_CANDIDATES = 0.6, 200
NN_THRESHOLD, NN_TOP_K = 0.3, 3
KNN_LIMIT = 50
CLAP_INT8_CAP = 0.25
FILLER = frozenset({
    "a", "an", "the", "and", "or", "but", "for", "to", "of", "in", "on", "at",
    "by", "with", "from", "into", "my", "me", "i", "you", "your", "we", "us",
    "it", "its", "this", "that", "these", "those", "some", "something",
    "anything", "like", "want", "need", "give", "play", "playing", "song",
    "songs", "music", "track", "tracks", "tune", "tunes", "sound", "sounds",
    "playlist", "vibe", "vibes", "mood", "feeling", "feel", "get", "got", "im",
    "am", "are", "is", "be", "now", "tonight", "today", "day", "night", "time",
    "really", "very", "more", "bit", "little", "kinda", "sorta", "stuff",
})




# --------------------------------------------------------------------------- #
# Encoders
# --------------------------------------------------------------------------- #
def sentence(q: str) -> str:
    q = q.strip()
    q = q[:1].upper() + q[1:]
    return q if q.endswith((".", "!", "?")) else q + "."


class ClapText:
    def __init__(self):
        self.sess = ce.make_session(str(RELEASE / "clap_text_encoder.onnx"))
        self.tok = ce.make_tokenizer(str(RELEASE / "clap_tokenizer.json"))

    def encode(self, texts):
        return np.stack([ce.embed_text(self.sess, self.tok, t) for t in texts]).astype(np.float32)


class Clap2Text:
    def __init__(self):
        import open_clip
        import torch
        self.torch = torch
        self.model, _ = open_clip.create_model_from_pretrained(HUB)
        self.model.eval()
        self.tok = open_clip.get_tokenizer(HUB)

    def encode(self, texts):
        with self.torch.no_grad():
            return self.model.encode_text(self.tok(list(texts)), normalize=True).float().numpy()


def i8(v: np.ndarray, cap: float) -> np.ndarray:
    """encode_embedding -> decode_embedding with a symmetric fixed cap."""
    s = 127.0 / cap
    return np.clip(np.round(v * s), -127, 127).astype(np.float32) / s


def unit(X: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(X, axis=-1, keepdims=True)
    return np.where(n > 0, X / np.maximum(n, 1e-12), X)


# --------------------------------------------------------------------------- #
# V/A heads: the deployed recipe (va_recipe.py), bit-for-bit
# --------------------------------------------------------------------------- #
def train_head(X, Y, tr, va, seed=T.SEED, replay_split_draw=True):
    """The deployed head's MLP fit. With replay_split_draw the RNG stream matches
    the original training run (its first draw is the split permutation)."""
    rng = np.random.default_rng(seed)
    if replay_split_draw:
        rng.permutation(len(X))
    y_mean, y_std = Y[tr].mean(0), Y[tr].std(0)
    Yn = (Y - y_mean) / y_std
    net = T.MLP(X.shape[1], T.HIDDEN, 2, rng)
    drop_rng = np.random.default_rng(seed + 1)
    best_val, best_w, bad = np.inf, None, 0
    for _ in range(T.EPOCHS):
        order = rng.permutation(tr)
        for s in range(0, len(order), T.BATCH):
            idx = order[s:s + T.BATCH]
            out, cache = net.forward(X[idx], drop_rng=drop_rng)
            net.step(net.backward(cache, 2 * (out - Yn[idx])), T.LR)
        val_mse = float(np.mean((net.predict(X[va]) - Yn[va]) ** 2))
        if val_mse < best_val - 1e-5:
            best_val, best_w, bad = val_mse, {k: getattr(net, k).copy() for k in ("W1", "b1", "W2", "b2")}, 0
        else:
            bad += 1
            if bad >= T.PATIENCE:
                break
    for k, w in best_w.items():
        setattr(net, k, w)

    def predict(Z):
        return np.clip(net.predict(unit(Z).astype(np.float32)) * y_std + y_mean, 1.0, 9.0)
    predict.net = net
    return predict


def ridge_fit(X, Y, tr):
    from sklearn.linear_model import Ridge
    y_mean, y_std = Y[tr].mean(0), Y[tr].std(0)
    r = Ridge(alpha=T.RIDGE_ALPHA).fit(X[tr], (Y[tr] - y_mean) / y_std)
    return lambda Z: r.predict(Z) * y_std + y_mean


def head_metrics(Y, P):
    return {ax: {"R2": T.r2(Y[:, j], P[:, j]), "CCC": T.ccc(Y[:, j], P[:, j]), "RMSE": T.rmse(Y[:, j], P[:, j])}
            for j, ax in enumerate(("valence", "arousal"))}


def cv_predictions(X, Y, folds=5, seed=T.SEED):
    """Out-of-fold V/A for every song: fold test, 15% of the rest as val."""
    perm = np.random.default_rng(seed + 7).permutation(len(X))
    P = np.zeros_like(Y)
    for f, te in enumerate(np.array_split(perm, folds)):
        rest = np.setdiff1d(perm, te, assume_unique=True)
        rest = rest[np.random.default_rng(seed + 100 + f).permutation(len(rest))]
        n_va = int(round(0.15 * len(X)))
        head = train_head(X, Y, rest[n_va:], rest[:n_va], seed=seed + f, replay_split_draw=False)
        P[te] = head(X[te])
    return P


# --------------------------------------------------------------------------- #
# Production search (searcher._do_search)
# --------------------------------------------------------------------------- #
def query_to_va(query, q_i8, mood, nn_fallback=True):
    words, va, emb = mood
    tokens = set(re.findall(r"[a-z]+", query.lower()))
    hits = [i for i, w in enumerate(words) if w in tokens]
    if hits:
        tv = float(np.mean([va[i][0] for i in hits]))
        ta = float(np.mean([va[i][1] for i in hits]))
        matched = {words[i] for i in hits}
        content = [t for t in tokens if len(t) >= 3 and t not in FILLER]
        share = sum(t in matched for t in content) / len(content) if content else 1.0
        return (tv, ta), float(share), "keyword"
    if not nn_fallback:
        return None, 0.0, "no-keyword"
    qn = float(np.linalg.norm(q_i8))
    if qn == 0.0:
        return None, 0.0, "none"
    sims = emb @ (q_i8 / qn)
    order = np.argsort(-sims)[:NN_TOP_K]
    top = float(sims[order[0]])
    if top < NN_THRESHOLD:
        return None, 0.0, "below-threshold"
    w = np.clip(sims[order], 0.0, None)
    if w.sum() <= 0:
        return None, 0.0, "none"
    tv = float((va[order, 0] * w).sum() / w.sum())
    ta = float((va[order, 1] * w).sum() / w.sum())
    conf = (top - NN_THRESHOLD) / (1.0 - NN_THRESHOLD)
    return (tv, ta), float(min(1.0, max(0.0, conf))), "nn"


def search(q_text, q_vec, Xq, cap, track_va=None, mood=None, nn_fallback=True, dist=None):
    """Ranked track indices (top KNN_LIMIT or blended candidates) + mood info.

    ``dist`` overrides the KNN distance (fusion); q_vec then only feeds the
    mood leg's nearest-neighbour fallback."""
    qb = i8(unit(q_vec), cap) if cap else unit(q_vec)
    if dist is None:
        dist = np.linalg.norm(Xq - qb, axis=1)
    knn = np.argsort(dist, kind="stable")[:KNN_LIMIT]
    d = dist[knn]
    rng = (d.max() - d.min()) or 1.0
    knn_map = {int(i): 1.0 - (dist[i] - d.min()) / rng for i in knn}
    info = {"conf": 0.0, "path": "off", "target": None}
    if track_va is None:
        return list(knn_map), info
    target, conf, path = query_to_va(q_text, qb, mood, nn_fallback)
    info.update(conf=conf, path=path, target=target)
    if target is None or conf <= 0.0:
        return list(knn_map), info
    w = MOOD_WEIGHT * conf
    vd = np.sqrt(((track_va - np.asarray(target)) ** 2).sum(1))
    cand = list(dict.fromkeys(list(knn_map) + [int(i) for i in np.argsort(vd, kind="stable")[:MOOD_CANDIDATES]]))
    dd = {i: math.dist(target, track_va[i]) for i in cand}
    mn, mx = min(dd.values()), max(dd.values())
    r = (mx - mn) or 1.0
    scored = [((1 - w) * knn_map.get(i, 0.0) + w * (1.0 - (dd[i] - mn) / r), i) for i in cand]
    scored.sort(key=lambda st: -st[0])
    return [i for _, i in scored], info


# --------------------------------------------------------------------------- #
# Metrics
# --------------------------------------------------------------------------- #
def relevance(c: Corpus, q):
    if c.tags is not None:
        return np.array([all(any(t in tags for t in g) for g in q.groups) for tags in c.tags], float), None
    z = (c.va - c.va.mean(0)) / c.va.std(0)
    tgt = q.va[0] * z[:, 0] + q.va[1] * z[:, 1]
    return (tgt >= np.quantile(tgt, 0.75)).astype(float), tgt


def score(ranked, rel, tgt):
    top = np.array(ranked[:K])
    r = rel[top]
    disc = 1.0 / np.log2(np.arange(2, K + 2))
    ideal = (np.sort(rel)[::-1][:K] * disc).sum()
    first = np.flatnonzero(r)
    m = {"p": float(r.mean()), "ndcg": float((r * disc[:len(r)]).sum() / ideal) if ideal else 0.0,
         "mrr": float(1.0 / (first[0] + 1)) if first.size else 0.0, "hit": float(r.any()),
         "rand": float(rel.mean())}
    m["lift"] = m["p"] - m["rand"]
    if tgt is not None:
        m["tgt"] = float(tgt[top].mean())
    return m


def paired(a, b, seed=0):
    from scipy.stats import wilcoxon
    d = np.asarray(b) - np.asarray(a)
    boots = np.random.default_rng(seed).choice(d, size=(10_000, len(d)), replace=True).mean(1)
    p = float(wilcoxon(d).pvalue) if np.any(d != 0) else 1.0
    return {"mean_diff": float(d.mean()), "ci95": [float(np.percentile(boots, 2.5)), float(np.percentile(boots, 97.5))],
            "wilcoxon_p": p, "b_wins": int((d > 0).sum()), "a_wins": int((d < 0).sum()), "n": len(d)}


# --------------------------------------------------------------------------- #
# Driver
# --------------------------------------------------------------------------- #
PRIMARY = ["CLAP", "CLAP + V/A", "CLAP2-S", "CLAP2-S + V/A"]
CONTRASTS = [("CLAP + V/A", "CLAP2-S"), ("CLAP + V/A", "CLAP2-S + V/A"), ("CLAP2-S", "CLAP2-S + V/A"),
             ("CLAP", "CLAP + V/A"), ("CLAP", "CLAP2-S"), ("CLAP2-S", "CLAP2-S float"),
             ("CLAP2-S", "CLAP2-S 30s"), ("CLAP2-S + V/A", "CLAP2-S 30s + V/A"),
             ("CLAP", "CLAP [Sentence.]"), ("CLAP2-S", "CLAP2-S [Sentence.]"),
             ("CLAP + V/A", "CLAP2-S [Sentence.] + V/A"),
             ("CLAP + V/A kw-only", "CLAP2-S + V/A kw-only"), ("CLAP + V/A", "CLAP + V/A kw-only"),
             ("CLAP + V/A", "CLAP2-S 30s"), ("CLAP + V/A", "CLAP2-S 30s + V/A"),
             ("CLAP + V/A kw-only", "CLAP2-S 30s + V/A kw-only"), ("CLAP", "CLAP2-S 30s"),
             ("CLAP + V/A", "TinyMU"), ("CLAP + V/A", "TinyMU + V/A"), ("CLAP", "TinyMU"),
             ("TinyMU", "TinyMU + V/A"), ("CLAP + V/A", "CLAP + TinyMU"),
             ("CLAP + V/A", "CLAP + TinyMU + V/A"), ("CLAP", "CLAP + TinyMU")]


class MiniLmText:
    def __init__(self):
        from minilm_onnx import MiniLmOnnx
        self.m = MiniLmOnnx(str(MINILM_DIR))

    def encode(self, texts):
        return self.m.encode(list(texts))


def tinymu_vectors(c: Corpus, minilm) -> np.ndarray | None:
    """Mean-pooled MiniLM vectors of each track's TinyMU window captions, or
    None while any production fragment window is still uncaptioned."""
    import soundfile as sf
    path = TINYMU_CAPS / c.name / f"captions_{TINYMU_PROMPT_SLUG}.jsonl"
    if not path.exists():
        return None
    caps = {}
    for line in open(path):
        r = json.loads(line)
        caps[(r["id"], r["w"])] = r["text"]
    need = [(tid, w) for tid, p in zip(c.ids, c.paths)
            for w in range(len(ce.fragment_starts_s(sf.info(p).frames / float(sf.info(p).samplerate))))]
    missing = sum(k not in caps for k in need)
    if missing:
        print(f"  TinyMU {c.name}: {missing}/{len(need)} windows uncaptioned, skipped")
        return None
    V = minilm.encode([caps[k] for k in need])
    rows = {}
    for (tid, _), v in zip(need, V):
        rows.setdefault(tid, []).append(v)
    return unit(np.stack([np.mean(rows[t], 0) for t in c.ids]).astype(np.float32))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(HERE / "results"))
    args = ap.parse_args()
    cache, out = CACHE, Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    base = {n: LOADERS[n]() for n in ("mtg600", "mtgshard", "deam")}
    emb = {}   # (encoder, corpus) -> X (unit float)
    for n, c in base.items():
        d = np.load(CLAP_CACHES[n], allow_pickle=True)
        ids = [str(x) for x in (d["ids"] if "ids" in d else d["song_ids"])]
        assert ids == c.ids, n
        emb[("clap", n)] = unit(d["X"].astype(np.float32))
        for v in ("frag10", "seg30"):
            if not (cache / n / f"{TAG}_{v}.npz").exists():
                print(f"missing {n}/{v}: its systems are skipped")
                continue
            d = np.load(cache / n / f"{TAG}_{v}.npz")
            assert list(d["ids"]) == c.ids, (n, v)
            emb[(f"clap2_{v}", n)] = unit(d["X"].astype(np.float32))
            emb[(f"clap2_{v}", n, "spw")] = float(d["sec_per_window"])

    clap_text, clap2_text, minilm = ClapText(), Clap2Text(), MiniLmText()
    enc_text = {"clap": clap_text, "clap2_frag10": clap2_text, "clap2_seg30": clap2_text, "tinymu": minilm}
    for n, c in base.items():
        V = tinymu_vectors(c, minilm)
        if V is not None:
            emb[("tinymu", n)] = V
    have_tinymu = all(("tinymu", n) in emb for n in base)

    # int8 cap for CLAP2: production's 0.25 saturates it, so refit with 10% headroom.
    have = [e for e in ("clap2_frag10", "clap2_seg30") if all((e, n) in emb for n in base)]
    absmax = max(float(np.abs(emb[(e, n)]).max()) for e in have for n in base)
    clap2_cap = math.ceil(absmax * 1.1 * 20) / 20
    clap_absmax = max(float(np.abs(emb[("clap", n)]).max()) for n in base)
    sat = {e: float(np.mean([(np.abs(emb[(e, n)]) > CLAP_INT8_CAP).mean() for n in base]))
           for e in ("clap", "clap2_frag10")}
    print(f"absmax CLAP {clap_absmax:.3f}  CLAP2 {absmax:.3f} -> CLAP2 int8 cap {clap2_cap}; "
          f"share of components beyond 0.25: {sat}")
    cap_of = {"clap": CLAP_INT8_CAP, "clap2_frag10": clap2_cap, "clap2_seg30": clap2_cap}
    if have_tinymu:
        cap_of["tinymu"] = math.ceil(max(float(np.abs(emb[("tinymu", n)]).max()) for n in base) * 1.1 * 20) / 20
        print(f"TinyMU/MiniLM int8 cap {cap_of['tinymu']}")

    # ---- V/A heads ----
    Ydeam = base["deam"].va
    tr, va_idx, te = deam_split(len(Ydeam))
    import onnxruntime as ort
    deployed = ort.InferenceSession(str(RELEASE / "va_head_v1.onnx"), providers=["CPUExecutionProvider"])

    def deployed_head(Z):
        return deployed.run(None, {"embedding": unit(Z).astype(np.float32)})[0]

    head_report, heads, cv_va = {}, {}, {}
    # reproduction check: the recipe on CLAP embeddings must give the deployed head
    repro = train_head(emb[("clap", "deam")], Ydeam, tr, va_idx)
    repro_diff = float(np.abs(repro(emb[("clap", "deam")]) - deployed_head(emb[("clap", "deam")])).max())
    print(f"recipe reproduces deployed head: max |V/A difference| = {repro_diff:.2e} on the 1-9 scale")
    heads["clap"] = deployed_head
    for e in ["clap"] + have + (["tinymu"] if have_tinymu else []):
        X = emb[(e, "deam")]
        if e != "clap":
            heads[e] = train_head(X, Ydeam, tr, va_idx)
        Xq = i8(X, cap_of[e])   # production: head sees the int8-decoded stored vector
        head_report[e] = {"mlp_test": head_metrics(Ydeam[te], heads[e](Xq[te])),
                          "ridge_test": head_metrics(Ydeam[te], ridge_fit(X, Ydeam, tr)(X[te]))}
        cv_va[e] = cv_predictions(X, Ydeam)
        head_report[e]["mlp_cv5"] = head_metrics(Ydeam, cv_va[e])
        print(f"V/A head {e}: test R2 V={head_report[e]['mlp_test']['valence']['R2']:.3f} "
              f"A={head_report[e]['mlp_test']['arousal']['R2']:.3f} | 5-fold R2 "
              f"V={head_report[e]['mlp_cv5']['valence']['R2']:.3f} A={head_report[e]['mlp_cv5']['arousal']['R2']:.3f}",
              flush=True)

    # mood vocabulary: deployed index for CLAP, re-embedded words for CLAP2
    mi = np.load(RELEASE / "mood_index_v1.npz", allow_pickle=True)
    words, mva = [str(w) for w in mi["words"]], mi["va"].astype(np.float32)
    mood_of = {"clap": (words, mva, mi["text_emb"].astype(np.float32))}
    w2 = clap2_text.encode(words)
    mood_of["clap2_frag10"] = mood_of["clap2_seg30"] = (words, mva, w2)
    mood_of["tinymu"] = (words, mva, minilm.encode(words))
    mood_of["fusion"] = mood_of["clap"]
    if have_tinymu:
        heads["fusion"], cv_va["fusion"], cap_of["fusion"] = heads["clap"], cv_va["clap"], CLAP_INT8_CAP
    print("mood index CLAP text_emb matches recompute:",
          float(np.abs(clap_text.encode(words[:5]) - mi["text_emb"][:5]).max()) < 1e-4)

    # system name -> (encoder, query form, V/A on, int8)
    systems = {
        "CLAP": ("clap", "raw", False, True),
        "CLAP + V/A": ("clap", "raw", True, True),
        "CLAP2-S": ("clap2_frag10", "raw", False, True),
        "CLAP2-S + V/A": ("clap2_frag10", "raw", True, True),
        "CLAP2-S float": ("clap2_frag10", "raw", False, False),
        "CLAP2-S 30s": ("clap2_seg30", "raw", False, True),
        "CLAP2-S 30s + V/A": ("clap2_seg30", "raw", True, True),
        "CLAP [Sentence.]": ("clap", "sentence", False, True),
        "CLAP2-S [Sentence.]": ("clap2_frag10", "sentence", False, True),
        "CLAP2-S [Sentence.] + V/A": ("clap2_frag10", "sentence", True, True),
        "CLAP + V/A kw-only": ("clap", "raw", "kw", True),
        "CLAP2-S + V/A kw-only": ("clap2_frag10", "raw", "kw", True),
        "CLAP2-S 30s + V/A kw-only": ("clap2_seg30", "raw", "kw", True),
        "TinyMU": ("tinymu", "raw", False, True),
        "TinyMU + V/A": ("tinymu", "raw", True, True),
        # mean of per-query z-scored CLAP and TinyMU similarities; V/A from CLAP
        "CLAP + TinyMU": ("fusion", "raw", False, True),
        "CLAP + TinyMU + V/A": ("fusion", "raw", True, True),
    }
    avail = {"clap"} | set(have) | ({"tinymu", "fusion"} if have_tinymu else set())
    systems = {k.replace("CLAP2-S", LABEL): v for k, v in systems.items() if v[0] in avail}
    contrasts = [(a.replace("CLAP2-S", LABEL), b.replace("CLAP2-S", LABEL)) for a, b in CONTRASTS]
    contrasts = [(a, b) for a, b in contrasts if a in systems and b in systems]

    # corpora: (name, base corpus, index subset, V/A source)
    def sub(c: Corpus, idx, name):
        return Corpus(name, [c.ids[i] for i in idx], [c.paths[i] for i in idx], c.queries,
                      tags=[c.tags[i] for i in idx] if c.tags is not None else None,
                      va=c.va[idx] if c.va is not None else None)

    runs = [("mtg600", base["mtg600"], None, "head"), ("mtgshard", base["mtgshard"], None, "head"),
            ("deam_test", sub(base["deam"], te, "deam_test"), te, "head"),
            ("deam_cv", base["deam"], None, "cv")]

    qcache = {}
    report = {"clap2_int8_cap": clap2_cap, "clap2_absmax": absmax, "clap_absmax": clap_absmax,
              "share_beyond_0.25": sat, "recipe_repro_va_maxdiff": repro_diff, "heads": head_report,
              "corpora": {}}
    for cname, c, idx, va_src in runs:
        src = "deam" if cname.startswith("deam") else cname
        res = {s: [] for s in systems}
        mood_info = {s: [] for s in systems}
        for sname, (e, qform, use_va, use_i8) in systems.items():
            pick = (lambda M: M) if idx is None else (lambda M: M[idx])
            if e == "fusion":
                Xc, Xt = i8(pick(emb[("clap", src)]), CLAP_INT8_CAP), i8(pick(emb[("tinymu", src)]), cap_of["tinymu"])
                Xq, cap = Xc, CLAP_INT8_CAP
            else:
                X = pick(emb[(e, src)])
                cap = cap_of[e] if use_i8 else None
                Xq = i8(X, cap) if cap else X
            track_va = None
            if use_va:
                if va_src == "cv":
                    track_va = cv_va[e]
                else:
                    track_va = heads[e](Xq)
            for q in c.queries:
                rel, tgt = relevance(c, q)
                if rel.sum() == 0:
                    continue
                text = sentence(q.text) if qform == "sentence" else q.text
                for enc in (("clap", "tinymu") if e == "fusion" else (e,)):
                    if (enc, text) not in qcache:
                        qcache[(enc, text)] = enc_text[enc].encode([text])[0]
                dist = None
                if e == "fusion":
                    qc = i8(unit(qcache[("clap", text)]), CLAP_INT8_CAP)
                    qt = i8(unit(qcache[("tinymu", text)]), cap_of["tinymu"])
                    sc, st = Xc @ qc, Xt @ qt
                    dist = -0.5 * ((sc - sc.mean()) / (sc.std() + 1e-9) + (st - st.mean()) / (st.std() + 1e-9))
                qv = qcache[("clap" if e == "fusion" else e, text)]
                ranked, info = search(q.text if qform == "raw" else text, qv, Xq, cap,
                                      track_va, mood_of[e] if use_va else None, nn_fallback=use_va != "kw",
                                      dist=dist)
                m = score(ranked, rel, tgt)
                m.update(q=q.text, cat=q.category, conf=info["conf"], path=info["path"],
                         top=[c.ids[i] for i in ranked[:5]])
                res[sname].append(m)
        summ = {}
        for sname, rows in res.items():
            cats = sorted({r["cat"] for r in rows}) + ["ALL"]
            summ[sname] = {}
            for cat in cats:
                sel = [r for r in rows if cat == "ALL" or r["cat"] == cat]
                keys = ["p", "ndcg", "mrr", "hit", "lift", "conf"] + (["tgt"] if "tgt" in sel[0] else [])
                summ[sname][cat] = {"n": len(sel), **{k: float(np.mean([r[k] for r in sel])) for k in keys},
                                    "fired": float(np.mean([r["conf"] > 0 for r in sel]))}
        tests = {f"{b} vs {a}": paired([r["ndcg"] for r in res[a]], [r["ndcg"] for r in res[b]])
                 for a, b in contrasts}
        report["corpora"][cname] = {"n_tracks": len(c.ids), "summary": summ, "paired_ndcg": tests, "per_query": res}
        print_summary(cname, len(c.ids), summ, tests)

    # pooled over corpora (deam_cv stands for DEAM: more songs, both heads by the same recipe)
    pooled = {}
    for a, b in contrasts:
        da, db = [], []
        for cname in ("mtg600", "mtgshard", "deam_cv"):
            da += [r["ndcg"] for r in report["corpora"][cname]["per_query"][a]]
            db += [r["ndcg"] for r in report["corpora"][cname]["per_query"][b]]
        pooled[f"{b} vs {a}"] = paired(da, db)
    report["pooled_ndcg"] = pooled
    print("\n### pooled per-query nDCG@10 (mtg600 + mtgshard + deam_cv)")
    for k, t in pooled.items():
        print(f"  {k:50} {t['mean_diff']:+.3f} [{t['ci95'][0]:+.3f}, {t['ci95'][1]:+.3f}] "
              f"p={t['wilcoxon_p']:.3g} wins {t['b_wins']}/{t['a_wins']} (n={t['n']})")
    report["timing_ms_per_window"] = {e: emb[(e, "deam", "spw")] * 1000 for e in ("clap2_frag10", "clap2_seg30")
                                      if (e, "deam", "spw") in emb}
    if have_tinymu:
        secs = [json.loads(l)["sec"] for n in base
                for l in open(TINYMU_CAPS / n / f"captions_{TINYMU_PROMPT_SLUG}.jsonl")]
        report["timing_ms_per_window"]["tinymu_caption"] = float(np.mean(secs)) * 1000
    json.dump(report, open(out / "results.json", "w"), indent=1)
    write_csvs(report, out)
    print(f"\nresults -> {out / 'results.json'}")


def write_csvs(report, out: Path) -> None:
    import csv
    with open(out / "summary.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["corpus", "system", "category", "queries", "p_at_10", "ndcg_at_10", "mrr_at_10",
                    "hit_at_10", "lift_at_10", "target_z_at_10", "mood_leg_fired", "mean_mood_confidence"])
        for cname, cr in report["corpora"].items():
            for sname, by in cr["summary"].items():
                for cat, m in by.items():
                    w.writerow([cname, sname, cat, m["n"]] + [f"{m[k]:.4f}" if k in m else ""
                               for k in ("p", "ndcg", "mrr", "hit", "lift", "tgt", "fired", "conf")])
    with open(out / "paired-ndcg.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["scope", "system_b", "system_a", "queries", "mean_ndcg_diff_b_minus_a", "ci95_low",
                    "ci95_high", "wilcoxon_p", "b_wins", "a_wins"])
        scopes = [(c, cr["paired_ndcg"]) for c, cr in report["corpora"].items()] + [("pooled", report["pooled_ndcg"])]
        for scope, tests in scopes:
            for k, t in tests.items():
                b, a = k.split(" vs ")
                w.writerow([scope, b, a, t["n"], f"{t['mean_diff']:.4f}", f"{t['ci95'][0]:.4f}",
                            f"{t['ci95'][1]:.4f}", f"{t['wilcoxon_p']:.4g}", t["b_wins"], t["a_wins"]])
    with open(out / "per-query.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["corpus", "system", "category", "query", "p_at_10", "ndcg_at_10", "mrr_at_10",
                    "target_z_at_10", "mood_path", "mood_confidence", "top5_track_ids"])
        for cname, cr in report["corpora"].items():
            for sname, rows in cr["per_query"].items():
                for r in rows:
                    w.writerow([cname, sname, r["cat"], r["q"], f"{r['p']:.3f}", f"{r['ndcg']:.4f}",
                                f"{r['mrr']:.4f}", f"{r['tgt']:.4f}" if "tgt" in r else "", r["path"],
                                f"{r['conf']:.3f}", " ".join(r["top"])])
    with open(out / "va-heads.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["encoder", "evaluation", "axis", "r2", "ccc", "rmse"])
        for e, h in report["heads"].items():
            for ev, axes in h.items():
                for ax, m in axes.items():
                    w.writerow([e, ev, ax, f"{m['R2']:.4f}", f"{m['CCC']:.4f}", f"{m['RMSE']:.4f}"])


def print_summary(name, n, summ, tests):
    first = next(iter(summ.values()))
    graded = "tgt" in first["ALL"]
    cols = ["p", "ndcg", "mrr", "hit", "lift"] + (["tgt"] if graded else []) + ["fired", "conf"]
    print(f"\n### {name} ({n} tracks)")
    for cat in first:
        print(f"\n{cat} (n={first[cat]['n']})")
        print(f"  {'system':28}" + "".join(f"{c:>7}" for c in cols))
        for s, by in summ.items():
            print(f"  {s:28}" + "".join(f"{by[cat][c]:7.3f}" for c in cols))
    print("\npaired per-query nDCG@10 (b - a, 95% CI, Wilcoxon p, wins b/a):")
    for k, t in tests.items():
        print(f"  {k:50} {t['mean_diff']:+.3f} [{t['ci95'][0]:+.3f}, {t['ci95'][1]:+.3f}] "
              f"p={t['wilcoxon_p']:.3g} {t['b_wins']}/{t['a_wins']}")


if __name__ == "__main__":
    main()
