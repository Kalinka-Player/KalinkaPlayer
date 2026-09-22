"""Turning an endpoint's answer into a scored row."""

import csv

from sddbench import retrieval

from sddbench.paths import Layout


def test_local_id_strips_the_entity_prefix():
    assert retrieval._local_id("kalinka:localfiles:track:track_abc") == "track_abc"


def test_distances_parse_and_survive_an_empty_dump():
    assert retrieval._distances("a:0.5;b:1.25") == {"a": 0.5, "b": 1.25}
    assert retrieval._distances("-") == {}
    assert retrieval._distances("") == {}


def test_results_round_trip_through_csv(tmp_path):
    layout = Layout(out=tmp_path)
    answers = [
        retrieval.Answer("c1", "t1", ["t1", "t2"], [0.1, 0.2], 12.5),
        retrieval.Answer("c2", "t9", [], [], 9.0, error="boom"),
    ]
    timings = [
        retrieval.QueryTiming("c1", 0.2, 0.1, 0.05, 0.0, 50),
        retrieval.QueryTiming("c2"),
    ]
    retrieval.write_results(answers, timings, layout, "run", 50)
    rows = list(csv.DictReader(layout.results("run").open()))
    assert rows[0]["rank"] == "1"
    assert rows[1]["rank"] == ">50"
    assert rows[0]["clap_distances"] == "0.100000;0.200000"

    read_back = retrieval.read_results(layout, "run")
    assert [a.rank for a in read_back] == [1, retrieval.MISS]
    assert read_back[0].scores == [0.1, 0.2]
    assert read_back[1].error == "boom"


def test_a_miss_is_named_by_the_depth_the_run_asked_for(tmp_path):
    layout = Layout(out=tmp_path)
    retrieval.write_results(
        [retrieval.Answer("c1", "t9", [], [], 1.0)],
        [retrieval.QueryTiming("c1")],
        layout, "run", 20,
    )
    assert list(csv.DictReader(layout.results("run").open()))[0]["rank"] == ">20"


def test_each_query_is_given_its_own_occurrence_of_a_repeated_text():
    """Two queries can carry the same words — a mood tag that is both an
    instrument and a genre — and each must get its own log lines."""
    queries = [
        retrieval.Query("q1", "happy piano"),
        retrieval.Query("q2", "happy piano"),
        retrieval.Query("q3", "never asked"),
    ]
    digest = retrieval._digest("happy piano")
    aligned = list(retrieval._aligned({digest: [{"a": 0.1}, {"b": 0.2}]}, queries))
    assert aligned == [{"a": 0.1}, {"b": 0.2}, {}]
