"""Corpora and queries: MTG-Jamendo (600 clips, 584 full tracks) and DEAM."""
from __future__ import annotations

import csv
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from kit_paths import DEAM_MANIFEST, KIT, MTG_BENCH, MTG_SHARD

DEAM_SPLIT_SEED = 1234          # the deployed head's 70/15/15 split

# Natural-language queries a user would type, mapped to MTG-Jamendo tags. Each
# inner list is an any-of group; a track is relevant when it matches every group.
NL_QUERIES = [
    ("something melancholic for tonight", [["mood/theme---sad", "mood/theme---melancholic"]]),
    ("upbeat happy summer vibes", [["mood/theme---happy", "mood/theme---upbeat", "mood/theme---summer",
                                    "mood/theme---positive", "mood/theme---fun"]]),
    ("calm relaxing music to unwind", [["mood/theme---relaxing", "mood/theme---calm",
                                        "mood/theme---meditative", "mood/theme---soft"]]),
    ("dark and brooding", [["mood/theme---dark"]]),
    ("music for deep focus and studying", [["genre---ambient", "mood/theme---meditative",
                                            "mood/theme---background", "mood/theme---calm"]]),
    ("energetic workout music", [["mood/theme---energetic", "mood/theme---sport", "mood/theme---powerful",
                                  "mood/theme---fast", "mood/theme---motivational"]]),
    ("relaxing music to fall asleep to", [["mood/theme---relaxing", "mood/theme---calm", "mood/theme---slow",
                                           "mood/theme---soft", "mood/theme---meditative"]]),
    ("epic orchestral film score", [["mood/theme---epic", "mood/theme---trailer", "mood/theme---dramatic"],
                                    ["genre---orchestral", "genre---soundtrack", "mood/theme---film"]]),
    ("acoustic guitar singer songwriter", [["instrument---acousticguitar"],
                                           ["instrument---voice", "genre---folk", "genre---singersongwriter"]]),
    ("smooth jazz", [["genre---jazz"]]),
    ("ambient electronic soundscapes", [["genre---ambient", "mood/theme---soundscape", "mood/theme---space"]]),
    ("inspiring music for a corporate video", [["mood/theme---inspiring", "mood/theme---uplifting",
                                                "mood/theme---motivational", "mood/theme---corporate",
                                                "mood/theme---advertising"]]),
    ("romantic love song", [["mood/theme---love", "mood/theme---romantic"]]),
    ("heavy distorted guitars", [["instrument---electricguitar"],
                                 ["genre---rock", "genre---metal", "genre---hardrock", "genre---punkrock",
                                  "mood/theme---heavy"]]),
    ("chilled lounge background music", [["genre---chillout", "genre---lounge", "mood/theme---background"]]),
    ("dreamy atmospheric music", [["mood/theme---dream", "mood/theme---space", "mood/theme---soundscape",
                                   "genre---ambient"]]),
    ("fast electronic dance beats", [["genre---dance", "genre---techno", "genre---house", "genre---trance",
                                      "genre---drumnbass"]]),
    ("sad piano", [["instrument---piano"],
                   ["mood/theme---sad", "mood/theme---melancholic", "mood/theme---emotional"]]),
    ("happy acoustic song", [["mood/theme---happy", "mood/theme---positive", "mood/theme---upbeat"],
                             ["instrument---acousticguitar", "instrument---guitar"]]),
    ("music for an action movie trailer", [["mood/theme---trailer", "mood/theme---action", "mood/theme---epic"]]),
]

# (query, valence sign, arousal sign); target = sv*z(V) + sa*z(A).
DEAM_QUERIES = [
    ("happy cheerful music", +1, 0),
    ("joyful uplifting music", +1, 0),
    ("sad melancholic music", -1, 0),
    ("something melancholic for tonight", -1, 0),
    ("dark gloomy music", -1, 0),
    ("energetic intense music", 0, +1),
    ("energetic workout music", 0, +1),
    ("loud aggressive music", 0, +1),
    ("calm relaxing music", 0, -1),
    ("quiet peaceful music", 0, -1),
    ("relaxing music to fall asleep to", 0, -1),
    ("happy energetic party music", +1, +1),
    ("angry aggressive music", -1, +1),
    ("peaceful serene music", +1, -1),
    ("sad slow ballad", -1, -1),
]


@dataclass
class Query:
    text: str
    category: str
    groups: list = field(default_factory=list)
    va: tuple = (0, 0)


@dataclass
class Corpus:
    name: str
    ids: list
    paths: list
    queries: list
    tags: list = None
    va: np.ndarray = None


def mtg_queries() -> list:
    qs = []
    for cat, items in json.load(open(KIT / "inputs/mtg_queries.json"))["categories"].items():
        for q in items:
            qs.append(Query(q["q"], cat, [[q["tag"]]]))
    qs += [Query(t, "natural", g) for t, g in NL_QUERIES]
    return qs


def load_mtg600() -> Corpus:
    ids, paths, tags = [], [], []
    for line in open(KIT / "inputs/mtg600_manifest.jsonl"):
        m = json.loads(line)
        wav = MTG_BENCH / "clips" / f"{m['jamendo_id']}.wav"
        if wav.exists():
            ids.append(str(m["jamendo_id"]))
            paths.append(str(wav))
            tags.append(set(m["tags"]))
    return Corpus("mtg600", ids, paths, mtg_queries(), tags=tags)


def load_mtgshard() -> Corpus:
    by_id: dict[str, set] = {}
    for fn in ("autotagging_genre.tsv", "autotagging_instrument.tsv", "autotagging_moodtheme.tsv"):
        with open(MTG_BENCH / fn) as f:
            next(f)
            for line in f:
                p = line.rstrip("\n").split("\t")
                if len(p) >= 6:
                    by_id.setdefault(Path(p[3]).stem, set()).update(t for t in p[5:] if t)
    ids, paths, tags = [], [], []
    for mp3 in sorted(MTG_SHARD.glob("*.mp3")):
        jid = mp3.name.split(".")[0]
        if jid in by_id:
            ids.append(jid)
            paths.append(str(mp3))
            tags.append(by_id[jid])
    return Corpus("mtgshard", ids, paths, mtg_queries(), tags=tags)


def load_deam() -> Corpus:
    ids, paths, va = [], [], []
    with open(DEAM_MANIFEST) as f:
        for r in csv.DictReader(f):
            if os.path.exists(r["audio_path"]):
                ids.append(r["song_id"])
                paths.append(r["audio_path"])
                va.append((float(r["valence_mean"]), float(r["arousal_mean"])))
    qs = []
    for text, sv, sa in DEAM_QUERIES:
        cat = "quadrant" if sv and sa else ("valence" if sv else "arousal")
        qs.append(Query(text, cat, va=(sv, sa)))
    return Corpus("deam", ids, paths, qs, va=np.array(va, dtype=np.float32))


LOADERS = {"mtg600": load_mtg600, "mtgshard": load_mtgshard, "deam": load_deam}


def deam_split(n: int, seed: int = DEAM_SPLIT_SEED):
    """The deployed head's song-level 70/15/15 split (its first RNG draw)."""
    perm = np.random.default_rng(seed).permutation(n)
    n_tr, n_va = int(0.70 * n), int(0.15 * n)
    return perm[:n_tr], perm[n_tr:n_tr + n_va], perm[n_tr + n_va:]
