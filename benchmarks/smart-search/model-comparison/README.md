# Text-to-music model comparison: LAION-CLAP, CLAP2-S and TinyMU

This benchmark asks which openly available model finds music best from a typed description such as “sad piano” or “energetic workout music”. It compares LAION’s 2023 CLAP music checkpoint, LAION’s CLAP2-S and the TinyMU captioner, each with and without a small valence/arousal (mood) model on top, on two public datasets whose tracks carry human labels. It also times each model on the local CPU.

Every system is scored through the same search code. That code is a line-for-line reimplementation of My Library’s Smart Search ranking in [`searcher.py`](https://github.com/Kalinka-Player/KalinkaPlayer/blob/4252aa65b0e6cea1a7a07bd8cd3ca356b07881ef/packages/kalinka-plugin-localfiles/src/kalinka_plugin_localfiles/searcher/searcher.py) and [`searcher_db.py`](https://github.com/Kalinka-Player/KalinkaPlayer/blob/4252aa65b0e6cea1a7a07bd8cd3ca356b07881ef/packages/kalinka-plugin-localfiles/src/kalinka_plugin_localfiles/searcher/searcher_db.py), with the defaults in [`config_model.py`](https://github.com/Kalinka-Player/KalinkaPlayer/blob/4252aa65b0e6cea1a7a07bd8cd3ca356b07881ef/packages/kalinka-plugin-localfiles/src/kalinka_plugin_localfiles/config_model.py). Only the models change between systems.

## Run

```sh
cd benchmarks/smart-search/model-comparison
python3.11 -m venv .venv
.venv/bin/pip install --index-url https://download.pytorch.org/whl/cpu torch==2.15.0 torchvision torchaudio
.venv/bin/pip install -r requirements.txt

.venv/bin/python deam_manifest.py --audio-dir DEAM/MEMD_audio \
    --annotations DEAM/annotations/static_annotations_averaged_songs_1_2000.csv \
                  DEAM/annotations/static_annotations_averaged_songs_2000_2058.csv
.venv/bin/python embed_clap.py                 # LAION-CLAP, production ONNX
.venv/bin/python embed_clap2.py                # CLAP2-S, 10 s fragments and 30 s windows
.venv-tinymu/bin/python caption_tinymu.py      # TinyMU captions; own environment, see requirements-tinymu.txt
.venv/bin/python evaluate.py --out results
.venv/bin/python figures.py --results results --out figures
.venv/bin/python benchmark_speed.py --model clap --threads 4
.venv/bin/python benchmark_speed.py --model clap2s --threads 4
.venv-tinymu/bin/python benchmark_speed.py --model tinymu --threads 4
```

`evaluate.py` skips any system whose embeddings or captions are not complete, so the CLAP and CLAP2-S rows can be reproduced without TinyMU. Set `CLAP2_SIZE=medium` (or `large`, `xs`, `xxs`) for `embed_clap2.py` and `evaluate.py` to test another CLAP2 size. Downloads and caches default to `work/`; [`kit_paths.py`](kit_paths.py) lists the environment variables that move them.

## Inputs

| Input | Source | Location |
| --- | --- | --- |
| LAION-CLAP music checkpoint `music_audioset_epoch_15_esc_90.14.pt` (HTSAT-base) as ONNX, its tokenizer, the DEAM-trained mood model `va_head_v1.onnx` and mood vocabulary `mood_index_v1.npz` | [clap-onnx-v2 release](https://github.com/Kalinka-Player/KalinkaPlayer/releases/tag/clap-onnx-v2). Audio encoder MD5 `28e87004e5d67e1993984b098895b842`, text encoder `9daeff66acd01cc455f2f889dae2680e`. | `KALINKA_MODELS`, default `work/models/` |
| all-MiniLM-L6-v2, int8 ONNX (`minilm/model.onnx`, `minilm/tokenizer.json`), MD5 `704fb8674c8a2e729e0f6b71905d66f8` | The text encoder Kalinka’s Jamendo plugin installs | under `KALINKA_MODELS` |
| CLAP2-S | [`laion/clap2-small-audioxl-s1B`](https://huggingface.co/laion/clap2-small-audioxl-s1B), revision `56bdd36775531b9b22ee7681437da32830bb50dd`, via `open_clip_torch` 4.2.2 | Hugging Face cache |
| TinyMU | Checkpoint from [AndreasXi/TinyMU](https://huggingface.co/AndreasXi/TinyMU); code from [xiquan-li/TinyMU](https://github.com/xiquan-li/TinyMU) at `385133d` | `TINYMU_DIR`, default `work/models/tinymu/` |
| MTG-Jamendo tags (`autotagging_genre.tsv`, `autotagging_instrument.tsv`, `autotagging_moodtheme.tsv`) | [MTG/mtg-jamendo-dataset](https://github.com/MTG/mtg-jamendo-dataset/tree/master/data) | `MTG_BENCH_DIR`, default `work/mtg_jamendo_eval/` |
| 600 MTG-Jamendo clips, the leading ~12 s of each track in [`inputs/mtg600_manifest.jsonl`](inputs/mtg600_manifest.jsonl), as `clips/<jamendo_id>.wav` | [`fetch_audio.py`](../../../.claude/skills/assess-ai-search/mtg_jamendo/fetch_audio.py) | under `MTG_BENCH_DIR` |
| 584 full-length MTG-Jamendo tracks: `raw_30s/audio-low` shard `00`, ids in [`inputs/mtgshard_ids.txt`](inputs/mtgshard_ids.txt) | MTG-Jamendo download script | `MTG_SHARD_DIR`, default `work/mtg_jamendo/00/` |
| DEAM audio (`MEMD_audio/`) and static annotations | [DEAM](https://cvml.unige.ch/databases/DEAM/) | `DEAM_MANIFEST`, default `work/deam_manifest.csv` |

## Corpora, queries and relevance

| Corpus | Tracks | Queries | A result counts as matching when |
| --- | --- | --- | --- |
| MTG-Jamendo clips | 600 leading ~12 s clips, stratified over the tag taxonomy | 75 | the track carries the query’s tag, or one tag from every group of a natural-language query |
| MTG-Jamendo full tracks | 584 tracks of shard `00` | the same 75 | as above |
| DEAM, held out | the 271 songs the deployed mood model never saw (seed 1234, 70/15/15 split) | 15 | the song is in the top quarter of the query’s target, ±z(valence) ± z(arousal) |
| DEAM, cross-validated | all 1,802 songs; every model’s mood predictions come from five folds trained with the same recipe | the same 15 | as above |

The MTG queries are 55 tag phrasings from [`inputs/mtg_queries.json`](inputs/mtg_queries.json) (25 genres such as “jazz music”, 15 instruments such as “music with piano”, 15 moods and themes such as “sad music”) and 20 natural-language requests in [`corpora.py`](corpora.py), such as “something melancholic for tonight”, each mapped to groups of acceptable tags. The DEAM queries name a mood (“calm relaxing music”) and the direction of valence, arousal or both that it implies. The cross-validated DEAM corpus stands for DEAM in pooled results because it has nearly seven times as many songs.

## Audio each model sees

Every model receives the same excerpts: Kalinka’s fragment rule in [`clap_embed.py`](clap_embed.py), which takes one 10 s window for a track shorter than 15 s, two at 33% and 66% for 15–30 s, and three at 25%, 50% and 75% otherwise.

- **LAION-CLAP** gets each window at 48 kHz mono after an int16 round trip, repeat-padded to 10 s as `laion_clap` does. The three embeddings are averaged and L2-normalised.
- **CLAP2-S** gets the same windows at their native length and resamples them to 32 kHz itself. A second variant, `30s`, gives it 30 s windows instead, its training length: one centred window below 60 s, two at 33% and 66% up to 90 s, three at 25%, 50% and 75% beyond.
- **TinyMU** captions each 10 s window, resampled to 16 kHz, with the prompt “Describe the music you hear.” and greedy decoding up to 200 tokens. The KV-cached decoder in [`tinymu_captioner.py`](tinymu_captioner.py) produces the same tokens as the authors’ `m2t.generate`. Each caption is embedded with MiniLM, and the window vectors are averaged.

## Ranking

For each query, the normalised text vector is quantised to int8 and compared by L2 distance with the int8 track vectors. The 50 nearest tracks receive a min-max scaled score. LAION-CLAP uses production’s symmetric int8 scale of ±0.25. CLAP2-S vectors reach ±0.68, so they use ±0.75. MiniLM caption vectors use a scale fitted the same way.

With a mood model, the query is mapped to a valence/arousal target. A literal mood word from the 45-word vocabulary sets the target, and the confidence is the share of content words that are mood words. Otherwise the three nearest vocabulary words in the model’s own text space set it, if the best match reaches cosine 0.3. The 200 tracks nearest the target in valence/arousal join the candidates. Each track scores `(1 − w) × sound + w × mood`, with `w = 0.6 × confidence`. LAION-CLAP uses the deployed `va_head_v1.onnx` and `mood_index_v1.npz`. Each other model gets a mood model trained with the deployed recipe in [`va_recipe.py`](va_recipe.py), on its own DEAM embeddings, with the same split and seed, and the mood words re-encoded by its own text encoder.

`CLAP + TinyMU` averages the per-query z-scored similarities of both models with fixed 0.5 weights, not tuned on these queries, and uses LAION-CLAP’s mood model.

## Mood model

The valence/arousal (mood) model is Kalinka’s own. [`va_recipe.py`](va_recipe.py) holds its recipe: a numpy MLP, `Linear(d, 128) → ReLU → Dropout(0.3) → Linear(128, 2)`, where `d` is 512 for both CLAP models and 384 for TinyMU’s MiniLM caption vectors. It has 65,922 parameters at `d = 512`. Training uses Adam at a learning rate of 1e-3, batches of 64, weight decay of 1e-4, at most 500 epochs and early stopping after 40 epochs without a gain in validation loss. Labels are z-scored on the training songs. The deployed graph undoes the z-score and clips the output to DEAM’s 1–9 scale.

The split is song-level, 70/15/15 with seed 1234: 1,261 songs for training, 270 for early stopping and 271 held out. The cross-validated DEAM corpus trains five more models per encoder, each with one fifth of the songs held out and 270 of the others used for early stopping. The 45-word vocabulary in `mood_index_v1.npz` places each word on the same scale by hand. `evaluate.py` re-encodes it with each model’s own text encoder for the nearest-word fallback.

| Vectors from | Valence R², held out | Arousal R², held out | Valence R², 5-fold | Arousal R², 5-fold |
| --- | --- | --- | --- | --- |
| LAION-CLAP (deployed `va_head_v1.onnx`) | 0.594 | 0.711 | 0.618 | 0.698 |
| CLAP2-S, 10 s fragments | 0.620 | 0.722 | 0.609 | 0.685 |
| CLAP2-S, 30 s windows | 0.554 | 0.645 | 0.554 | 0.667 |
| TinyMU captions through MiniLM | 0.467 | 0.590 | 0.458 | 0.526 |

[`results/va-heads.csv`](results/va-heads.csv) adds CCC and RMSE. `evaluate.py` runs two mood-leg variants per encoder. `+ V/A` is the full leg, and `+ V/A kw-only` drops the nearest-word fallback. On cross-validated DEAM, the full leg raised per-query nDCG@10 by 0.136 for LAION-CLAP, 0.143 for CLAP2-S and 0.098 for TinyMU (Wilcoxon p < 0.01 each). On both MTG-Jamendo corpora the change stayed within ±0.01 and was not significant. Dropping the fallback changed LAION-CLAP’s pooled nDCG@10 by +0.003 (p = 0.58). With CLAP2-S’s text encoder, the fallback’s 0.3 cosine threshold gives genre queries a mean mood weight of 0.47, against 0.28 with LAION-CLAP. The threshold therefore has to be recalibrated, or the fallback dropped, when the text encoder changes. [`results/paired-ndcg.csv`](results/paired-ndcg.csv) has each comparison per corpus.

## Metrics and statistics

For each query, [`evaluate.py`](evaluate.py) reports how many of the first ten results match (P@10), nDCG@10, the reciprocal rank of the first match and whether any match appears. DEAM adds the mean target z-score of the first ten results. Systems are compared on per-query nDCG@10, with a 95% paired bootstrap interval of 10,000 resamples and a Wilcoxon signed-rank test. Pooled comparisons cover 165 query–corpus pairs: 75 on each MTG corpus and 15 on cross-validated DEAM.

## Checks the evaluation prints

- The mood recipe retrained on the release LAION-CLAP embeddings of DEAM reproduces the deployed `va_head_v1.onnx` predictions to within about 1e-6 on the 1–9 scale, with held-out R² of 0.594 for valence and 0.711 for arousal.
- The deployed mood-vocabulary vectors match a fresh encode with the release text encoder.
- Before this benchmark, its LAION-CLAP embeddings were compared with fresh encodes from the release encoder. The cosine similarity rounded to 1.000000 on every sampled track.

## Speed

[`benchmark_speed.py`](benchmark_speed.py) loads one model per process, analyses full-length MTG-Jamendo tracks with three 10 s windows each (20 tracks, or 8 for TinyMU), and encodes 24 text queries after one warm-up of each. It records the median time per track and per query, the load time and the peak resident memory of the process. The published run used an Intel Core Ultra 9 185H with 4 threads per model, onnxruntime 1.32.0 for LAION-CLAP, and PyTorch 2.15.0 (CLAP2-S) or 2.4.1 (TinyMU) on CPU. Neither CLAP2-S nor TinyMU has an ONNX export, so their PyTorch timings are not a like-for-like runtime comparison.

## Outputs

| File | Contents |
| --- | --- |
| [`results/summary.csv`](results/summary.csv) | P, nDCG, MRR, hit rate and lift at 10 for each corpus, system and query category |
| [`results/paired-ndcg.csv`](results/paired-ndcg.csv) | Per-query nDCG@10 differences with bootstrap intervals, Wilcoxon p and win counts |
| [`results/per-query.csv`](results/per-query.csv) | Each query’s scores, mood path and confidence, and its top five track ids |
| [`results/va-heads.csv`](results/va-heads.csv) | R², CCC and RMSE of each model’s mood predictions on DEAM |
| [`results/speed.csv`](results/speed.csv) | Timing and memory per model |

## Limits

LAION-Audio-XL, CLAP2’s training set, lists Jamendo and the Free Music Archive among its sources, and DEAM’s songs come from the Free Music Archive, Jamendo and MedleyDB. TinyMU’s training mix includes Free Music Archive audio. Any overlap would favour the newer models. MTG-Jamendo tags are incomplete, so a track without a tag can still fit a query. The DEAM query set is small, and its 271-song held-out corpus moves more between systems than the cross-validated one.
