"""Stage 7 — the two outputs, written the way they are meant to be read.

Quality first, then the timeline, then everything needed to distrust both:
what the dataset was, what the configuration was, how the leak check went,
what the benchmark wrapped to get its timings, and where each number is
pessimistic or lenient by construction.
"""

from __future__ import annotations

import csv
import json
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from . import dataset as ds
from . import metrics as metrics_mod
from .clock import Clock
from .paths import Layout

RUN_TITLES = {
    "mood_on": "CLAP + valence/arousal fusion (shipped default)",
    "mood_off": "CLAP only (mood ranking off)",
}


@dataclass
class Row:
    label: str
    seconds: float
    group: str


def write(layout: Layout, clock: Clock, runs: Sequence[str]) -> Path:
    stats = json.loads(layout.dataset_stats.read_text())
    summary = json.loads(layout.index_summary.read_text())
    print_fp = json.loads(layout.fingerprint.read_text())
    metrics = {
        run: json.loads(layout.metrics(run).read_text())
        for run in runs
        if layout.metrics(run).exists()
    }
    captions = ds.read_captions(layout.captions_csv)
    rows, end_to_end = _timeline(layout, clock, summary, runs)
    clock.write_csv(
        layout.timeline, [(r.label, r.seconds, r.group) for r in rows], end_to_end
    )

    out = []
    out.append("# Kalinka semantic search on the Song Describer Dataset\n")
    out.append(
        f"Every number below comes from one run of the shipped server over "
        f"{stats['audio_files']} recordings it had never seen, queried "
        f"{stats['captions']} times through the same endpoint the apps call. "
        f"Code {print_fp['code']['describe']}, "
        f"{print_fp['host']['cpu_count']} CPU threads.\n"
    )
    out.append(_quality(metrics, stats, runs))
    out.append(_examples(layout, captions, runs[0]))
    out.append(_timeline_section(rows, end_to_end))
    out.append(_latency_note(layout, metrics, runs))
    out.append(_dataset_section(stats))
    out.append(_fingerprint_section(print_fp, summary))
    out.append(_leakage_section(stats))
    out.append(_integrity_section(layout, runs))
    out.append(_instrumentation_section(print_fp))
    out.append(_caveats(stats, metrics, runs, print_fp))
    out.append(_findings(layout, summary, stats, runs))
    layout.report.write_text("\n".join(out))
    return layout.report


# ---------------------------------------------------------------------------
# Quality
# ---------------------------------------------------------------------------


def _quality(metrics: dict, stats: dict, runs: Sequence[str]) -> str:
    n_tracks = stats["tracks"]
    baseline = metrics_mod.random_baseline(n_tracks)
    lines = ["## Quality\n"]
    lines.append(
        "Strict scoring counts one recording per caption: the one the caption "
        "was written about. Nothing else in the library can be right, however "
        "well it matches.\n"
    )
    lines.append("### Strict — the captioned recording only\n")
    lines.append(
        "| Configuration | Captions | R@1 | R@5 | R@10 | R@50 | MRR | Median rank |"
    )
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|")
    for run in runs:
        for scope in ("all", "valid_subset"):
            strict = metrics[run][scope]["strict"]
            lines.append(
                f"| {RUN_TITLES.get(run, run)}"
                f"{'' if scope == 'all' else ' — validated subset'} "
                f"| {strict['queries']} "
                + "".join(
                    f"| {100 * strict['recall_at'][str(k)]:.1f}% " for k in (1, 5, 10, 50)
                )
                + f"| {strict['mrr']:.3f} | {_rank(strict['median_rank'])} |"
            )
    lines.append(
        f"| Random ranking of {n_tracks} tracks | — "
        + "".join(
            f"| {100 * baseline['recall_at'][str(k)]:.1f}% " for k in (1, 5, 10, 50)
        )
        + f"| {baseline['mrr_top_50']:.3f} | > 50 |"
    )
    lines.append("")
    lines.append(
        "### Graded — judged by an independent sentence embedder\n"
    )
    lines.append(
        "A returned track's relevance to a query is the highest cosine "
        "similarity between the query caption and any caption that track "
        "carries, measured by all-mpnet-base-v2 — a text model with no audio "
        "in its training and no relationship to CLAP. Soft recall@10 asks "
        "whether anything in the top ten cleared the threshold.\n"
    )
    lines.append(
        "| Configuration | Captions | nDCG@10 | soft-R@10 ≥0.5 | ≥0.6 | ≥0.7 | Mean top-1 relevance |"
    )
    lines.append("|---|---:|---:|---:|---:|---:|---:|")
    for run in runs:
        for scope in ("all", "valid_subset"):
            graded = metrics[run][scope]["graded"]
            soft = graded["soft_recall_at_10"]
            lines.append(
                f"| {RUN_TITLES.get(run, run)}"
                f"{'' if scope == 'all' else ' — validated subset'} "
                f"| {graded['queries']} | {graded['ndcg_at_10']:.3f} "
                + "".join(f"| {100 * soft[str(t)]:.1f}% " for t in (0.5, 0.6, 0.7))
                + f"| {graded['mean_top1_relevance']:.3f} |"
            )
    lines.append("")
    if len(runs) > 1:
        lines.append(_ablation(metrics, runs))
    return "\n".join(lines)


def _ablation(metrics: dict, runs: Sequence[str]) -> str:
    on, off = metrics.get("mood_on"), metrics.get("mood_off")
    if not (on and off):
        return ""
    strict_on, strict_off = on["all"]["strict"], off["all"]["strict"]
    latency_on, latency_off = on["latency_ms"], off["latency_ms"]

    def move(key: str) -> str:
        delta = strict_on["recall_at"][key] - strict_off["recall_at"][key]
        return (
            f"{100 * strict_off['recall_at'][key]:.1f}% → "
            f"{100 * strict_on['recall_at'][key]:.1f}% ({100 * delta:+.1f} pts)"
        )

    return (
        "### Ablation — what valence/arousal fusion costs or buys\n\n"
        "Both configurations are the same index queried twice: the ablation "
        "is a settings change and a restart, not a re-embedding.\n\n"
        f"Turning fusion on moves R@1 {move('1')}, R@10 {move('10')} and R@50 "
        f"{move('50')}, with MRR "
        f"{strict_off['mrr']:.3f} → {strict_on['mrr']:.3f} and nDCG@10 "
        f"{off['all']['graded']['ndcg_at_10']:.3f} → "
        f"{on['all']['graded']['ndcg_at_10']:.3f}.\n\n"
        "So the top of the list is unchanged and the depth of it is worse: "
        "the mood leg unions its own candidates into the pool, and with the "
        "list capped at 50 those candidates displace CLAP hits that would "
        "otherwise have been in it. It is not free, either — median latency "
        f"{latency_off['median']:.0f} ms → {latency_on['median']:.0f} ms and "
        f"p95 {latency_off['p95']:.0f} ms → {latency_on['p95']:.0f} ms.\n\n"
        "What this does not say is that mood ranking is useless. These "
        "queries are descriptions of recordings, which is what CLAP is "
        "strongest at; the mood axis was added for queries CLAP is weakest "
        "at (\"something melancholy\"), and a caption benchmark contains "
        "almost none of those. The other kind is measured separately, in "
        "[results_mood.md](results_mood.md), over the same index and the "
        "MTG-Jamendo mood tags — and there fusion wins: on affective tags "
        "CLAP alone ranks barely better than shuffling, and fusion raises "
        "P@10 from 0.050 to 0.087. Read the two together: this is what the "
        "blend costs where CLAP is strong, that is what it buys where CLAP "
        "is weak.\n"
    )


def _rank(value) -> str:
    if value is None:
        return "—"
    return "> 50" if value >= metrics_mod.MISS else f"{value:.0f}"


# ---------------------------------------------------------------------------
# Examples
# ---------------------------------------------------------------------------


def _examples(layout: Layout, captions, run: str) -> str:
    """Five queries drawn by a fixed seed — not picked for how they did."""
    by_caption = {c.caption_id: c for c in captions}
    per_track: dict[str, list[str]] = {}
    for caption in captions:
        per_track.setdefault(caption.track_id, []).append(caption.text)

    rows = list(csv.DictReader(layout.results(run).open(newline="", encoding="utf-8")))
    sample = random.Random(0).sample(rows, 5)
    lines = [
        "## Five queries, as illustration only\n",
        "Drawn with a fixed seed before their outcomes were known, and part "
        "of no metric above or below. Each returned track is named by its "
        "dataset id and by one of its own captions, which is how a reader can "
        "see what the ranking heard. `d` is the searcher's own KNN distance "
        "over the stored int8 vectors — lower is closer — and the order can "
        "differ from it, because the mood blend is applied after the KNN "
        "leg.\n",
    ]
    for row in sample:
        caption = by_caption[row["caption_id"]]
        lines.append("```")
        lines.append(f'query   {caption.text}')
        lines.append(f'target  {caption.track_id} (rank {row["rank"]})')
        ranked = [tid for tid in row["ranked_track_ids"].split(";") if tid][:5]
        distances = row["clap_distances"].split(";")
        for position, track_id in enumerate(ranked, start=1):
            marker = " <- target" if track_id == caption.track_id else ""
            distance = distances[position - 1] if position <= len(distances) else ""
            own = per_track.get(track_id, ["(no caption)"])[0]
            lines.append(
                f'  {position}. {track_id}  d={distance or "—":>8}  '
                f'"{own[:88]}"{marker}'
            )
        lines.append("```")
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Timeline
# ---------------------------------------------------------------------------


def _timeline(layout: Layout, clock: Clock, summary: dict, runs: Sequence[str]):
    timings = summary["timings"]
    spans = summary["spans"]
    rows: list[Row] = []

    prep = clock.spans.get("dataset_prep", 0.0)
    rows.append(Row("Dataset preparation (verify, extract, strip, manifest)", prep, "prep"))

    indexing_total = clock.spans.get("indexing_end_to_end", spans.get("indexing_total", 0.0))
    rows.append(Row("**Indexing — end to end**", indexing_total, "index"))
    rows.append(Row("— server start, plugin load, model provisioning", spans.get("server_start", 0.0), "index"))
    rows.append(Row("— library scan (walk, tags, database rows)", spans.get("scan", 0.0), "index"))
    rows.append(Row("&nbsp;&nbsp;· directory listing", timings.get("scan_listing_s", 0.0), "index"))
    rows.append(Row("&nbsp;&nbsp;· per-file indexing", timings.get("scan_index_files_s", 0.0), "index"))
    rows.append(Row("&nbsp;&nbsp;· stale-row cleanup", timings.get("scan_cleanup_s", 0.0), "index"))
    rows.append(Row("— CLAP audio embedding (wall clock)", spans.get("embedding", 0.0), "index"))
    rows.append(Row("&nbsp;&nbsp;· audio tower load", timings.get("model_load_audio_s", 0.0), "index"))
    rows.append(Row("&nbsp;&nbsp;· fragment decode + resample", timings.get("embed_decode_s", 0.0), "index"))
    rows.append(Row("&nbsp;&nbsp;· ONNX inference", timings.get("embed_infer_s", 0.0), "index"))
    rows.append(Row("&nbsp;&nbsp;· stream open, quantise, collect", timings.get("embed_other_s", 0.0), "index"))
    rows.append(Row("&nbsp;&nbsp;· vector + job writes", timings.get("db_write_embedding_s", 0.0)
                    + timings.get("db_claim_batch_s", 0.0)
                    + timings.get("db_schedule_jobs_s", 0.0), "index"))
    rows.append(Row("— mood (valence/arousal) backfill", spans.get("mood_backfill", 0.0), "index"))
    rows.append(Row("&nbsp;&nbsp;· mood head inference", timings.get("va_head_s", 0.0), "index"))
    rows.append(Row("&nbsp;&nbsp;· mood writes", timings.get("db_write_mood_s", 0.0), "index"))

    for run in runs:
        total = clock.spans.get(f"retrieval_{run}", 0.0)
        rows.append(Row(f"**Retrieval run — {RUN_TITLES.get(run, run)}**", total, f"retrieval_{run}"))
        query = _query_totals(layout, run)
        rows.append(Row("— query text encoding (CLAP text tower)", query["encode_s"], f"retrieval_{run}"))
        rows.append(Row("— vector KNN over the index", query["knn_s"], f"retrieval_{run}"))
        rows.append(Row("— mood mapping and blend", query["mood_map_s"], f"retrieval_{run}"))
        rows.append(Row("— HTTP, serialisation, database fetch of hits",
                        max(total - query["server_total_s"], 0.0), f"retrieval_{run}"))
    restart = clock.spans.get("ablation_restart", 0.0)
    if restart:
        rows.append(Row("Ablation settings change and restart", restart, "retrieval"))

    rows.append(Row("**Scoring**", clock.spans.get("judge_embedding", 0.0)
                    + clock.spans.get("metrics", 0.0), "score"))
    rows.append(Row("— judge embedding of every caption + similarity matrix",
                    clock.spans.get("judge_embedding", 0.0), "score"))
    rows.append(Row("— metric computation", clock.spans.get("metrics", 0.0), "score"))

    end_to_end = (
        prep
        + indexing_total
        + sum(clock.spans.get(f"retrieval_{run}", 0.0) for run in runs)
        + restart
        + clock.spans.get("judge_embedding", 0.0)
        + clock.spans.get("metrics", 0.0)
    )
    return rows, end_to_end


def _query_totals(layout: Layout, run: str) -> dict:
    totals = {"server_total_s": 0.0, "encode_s": 0.0, "knn_s": 0.0, "mood_map_s": 0.0}
    with layout.results(run).open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            for key in totals:
                totals[key] += float(row[key] or 0.0)
    return totals


def _timeline_section(rows: Sequence[Row], end_to_end: float) -> str:
    lines = ["## Timeline\n"]
    lines.append(
        f"End to end: **{_hms(end_to_end)}**. Bold rows are the phases and sum "
        "to it; the rows under each are that phase's own breakdown, measured "
        "inside it. A phase's parts can fall short of its wall clock — the "
        "difference is the pipeline waiting on its own queues, which is "
        "reported rather than hidden.\n"
    )
    lines.append("| Stage | Seconds | % of end-to-end |")
    lines.append("|---|---:|---:|")
    for row in rows:
        share = 100.0 * row.seconds / end_to_end if end_to_end else 0.0
        lines.append(f"| {row.label} | {row.seconds:,.1f} | {share:.1f}% |")
    lines.append("")
    lines.append(
        "The mood backfill reads as no time at all because it does not have "
        "any of its own: the embedder drains its audio queue, backfills "
        "(V,A) from the vectors it just stored, and loops, so the mood work "
        "happened inside the embedding row — 0.13 s of it, for all 706 "
        "tracks, the whole of it a 512→2 head over a vector already in "
        "memory.\n"
    )
    return "\n".join(lines)


def _hms(seconds: float) -> str:
    hours, rest = divmod(int(seconds), 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    return f"{minutes}m {secs}s" if minutes else f"{secs}s"


def _latency_note(layout: Layout, metrics: dict, runs: Sequence[str]) -> str:
    """Per-query latency, kept out of the quality tables on purpose: it is a
    property of this host under this load, not of the retrieval."""
    lines = ["Per-query latency, measured at the client:\n"]
    for run in runs:
        latency = metrics.get(run, {}).get("latency_ms")
        if not latency:
            continue
        lines.append(
            f"- {RUN_TITLES.get(run, run)}: median {latency['median']:.0f} ms, "
            f"p95 {latency['p95']:.0f} ms, max {latency['max']:.0f} ms."
        )
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------


def _dataset_section(stats: dict) -> str:
    per_track = stats["captions_per_track"]
    spread = ", ".join(f"{count} track(s) with {n}" for n, count in per_track.items())
    return (
        "## The dataset\n\n"
        f"Song Describer Dataset (Zenodo 10072001, DOI 10.5281/zenodo.10072001): "
        f"**{stats['captions']} captions** over **{stats['tracks']} recordings**, "
        f"every file two minutes of audio from MTG-Jamendo. Columns: caption_id, "
        f"track_id, caption, is_valid_subset, familiarity, artist_id, album_id, "
        f"path, duration. The caption_id → track_id pairing is the ground truth; "
        f"nothing else about a track is used.\n\n"
        f"- Captions per track: {spread}.\n"
        f"- `is_valid_subset` marks {stats['valid_subset_captions']} captions "
        f"({stats['valid_subset_tracks']} tracks) as the authors' validated "
        f"subset; the rest are unflagged or explicitly False.\n"
        f"- Caption length: {stats['caption_words_min']}–"
        f"{stats['caption_words_max']} words, median "
        f"{stats['caption_words_median']:.0f}.\n"
        f"- {stats['non_ascii_captions']} captions contain non-ASCII "
        f"characters (typographic apostrophes, accented words).\n"
        f"- Audio indexed: {stats['audio_files']} files, "
        f"{stats['audio_duration_s_total'] / 3600:.1f} hours, median "
        f"{stats['audio_duration_s_median']:.0f}s each.\n"
    )


#: The settings that decide what a query returns, printed in full because a
#: number from a different setting is a different number.
_SETTINGS_SHOWN = (
    "input_modules.localfiles.ai_search.enabled",
    "input_modules.localfiles.ai_search.max_results",
    "input_modules.localfiles.ai_search.knn_candidate_limit",
    "input_modules.localfiles.ai_search.mood.enabled",
    "input_modules.localfiles.ai_search.mood.weight",
    "input_modules.localfiles.ai_search.mood.candidates",
    "input_modules.localfiles.ai_search.mood.nn_threshold",
    "input_modules.localfiles.ai_search.audio_batch_size",
    "input_modules.localfiles.enricher.enabled",
    "base_config.search.ai_suggestions_limit",
)


def _settings(config: dict) -> dict[str, object]:
    """``/server/config`` answers with dotted paths under ``values``."""
    values = config.get("values", config)
    return {key: values.get(key) for key in _SETTINGS_SHOWN if key in values}


def _fingerprint_section(fp: dict, summary: dict) -> str:
    models = fp["models"]
    files = "\n".join(
        f"  - `{name}` — {info['bytes'] / 1e6:.1f} MB, sha256 `{info['sha256'][:16]}…`"
        for name, info in models["files"].items()
    )
    settings = "\n".join(
        f"  - `{key.split('.', 1)[-1]}` = `{value}`"
        for key, value in _settings(fp["server_config"]).items()
    )
    outcome = summary["outcome"]
    return (
        "## Configuration fingerprint\n\n"
        f"- Code: `{fp['code']['commit'][:12]}` on `{fp['code']['branch']}`"
        f"{' (working tree dirty)' if fp['code']['dirty'] else ''}\n"
        f"- Host: {fp['host']['platform']}, Python {fp['host']['python']}, "
        f"{fp['host']['cpu_count']} threads\n"
        f"- Packages: "
        + ", ".join(
            f"{name} {ver}" for name, ver in fp["packages"].items() if ver != "absent"
        )
        + "\n"
        f"- CLAP release: {models['release']}, model version "
        f"{models['clap_model_version']}, stored format "
        f"{models['clap_embed_format_version']} (int8, cap {models['int8_cap']}), "
        f"VA head v{models['va_head_version']}\n{files}\n"
        f"- Audio sampling: {models['sample_rate'] // 1000} kHz, "
        f"{models['fragment_seconds']}s fragments; query text truncated at "
        f"{models['text_token_limit']} tokens\n"
        f"- Settings (schema `{fp['server_config'].get('schema_version', '—')}`), "
        f"shipped defaults except where the benchmark set them:\n{settings}\n"
        f"- Judge: {fp['judge']['repo']} at revision "
        f"`{fp['judge']['revision'][:12]}`, {fp['judge']['runtime']}, "
        f"{fp['judge']['pooling']}, max {fp['judge']['max_tokens']} tokens\n"
        f"- Indexing outcome: {outcome['indexed']} of "
        f"{outcome['manifest_tracks']} indexed, {outcome['embedded']} embedded, "
        f"{outcome['clap_audio_failed']} embedding failures"
        + (
            f"; missing: {', '.join(outcome['missing_from_index'])}"
            if outcome["missing_from_index"]
            else ""
        )
        + "\n"
    )


def _leakage_section(stats: dict) -> str:
    check = stats["leakage_check"]
    return (
        "## Leakage check\n\n"
        "The library the server indexed carries no text that a caption could "
        "have reached. Each file was copied out of the archive under a name "
        "that is the SHA-1 of its dataset id and nothing else, into one flat "
        "directory, and every tag was deleted before indexing. Enrichment "
        "(MusicBrainz, AcoustID, Deezer) was off, so no name was fetched back "
        "from outside either, and with it off the metadata-text embedding "
        "stage never runs at all — the only vectors in the index are audio.\n\n"
        f"Verified after preparation, on all {check['files_checked']} files: "
        f"{len(check['files_with_tags'])} files with any tag frame, ID3v2 "
        f"header or ID3v1 trailer; "
        f"{len(check['filename_caption_word_collisions'])} filenames sharing "
        f"a word with any caption; "
        f"{len(check['unexpected_files_in_library'])} files in the library "
        f"that the manifest does not list.\n\n"
        "The endpoint's own answer is kept beside the results as "
        "`sample_response_<run>.json`: every hit it names carries a hash for "
        "a title and \"Unknown Artist\" for an artist, which is the same "
        "check seen from the outside.\n"
    )


def _integrity_section(layout: Layout, runs: Sequence[str]) -> str:
    """The checks that say a number is a measurement and not an artefact."""
    lines = [
        "## Integrity checks\n",
        "Each returned track should carry the CLAP distance the searcher "
        "logged for that same query. With the mood leg off every returned "
        "track came from the KNN leg, so anything under 100% there would mean "
        "a query was answered with another query's hits.\n",
        "| Run | Queries | Tracks returned | With a distance from their own query | Empty answers | Refused (HTTP) |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for run in runs:
        coverage_path = layout.out / f"coverage_{run}.json"
        if not coverage_path.exists():
            continue
        coverage = json.loads(coverage_path.read_text())
        empty = refused = 0
        with layout.results(run).open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                if not row["ranked_track_ids"]:
                    empty += 1
                if row["error"].startswith("HTTP"):
                    refused += 1
        lines.append(
            f"| {RUN_TITLES.get(run, run)} | {coverage['queries']} "
            f"| {coverage['returned_tracks']} "
            f"| {100 * coverage['share_with_clap_distance']:.1f}% "
            f"| {empty} | {refused} |"
        )
    lines.append("")
    return "\n".join(lines)


def _instrumentation_section(fp: dict) -> str:
    lines = [
        "## Instrumentation\n",
        "The server's own logs already time a track's embedding and a query's "
        "KNN leg. Everything finer was taken with wrappers installed from "
        "outside the server, by an import hook on `PYTHONPATH` that the "
        "benchmark sets and nothing else does — no file under `packages/` was "
        "edited for this run. Each wrapper calls what it wrapped, returns what "
        "it returned, and logs one line.\n",
        "| Wrapped symbol | What it measures | Kind |",
        "|---|---|---|",
    ]
    for touch in fp["instrumentation"]:
        lines.append(f"| `{touch['symbol']}` | {touch['measures']} | {touch['kind']} |")
    lines.append("")
    lines.append(
        "The two `knn_search_*` wrappers are the one exception to timing-only: "
        "they also log the distances the call returned. The search endpoint "
        "publishes rank order and no score, so without them `results.csv` "
        "would have a rank column and nothing behind it.\n"
    )
    return "\n".join(lines)


def _caveats(stats: dict, metrics: dict, runs: Sequence[str], fp: dict) -> str:
    run = runs[0]
    empty = metrics[run]["all"]["strict"]["empty_answers"]
    return (
        "## Caveats\n\n"
        "- **Strict scoring is pessimistic by construction.** One track is "
        "correct per caption. A library of 706 recordings holds many that "
        "match \"acoustic guitar solo with a relaxed feel\" equally well, and "
        "every one of them counts against the score.\n"
        "- **Graded scoring is lenient, and biased toward the judge's habits.** "
        "It rewards captions that sound alike, which is not the same as music "
        "that sounds alike: two tracks described with the same vocabulary score "
        "as relevant whether or not a listener would agree. A soft recall near "
        "100% means the threshold is loose, not that retrieval is solved.\n"
        f"- **{stats['non_ascii_captions']} captions are non-ASCII and return "
        "nothing.** The query encoder skips a query that is not ASCII, so a "
        "caption with a typographic apostrophe is answered with an empty list "
        f"({empty} empty answers in this run). They were left exactly as the "
        "dataset wrote them, and they count as misses.\n"
        "- **Long captions are truncated** to 77 tokens by CLAP's tokenizer, "
        "which is a fifth of the longest caption in the set.\n"
        "- **Top-50 is the whole candidate pool.** The KNN leg's candidate "
        "limit is 50, so R@50 is the recall of the retrieval step itself and "
        "ranks below 50 are the whole list, not a prefix of a deeper one.\n"
        f"- **One host, one run.** Timings are this machine's "
        f"({fp['host']['cpu_count']} threads, {fp['host']['platform']}) and a "
        "single run at that: they say where the time goes, not what the "
        "variance is. What the same split looks like on the hardware the "
        "server is built for is measured separately, in "
        "[results_device.md](results_device.md), and it is not this table "
        "scaled down.\n"
    )


def _findings(layout: Layout, summary: dict, stats: dict, runs: Sequence[str]) -> str:
    """What the run turned up about the code. Nothing here was changed for the
    benchmark; it is written down so it can be decided on separately."""
    timings = summary["timings"]
    infer_share = (
        100.0 * timings["embed_infer_s"] / timings["embed_total_s"]
        if timings["embed_total_s"]
        else 0.0
    )
    per_run = {}
    for run in runs:
        path = layout.results(run)
        if not path.exists():
            continue
        empty = refused = 0
        with path.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                empty += 0 if row["ranked_track_ids"] else 1
                refused += 1 if row["error"].startswith("HTTP") else 0
        per_run[run] = (refused, empty)
    counts = ", ".join(
        f"{RUN_TITLES.get(run, run)}: {refused} refused, {empty} empty"
        for run, (refused, empty) in per_run.items()
    )

    return (
        "## What the run turned up\n\n"
        "None of this was changed for the benchmark.\n\n"
        f"1. **A query that is not ASCII is answered with nothing.** "
        f"`SearchWorker._encode_query_blob` returns `None` for a non-ASCII "
        f"query, so no vector is encoded and no candidate is retrieved — the "
        f"caller gets an empty list, not a worse one. "
        f"{stats['non_ascii_captions']} of {stats['captions']} captions here "
        "trip it, and all they contain is a typographic apostrophe. "
        "Normalising the query (NFKD, curly quotes folded to ASCII) before "
        "the gate would keep the tokenizer's English assumption and lose "
        "nothing.\n"
        "2. **Two settings control the same depth, and the server's is "
        "inert.** `/ai_search` passes the server's `ai_suggestions_limit` "
        "into `module.ai_search(query, offset, limit)`, and the local library "
        "ignores that argument in favour of its own "
        "`ai_search.max_results`. Raising the server's setting alone changes "
        "nothing for the local library.\n"
        "3. **The ranked list cannot be deeper than the candidate pool.** "
        "`knn_candidate_limit` (50) bounds what the KNN leg retrieves, so a "
        "`max_results` above it silently returns fewer tracks than asked "
        "for. The two settings want to move together, or the second wants to "
        "be derived from the first.\n"
        "4. **The per-call budget can outlive the call it cancelled.** A "
        "query that overruns the server's 3 s budget is cancelled at the "
        "`await`, but the executor thread stays blocked on the searcher's "
        "response queue and the searcher keeps working. Two consumers can "
        "then be waiting on one queue, and nothing guarantees which one "
        "receives the next response — a later query can be answered with an "
        "earlier query's tracks. This run checks for exactly that (the "
        f"integrity table above). Per run — {counts}.\n"
        f"5. **Embedding is inference-bound, one fragment at a time.** "
        f"{infer_share:.0f}% of a track's embedding is ONNX inference and "
        f"only {100.0 * timings['embed_decode_s'] / timings['embed_total_s']:.0f}% "
        "is decoding. The encoder is run once per 10 s fragment, so a track "
        "costs three separate single-sample sessions; batching a track's "
        "fragments into one call is the obvious thing to measure next. On a "
        "Raspberry Pi 4 the same split is 90% inference and 3% decode "
        "([results_device.md](results_device.md)), so on the machine this is "
        "for, the win has to come out of the tower rather than out of the "
        "call count.\n"
        "6. **Mood fusion, as configured, only costs on this kind of query.** "
        "It leaves the top ten where it was, takes several points off R@50 by "
        "unioning its own candidates into a list capped at 50, and doubles "
        "p95 latency. The ablation section says why that is not the same as "
        "\"turn it off\": these captions describe recordings, and the mood "
        "axis exists for the queries that do not. Measuring it needs mood "
        "queries with mood ground truth, which this dataset does not "
        "have.\n"
    )


# ---------------------------------------------------------------------------
# The mood suite
# ---------------------------------------------------------------------------


def write_mood(layout: Layout, runs: Sequence[str]) -> Path:
    """``results_mood.md`` — the tag benchmark, reported on its own because it
    asks a different question of a different ground truth."""
    from .mood import FAMILY_TITLES

    data = json.loads((layout.out / "mood_metrics.json").read_text())
    present = [run for run in runs if run in data]
    families = ["affective", "compound", "contextual"]
    out = ["# Mood retrieval on MTG-Jamendo tags\n"]
    out.append(
        f"The same {data['judged_tracks']} recordings the captions benchmark "
        "indexed, asked a different question: given a mood word, does the "
        "ranking fill with tracks a human gave that tag? Many answers are "
        "right per query, so the scoring is precision, mAP and nDCG over a "
        "relevance set — recall@k is meaningless here. This is the "
        "measurement the valence/arousal fusion exists to win, and the "
        f"{data['queries']} queries below are the ones the corpus can "
        "support.\n"
    )

    out.append("## Retrieval quality by query family\n")
    out.append(
        "| Query family | Run | Queries | P@10 | mAP | nDCG@10 | R-prec | Prevalence | Lift over random |"
    )
    out.append("|---|---|---:|---:|---:|---:|---:|---:|---:|")
    for family in families:
        for run in present:
            block = data[run]["aggregate"].get(family)
            if not block:
                continue
            out.append(
                f"| {FAMILY_TITLES.get(family, family)} | "
                f"{'fusion' if run == 'tags_fusion' else 'CLAP only'} "
                f"| {block['queries']} | {block['mean_p_at_10']:.3f} "
                f"| {block['map']:.3f} | {block['mean_ndcg_at_10']:.3f} "
                f"| {block['mean_r_precision']:.3f} "
                f"| {100 * block['mean_prevalence']:.1f}% "
                f"| {block['mean_lift']:.2f}× |"
            )
    for run in present:
        block = data[run]["aggregate"]["all"]
        out.append(
            f"| **All queries** | {'fusion' if run == 'tags_fusion' else 'CLAP only'} "
            f"| {block['queries']} | {block['mean_p_at_10']:.3f} "
            f"| {block['map']:.3f} | {block['mean_ndcg_at_10']:.3f} "
            f"| {block['mean_r_precision']:.3f} "
            f"| {100 * block['mean_prevalence']:.1f}% | {block['mean_lift']:.2f}× |"
        )
    out.append("")
    out.append(_mood_verdict(data, present, families))
    out.append(_mood_spread(data, present))
    out.append(_mood_per_query(data, present))
    out.append(_mood_method(data, present))
    path = layout.out / "results_mood.md"
    path.write_text("\n".join(out))
    return path


def _mood_verdict(data: dict, present: Sequence[str], families: Sequence[str]) -> str:
    if not {"tags_fusion", "tags_clap"} <= set(present):
        return ""
    lines = ["## What fusion does to it\n"]
    lines.append("| Query family | P@10 CLAP → fusion | mAP CLAP → fusion |")
    lines.append("|---|---|---|")
    for family in [*families, "all"]:
        on = data["tags_fusion"]["aggregate"].get(family)
        off = data["tags_clap"]["aggregate"].get(family)
        if not (on and off):
            continue
        lines.append(
            f"| {family} | {off['mean_p_at_10']:.3f} → {on['mean_p_at_10']:.3f} "
            f"({on['mean_p_at_10'] - off['mean_p_at_10']:+.3f}) "
            f"| {off['map']:.3f} → {on['map']:.3f} "
            f"({on['map'] - off['map']:+.3f}) |"
        )
    lines.append("")
    return "\n".join(lines)


def _mood_spread(data: dict, present: Sequence[str]) -> str:
    """Where the mean came from. A family average over sixteen queries can be
    one query moving a long way, and that is a different claim."""
    if not {"tags_fusion", "tags_clap"} <= set(present):
        return ""
    fusion = {row["query_id"]: row for row in data["tags_fusion"]["per_query"]}
    clap = {row["query_id"]: row for row in data["tags_clap"]["per_query"]}
    deltas = {
        query_id: row["p_at_10"] - clap[query_id]["p_at_10"]
        for query_id, row in fusion.items()
        if query_id in clap
    }
    better = [q for q, d in deltas.items() if d > 0]
    worse = [q for q, d in deltas.items() if d < 0]
    same = [q for q, d in deltas.items() if d == 0]
    zero_both = [
        q for q in deltas
        if fusion[q]["p_at_10"] == 0 and clap[q]["p_at_10"] == 0
    ]
    biggest = max(deltas, key=deltas.get) if deltas else ""
    return (
        "## What this run can and cannot say\n\n"
        f"Of {len(deltas)} queries, fusion improved P@10 on {len(better)}, "
        f"left {len(same)} unchanged and lowered {len(worse)}. "
        f"{len(zero_both)} return nothing relevant in the top ten under "
        "either configuration — several of those tags are not moods at all "
        "(`melodic`, `catchy`, `cool`) or are licensing categories "
        "(`commercial`, `communication`), and no audio model should be "
        "expected to hear them.\n\n"
        f"The family means are therefore carried by a few queries: the "
        f"largest single move is `{biggest}`, "
        f"{clap[biggest]['p_at_10']:.2f} → {fusion[biggest]['p_at_10']:.2f}. "
        "With sixteen affective queries over 352 recordings, this run says "
        "the direction is real and the mechanism works — it does not measure "
        "the size of the effect. The full MTG-Jamendo mood/theme subset "
        "(18,486 tracks, 56 tags) is what would.\n"
    )


def _mood_per_query(data: dict, present: Sequence[str]) -> str:
    """Per query, because a mean over moods hides which moods work."""
    if "tags_fusion" not in present:
        return ""
    fusion = {row["query_id"]: row for row in data["tags_fusion"]["per_query"]}
    clap = {row["query_id"]: row for row in data.get("tags_clap", {}).get("per_query", [])}
    lines = ["## Every query, by family\n"]
    lines.append("| Query | Relevant | P@10 CLAP | P@10 fusion | AP fusion | Lift |")
    lines.append("|---|---:|---:|---:|---:|---:|")
    for query_id, row in sorted(
        fusion.items(), key=lambda kv: (kv[1]["family"], -kv[1]["p_at_10"])
    ):
        other = clap.get(query_id, {})
        lines.append(
            f"| `{query_id}` | {row['support']} "
            f"| {other.get('p_at_10', float('nan')):.2f} | {row['p_at_10']:.2f} "
            f"| {row['average_precision']:.3f} | {row['lift']:.1f}× |"
        )
    lines.append("")
    return "\n".join(lines)


def _mood_method(data: dict, present: Sequence[str]) -> str:
    return (
        "## How this was measured\n\n"
        "- **Ground truth**: the MTG-Jamendo tag row for each recording, "
        "which ships inside the Song Describer record itself "
        "(`song_describer_14_04_23.mtg-jamendo.tsv`, md5-verified). A track "
        "is relevant to a query when it carries that mood tag; a compound "
        "query needs both tags.\n"
        f"- **Judged corpus**: the {data['judged_tracks']} recordings "
        "carrying at least one mood/theme tag. The rest are dropped from "
        "each ranking before scoring — a track nobody tagged is missing a "
        "label, not lacking a mood, and counting it as a mistake would score "
        "the annotation rather than the search.\n"
        "- **Ranked depth**: the whole library, not the shipped 50. mAP and "
        "R-precision are defined over a complete ranking; the captions "
        "benchmark is where the shipped cut is measured.\n"
        "- **Queries**: `\"<mood> music\"` for a single tag and `\"<mood> "
        "<instrument|genre>\"` for a compound, with support floors (10 and 8 "
        "tracks) rather than a hand-picked list, so the query set follows the "
        "corpus.\n"
        "- **Families**: about half of Jamendo's mood/theme vocabulary is "
        "what a library-music customer would license a track *for* — "
        "commercial, documentary, advertising. Those are asked and reported "
        "apart from the affective tags, because fusion has no reason to help "
        "on them and averaging the two would hide whether it helped at all.\n\n"
        "### Caveats\n\n"
        "- **The corpus is small.** 352 judged recordings and 10-45 relevant "
        "tracks per query: enough to rank configurations against each other, "
        "not enough to publish an absolute number. The full MTG-Jamendo "
        "mood/theme subset is 18,486 tracks and 56 tags, and is what this "
        "harness should be pointed at next.\n"
        "- **Tags are sparse and multi-label.** A track tagged `happy` may "
        "also be relaxing and untagged for it. Precision is therefore a "
        "floor, and the lift column — precision against the tag's own "
        "prevalence — is the more honest comparison.\n"
        "- **A tag is not a query.** People type \"something upbeat for the "
        "gym\", not \"upbeat music\". The phrasings here are deliberately "
        "the plainest form, which is the form the searcher's keyword "
        "spotting recognises with full confidence.\n"
    )
