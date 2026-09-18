#!/usr/bin/env python3
"""Choosing an album's cover out of the images filed beside it.

The names in a real scan set say nothing useful — ``beatles_abbey_1.jpg`` is
the sleeve and ``beatles_abbey_d1.jpg`` is a disc label — and every scan is
square, so neither the name nor the shape decides. What separates them is
physical size: at one scan resolution a 31 cm sleeve is about three times a
10 cm label.
"""

import os

import pytest
from PIL import Image

from kalinka_plugin_localfiles.storage.local import LocalStorage
from kalinka_plugin_localfiles.utils.folder_art import find_folder_cover

LOCAL = LocalStorage()


def _image(path, size=(1000, 1000)):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    Image.new("RGB", size, (90, 120, 160)).save(path)
    return path


def _name(cover):
    return os.path.basename(cover.path) if cover else None


def _box(cover):
    return cover.box if cover else None


class TestWhatIsAdmissible:
    def test_an_empty_folder_offers_nothing(self, tmp_path):
        assert find_folder_cover(LOCAL, str(tmp_path)) is None

    def test_a_missing_folder_is_not_an_error(self, tmp_path):
        assert find_folder_cover(LOCAL, str(tmp_path / "nope")) is None
        assert find_folder_cover(LOCAL, "") is None

    def test_audio_and_text_are_not_covers(self, tmp_path):
        (tmp_path / "01.flac").write_bytes(b"not audio really")
        (tmp_path / "notes.txt").write_text("hello")
        assert find_folder_cover(LOCAL, str(tmp_path)) is None

    def test_an_archival_tiff_is_never_chosen(self, tmp_path):
        """A sleeve TIFF runs to hundreds of megabytes; a player has no use
        for one and decoding it on a Pi is not worth the cover."""
        _image(str(tmp_path / "sleeve_front.tif"), (4000, 4000))
        assert find_folder_cover(LOCAL, str(tmp_path)) is None

    def test_a_tiff_does_not_displace_a_jpeg(self, tmp_path):
        _image(str(tmp_path / "scan_1.tif"), (4000, 4000))
        _image(str(tmp_path / "scan_2.jpg"), (900, 900))
        assert _name(find_folder_cover(LOCAL, str(tmp_path))) == "scan_2.jpg"

    def test_an_oversized_file_is_skipped_without_being_opened(
        self, tmp_path, monkeypatch
    ):
        import kalinka_plugin_localfiles.utils.folder_art as folder_art

        _image(str(tmp_path / "cover.jpg"))
        monkeypatch.setattr(folder_art, "_MAX_BYTES", 10)
        monkeypatch.setattr(
            folder_art,
            "_measure",
            lambda _storage, _path: pytest.fail("file was opened"),
        )
        assert find_folder_cover(LOCAL, str(tmp_path)) is None

    def test_an_oversized_image_is_skipped_from_its_header(
        self, tmp_path, monkeypatch
    ):
        """A byte cap does not bound decode cost, so the pixel count is
        checked too — from the header, before any pixel is read."""
        import kalinka_plugin_localfiles.utils.folder_art as folder_art

        _image(str(tmp_path / "cover.jpg"), (1200, 1200))
        monkeypatch.setattr(folder_art, "_MAX_PIXELS", 1000)
        assert find_folder_cover(LOCAL, str(tmp_path)) is None

    def test_a_gif_is_admissible(self, tmp_path):
        """Rare, but sometimes the only art a rip shipped."""
        _image(str(tmp_path / "pic.gif"), (400, 400))
        assert _name(find_folder_cover(LOCAL, str(tmp_path))) == "pic.gif"

    def test_the_header_decides_the_format_not_the_name(self, tmp_path):
        """Rips misname things: a real library had a JPEG called "pic.gif"."""
        path = str(tmp_path / "pic.gif")
        Image.new("RGB", (400, 400), (10, 20, 30)).save(path, "JPEG")
        assert _name(find_folder_cover(LOCAL, str(tmp_path))) == "pic.gif"

    def test_a_corrupt_image_is_ignored(self, tmp_path):
        (tmp_path / "cover.jpg").write_bytes(b"\xff\xd8 not really a jpeg")
        _image(str(tmp_path / "art_1.jpg"))
        assert _name(find_folder_cover(LOCAL, str(tmp_path))) == "art_1.jpg"


class TestTheNameWhenItSaysSomething:
    @pytest.mark.parametrize(
        "name", ["cover.jpg", "front.png", "folder.jpg", "AlbumArt.jpg", "Sleeve.jpg"]
    )
    def test_a_name_that_claims_to_be_the_cover_wins(self, tmp_path, name):
        _image(str(tmp_path / "scan_1.jpg"), (2000, 2000))
        _image(str(tmp_path / name), (600, 600))
        assert _name(find_folder_cover(LOCAL, str(tmp_path))) == name

    @pytest.mark.parametrize(
        "name", ["back.jpg", "cd1.jpg", "booklet.jpg", "disc.jpg", "spine.jpg"]
    )
    def test_a_name_that_disclaims_it_is_dropped(self, tmp_path, name):
        _image(str(tmp_path / name))
        assert find_folder_cover(LOCAL, str(tmp_path)) is None

    def test_the_largest_of_several_cover_names_wins(self, tmp_path):
        _image(str(tmp_path / "cover_small.jpg"), (300, 300))
        _image(str(tmp_path / "cover_big.jpg"), (1500, 1500))
        assert _name(find_folder_cover(LOCAL, str(tmp_path))) == "cover_big.jpg"


class TestWhenTheNamesAreNoHelp:
    def test_a_disc_label_loses_to_the_sleeve_on_physical_size(self, tmp_path):
        """The Abbey Road case: both square, both unhelpfully named, and the
        label is a third of the sleeve because that is how big it really is."""
        _image(str(tmp_path / "abbey_1.jpg"), (7439, 7405))
        _image(str(tmp_path / "abbey_d1.jpg"), (2384, 2387))
        assert _name(find_folder_cover(LOCAL, str(tmp_path))) == "abbey_1.jpg"

    def test_the_lower_ordinal_breaks_a_front_and_back_tie(self, tmp_path):
        _image(str(tmp_path / "scan_2.jpg"), (2000, 2000))
        _image(str(tmp_path / "scan_1.jpg"), (2000, 2000))
        assert _name(find_folder_cover(LOCAL, str(tmp_path))) == "scan_1.jpg"

    def test_a_spine_is_not_a_cover(self, tmp_path):
        _image(str(tmp_path / "img_1.jpg"), (2400, 200))
        assert find_folder_cover(LOCAL, str(tmp_path)) is None

    def test_a_scan_border_does_not_disqualify_a_cover(self, tmp_path):
        _image(str(tmp_path / "img_1.jpg"), (1000, 1180))
        assert _name(find_folder_cover(LOCAL, str(tmp_path))) == "img_1.jpg"

    def test_the_choice_does_not_depend_on_listing_order(self, tmp_path):
        for name in ("c.jpg", "a.jpg", "b.jpg"):
            _image(str(tmp_path / name), (1200, 1200))
        assert _name(find_folder_cover(LOCAL, str(tmp_path))) == "a.jpg"


class TestSubdirectories:
    def test_a_scan_folder_one_level_down_is_searched(self, tmp_path):
        _image(str(tmp_path / "PIC" / "abbey_1.jpg"), (3000, 3000))
        assert _name(find_folder_cover(LOCAL, str(tmp_path))) == "abbey_1.jpg"

    def test_two_levels_down_is_too_far(self, tmp_path):
        _image(str(tmp_path / "extras" / "scans" / "front.jpg"))
        assert find_folder_cover(LOCAL, str(tmp_path)) is None


class TestAFoldedInlay:
    """A CD inlay is often scanned unfolded in one pass: back panel on the
    left, front on the right. Used whole it shows the back beside the front,
    squashed; the front is the right half. Real rips measure 1.57 to 1.86
    rather than a clean 2:1, because the back tray is shorter than the front
    is wide.
    """

    @pytest.mark.parametrize("size", [(3110, 1692), (3110, 1977), (3071, 1654)])
    def test_a_two_panel_scan_keeps_only_its_right_half(self, tmp_path, size):
        _image(str(tmp_path / "cover.jpg"), size)
        assert _box(find_folder_cover(LOCAL, str(tmp_path))) == (0.5, 0.0, 1.0, 1.0)

    @pytest.mark.parametrize("size", [(1420, 1420), (2047, 1657), (2094, 1657)])
    def test_a_single_panel_is_used_whole(self, tmp_path, size):
        """A square cover, a tray inlay and a back are all one panel; the
        widest of them is 1.26, comfortably clear of the fold band."""
        _image(str(tmp_path / "cover.jpg"), size)
        assert _box(find_folder_cover(LOCAL, str(tmp_path))) is None

    def test_an_unnamed_fold_is_still_admissible(self, tmp_path):
        """The shape filter must not drop a fold for being too wide before
        the fold rule ever sees it."""
        _image(str(tmp_path / "img_1.jpg"), (3110, 1692))
        cover = find_folder_cover(LOCAL, str(tmp_path))
        assert _name(cover) == "img_1.jpg"
        assert cover.box == (0.5, 0.0, 1.0, 1.0)

    def test_a_panorama_is_still_not_a_cover(self, tmp_path):
        """Widening the band for folds must not let a spine back in."""
        _image(str(tmp_path / "img_1.jpg"), (2400, 200))
        assert find_folder_cover(LOCAL, str(tmp_path)) is None

    def test_a_dedicated_front_beats_a_fold_it_would_be_cut_from(self, tmp_path):
        """Zero-project's Darkness Falls ships both; the one that is already
        the front needs no guess about where to cut."""
        _image(str(tmp_path / "full cover.jpg"), (3071, 1654))
        _image(str(tmp_path / "front cover.jpg"), (1417, 1417))

        cover = find_folder_cover(LOCAL, str(tmp_path))

        assert _name(cover) == "front cover.jpg"
        assert cover.box is None


class TestASubfolderThatIsAnotherAlbum:
    """A loose track's folder is whatever it happens to sit in. Where that is
    a dump shared with whole album folders, descending into them hands the
    track a stranger's sleeve — a single Bach file was given The Piano Guys'
    cover because theirs was the biggest scan in the neighbourhood.
    """

    def _loose_track_among_albums(self, tmp_path):
        (tmp_path / "bach.flac").write_bytes(b"audio")
        neighbour = tmp_path / "The Piano Guys"
        neighbour.mkdir()
        (neighbour / "01.flac").write_bytes(b"audio")
        _image(str(neighbour / "sleeve.jpg"), (3000, 3000))

    def test_an_album_folders_art_is_not_taken_by_its_neighbour(self, tmp_path):
        self._loose_track_among_albums(tmp_path)
        assert find_folder_cover(LOCAL, str(tmp_path)) is None

    def test_a_cover_beside_the_loose_track_is_still_used(self, tmp_path):
        """Only the neighbour is out of bounds, not the folder itself."""
        self._loose_track_among_albums(tmp_path)
        _image(str(tmp_path / "cover.jpg"), (600, 600))

        assert _name(find_folder_cover(LOCAL, str(tmp_path))) == "cover.jpg"

    def test_a_scan_folder_is_still_searched(self, tmp_path):
        """The distinction is music, not depth: PIC/ holds no audio."""
        (tmp_path / "01.flac").write_bytes(b"audio")
        _image(str(tmp_path / "PIC" / "abbey_1.jpg"), (3000, 3000))

        assert _name(find_folder_cover(LOCAL, str(tmp_path))) == "abbey_1.jpg"

    @pytest.mark.parametrize("audio", ["01.dsf", "01.ape", "01.m4a", "01.wv"])
    def test_music_this_library_cannot_play_still_marks_a_folder(
        self, tmp_path, audio
    ):
        """A folder is somebody's album whether or not we can read it."""
        other = tmp_path / "Another Album"
        other.mkdir()
        (other / audio).write_bytes(b"audio")
        _image(str(other / "front.jpg"), (3000, 3000))

        assert find_folder_cover(LOCAL, str(tmp_path)) is None

    def test_an_artwork_folder_kept_as_a_link_is_searched(self, tmp_path):
        """A listing reports a link to a directory as a file, because a scan
        must not descend through one. Artwork is the other question: exactly
        one level down is looked at, so following a link cannot loop, and a
        ``Scans`` folder kept elsewhere on the disk is an ordinary way to
        file them."""
        album = tmp_path / "album"
        album.mkdir()
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        _image(str(elsewhere / "front.jpg"), (900, 900))
        (album / "Scans").symlink_to(elsewhere)

        assert _name(find_folder_cover(LOCAL, str(album))) == "front.jpg"

    def test_an_unreadable_subfolder_does_not_break_the_search(
        self, tmp_path, monkeypatch
    ):
        """One directory refusing to be listed must not cost the album the
        cover sitting beside its own audio."""
        _image(str(tmp_path / "cover.jpg"), (600, 600))
        (tmp_path / "locked").mkdir()
        real_listdir = LocalStorage.listdir

        def listdir(self, path):
            if str(path).endswith("locked"):
                raise OSError("permission denied")
            return real_listdir(self, path)

        monkeypatch.setattr(LocalStorage, "listdir", listdir)

        assert _name(find_folder_cover(LOCAL, str(tmp_path))) == "cover.jpg"
