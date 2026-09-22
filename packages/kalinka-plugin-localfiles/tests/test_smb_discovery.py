"""Finding the file servers on the network.

The wire format is the part worth pinning: a node-status request is built by
hand out of a name encoding nothing else in this repo uses, and a reply is
parsed from a table whose length the sender declares. A packet that lies
about that length must be refused rather than read past.

The rest is about not making the settings page wait. Discovery is asked for
suggestions on every read of the page, and every one of those asks is
allowed to start a broadcast — so what stops it is a floor between scans,
not the caller's patience.
"""

from __future__ import annotations

import logging
import struct
import threading
import time

import pytest

from kalinka_plugin_localfiles.suggest import smb_discovery
from kalinka_plugin_localfiles.suggest.smb_discovery import (
    SmbHostDiscovery,
    encode_netbios_name,
    file_server_name,
    nbstat_query,
    parse_nbstat_reply,
)

#: Reserved for documentation (RFC 5737), so the machine running the tests
#: cannot hold one and the kernel is left free to answer for itself.
NAS = "198.51.100.20"
OTHER_NAS = "203.0.113.7"


def _ours(monkeypatch, *addresses):
    """Make these look like this machine's own, and nothing else."""
    held = set(addresses)
    monkeypatch.setattr(
        smb_discovery, "_belongs_to_this_machine", lambda a: a in held
    )


def _reply(names, *, flags=0x8400, answers=1, rr_type=0x0021, claimed=None):
    question = encode_netbios_name("*")
    header = struct.pack(">HHHHHH", 1, flags, 0, answers, 0, 0)
    table = bytes([claimed if claimed is not None else len(names)])
    for name, suffix, is_group in names:
        table += (
            name.encode().ljust(15, b" ")
            + bytes([suffix])
            + struct.pack(">H", 0x8000 if is_group else 0x0400)
        )
    body = struct.pack(">HHIH", rr_type, 1, 0, len(table)) + table
    return header + bytes([len(question)]) + question + b"\x00" + body


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class _Watch:
    """An mDNS subscription that does nothing but remember being asked."""

    started = 0
    stopped = 0

    def __init__(self, on_seen=None, on_lost=None):
        self.on_seen = on_seen
        self.on_lost = on_lost
        _Watch.started = _Watch.stopped = 0

    def start(self):
        _Watch.started += 1

    def stop(self):
        _Watch.stopped += 1


def _discovery(**kwargs):
    kwargs.setdefault("mdns_factory", _Watch)
    made = SmbHostDiscovery(**kwargs)
    made._scan_once = lambda: None
    return made


class TestTheNameOnTheWire:
    def test_the_wildcard_is_the_one_every_host_answers_for(self):
        assert encode_netbios_name("*") == b"CK" + b"AA" * 15

    def test_a_name_is_padded_to_its_full_length_and_given_its_suffix(self):
        encoded = encode_netbios_name("NAS", suffix=0x20)
        assert len(encoded) == 32
        assert encoded.endswith(b"CA")  # 0x20 -> 'C','A'

    def test_a_name_is_padded_with_the_space_the_wire_expects(self):
        """Nulls are the wildcard's padding alone; a host asked about a
        null-padded name of its own does not recognise it."""
        encoded = encode_netbios_name("NAS", suffix=0x20)
        assert encoded[6:30] == b"CA" * 12  # 0x20 -> 'C','A'

    def test_a_name_too_long_for_the_wire_is_cut_to_fit(self):
        assert len(encode_netbios_name("A" * 40)) == 32


class TestTheRequest:
    def test_it_asks_one_question_about_every_name(self):
        query = nbstat_query()
        _id, flags, questions, answers, _ns, _ar = struct.unpack(">HHHHHH", query[:12])
        assert (questions, answers) == (1, 0)
        assert flags & 0x0010  # broadcast
        assert query[-4:] == struct.pack(">HH", 0x0021, 0x0001)

    def test_the_name_it_asks_about_carries_its_own_length(self):
        query = nbstat_query()
        assert query[12] == 32
        assert query[45] == 0


class TestTheReply:
    def test_the_name_table_is_read_out_whole(self):
        names = parse_nbstat_reply(
            _reply([("NAS", 0x00, False), ("WORKGROUP", 0x00, True)])
        )
        assert names == [("NAS", 0x00, False), ("WORKGROUP", 0x00, True)]

    def test_the_server_service_is_what_says_it_serves_shares(self):
        assert (
            file_server_name(
                parse_nbstat_reply(
                    _reply([("NAS", 0x00, False), ("NAS", 0x20, False)])
                )
            )
            == "NAS"
        )

    def test_a_host_that_registers_no_server_service_is_not_one(self):
        assert file_server_name(parse_nbstat_reply(_reply([("PC", 0x00, False)]))) is None

    def test_a_group_name_is_not_a_host(self):
        """A workgroup registers the server suffix as a group, and connecting
        to it would name the workgroup rather than a machine."""
        assert (
            file_server_name(parse_nbstat_reply(_reply([("SALES", 0x20, True)])))
            is None
        )

    @pytest.mark.parametrize(
        "packet, why",
        [
            (b"\x00" * 4, "shorter than a header"),
            (_reply([("NAS", 0x20, False)], flags=0x0010), "a question, not an answer"),
            (_reply([], answers=0), "nothing answered"),
            (_reply([("NAS", 0x20, False)], rr_type=0x0020), "a different record"),
            (_reply([("NAS", 0x20, False)], claimed=9), "more names than it carries"),
        ],
    )
    def test_a_packet_that_is_not_what_it_claims_is_refused(self, packet, why):
        with pytest.raises(ValueError):
            parse_nbstat_reply(packet)


class TestWhatHasBeenSeen:
    def test_a_host_is_reported_once_however_many_ways_it_was_found(self):
        made = _discovery()
        made._netbios_seen("198.51.100.20", "NAS")
        made._mdns_seen("NAS._smb._tcp.local.", "NAS", ["198.51.100.20"])
        assert [h.address for h in made.hosts()] == ["198.51.100.20"]

    def test_an_announcement_that_names_the_host_wins_over_one_that_does_not(self):
        made = _discovery()
        made._netbios_seen("198.51.100.20", "")
        made._mdns_seen("NAS._smb._tcp.local.", "NAS", ["198.51.100.20"])
        assert made.hosts()[0].name == "NAS"

    def test_a_service_that_is_withdrawn_takes_its_addresses_with_it(self):
        made = _discovery()
        made._mdns_seen("NAS._smb._tcp.local.", "NAS", ["198.51.100.20", "fe80::1"])
        made._mdns_lost("NAS._smb._tcp.local.")
        assert made.hosts() == []

    def test_a_host_that_stops_answering_broadcasts_stops_being_offered(self):
        clock = _Clock()
        made = _discovery(now=clock, stale_after=600.0)
        made._netbios_seen("198.51.100.20", "NAS")
        clock.now += 599
        assert made.hosts()
        clock.now += 2
        assert made.hosts() == []

    def test_an_announced_host_does_not_expire_while_it_is_announced(self):
        """mDNS withdraws its own; ageing them out too would drop a server
        that is present and simply quiet."""
        clock = _Clock()
        made = _discovery(now=clock)
        made._mdns_seen("NAS._smb._tcp.local.", "NAS", ["198.51.100.20"])
        clock.now += 86400
        assert len(made.hosts()) == 1

    def test_hosts_are_ordered_by_what_the_user_reads(self):
        made = _discovery()
        made._netbios_seen("198.51.100.30", "ZULU")
        made._netbios_seen("198.51.100.10", "ALPHA")
        assert [h.name for h in made.hosts()] == ["ALPHA", "ZULU"]


class TestWhichAddressesAreWorthOffering:
    """A host is offered only where an SMB client here could usefully be
    pointed at it, whichever way it was found."""

    def test_a_link_local_address_is_left_out(self):
        """Reaching one needs a zone, which the smb:// URL cannot carry."""
        made = _discovery()
        made._mdns_seen("NAS._smb._tcp.local.", "NAS", ["fe80::1", NAS])
        assert [h.address for h in made.hosts()] == [NAS]

    @pytest.mark.parametrize(
        "address", ["127.0.0.1", "::1", "0.0.0.0", "224.0.0.1", "255.255.255.255"]
    )
    def test_an_address_that_names_no_one_host_is_left_out(self, address):
        made = _discovery()
        made._mdns_seen("NAS._smb._tcp.local.", "NAS", [address, NAS])
        assert [h.address for h in made.hosts()] == [NAS]

    def test_an_address_this_machine_holds_is_left_out(self, monkeypatch):
        """Samba answers on every address the box has, so the box announces
        itself on each of them."""
        mine = ["10.10.10.1", OTHER_NAS, "2a01:4b00:b8fe:3100::1"]
        _ours(monkeypatch, *mine)
        made = _discovery()
        made._mdns_seen("PI._smb._tcp.local.", "PI", mine + [NAS])
        assert [h.address for h in made.hosts()] == [NAS]

    def test_this_machine_answering_its_own_broadcast_is_left_out(self, monkeypatch):
        """nmbd here replies to the node-status request sent from here,
        which no amount of mDNS filtering would have caught."""
        _ours(monkeypatch, OTHER_NAS)
        made = _discovery()
        made._netbios_seen(OTHER_NAS, "RASPBERRYPI")
        made._netbios_seen(NAS, "NAS")
        assert [h.address for h in made.hosts()] == [NAS]

    def test_whose_an_address_is_gets_asked_again_on_every_read(self, monkeypatch):
        """One can arrive before DHCP has answered, or while duplicate-address
        detection is still running, and be this machine's a moment later."""
        made = _discovery()
        made._mdns_seen("PI._smb._tcp.local.", "PI", [OTHER_NAS])
        assert [h.address for h in made.hosts()] == [OTHER_NAS]
        _ours(monkeypatch, OTHER_NAS)
        assert made.hosts() == []

    def test_an_announcement_with_nothing_worth_offering_leaves_no_host(self):
        made = _discovery()
        made._mdns_seen("SELF._smb._tcp.local.", "SELF", ["127.0.0.1", "fe80::1"])
        assert made.hosts() == []

    def test_a_server_that_gives_up_its_addresses_takes_them_off_the_list(self):
        made = _discovery()
        made._mdns_seen("NAS._smb._tcp.local.", "NAS", [NAS])
        made._mdns_seen("NAS._smb._tcp.local.", "NAS", [])
        assert made.hosts() == []


class TestAMachineThatWouldBindAnything:
    """Where ip_nonlocal_bind is set the kernel stops being an oracle: every
    address binds, so every server would look like this one."""

    def test_the_rule_stands_down_rather_than_empty_the_list(self, monkeypatch):
        monkeypatch.setattr(smb_discovery, "_belongs_to_this_machine", lambda _: True)
        made = _discovery()
        made._netbios_seen(NAS, "NAS")
        assert [h.address for h in made.hosts()] == [NAS]

    def test_it_says_so_once_rather_than_on_every_read(self, monkeypatch, caplog):
        monkeypatch.setattr(smb_discovery, "_belongs_to_this_machine", lambda _: True)
        made = _discovery()
        made._netbios_seen(NAS, "NAS")
        with caplog.at_level(logging.WARNING):
            made.hosts()
            made.hosts()
        assert sum("binds addresses it does not hold" in r.message
                   for r in caplog.records) == 1

    def test_the_rules_that_never_needed_the_kernel_still_hold(self, monkeypatch):
        monkeypatch.setattr(smb_discovery, "_belongs_to_this_machine", lambda _: True)
        made = _discovery()
        made._netbios_seen("127.0.0.1", "SELF")
        assert made.hosts() == []


class TestAskingTheKernelWhoseAddressItIs:
    def test_loopback_is_this_machine(self):
        assert smb_discovery._belongs_to_this_machine("127.0.0.1")

    def test_an_address_assigned_nowhere_is_not(self):
        """192.0.2.0/24 is TEST-NET-1, reserved for documentation, so no
        machine running this test can hold it."""
        assert not smb_discovery._belongs_to_this_machine("192.0.2.1")

    def test_a_name_is_refused_rather_than_resolved(self):
        """Resolving it would answer a different question, and a broken
        resolver would make the settings page wait out its timeouts."""
        assert not smb_discovery._belongs_to_this_machine("nas.local")


class TestAskingTheNetworkAgain:
    def test_starting_listens_and_asks_once(self):
        made = _discovery()
        scans = []
        made._scan_once = lambda: scans.append(1)
        made.start()
        _join_scan()
        assert _Watch.started == 1 and len(scans) == 1

    def test_asking_again_too_soon_does_not_broadcast_again(self):
        clock = _Clock()
        made = _discovery(now=clock, min_scan_interval=10.0)
        scans = []
        made._scan_once = lambda: scans.append(1)
        made.refresh()
        _join_scan()
        clock.now += 9
        made.refresh()
        _join_scan()
        assert len(scans) == 1
        clock.now += 2
        made.refresh()
        _join_scan()
        assert len(scans) == 2

    def test_asking_never_makes_the_caller_wait(self):
        made = _discovery(min_scan_interval=0.0)
        made._scan_once = lambda: time.sleep(0.5)
        started = time.monotonic()
        made.refresh()
        assert time.monotonic() - started < 0.2

    def test_a_scan_with_no_thread_to_run_on_leaves_the_next_one_free(
        self, monkeypatch, caplog
    ):
        """A lock held by a thread that never started would retire the
        broadcast for the life of the process."""
        made = _discovery(min_scan_interval=0.0)
        scans = []
        made._scan_once = lambda: scans.append(1)

        real_thread = threading.Thread

        def no_thread(*_args, **_kwargs):
            raise RuntimeError("can't start new thread")

        monkeypatch.setattr(threading, "Thread", no_thread)
        with caplog.at_level("WARNING"):
            made.refresh()
        monkeypatch.setattr(threading, "Thread", real_thread)

        made.refresh()
        _join_scan()
        assert len(scans) == 1
        assert "No thread to scan" in caplog.text

    def test_a_scan_that_fails_leaves_the_next_one_free_to_run(self):
        made = _discovery(min_scan_interval=0.0)
        attempts = []

        def fail():
            attempts.append(1)
            raise OSError("the network is down")

        made._scan_once = fail
        made.refresh()
        _join_scan()
        made.refresh()
        _join_scan()
        assert len(attempts) == 2

    def test_stopping_closes_the_subscription_and_refuses_further_scans(self):
        made = _discovery(min_scan_interval=0.0)
        scans = []
        made._scan_once = lambda: scans.append(1)
        made.start()
        _join_scan()
        made.stop()
        made.refresh()
        _join_scan()
        assert _Watch.stopped == 1 and len(scans) == 1

    def test_mdns_that_cannot_start_costs_only_its_own_half(self, caplog):
        def refuse(on_seen, on_lost):
            raise RuntimeError("no multicast here")

        made = _discovery(mdns_factory=refuse)
        scans = []
        made._scan_once = lambda: scans.append(1)
        with caplog.at_level("WARNING"):
            made.start()
        _join_scan()
        assert len(scans) == 1
        assert "will not be suggested" in caplog.text


class _Announcement:
    """What zeroconf hands back for one resolved service."""

    def __init__(self, addresses):
        self._addresses = addresses

    def parsed_addresses(self):
        return list(self._addresses)


class _Zeroconf:
    def __init__(self, info):
        self._info = info

    def get_service_info(self, *_args, **_kwargs):
        return self._info


class TestAnAnnouncementThatArrives:
    def _seen(self, info):
        from kalinka_plugin_localfiles.suggest.smb_discovery import _ZeroconfWatch

        seen = []
        watch = _ZeroconfWatch(
            lambda key, name, addresses: seen.append((key, name, addresses)),
            lambda key: None,
        )
        watch.add_service(_Zeroconf(info), "_smb._tcp.local.", "NAS._smb._tcp.local.")
        return seen

    def test_a_server_is_taken_under_the_name_it_announced(self):
        seen = self._seen(_Announcement(["198.51.100.20"]))
        assert seen == [("NAS._smb._tcp.local.", "NAS", ["198.51.100.20"])]

    def test_a_server_that_answers_only_over_ipv6_is_still_offered(self):
        seen = self._seen(_Announcement(["2001:db8::5"]))
        assert seen[0][2] == ["2001:db8::5"]

    def test_every_address_announced_is_handed_on_to_be_judged(self):
        """The adapter translates; which addresses are worth offering is
        the discovery's rule, so that it holds for NetBIOS too."""
        seen = self._seen(_Announcement(["fe80::1", "198.51.100.20"]))
        assert seen[0][2] == ["fe80::1", "198.51.100.20"]

    def test_a_service_that_has_lost_its_addresses_is_still_reported(self):
        """Its previous addresses are on the list, and only this says to
        take them off — the records expired rather than being withdrawn,
        so remove_service may never come."""
        seen = self._seen(_Announcement([]))
        assert seen == [("NAS._smb._tcp.local.", "NAS", [])]

    def test_a_service_that_cannot_be_resolved_is_not_reported(self):
        assert self._seen(None) == []


class TestLettingGoOfTheSubscription:
    """A browser is a thread of its own, and it is released by its own name:
    calling the wrong one leaves it running for the life of the process, with
    only a log line to say so.
    """

    def test_the_browser_is_cancelled_and_the_listener_closed(self):
        from kalinka_plugin_localfiles.suggest.smb_discovery import _ZeroconfWatch

        released = []
        watch = _ZeroconfWatch(lambda *a: None, lambda *a: None)
        watch._browser = _Releasable(released, "browser")
        watch._zeroconf = _Releasable(released, "zeroconf")

        watch.stop()

        assert released == [("browser", "cancel"), ("zeroconf", "close")]

    def test_stopping_twice_releases_once(self):
        from kalinka_plugin_localfiles.suggest.smb_discovery import _ZeroconfWatch

        released = []
        watch = _ZeroconfWatch(lambda *a: None, lambda *a: None)
        watch._browser = _Releasable(released, "browser")
        watch._zeroconf = _Releasable(released, "zeroconf")

        watch.stop()
        watch.stop()

        assert len(released) == 2

    def test_a_listener_that_cannot_be_browsed_is_closed_rather_than_dropped(
        self, monkeypatch
    ):
        """It has joined the multicast group by then; the caller lets the
        whole watch go on a failed start, so only this can release it."""
        import sys
        import types

        from kalinka_plugin_localfiles.suggest.smb_discovery import _ZeroconfWatch

        released = []
        fake = types.ModuleType("zeroconf")
        fake.Zeroconf = lambda: _Releasable(released, "zeroconf")

        def refuse(*_args, **_kwargs):
            raise OSError("no multicast here")

        fake.ServiceBrowser = refuse
        monkeypatch.setitem(sys.modules, "zeroconf", fake)

        watch = _ZeroconfWatch(lambda *a: None, lambda *a: None)
        with pytest.raises(OSError):
            watch.start()

        assert released == [("zeroconf", "close")]
        assert watch._zeroconf is None


class _Releasable:
    """Answers only the one method its real counterpart has, so calling the
    other raises the way zeroconf would."""

    def __init__(self, log, name):
        self._log = log
        self._name = name

    def cancel(self):
        if self._name != "browser":
            raise AttributeError("cancel")
        self._log.append((self._name, "cancel"))

    def close(self):
        if self._name != "zeroconf":
            raise AttributeError("close")
        self._log.append((self._name, "close"))


def _join_scan():
    """Wait out the scan thread, which is where the work of a refresh is."""
    for thread in threading.enumerate():
        if thread.name == "smb-discovery":
            thread.join(timeout=5)
