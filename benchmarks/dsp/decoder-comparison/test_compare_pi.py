import csv
import statistics
import tempfile
import unittest
from pathlib import Path

from compare_pi import read_timings


class PiTimingTest(unittest.TestCase):
    def test_published_trials_and_invalid_readings(self):
        results = Path(__file__).parent / "pi-results"
        with (results / "cpu-runs.csv").open() as stream:
            trials = list(csv.DictReader(stream))
        with (results / "comparison.csv").open() as stream:
            summaries = list(csv.DictReader(stream))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cpu-runs.csv"

            def write(rows):
                with path.open("w", newline="") as stream:
                    writer = csv.DictWriter(stream, fieldnames=rows[0].keys())
                    writer.writeheader()
                    writer.writerows(rows)

            for board in sorted({row["board"] for row in trials}):
                rows = [row for row in trials if row["board"] == board]
                write(rows)
                timings = read_timings(path)
                for summary in (row for row in summaries if row["board"] == board):
                    self.assertAlmostEqual(
                        statistics.median(timings[summary["converter"]]),
                        float(summary["median_percent_one_core"]),
                    )

            for field in ("cpu_seconds", "wall_seconds", "percent_one_core"):
                for bad in ("nan", "inf", "-1", "0"):
                    with self.subTest(field=field, value=bad):
                        write([{**rows[0], field: bad}, *rows[1:]])
                        with self.assertRaisesRegex(ValueError, "invalid CPU timing"):
                            read_timings(path)
