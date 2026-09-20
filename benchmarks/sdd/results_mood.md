# Mood retrieval on MTG-Jamendo tags

The same 352 recordings the captions benchmark indexed, asked a different question: given a mood word, does the ranking fill with tracks a human gave that tag? Many answers are right per query, so the scoring is precision, mAP and nDCG over a relevance set — recall@k is meaningless here. This is the measurement the valence/arousal fusion exists to win, and the 39 queries below are the ones the corpus can support.

## Retrieval quality by query family

| Query family | Run | Queries | P@10 | mAP | nDCG@10 | R-prec | Prevalence | Lift over random |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| Affective — happy, sad, relaxing, dreamy … | fusion | 16 | 0.087 | 0.107 | 0.083 | 0.072 | 5.5% | 1.37× |
| Affective — happy, sad, relaxing, dreamy … | CLAP only | 16 | 0.050 | 0.091 | 0.061 | 0.059 | 5.5% | 1.04× |
| Compound — a mood and a sound, e.g. "emotional piano" | fusion | 12 | 0.125 | 0.143 | 0.107 | 0.128 | 4.6% | 2.85× |
| Compound — a mood and a sound, e.g. "emotional piano" | CLAP only | 12 | 0.108 | 0.143 | 0.104 | 0.111 | 4.6% | 2.77× |
| Contextual — commercial, documentary, children … | fusion | 11 | 0.082 | 0.098 | 0.083 | 0.081 | 4.2% | 2.58× |
| Contextual — commercial, documentary, children … | CLAP only | 11 | 0.064 | 0.102 | 0.086 | 0.075 | 4.2% | 1.99× |
| **All queries** | fusion | 39 | 0.097 | 0.115 | 0.091 | 0.092 | 4.8% | 2.17× |
| **All queries** | CLAP only | 39 | 0.072 | 0.111 | 0.081 | 0.080 | 4.8% | 1.84× |

## What fusion does to it

| Query family | P@10 CLAP → fusion | mAP CLAP → fusion |
|---|---|---|
| affective | 0.050 → 0.087 (+0.037) | 0.091 → 0.107 (+0.015) |
| compound | 0.108 → 0.125 (+0.017) | 0.143 → 0.143 (-0.001) |
| contextual | 0.064 → 0.082 (+0.018) | 0.102 → 0.098 (-0.005) |
| all | 0.072 → 0.097 (+0.026) | 0.111 → 0.115 (+0.005) |

## What this run can and cannot say

Of 39 queries, fusion improved P@10 on 7, left 27 unchanged and lowered 5. 21 return nothing relevant in the top ten under either configuration — several of those tags are not moods at all (`melodic`, `catchy`, `cool`) or are licensing categories (`commercial`, `communication`), and no audio model should be expected to hear them.

The family means are therefore carried by a few queries: the largest single move is `affective:happy`, 0.00 → 0.60. With sixteen affective queries over 352 recordings, this run says the direction is real and the mechanism works — it does not measure the size of the effect. The full MTG-Jamendo mood/theme subset (18,486 tracks, 56 tags) is what would.

## Every query, by family

| Query | Relevant | P@10 CLAP | P@10 fusion | AP fusion | Lift |
|---|---:|---:|---:|---:|---:|
| `affective:happy` | 45 | 0.00 | 0.60 | 0.381 | 4.7× |
| `affective:sad` | 18 | 0.20 | 0.20 | 0.184 | 3.9× |
| `affective:emotion` | 15 | 0.00 | 0.20 | 0.092 | 4.7× |
| `affective:soft` | 12 | 0.20 | 0.20 | 0.118 | 5.9× |
| `affective:relaxing` | 32 | 0.20 | 0.10 | 0.162 | 1.1× |
| `affective:love` | 21 | 0.00 | 0.10 | 0.118 | 1.7× |
| `affective:emotional` | 34 | 0.00 | 0.00 | 0.135 | 0.0× |
| `affective:melodic` | 21 | 0.00 | 0.00 | 0.083 | 0.0× |
| `affective:dreamy` | 18 | 0.10 | 0.00 | 0.056 | 0.0× |
| `affective:motivational` | 18 | 0.00 | 0.00 | 0.046 | 0.0× |
| `affective:catchy` | 15 | 0.00 | 0.00 | 0.043 | 0.0× |
| `affective:cool` | 14 | 0.00 | 0.00 | 0.043 | 0.0× |
| `affective:romantic` | 13 | 0.10 | 0.00 | 0.054 | 0.0× |
| `affective:fun` | 11 | 0.00 | 0.00 | 0.049 | 0.0× |
| `affective:inspiring` | 11 | 0.00 | 0.00 | 0.057 | 0.0× |
| `affective:upbeat` | 10 | 0.00 | 0.00 | 0.085 | 0.0× |
| `compound:happy+indie` | 14 | 0.80 | 0.80 | 0.490 | 20.1× |
| `compound:happy+pop` | 24 | 0.00 | 0.30 | 0.155 | 4.4× |
| `compound:happy+popfolk` | 14 | 0.20 | 0.30 | 0.188 | 7.5× |
| `compound:relaxing+piano` | 16 | 0.10 | 0.10 | 0.193 | 2.2× |
| `compound:emotional+piano` | 27 | 0.00 | 0.00 | 0.248 | 0.0× |
| `compound:emotional+classical` | 25 | 0.00 | 0.00 | 0.086 | 0.0× |
| `compound:motivational+pop` | 16 | 0.00 | 0.00 | 0.097 | 0.0× |
| `compound:catchy+indie` | 13 | 0.10 | 0.00 | 0.056 | 0.0× |
| `compound:dreamy+piano` | 12 | 0.00 | 0.00 | 0.040 | 0.0× |
| `compound:dreamy+pop` | 12 | 0.00 | 0.00 | 0.033 | 0.0× |
| `compound:catchy+poprock` | 12 | 0.00 | 0.00 | 0.072 | 0.0× |
| `compound:dreamy+acousticguitar` | 11 | 0.10 | 0.00 | 0.057 | 0.0× |
| `contextual:christmas` | 10 | 0.30 | 0.40 | 0.337 | 14.1× |
| `contextual:documentary` | 13 | 0.30 | 0.30 | 0.205 | 8.1× |
| `contextual:children` | 12 | 0.00 | 0.10 | 0.105 | 2.9× |
| `contextual:fashion` | 11 | 0.10 | 0.10 | 0.075 | 3.2× |
| `contextual:commercial` | 24 | 0.00 | 0.00 | 0.051 | 0.0× |
| `contextual:communication` | 22 | 0.00 | 0.00 | 0.054 | 0.0× |
| `contextual:entertainment` | 21 | 0.00 | 0.00 | 0.050 | 0.0× |
| `contextual:film` | 13 | 0.00 | 0.00 | 0.038 | 0.0× |
| `contextual:summer` | 13 | 0.00 | 0.00 | 0.027 | 0.0× |
| `contextual:advertising` | 11 | 0.00 | 0.00 | 0.067 | 0.0× |
| `contextual:corporate` | 11 | 0.00 | 0.00 | 0.066 | 0.0× |

## How this was measured

- **Ground truth**: the MTG-Jamendo tag row for each recording, which ships inside the Song Describer record itself (`song_describer_14_04_23.mtg-jamendo.tsv`, md5-verified). A track is relevant to a query when it carries that mood tag; a compound query needs both tags.
- **Judged corpus**: the 352 recordings carrying at least one mood/theme tag. The rest are dropped from each ranking before scoring — a track nobody tagged is missing a label, not lacking a mood, and counting it as a mistake would score the annotation rather than the search.
- **Ranked depth**: the whole library, not the shipped 50. mAP and R-precision are defined over a complete ranking; the captions benchmark is where the shipped cut is measured.
- **Queries**: `"<mood> music"` for a single tag and `"<mood> <instrument|genre>"` for a compound, with support floors (10 and 8 tracks) rather than a hand-picked list, so the query set follows the corpus.
- **Families**: about half of Jamendo's mood/theme vocabulary is what a library-music customer would license a track *for* — commercial, documentary, advertising. Those are asked and reported apart from the affective tags, because fusion has no reason to help on them and averaging the two would hide whether it helped at all.

### Caveats

- **The corpus is small.** 352 judged recordings and 10-45 relevant tracks per query: enough to rank configurations against each other, not enough to publish an absolute number. The full MTG-Jamendo mood/theme subset is 18,486 tracks and 56 tags, and is what this harness should be pointed at next.
- **Tags are sparse and multi-label.** A track tagged `happy` may also be relaxing and untagged for it. Precision is therefore a floor, and the lift column — precision against the tag's own prevalence — is the more honest comparison.
- **A tag is not a query.** People type "something upbeat for the gym", not "upbeat music". The phrasings here are deliberately the plainest form, which is the form the searcher's keyword spotting recognises with full confidence.
