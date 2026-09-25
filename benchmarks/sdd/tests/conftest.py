import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from sddbench import paths  # noqa: E402


@pytest.fixture(autouse=True)
def _private_cache(tmp_path, monkeypatch):
    """Keep tests off the shared dataset and model cache, which a real run spends hours filling."""
    monkeypatch.setattr(paths, "CACHE_ROOT", tmp_path / "cache")
