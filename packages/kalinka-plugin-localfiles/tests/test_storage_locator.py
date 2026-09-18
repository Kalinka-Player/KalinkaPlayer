#!/usr/bin/env python3
"""Reading the location of a music folder.

A share is named the way a file manager names it — ``smb://192.168.1.1/music``
— and that string becomes the prefix of every indexed path under it, so its
canonical form has to be stable and has to survive being taken apart by
``os.path``. What must never happen is the location being normalised: that
collapses the ``//`` after the scheme and quietly turns the share into a
directory name.
"""

import os

import pytest

from kalinka_plugin_localfiles.storage.locator import (
    FILE_SCHEME,
    SMB_SCHEME,
    LocatorError,
    is_within,
    media_type_of,
    parse,
    root_of,
    scheme_of,
)


class TestWhatAShareUrlMeans:
    def test_an_address_and_a_share(self):
        locator = parse("smb://192.168.1.1/music")
        assert locator.scheme == SMB_SCHEME
        assert locator.host == "192.168.1.1"
        assert locator.share == "music"
        assert locator.share_path == ""
        assert str(locator) == "smb://192.168.1.1/music"

    def test_a_name_instead_of_an_address(self):
        assert str(parse("smb://nas/music")) == "smb://nas/music"

    def test_a_folder_inside_the_share(self):
        locator = parse("smb://nas/music/FLAC/Roxy Music")
        assert locator.share == "music"
        assert locator.share_path == "FLAC/Roxy Music"

    def test_cifs_is_the_same_protocol_under_its_older_name(self):
        assert str(parse("cifs://nas/music")) == "smb://nas/music"
        assert scheme_of("cifs://nas/music") == SMB_SCHEME

    def test_the_host_is_matched_without_regard_to_case(self):
        assert str(parse("smb://NAS.local/Music")) == "smb://nas.local/Music"

    def test_a_trailing_slash_does_not_make_a_second_folder(self):
        assert str(parse("smb://nas/music/")) == "smb://nas/music"

    def test_a_port_is_kept_only_when_it_is_not_the_default(self):
        assert str(parse("smb://nas:4450/music")) == "smb://nas:4450/music"
        assert parse("smb://nas:4450/music").port == 4450
        assert str(parse("smb://nas:445/music")) == "smb://nas/music"
        assert parse("smb://nas:445/music").port is None

    def test_an_ipv6_address_keeps_its_brackets(self):
        locator = parse("smb://[fe80::1]/music")
        assert locator.host == "[fe80::1]"
        assert locator.port is None
        assert str(locator) == "smb://[fe80::1]/music"

    def test_a_user_may_be_named_in_the_url(self):
        locator = parse("smb://media@nas/music")
        assert locator.username == "media"
        assert str(locator) == "smb://media@nas/music"

    def test_a_space_is_a_space(self):
        """Folders are named as the server spells them. Percent-decoding
        would break a folder genuinely called ``100%``, and a settings field
        is where people type what they see."""
        assert str(parse("smb://nas/music/The Beatles")) == (
            "smb://nas/music/The Beatles"
        )
        assert parse("smb://nas/music/100%").share_path == "100%"


class TestWhatIsRefused:
    def test_a_password_belongs_in_the_settings(self):
        with pytest.raises(LocatorError, match="password"):
            parse("smb://media:secret@nas/music")

    def test_an_address_with_no_share(self):
        with pytest.raises(LocatorError, match="share"):
            parse("smb://nas")

    def test_a_share_with_no_address(self):
        with pytest.raises(LocatorError, match="server"):
            parse("smb:///music")

    def test_climbing_out_of_the_share(self):
        with pytest.raises(LocatorError, match=r"\.\."):
            parse("smb://nas/music/../secrets")

    def test_a_protocol_nobody_here_speaks(self):
        with pytest.raises(LocatorError, match="ftp"):
            parse("ftp://host/music")

    def test_nothing_at_all(self):
        with pytest.raises(LocatorError):
            parse("   ")

    def test_a_protocol_written_with_one_slash(self):
        """It would otherwise read as a relative path, and the music folder
        would silently become a directory beside the working directory."""
        with pytest.raises(LocatorError, match="two slashes"):
            parse("smb:/nas/music")
        with pytest.raises(LocatorError, match="two slashes"):
            parse("cifs:/nas/music")

    def test_a_windows_style_share_path(self):
        with pytest.raises(LocatorError, match="smb://nas/music"):
            parse(r"\\nas\music")

    def test_an_unbracketed_ipv6_address(self):
        with pytest.raises(LocatorError, match="brackets"):
            parse("smb://fe80::1/music")

    def test_a_colon_in_a_local_name_is_still_a_local_name(self):
        """Only a protocol this module speaks is second-guessed; anything
        else with a colon is an ordinary filename."""
        assert parse("odd:name/music").scheme == FILE_SCHEME

    def test_a_port_that_is_not_a_number(self):
        with pytest.raises(LocatorError, match="port"):
            parse("smb://nas:hello/music")


class TestALocalPath:
    def test_it_has_no_scheme_and_resolves(self, tmp_path):
        target = tmp_path / "music"
        target.mkdir()
        link = tmp_path / "link"
        link.symlink_to(target)

        locator = parse(str(link))
        assert locator.scheme == FILE_SCHEME
        assert locator.path == str(target)
        assert str(locator) == str(target)

    def test_a_home_relative_folder_is_expanded(self):
        assert parse("~/Music").path == os.path.expanduser("~/Music")

    def test_anything_without_a_scheme_is_a_path(self):
        assert scheme_of("/srv/kalinka/music") == FILE_SCHEME
        assert scheme_of("relative/music") == FILE_SCHEME


class TestTheParsedFormSurvivesPathArithmetic:
    """Everything downstream treats a location as a POSIX string. These are
    the operations the indexer, the clusterer and the filename parser perform
    on every indexed path."""

    LOCATION = "smb://nas/music/The Beatles/01 - Come Together.flac"

    def test_the_folder_and_the_file_come_apart(self):
        assert os.path.dirname(self.LOCATION) == "smb://nas/music/The Beatles"
        assert os.path.basename(self.LOCATION) == "01 - Come Together.flac"

    def test_a_child_is_joined_on(self):
        assert (
            os.path.join("smb://nas/music", "a.flac") == "smb://nas/music/a.flac"
        )

    def test_the_type_is_read_off_the_name(self):
        """The scheme must not throw the type guess off: the format decides
        which tag reader runs and what Content-Type a renderer is given."""
        assert media_type_of(self.LOCATION).endswith("flac")

    @pytest.mark.parametrize(
        "name", ["Bonus #1.mp3", "Symphony #5 (Live).mp3", "Where?.mp3"]
    )
    def test_a_hash_or_a_question_mark_in_a_name(self, name):
        """``mimetypes.guess_type`` treats a scheme-bearing string as a URL
        and truncates it at the fragment or the query, which leaves a track
        with no format — refused as unsupported and never indexed. Track
        names like "Symphony #5" are ordinary."""
        assert media_type_of(f"smb://nas/music/{name}") == "audio/mpeg"

    def test_a_local_path_reads_the_same_way(self):
        assert media_type_of("/mnt/nas/music/Bonus #1.mp3") == "audio/mpeg"

    def test_a_name_with_no_extension_has_no_type(self):
        assert media_type_of("smb://nas/music/README") is None


class TestContainment:
    ROOTS = ["smb://nas/music", "/srv/kalinka/music"]

    def test_a_file_under_a_share(self):
        assert root_of("smb://nas/music/a/b.flac", self.ROOTS) == "smb://nas/music"

    def test_the_root_itself(self):
        assert is_within("smb://nas/music", "smb://nas/music")

    def test_a_share_that_merely_shares_a_name_prefix(self):
        assert root_of("smb://nas/music2/b.flac", self.ROOTS) is None
        assert not is_within("smb://nas/musicology", "smb://nas/music")

    def test_a_different_server(self):
        assert root_of("smb://other/music/b.flac", self.ROOTS) is None

    def test_a_local_path_is_not_inside_a_share(self):
        assert root_of("/srv/kalinka/music/a.flac", self.ROOTS) == (
            "/srv/kalinka/music"
        )
        assert not is_within("/srv/kalinka/music/a.flac", "smb://nas/music")

    def test_nothing_is_inside_nothing(self):
        assert not is_within("", "smb://nas/music")
        assert not is_within("smb://nas/music/a", "")

    def test_the_filesystem_root_contains_everything(self):
        """A root that already ends in the separator must not have a second
        one appended. It matched nothing before, which made every indexed
        file read as outside its own music folder — and cleanup deletes a
        track it cannot place under any root."""
        assert is_within("/music/a.flac", "/")
        assert is_within("/", "/")
        assert root_of("/music/a.flac", ["/"]) == "/"
