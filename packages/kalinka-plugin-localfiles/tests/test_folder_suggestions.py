"""What the settings page offers when it asks where the music is.

A drive plugged into the box and a server found on the network are the same
question to the user and two different jobs underneath. Both are read from
what is already known, because this is answered on every read of the page:
the slow half is a broadcast that has already gone out, and its replies are
collected by the read after.
"""

from __future__ import annotations

import asyncio

import pytest

from kalinka_plugin_localfiles.config_model import LocalFilesConfig
from kalinka_plugin_localfiles.module_setup import KalinkaPluginLocalFiles
from kalinka_plugin_localfiles.suggest import (
    DiscoveredHost,
    LocalMountSuggester,
    SmbHostSuggester,
    host_option,
    mount_options,
)
from kalinka_plugin_localfiles.utils.mount_status import list_mounts

MOUNTINFO = """
25 1 179:2 / / rw,relatime shared:1 - ext4 /dev/mmcblk0p2 rw
26 25 179:1 / /boot/firmware rw,relatime shared:2 - vfat /dev/mmcblk0p1 rw
27 25 0:22 / /mnt/scratch rw,relatime shared:3 - tmpfs tmpfs rw
28 25 8:1 / /media/usb0 rw,relatime shared:4 - vfat /dev/sda1 rw
29 25 0:40 / /mnt/usb rw,relatime shared:5 - autofs systemd-1 rw
30 25 0:41 / /mnt/nas rw,relatime shared:6 - cifs //nas/music rw
31 25 8:17 / /srv/library rw,relatime shared:7 - ext4 /dev/sdb1 rw
32 25 0:50 / /run/media/envel/MUSIC\\040DISK rw - exfat /dev/sdc1 rw
33 25 0:60 / /mnt/snap rw,relatime shared:8 - squashfs /dev/loop0 rw
"""


def _offered(text=MOUNTINFO):
    return {o.value: o.description for o in mount_options(list_mounts(text))}


class _Discovery:
    def __init__(self, hosts=()):
        self._hosts = list(hosts)
        self.refreshed = 0

    def hosts(self):
        return list(self._hosts)

    def refresh(self):
        self.refreshed += 1


class TestDrivesOnThisMachine:
    def test_a_disk_where_removable_media_lands_is_offered(self):
        assert _offered()["/media/usb0"] == "vfat · /dev/sda1"

    def test_a_disk_mounted_somewhere_of_its_own_is_offered_too(self):
        assert "/srv/library" in _offered()

    def test_an_automount_is_offered_before_anything_is_mounted_on_it(self):
        """The folder behind it is the one the user means, and the
        automounter is what makes it appear when the library reads it."""
        assert _offered()["/mnt/usb"] == "mounted on demand"

    def test_a_share_the_kernel_has_mounted_is_offered_as_the_path_it_is_at(self):
        assert _offered()["/mnt/nas"] == "cifs · //nas/music"

    @pytest.mark.parametrize(
        "mount_point", ["/", "/boot/firmware", "/mnt/scratch", "/mnt/snap"]
    )
    def test_what_is_not_somewhere_to_keep_music_is_not_offered(self, mount_point):
        assert mount_point not in _offered()

    def test_a_mount_point_with_a_space_in_it_is_offered_as_it_reads(self):
        assert "/run/media/envel/MUSIC DISK" in _offered()

    def test_the_same_point_mounted_over_is_offered_once(self):
        doubled = MOUNTINFO + (
            "34 25 8:33 / /media/usb0 rw,relatime shared:9 - ext4 /dev/sdd1 rw\n"
        )
        assert list(_offered(doubled)).count("/media/usb0") == 1

    def test_reading_the_disks_never_waits_on_one(self):
        """mountinfo is kernel memory; a sleeping disk is not touched."""
        assert isinstance(LocalMountSuggester().options(), list)


class TestServersOnTheNetwork:
    def test_a_host_is_offered_with_the_share_still_to_be_named(self):
        option = host_option(DiscoveredHost("192.168.1.20", "NAS", "mDNS"))
        assert option.value == "smb://192.168.1.20/"

    def test_what_is_stored_is_the_address_and_what_is_read_is_the_name(self):
        """A name learned over mDNS or NetBIOS is not one this machine's
        resolver can necessarily look up."""
        option = host_option(DiscoveredHost("192.168.1.20", "NAS", "mDNS"))
        assert option.label == "NAS"
        assert "192.168.1.20" in option.description

    def test_a_host_that_gave_no_name_reads_as_its_address(self):
        option = host_option(DiscoveredHost("10.0.0.5", "", "NetBIOS"))
        assert option.label == "10.0.0.5"
        assert option.description == "found over NetBIOS"

    def test_an_ipv6_host_is_bracketed_so_its_colons_are_not_a_port(self):
        option = host_option(DiscoveredHost("fe80::1", "MAC", "mDNS"))
        assert option.value == "smb://[fe80::1]/"

    def test_asking_for_suggestions_asks_the_network_for_more(self):
        discovery = _Discovery([DiscoveredHost("192.168.1.20", "NAS", "mDNS")])
        suggester = SmbHostSuggester(discovery)
        suggester.refresh()
        assert discovery.refreshed == 1
        assert [o.label for o in suggester.options()] == ["NAS"]


class TestWhatThePluginAnswers:
    def _plugin(self, discovery=None):
        made = KalinkaPluginLocalFiles()
        made._suggesters = [
            LocalMountSuggester(),
            SmbHostSuggester(discovery or _Discovery()),
        ]
        return made

    def test_both_sources_are_offered_together(self):
        discovery = _Discovery([DiscoveredHost("192.168.1.20", "NAS", "mDNS")])
        options = asyncio.run(self._plugin(discovery).resolve_options("music_folders"))
        assert "smb://192.168.1.20/" in [o.value for o in options]

    def test_every_source_is_asked_for_something_fresher(self):
        discovery = _Discovery()
        asyncio.run(self._plugin(discovery).resolve_options("music_folders"))
        assert discovery.refreshed == 1

    def test_a_field_it_offers_nothing_for_says_so(self):
        with pytest.raises(KeyError):
            asyncio.run(self._plugin().resolve_options("db_path"))

    def test_a_plugin_that_was_never_set_up_offers_nothing_rather_than_failing(self):
        """The settings page reads a module that failed to start too."""
        made = KalinkaPluginLocalFiles()
        assert asyncio.run(made.resolve_options("music_folders")) == []


def test_the_field_asks_for_suggestions():
    """The tag on the field is the whole declaration; without it the server
    never binds the plugin's resolver to it."""
    extra = LocalFilesConfig.model_fields["music_folders"].json_schema_extra
    assert extra["dynamic_options"] is True
