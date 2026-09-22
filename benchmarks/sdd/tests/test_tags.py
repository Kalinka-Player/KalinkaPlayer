"""Tag ground truth: what counts as relevant, and what counts as judged."""

from sddbench import tags
from sddbench.dataset import Track

TSV = (
    "TRACK_ID\tARTIST_ID\tALBUM_ID\tPATH\tDURATION\tTAGS\n"
    "track_1\ta\tb\t01/1.mp3\t120.0\tmood/theme---happy\tgenre---pop\n"
    "track_2\ta\tb\t02/2.mp3\t120.0\tmood/theme---happy\tinstrument---piano\n"
    "track_3\ta\tb\t03/3.mp3\t120.0\tmood/theme---sad\tinstrument---piano\n"
    "track_4\ta\tb\t04/4.mp3\t120.0\tgenre---rock\n"
)


def _tsv(tmp_path):
    path = tmp_path / tags.TAG_FILE
    path.write_text(TSV)
    return path


def _tracks():
    return {
        str(n): Track(str(n), f"0{n}/{n}.mp3", f"{n}.mp3", 120.0, 1)
        for n in range(1, 5)
    }


def test_tags_are_grouped_by_category(tmp_path):
    rows = tags.load_tags(_tsv(tmp_path))
    assert rows["01/1.mp3"]["mood/theme"] == ["happy"]
    assert rows["01/1.mp3"]["genre"] == ["pop"]
    assert "mood/theme" not in rows["04/4.mp3"]


def test_only_mood_tagged_tracks_are_judged(tmp_path):
    rows = tags.load_tags(_tsv(tmp_path))
    corpus = tags.judged_corpus(_tracks(), rows)
    assert corpus == {"1", "2", "3"}


def test_queries_respect_support_floors(tmp_path):
    rows = tags.load_tags(_tsv(tmp_path))
    tracks = _tracks()

    assert tags.build_queries(tracks, rows, min_support=3, min_compound=3) == []

    built = tags.build_queries(tracks, rows, min_support=2, min_compound=2)
    single = [q for q in built if q.family == "affective"]
    assert [q.text for q in single] == ["happy music"]
    assert single[0].relevant == {"1", "2"}


def test_a_compound_query_needs_both_tags(tmp_path):
    rows = tags.load_tags(_tsv(tmp_path))
    built = tags.build_queries(_tracks(), rows, min_support=2, min_compound=1)
    compound = {q.text: q.relevant for q in built if q.family == "compound"}
    assert compound["happy piano"] == {"2"}
    assert compound["sad piano"] == {"3"}


def test_a_compound_query_id_names_the_category_it_came_from(tmp_path):
    """An instrument and a genre can share a name; two queries with the same
    id would score one of them against the other's relevance set."""
    path = tmp_path / tags.TAG_FILE
    path.write_text(
        "track_1\ta\tb\t01/1.mp3\t120.0\tmood/theme---happy"
        "\tinstrument---jazz\tgenre---jazz\n"
        "track_2\ta\tb\t02/2.mp3\t120.0\tmood/theme---happy\tgenre---jazz\n"
    )
    rows = tags.load_tags(path)
    built = tags.build_queries(_tracks(), rows, min_support=2, min_compound=1)
    compound = [q for q in built if q.family == "compound"]
    assert len({q.query_id for q in compound}) == len(compound)
    by_id = {q.query_id: q for q in compound}
    assert by_id["compound:happy+instrument:jazz"].relevant == {"1"}
    assert by_id["compound:happy+genre:jazz"].relevant == {"1", "2"}


def test_contextual_tags_are_separated_from_affective():
    assert "commercial" in tags.CONTEXTUAL
    assert "documentary" in tags.CONTEXTUAL
    assert "happy" not in tags.CONTEXTUAL
