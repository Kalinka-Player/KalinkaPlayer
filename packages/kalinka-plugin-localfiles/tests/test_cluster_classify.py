#!/usr/bin/env python3
"""Tests for the shared V/A / disc-suffix classification helpers (Phase 1 1d)."""

import pytest

from kalinka_plugin_localfiles.clustering.classify import (
    compilation_title,
    is_va_folder,
    strip_disc_suffix,
)


def test_is_va_folder_thresholds():
    assert is_va_folder(4, 6)          # >=4 artists, ratio 0.67
    assert not is_va_folder(3, 6)      # too few distinct artists
    assert not is_va_folder(4, 12)     # ratio 0.33 < 0.5
    assert not is_va_folder(0, 0)


def test_compilation_title_generic_dump_rejected():
    assert compilation_title("/m/90s Mixes") is None
    assert compilation_title("/m/music") is None
    assert compilation_title("/m/VA - Trance Mixes") == "Trance Mixes"
    assert compilation_title("/m/Now That's Music 50") == "Now That's Music 50"


def test_compilation_title_bare_disc_borrows_parent():
    assert compilation_title("/m/Ministry of Sound/CD1") == "Ministry of Sound CD1"


def test_strip_disc_suffix():
    assert strip_disc_suffix("Album (Disc 1)") == "Album"
    assert strip_disc_suffix("Album CD2") == "Album"
    assert strip_disc_suffix("Album - Vol. 3") == "Album"
    assert strip_disc_suffix("Album [Disk 2]") == "Album"
    assert strip_disc_suffix("Greatest Hits") == "Greatest Hits"  # no suffix
    # A title that is only a disc marker stays intact (nothing left otherwise).
    assert strip_disc_suffix("CD1") == "CD1"


@pytest.mark.parametrize(
    "suffix",
    ["CD I", "CD II", "(Disc III)", "[Disk IV]", "- Vol. IX",
     "Volume XII", "CDXL", "cd xcix"],
)
def test_roman_disc_suffixes(suffix):
    assert strip_disc_suffix(f"The Best of Vangelis {suffix}") == "The Best of Vangelis"


@pytest.mark.parametrize(
    "title",
    ["Album CD Mix", "Album Disc Live", "Album CD IIII", "Album CD IIX",
     "Album CD", "Album II", "CD II"],
)
def test_words_invalid_numerals_and_unmarked_titles_are_preserved(title):
    assert strip_disc_suffix(title) == title
