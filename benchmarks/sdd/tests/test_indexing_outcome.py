"""Every manifest track accounted for — and accounted for under the heading
that says what actually happened to it."""

import csv

from sddbench import indexing
from sddbench.paths import Layout


class _Instance:
    """Only what `_outcome` asks of a server: what it indexed, and which of
    those rows carry a CLAP vector."""

    def __init__(self, indexed: list[str], embedded: list[str]):
        self._indexed = indexed
        self._embedded = embedded

    def tracks(self) -> list[str]:
        return [f"/library/{name}" for name in self._indexed]

    def rows(self, query: str) -> list[dict]:
        return [{"file_path": f"/library/{name}"} for name in self._embedded]


def _manifest(tmp_path, names: list[str]) -> Layout:
    layout = Layout(out=tmp_path)
    with layout.manifest.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow([
            "track_id", "file", "duration_s", "bytes", "n_captions",
            "source_path", "kalinka_track_id",
        ])
        for n, name in enumerate(names):
            writer.writerow([f"t{n}", name, 120.0, 100, 2, f"0{n}/{name}", f"k{n}"])
    return layout


def test_a_track_the_scan_never_saw_is_not_reported_as_an_embedding_failure(tmp_path):
    """The two lists point at different repairs — a scan that missed a file
    and an embedder that choked on one — so a file may only be in one."""
    layout = _manifest(tmp_path, ["a.mp3", "b.mp3", "c.mp3"])
    outcome = indexing._outcome(
        _Instance(indexed=["a.mp3", "b.mp3"], embedded=["a.mp3"]), layout, {}
    )
    assert outcome["missing_from_index"] == ["c.mp3"]
    assert outcome["indexed_but_not_embedded"] == ["b.mp3"]
    assert outcome["indexed"] == 2
    assert outcome["embedded"] == 1
