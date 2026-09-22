"""The judge's similarity matrix, and the precision a run scores it at."""

from sddbench import judge


def test_a_row_is_addressed_by_caption_id(tmp_path):
    path = tmp_path / "similarity.csv"
    judge.write_matrix([[1.0, 0.25], [0.25, 1.0]], ["c1", "c2"], path)
    similarity = judge.read_matrix(path)
    assert similarity["c1"] == {"c1": 1.0, "c2": 0.25}
    assert similarity.get("absent") is None
    assert len(similarity) == 2


def test_a_computed_matrix_is_scored_at_the_precision_it_was_cached_at(
    tmp_path, monkeypatch
):
    """A resumed run reads four decimals back out of the cache. The run that
    computed the matrix has to score those same numbers, or the two runs
    disagree about every threshold they cross."""
    monkeypatch.setattr(judge, "Judge", lambda model_dir: object())
    monkeypatch.setattr(
        judge, "similarity_matrix",
        lambda judging, texts: [[1.0, 0.123456], [0.123456, 1.0]],
    )
    cache = tmp_path / "similarity.csv"
    fresh = judge.similarity(tmp_path / "model", ["c1", "c2"], ["one", "two"], cache)
    assert fresh["c1"]["c2"] == judge.read_matrix(cache)["c1"]["c2"]
    assert abs(fresh["c1"]["c2"] - 0.1235) < 1e-6


def test_an_existing_cache_is_never_recomputed(tmp_path, monkeypatch):
    cache = tmp_path / "similarity.csv"
    judge.write_matrix([[1.0]], ["c1"], cache)
    monkeypatch.setattr(judge, "Judge", _refuse)
    assert judge.similarity(tmp_path / "model", ["c1"], ["one"], cache)["c1"] == {
        "c1": 1.0
    }


def _refuse(model_dir):
    raise AssertionError("the cached matrix was thrown away")
