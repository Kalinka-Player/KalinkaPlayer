"""make help lists the Makefile's targets: none that is missing, none left out.

It once offered two targets that had gone with the native extension and
omitted the renderer, proto and supervisor ones.
"""

import functools
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
MAKEFILE = REPO / "Makefile"
MAKE = shutil.which("make")
# Phony targets meant only as another target's step, so help leaves them out.
INTERNAL = frozenset({"require-venv"})

pytestmark = pytest.mark.skipif(MAKE is None, reason="needs make")


def _make(*args):
    return subprocess.run(
        [MAKE, *args], cwd=REPO, capture_output=True, encoding="utf-8", timeout=60
    )


@functools.cache
def _help_targets():
    result = _make("help")
    assert result.returncode == 0, result.stderr
    return frozenset(re.findall(r"^  (\S+)\s", result.stdout, re.M))


def _makefile_names(pattern):
    text = MAKEFILE.read_text().replace("\\\n", " ")
    return {
        name for names in re.findall(pattern, text, re.M) for name in names.split()
    }


def _phony_targets():
    return _makefile_names(r"^\.PHONY:(.*)$")


def _rule_targets():
    return _makefile_names(r"^([^\s#=:][^#=:\n]*?)\s*::?(?![:=])")


def test_every_target_help_lists_exists():
    # make -n succeeds on a .PHONY name that has no rule at all.
    assert _help_targets() - _rule_targets() == set()
    result = _make("-n", "-k", *sorted(_help_targets()))
    assert result.returncode == 0, result.stderr


def test_every_target_help_lists_is_phony():
    assert _help_targets() - _phony_targets() == set()


def test_help_lists_every_phony_target_but_the_internal_ones():
    phony = _phony_targets()
    listed = _help_targets()
    assert phony - INTERNAL - listed == set()
    assert INTERNAL - (phony - listed) == set(), "stale or listed INTERNAL entry"
