"""The report is written after everything expensive has already happened, so
it renders from whatever a run actually left behind — including a run that
was interrupted, resumed, or asked for a different depth."""

import csv
import json

import pytest

from sddbench import report
from sddbench.clock import Clock
from sddbench.paths import Layout

RUNS = ["mood_on", "mood_off"]


def _strict(depth: int, recall: dict[str, float]) -> dict:
    return {
        "queries": 6, "depth": depth, "recall_at": recall, "mrr": 0.3,
        "median_rank": 4.0, "median_rank_of_found": 2.0, "found_in_list": 5,
        "empty_answers": 1,
    }


def _metrics(depth: int, recall: dict[str, float]) -> dict:
    graded = {
        "queries": 6, "ndcg_at_10": 0.41,
        "soft_recall_at_10": {"0.5": 0.9, "0.6": 0.6, "0.7": 0.2},
        "mean_top1_relevance": 0.55,
    }
    scope = {"strict": _strict(depth, recall), "graded": graded}
    return {
        "all": scope,
        "valid_subset": scope,
        "latency_ms": {"median": 30.0, "p95": 80.0, "max": 120.0, "mean": 40.0},
    }


def _seed(tmp_path, runs=RUNS, depth=50, embedded_seconds=120.0) -> Layout:
    layout = Layout(out=tmp_path).ensure()
    recall = {str(k): 0.1 * n for n, k in enumerate(report.metrics_mod.recall_ks(depth), 1)}
    layout.dataset_stats.write_text(json.dumps({
        "captions": 6, "tracks": 3, "audio_files": 3,
        "captions_per_track": {"2": 3}, "valid_subset_captions": 6,
        "valid_subset_tracks": 3, "caption_words_min": 3,
        "caption_words_max": 30, "caption_words_median": 12.0,
        "non_ascii_captions": 1, "audio_duration_s_total": 360.0,
        "audio_duration_s_median": 120.0,
        "leakage_check": {
            "files_checked": 3, "files_with_tags": [],
            "filename_caption_word_collisions": [],
            "unexpected_files_in_library": [],
        },
    }))
    layout.index_summary.write_text(json.dumps({
        "spans": {"server_start": 5.0, "scan": 3.0, "embedding": 200.0,
                  "mood_backfill": 0.2, "indexing_total": 208.0},
        "stage_status": {},
        "outcome": {"manifest_tracks": 3, "indexed": 3, "embedded": 3,
                    "missing_from_index": [], "indexed_but_not_embedded": [],
                    "clap_audio_failed": 0},
        "timings": {
            "embedded_tracks": 3, "embed_total_s": embedded_seconds,
            "embed_decode_s": embedded_seconds / 10,
            "embed_infer_s": embedded_seconds * 0.8,
            "embed_other_s": 1.0, "embed_median_s": 40.0,
            "va_head_s": 0.13, "scan_listing_s": 0.1,
            "scan_index_files_s": 1.0, "scan_cleanup_s": 0.1,
            "model_load_audio_s": 2.0, "model_load_text_s": 1.0,
            "db_write_embedding_s": 0.2, "db_claim_batch_s": 0.1,
            "db_schedule_jobs_s": 0.1, "db_write_mood_s": 0.1,
            "va_backfill_s": 0.2,
        },
    }))
    layout.fingerprint.write_text(json.dumps({
        "code": {"commit": "0" * 40, "branch": "bench", "dirty": False,
                 "describe": "v1.2.3"},
        "host": {"python": "3.14.0", "platform": "Linux", "cpu_count": 4},
        "packages": {"numpy": "2.0.0", "soxr": "absent"},
        "models": {
            "release": "clap-onnx-v2", "clap_model_version": 2,
            "clap_embed_format_version": 1, "int8_cap": 0.25,
            "va_head_version": 1, "sample_rate": 48000,
            "fragment_seconds": 10, "text_token_limit": 77,
            "files": {"clap_audio_encoder.onnx": {"bytes": 1e6, "sha256": "a" * 64}},
        },
        "server_config": {"schema_version": "abc", "values": {
            "input_modules.localfiles.ai_search.max_results": 50,
            "input_modules.localfiles.ai_search.knn_candidate_limit": 50,
            "input_modules.localfiles.ai_search.mood.enabled": True,
        }},
        "judge": {"repo": "sentence-transformers/all-mpnet-base-v2",
                  "revision": "e" * 40, "runtime": "onnxruntime",
                  "pooling": "mean", "max_tokens": 384},
        "instrumentation": [{"symbol": "searcher._do_search",
                             "measures": "whole-query time", "kind": "timing"}],
    }))
    with layout.captions_csv.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["caption_id", "track_id", "caption", "is_valid_subset"])
        for n in range(6):
            writer.writerow([f"c{n}", f"t{n % 3}", f"caption number {n}", "True"])

    for run in runs:
        with layout.results(run).open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow([
                "caption_id", "target_track_id", "rank", "n_results",
                "latency_ms", "server_total_s", "encode_s", "knn_s",
                "mood_map_s", "ranked_track_ids", "clap_distances", "error",
            ])
            for n in range(6):
                writer.writerow([
                    f"c{n}", f"t{n % 3}", 1 + n, 3, 30.0, 0.02, 0.01, 0.005,
                    0.001, "t0;t1;t2", "0.1;0.2;0.3", "",
                ])
        layout.metrics(run).write_text(json.dumps(_metrics(depth, recall)))
        (layout.out / f"coverage_{run}.json").write_text(json.dumps({
            "queries": 6, "queries_with_distances": 6, "returned_tracks": 18,
            "share_with_clap_distance": 1.0,
        }))
    return layout


def test_the_report_renders_from_a_finished_run(tmp_path):
    layout = _seed(tmp_path)
    text = report.write(layout, Clock(tmp_path / "clock.json"), RUNS).read_text()
    assert "## Quality" in text
    assert "| R@1 | R@5 | R@10 | R@50 |" in text
    assert "Random ranking of 3 tracks" in text
    assert "A library of 3 recordings" in text
    assert "Top-50 is the whole candidate pool" in text
    assert "0.13 s of it, for all 3 tracks" in text


def test_a_run_that_never_finished_is_left_out_rather_than_crashing(tmp_path):
    layout = _seed(tmp_path, runs=["mood_on"])
    text = report.write(layout, Clock(tmp_path / "clock.json"), RUNS).read_text()
    assert "CLAP + valence/arousal fusion" in text
    assert "Ablation" not in text


def test_a_report_with_nothing_scored_says_so(tmp_path):
    layout = _seed(tmp_path, runs=[])
    with pytest.raises(FileNotFoundError, match="score stage"):
        report.write(layout, Clock(tmp_path / "clock.json"), RUNS)


def test_a_resumed_run_that_embedded_nothing_still_reports(tmp_path):
    """The index stage is the one worth skipping, and skipping it leaves
    every embedding total at zero."""
    layout = _seed(tmp_path, embedded_seconds=0.0)
    text = report.write(layout, Clock(tmp_path / "clock.json"), RUNS).read_text()
    assert "—% of a track's embedding is ONNX inference" in text


def test_a_shallower_run_is_labelled_at_its_own_depth(tmp_path):
    layout = _seed(tmp_path, depth=20)
    text = report.write(layout, Clock(tmp_path / "clock.json"), RUNS).read_text()
    assert "| R@1 | R@5 | R@10 | R@20 |" in text
    assert "R@50" not in text
    assert "> 20 |" in text


def test_the_mood_report_carries_this_run_s_own_corpus(tmp_path):
    layout = Layout(out=tmp_path)
    per_query = [
        {"query_id": "affective:happy", "family": "affective", "support": 11,
         "judged": 30, "p_at_10": 0.2, "average_precision": 0.1,
         "ndcg_at_10": 0.3, "r_precision": 0.2, "prevalence": 0.08,
         "lift": 2.5},
        {"query_id": "compound:happy+genre:jazz", "family": "compound",
         "support": 40, "judged": 30, "p_at_10": 0.1,
         "average_precision": 0.05, "ndcg_at_10": 0.1, "r_precision": 0.1,
         "prevalence": 0.05, "lift": 2.0},
    ]

    def block(p_at_10: float) -> dict:
        summary = {"queries": 1, "mean_p_at_10": p_at_10, "map": 0.1,
                   "mean_ndcg_at_10": 0.2, "mean_r_precision": 0.1,
                   "mean_prevalence": 0.06, "mean_lift": 2.0}
        return {"all": summary, "affective": summary, "compound": summary}

    (layout.out / "mood_metrics.json").write_text(json.dumps({
        "judged_tracks": 57, "queries": 2,
        "tags_fusion": {"aggregate": block(0.2), "per_query": per_query},
        "tags_clap": {"aggregate": block(0.1),
                      "per_query": [dict(row, p_at_10=0.1) for row in per_query]},
    }))
    text = report.write_mood(layout, ["tags_fusion", "tags_clap"]).read_text()
    assert "over 57 recordings" in text
    assert "57 judged recordings and 11-40 relevant tracks per query" in text
    assert "1 affective query over 57 recordings" in text


def test_the_timeline_counts_each_ablation_restart(tmp_path):
    """A restart is timed per run, because a run list can ask for more than
    one, and the end-to-end total has to include them all."""
    layout = _seed(tmp_path)
    (tmp_path / "clock.json").write_text(json.dumps({
        "dataset_prep": 10.0, "indexing_end_to_end": 200.0,
        "retrieval_mood_on": 30.0, "retrieval_mood_off": 30.0,
        "ablation_mood_off": 12.0, "ablation_mood_on": 8.0,
        "judge_embedding": 5.0, "metrics": 1.0,
    }))
    report.write(layout, Clock(tmp_path / "clock.json"), RUNS)
    timeline = {
        row["stage"]: float(row["seconds"])
        for row in csv.DictReader(layout.timeline.open())
    }
    assert timeline["Ablation settings change and restart"] == 20.0
