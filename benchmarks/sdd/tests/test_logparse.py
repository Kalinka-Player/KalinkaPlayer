"""Reading timings back out of the server's log."""

import pytest

from sddbench import logparse

LOG = """2026-09-20 18:41:02.123 INFO 140 bench: BENCH embed_track t=0.8120 decode=0.2000 infer=0.5000 other=0.1120 ok=1
2026-09-20 18:41:03.000 INFO 140 embedder: CLAP audio embedding: 0.812s
not a log line at all
2026-09-20 18:41:04.500 INFO 141 bench: BENCH knn_audio t=0.0030 hits=2 dist=track_a:0.100000;track_b:0.200000
2026-09-20 18:41:04.900 INFO 141 bench: BENCH query_total t=0.1000 q=abc123def456 n=50
"""


def test_entries_skip_unparseable_lines(tmp_path):
    path = tmp_path / "server.log"
    path.write_text(LOG)
    entries = list(logparse.entries(path))
    assert len(entries) == 4
    assert entries[1].message.startswith("CLAP audio embedding")


def test_bench_lines_carry_named_fields(tmp_path):
    path = tmp_path / "server.log"
    path.write_text(LOG)
    embed = logparse.bench(path, "embed_track")[0]
    assert embed.number("t") == 0.8120
    assert embed.number("decode") + embed.number("infer") + embed.number("other") == pytest.approx(0.8120)
    knn = logparse.bench(path, "knn_audio")[0]
    assert knn.fields["dist"] == "track_a:0.100000;track_b:0.200000"
    assert logparse.bench(path, "query_total")[0].fields["q"] == "abc123def456"


def test_first_and_last_find_a_message(tmp_path):
    path = tmp_path / "server.log"
    path.write_text(LOG)
    assert logparse.first(path, "CLAP audio embedding") is not None
    assert logparse.last(path, "nothing here") is None


def test_by_event_groups_in_one_pass(tmp_path):
    path = tmp_path / "server.log"
    path.write_text(LOG)
    grouped = logparse.by_event(path)
    assert set(grouped) == {"embed_track", "knn_audio", "query_total"}
    assert len(grouped["embed_track"]) == 1
