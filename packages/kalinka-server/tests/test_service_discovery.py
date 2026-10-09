"""Per-address mDNS announcements: one single-address instance each, grouped
by clients through the server_id TXT value."""

import asyncio
import socket

import pytest

from kalinka_server import service_discovery
from kalinka_server.config_model import KalinkaConfig
from kalinka_server.service_discovery import ServiceDiscovery, get_service_info
from tests.ifaddr_fake import fake_adapters

SERVER_ID = "9f1c9f2e-1111-2222-3333-444455556666"


@pytest.fixture(autouse=True)
def stable_server_id(monkeypatch):
    monkeypatch.setattr(service_discovery, "get_server_id", lambda: SERVER_ID)


@pytest.fixture
def two_interfaces(monkeypatch):
    monkeypatch.setattr(
        service_discovery,
        "get_interface_ip_mappings",
        lambda: {"eth0": ["192.168.1.20"], "wlan0": ["10.20.0.15"]},
    )


def addresses_of(info):
    return [socket.inet_ntoa(addr) for addr in info.addresses]


def test_announcements_follow_the_interfaces_the_machine_reports(monkeypatch):
    fake_adapters(
        monkeypatch,
        {
            "lo": ["127.0.0.1"],
            "eth0": ["192.168.1.20", ("fe80::1", 0, 2)],
            "wlan0": ["10.20.0.15"],
        },
    )

    discovery = ServiceDiscovery(KalinkaConfig(), bind_host="0.0.0.0")

    assert [(a.interface, a.ip_address) for a in discovery.announcements] == [
        ("eth0", "192.168.1.20"),
        ("wlan0", "10.20.0.15"),
    ]


def test_one_single_address_instance_per_interface(two_interfaces):
    discovery = ServiceDiscovery(KalinkaConfig(), bind_host="0.0.0.0")

    assert [a.interface for a in discovery.announcements] == ["eth0", "wlan0"]
    by_interface = {a.interface: a for a in discovery.announcements}
    assert addresses_of(by_interface["eth0"].service_info) == ["192.168.1.20"]
    assert addresses_of(by_interface["wlan0"].service_info) == ["10.20.0.15"]


def test_instance_names_carry_the_interface_suffix(two_interfaces):
    discovery = ServiceDiscovery(KalinkaConfig())

    assert [a.service_info.name for a in discovery.announcements] == [
        "My Kalinka Service (eth0)._kalinkaplayer._tcp.local.",
        "My Kalinka Service (wlan0)._kalinkaplayer._tcp.local.",
    ]


def test_every_ipv4_address_gets_a_distinct_single_address_instance(
    monkeypatch,
):
    monkeypatch.setattr(
        service_discovery,
        "get_interface_ip_mappings",
        lambda: {"eth0": ["192.168.1.20", "10.20.0.15"]},
    )

    discovery = ServiceDiscovery(KalinkaConfig())

    assert [a.ip_address for a in discovery.announcements] == [
        "192.168.1.20",
        "10.20.0.15",
    ]
    assert [addresses_of(a.service_info) for a in discovery.announcements] == [
        ["192.168.1.20"],
        ["10.20.0.15"],
    ]
    names = [a.service_info.name for a in discovery.announcements]
    assert len(set(names)) == 2
    assert all("(eth0-" in name for name in names)


def test_long_service_name_is_truncated_only_in_the_dns_instance():
    config = KalinkaConfig()
    config.server.service_name = "A" * 52

    info = get_service_info(config, "wlp0s20f3", "192.168.1.20")

    instance = info.name.removesuffix("._kalinkaplayer._tcp.local.")
    assert len(instance.encode("utf-8")) <= 63
    assert info.decoded_properties["display_name"] == "A" * 52


def test_dns_instance_truncation_does_not_split_unicode():
    config = KalinkaConfig()
    config.server.service_name = "é" * 40

    info = get_service_info(config, "wlp0s20f3", "192.168.1.20")

    instance = info.name.removesuffix("._kalinkaplayer._tcp.local.")
    assert len(instance.encode("utf-8")) <= 63
    assert info.decoded_properties["display_name"] == "é" * 40


def test_oversized_display_name_is_bounded_to_one_txt_string():
    config = KalinkaConfig()
    config.server.service_name = "A" * 300

    info = get_service_info(config, "eth0", "192.168.1.20")

    assert len(info.decoded_properties["display_name"].encode("utf-8")) == 242


def test_oversized_endpoint_suffix_uses_a_short_stable_token():
    config = KalinkaConfig()
    endpoint_name = "é" * 100

    first = get_service_info(config, endpoint_name, "192.168.1.20")
    second = get_service_info(config, endpoint_name, "192.168.1.20")

    instance = first.name.removesuffix("._kalinkaplayer._tcp.local.")
    assert len(instance.encode("utf-8")) <= 63
    assert first.name == second.name


def test_txt_groups_instances_by_server_id_and_display_name(two_interfaces):
    discovery = ServiceDiscovery(KalinkaConfig())

    for announcement in discovery.announcements:
        properties = announcement.service_info.decoded_properties
        assert properties["server_id"] == SERVER_ID
        assert properties["display_name"] == "My Kalinka Service"
        assert properties["renderer_proto"] is not None


def test_configured_interface_announces_only_there(two_interfaces):
    config = KalinkaConfig()
    config.server.interface = "wlan0"

    discovery = ServiceDiscovery(config)

    assert [a.interface for a in discovery.announcements] == ["wlan0"]
    assert addresses_of(discovery.announcements[0].service_info) == [
        "10.20.0.15"
    ]
    assert discovery.announcements[0].service_info.name.startswith(
        "My Kalinka Service (wlan0)."
    )


def test_configured_interface_announces_only_the_address_uvicorn_binds(
    monkeypatch,
):
    monkeypatch.setattr(
        service_discovery,
        "get_interface_ip_mappings",
        lambda: {"eth0": ["192.168.1.20", "10.20.0.15"]},
    )
    config = KalinkaConfig()
    config.server.interface = "eth0"

    discovery = ServiceDiscovery(config, bind_host="10.20.0.15")

    assert [a.ip_address for a in discovery.announcements] == ["10.20.0.15"]


def test_stale_uvicorn_bind_address_announces_nothing(two_interfaces):
    discovery = ServiceDiscovery(KalinkaConfig(), bind_host="192.0.2.10")

    assert discovery.announcements == []


def test_unknown_configured_interface_falls_back_to_all(two_interfaces):
    config = KalinkaConfig()
    config.server.interface = "tun9"

    discovery = ServiceDiscovery(config)

    assert [a.interface for a in discovery.announcements] == ["eth0", "wlan0"]


def test_no_interfaces_announces_nothing(monkeypatch):
    monkeypatch.setattr(
        service_discovery, "get_interface_ip_mappings", lambda: {}
    )

    assert ServiceDiscovery(KalinkaConfig()).announcements == []


def test_service_info_port_follows_config():
    config = KalinkaConfig()
    config.server.port = 9123

    info = get_service_info(config, "eth0", "192.168.1.20")

    assert info.port == 9123


class FakeAsyncZeroconf:
    def __init__(self, interfaces, fail_register, tracker):
        self.interfaces = interfaces
        self.fail_register = fail_register
        self.tracker = tracker
        self.registered = []
        self.closed = False

    async def async_register_service(self, info):
        self.tracker["register_active"] += 1
        self.tracker["register_max"] = max(
            self.tracker["register_max"], self.tracker["register_active"]
        )
        try:
            await asyncio.sleep(0)
            if self.fail_register:
                raise RuntimeError("bind failed")
            self.registered.append(info)
        finally:
            self.tracker["register_active"] -= 1

    async def async_close(self):
        self.tracker["close_active"] += 1
        self.tracker["close_max"] = max(
            self.tracker["close_max"], self.tracker["close_active"]
        )
        try:
            await asyncio.sleep(0)
            self.closed = True
        finally:
            self.tracker["close_active"] -= 1


def install_fake_zeroconf(monkeypatch, fail_register=()):
    created = []
    tracker = {
        "register_active": 0,
        "register_max": 0,
        "close_active": 0,
        "close_max": 0,
    }

    def factory(*, ip_version, interfaces):
        fake = FakeAsyncZeroconf(
            interfaces, interfaces[0] in fail_register, tracker
        )
        created.append(fake)
        return fake

    monkeypatch.setattr(service_discovery, "AsyncZeroconf", factory)
    return created, tracker


async def test_register_binds_one_responder_per_address(
    two_interfaces, monkeypatch
):
    created, tracker = install_fake_zeroconf(monkeypatch)
    discovery = ServiceDiscovery(KalinkaConfig())

    await discovery.register_service()

    assert [f.interfaces for f in created] == [
        ["192.168.1.20"],
        ["10.20.0.15"],
    ]
    assert all(len(f.registered) == 1 for f in created)
    assert tracker["register_max"] == 2

    await discovery.unregister_service()


async def test_registration_failure_closes_its_responder_and_spares_the_rest(
    two_interfaces, monkeypatch
):
    created, _ = install_fake_zeroconf(
        monkeypatch, fail_register={"192.168.1.20"}
    )
    discovery = ServiceDiscovery(KalinkaConfig())

    await discovery.register_service()

    assert created[0].closed
    assert discovery.announcements[0].zeroconf is None
    assert discovery.announcements[1].zeroconf is created[1]
    assert not created[1].closed

    await discovery.unregister_service()


async def test_unregister_closes_every_responder(two_interfaces, monkeypatch):
    created, tracker = install_fake_zeroconf(monkeypatch)
    discovery = ServiceDiscovery(KalinkaConfig())

    await discovery.register_service()
    await discovery.unregister_service()

    assert all(f.closed for f in created)
    assert all(a.zeroconf is None for a in discovery.announcements)
    assert tracker["close_max"] == 2


@pytest.fixture
def mutable_interfaces(monkeypatch):
    mapping = {"eth0": ["192.168.1.20"], "wlan0": ["10.20.0.15"]}
    monkeypatch.setattr(
        service_discovery,
        "get_interface_ip_mappings",
        lambda: dict(mapping),
    )
    return mapping


async def test_reconcile_announces_addresses_that_appear_later(
    mutable_interfaces, monkeypatch
):
    created, _ = install_fake_zeroconf(monkeypatch)
    saved = dict(mutable_interfaces)
    mutable_interfaces.clear()
    discovery = ServiceDiscovery(KalinkaConfig())

    await discovery.register_service()
    assert discovery.announcements == []

    mutable_interfaces.update(saved)
    await discovery._reconcile()

    assert [a.ip_address for a in discovery.announcements] == [
        "192.168.1.20",
        "10.20.0.15",
    ]
    assert all(a.zeroconf is not None for a in discovery.announcements)

    await discovery.unregister_service()


async def test_reconcile_spares_live_responders_on_surviving_addresses(
    mutable_interfaces, monkeypatch
):
    created, _ = install_fake_zeroconf(monkeypatch)
    discovery = ServiceDiscovery(KalinkaConfig())
    await discovery.register_service()
    survivor = discovery.announcements[0].zeroconf

    del mutable_interfaces["wlan0"]
    await discovery._reconcile()

    assert [a.interface for a in discovery.announcements] == ["eth0"]
    # The surviving responder is the same live object: no goodbye, no
    # re-registration.
    assert discovery.announcements[0].zeroconf is survivor
    assert not created[0].closed
    assert len(created[0].registered) == 1
    assert created[1].closed

    await discovery.unregister_service()


async def test_reconcile_is_a_noop_while_nothing_changes(
    mutable_interfaces, monkeypatch
):
    created, _ = install_fake_zeroconf(monkeypatch)
    discovery = ServiceDiscovery(KalinkaConfig())
    await discovery.register_service()

    await discovery._reconcile()

    assert len(created) == 2
    assert all(len(f.registered) == 1 for f in created)
    assert not any(f.closed for f in created)

    await discovery.unregister_service()


async def test_reconcile_retries_failed_registration_on_present_address(
    mutable_interfaces, monkeypatch
):
    failing = {"192.168.1.20"}
    created, _ = install_fake_zeroconf(monkeypatch, fail_register=failing)
    discovery = ServiceDiscovery(KalinkaConfig())
    await discovery.register_service()
    assert discovery.announcements[0].zeroconf is None

    failing.clear()
    await discovery._reconcile()

    assert discovery.announcements[0].zeroconf is created[-1]
    assert len(created[-1].registered) == 1

    await discovery.unregister_service()


async def test_register_starts_watcher_and_unregister_stops_it(
    mutable_interfaces, monkeypatch
):
    install_fake_zeroconf(monkeypatch)
    discovery = ServiceDiscovery(KalinkaConfig())

    await discovery.register_service()
    watcher = discovery._watcher
    assert watcher is not None and not watcher.done()

    await discovery.unregister_service()
    assert discovery._watcher is None
    assert watcher.cancelled()


async def test_unregister_waits_out_cancelled_reconcile_cleanup(
    mutable_interfaces, monkeypatch
):
    created, _ = install_fake_zeroconf(monkeypatch)
    discovery = ServiceDiscovery(KalinkaConfig())
    await discovery.register_service()

    close_started = asyncio.Event()
    release_close = asyncio.Event()
    stale = created[1]
    original_close = stale.async_close

    async def blocked_close():
        close_started.set()
        await release_close.wait()
        await original_close()

    stale.async_close = blocked_close
    del mutable_interfaces["wlan0"]
    mutable_interfaces["usb0"] = ["172.16.0.8"]

    reconcile = asyncio.create_task(discovery._reconcile())
    discovery._watcher = reconcile
    await close_started.wait()

    unregister = asyncio.create_task(discovery.unregister_service())
    await asyncio.sleep(0)
    assert not unregister.done()

    release_close.set()
    await unregister

    assert all(fake.closed for fake in created)
