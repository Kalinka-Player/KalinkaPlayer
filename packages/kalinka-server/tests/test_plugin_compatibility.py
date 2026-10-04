"""Compatibility declarations never replace package or installed-payload trust."""

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import subprocess
from types import SimpleNamespace

import pytest

from kalinka_server.plugin_management import compatibility as module
from kalinka_server.plugin_management.compatibility import (
    CatalogCompatibility,
    HostEnvironment,
    detect_host,
    evaluate_release,
)

NOW = datetime(2026, 10, 2, tzinfo=timezone.utc)
HOST = HostEnvironment(
    "linux", "x86_64", "deb", "debian", "13", "5.3.0", "3.5.0", "3.11.0"
)


@pytest.fixture
def plugin():
    return {
        "id": "demo",
        "distribution": "kalinka-plugin-demo",
        "delivery": "independent",
        "releases": [
            {
                "version": "1.0.0",
                "channel": "stable",
                "withdrawn": False,
                "published_at": "2026-10-01T00:00:00Z",
                "requires": {
                    "server": ">=5.3,<6",
                    "sdk": ">=3.5,<4",
                    "python": ">=3.11",
                    "platforms": ["linux"],
                    "architectures": ["all"],
                    "capabilities": [],
                    "notes": [],
                },
                "artifacts": [
                    {
                        "format": "deb",
                        "platform": "linux",
                        "architectures": ["all"],
                        "filename": "kalinka-plugin-demo_1.0.0_all.deb",
                        "sha256": "a" * 64,
                        "package": {
                            "name": "kalinka-plugin-demo",
                            "version": "1.0.0",
                            "architecture": "all",
                        },
                        "targets": [{"id": "debian", "versions": ["13"]}],
                    }
                ],
            }
        ],
    }


def release_at(plugin, version):
    release = deepcopy(plugin["releases"][0])
    release["version"] = version
    artifact = release["artifacts"][0]
    artifact["package"]["version"] = version
    artifact["filename"] = f"kalinka-plugin-demo_{version}_all.deb"
    return release


def evaluate(plugin, host=HOST, renderers=()):
    return evaluate_release(
        plugin, plugin["releases"][0], host, renderers, channel="stable", now=NOW
    )


def codes(result):
    return {reason["code"] for reason in result["reasons"]}


def annotated(plugin, host=HOST):
    snapshot = {
        "status": "available",
        "plugins": [deepcopy(plugin)],
        "trust": {"status": "unverified"},
        "installation_allowed": False,
        "automatic_updates_enabled": False,
    }
    return CatalogCompatibility(lambda: host, lambda: NOW).annotate(snapshot)


@pytest.mark.parametrize(
    "field,version,expected",
    [
        ("server_version", "5.2.9", "incompatible_server"),
        ("server_version", "5.3.0", None),
        ("server_version", "5.5.1.dev1+build", None),
        ("server_version", "6.0.0", "incompatible_server"),
        ("server_version", "source-commit", "server_version_unknown"),
        ("sdk_version", "3.4.9", "incompatible_sdk"),
        ("sdk_version", "3.5.0", None),
        ("sdk_version", "4.0.0", "incompatible_sdk"),
        ("sdk_version", None, "sdk_version_unknown"),
        ("python_version", "3.10.9", "incompatible_python"),
        ("python_version", "3.11.0", None),
        ("python_version", "", "python_version_unknown"),
    ],
)
def test_version_boundaries_and_unknown_versions(plugin, field, version, expected):
    result = evaluate(plugin, replace(HOST, **{field: version}))
    assert codes(result) == ({expected} if expected else set())
    assert result["status"] == ("blocked" if expected else "metadata_compatible")


@pytest.mark.parametrize(
    "field,value,expected",
    [
        ("platform", "windows", "unsupported_platform"),
        ("platform", "macos", "unsupported_platform"),
        ("platform", None, "platform_unknown"),
        ("architecture", None, "architecture_unknown"),
    ],
)
def test_platform_and_architecture_unknown_do_not_pass_all(
    plugin, field, value, expected
):
    assert expected in codes(evaluate(plugin, replace(HOST, **{field: value})))


def test_release_and_artifact_architectures_are_both_checked(plugin):
    release = plugin["releases"][0]
    release["requires"]["architectures"] = ["aarch64"]
    assert "unsupported_architecture" in codes(evaluate(plugin))
    assert (
        evaluate(plugin, replace(HOST, architecture="aarch64"))["status"]
        == "metadata_compatible"
    )
    release["requires"]["architectures"] = ["all"]
    artifact = release["artifacts"][0]
    artifact.update(
        architectures=["aarch64"], filename="kalinka-plugin-demo_1.0.0_arm64.deb"
    )
    artifact["package"]["architecture"] = "arm64"
    result = evaluate(plugin)
    assert "no_compatible_artifact" in codes(result)
    assert "unsupported_architecture" in codes(result["artifacts"][0])
    assert (
        evaluate(plugin, replace(HOST, architecture="aarch64"))["status"]
        == "metadata_compatible"
    )


@pytest.mark.parametrize(
    "distribution,version,expected",
    [
        ("debian", "13", None),
        ("debian", "14", "unsupported_distribution"),
        ("ubuntu", "24.04", "unsupported_distribution"),
        ("raspbian", "13", "unsupported_distribution"),
        (None, None, "distribution_unknown"),
    ],
)
def test_exact_distro_allowlist(plugin, distribution, version, expected):
    result = evaluate(
        plugin, replace(HOST, distribution=distribution, distribution_version=version)
    )
    assert codes(result["artifacts"][0]) == ({expected} if expected else set())


def test_all_is_not_cross_platform_or_package_format(plugin):
    plugin["releases"][0]["requires"]["platforms"] = ["all"]
    result = evaluate(plugin, replace(HOST, platform="macos", package_format=None))
    assert "unsupported_platform" in codes(result["artifacts"][0])
    assert "unsupported_package_format" in codes(result["artifacts"][0])
    assert result["status"] == "blocked"


def test_rpm_is_a_distinct_native_target(plugin):
    artifact = plugin["releases"][0]["artifacts"][0]
    artifact.update(
        format="rpm",
        architectures=["aarch64"],
        filename="kalinka-plugin-demo-1.0.0-1.fc44.aarch64.rpm",
        package={
            "name": "kalinka-plugin-demo",
            "version": "0:1.0.0-1.fc44",
            "architecture": "aarch64",
        },
        targets=[{"id": "fedora", "versions": ["44"]}],
    )
    host = replace(
        HOST,
        architecture="aarch64",
        package_format="rpm",
        distribution="fedora",
        distribution_version="44",
    )
    assert evaluate(plugin, host)["status"] == "metadata_compatible"
    assert evaluate(plugin)["status"] == "blocked"


@pytest.mark.parametrize(
    "change",
    [
        lambda a: a["package"].update(name="another-package"),
        lambda a: a["package"].update(architecture="noarch"),
        lambda a: a.update(architectures=["x86_64"]),
        lambda a: a.update(filename="another-file.deb"),
        lambda a: a.update(targets=[{"id": "fedora", "versions": ["44"]}]),
    ],
)
def test_inconsistent_native_metadata_is_blocked(plugin, change):
    change(plugin["releases"][0]["artifacts"][0])
    result = evaluate(plugin)
    assert codes(result["artifacts"][0]) == {"invalid_package_metadata"}
    assert result["status"] == "blocked"


def test_package_must_hold_the_advertised_release(plugin):
    plugin["releases"][0]["version"] = "2.0.0"
    result = annotated(plugin)["plugins"][0]["compatibility"]
    assert result["latest_compatible_version"] is None
    assert codes(result["releases"][0]["artifacts"][0]) == {"invalid_package_metadata"}


@pytest.mark.parametrize(
    "native,consistent",
    [
        ("1.0.0", True),
        ("1.0.0-1", True),
        ("2:1.0.0-1+deb13u1", True),
        ("1.0", True),
        ("1.0.0-", False),
        ("x:1.0.0", False),
        ("1.0.0~rc1", False),
        ("1.0.1", False),
    ],
)
def test_deb_version_forms(plugin, native, consistent):
    artifact = plugin["releases"][0]["artifacts"][0]
    artifact["package"]["version"] = native
    artifact["filename"] = f"kalinka-plugin-demo_{native.split(':', 1)[-1]}_all.deb"
    assert (evaluate(plugin)["status"] == "metadata_compatible") is consistent


@pytest.mark.parametrize(
    "release,native,consistent",
    [
        ("1.0.0", "0:1.0.0-1.fc44", True),
        ("1.0.0", "1.0.0-1.fc44", True),
        ("1.1.0rc1", "1.1.0~rc1-1.fc44", True),
        ("1.0.0", "1.0.0", False),
        ("1.0.0", "0:1.0.1-1.fc44", False),
    ],
)
def test_rpm_version_forms(plugin, release, native, consistent):
    release_record = plugin["releases"][0]
    release_record["version"] = release
    filename = f"kalinka-plugin-demo-{native.split(':', 1)[-1]}.noarch.rpm"
    release_record["artifacts"][0].update(
        format="rpm",
        filename=filename,
        package={
            "name": "kalinka-plugin-demo",
            "version": native,
            "architecture": "noarch",
        },
        targets=[{"id": "fedora", "versions": ["44"]}],
    )
    host = replace(
        HOST, package_format="rpm", distribution="fedora", distribution_version="44"
    )
    assert (evaluate(plugin, host)["status"] == "metadata_compatible") is consistent


def test_wheel_does_not_fall_through_to_native_support(plugin):
    plugin["releases"][0]["artifacts"][0]["format"] = "wheel"
    assert codes(evaluate(plugin)["artifacts"][0]) == {"unsupported_package_format"}


def test_unknown_capability_blocks_without_running_a_probe(plugin):
    plugin["releases"][0]["requires"]["capabilities"] = ["serial.rs232"]
    assert codes(evaluate(plugin)) == {"missing_capability"}
    assert (
        evaluate(plugin, replace(HOST, capabilities=frozenset({"serial.rs232"})))[
            "status"
        ]
        == "metadata_compatible"
    )


def test_renderer_versions_require_live_known_evidence(plugin):
    plugin["releases"][0]["requires"]["renderer"] = ">=0.5,<1"
    assert codes(evaluate(plugin)) == {"renderer_version_unknown"}
    renderer = {
        "renderer_id": "living-room",
        "software_version": "0.5.0",
        "status": "connected",
        "compatible": True,
    }
    assert evaluate(plugin, renderers=(renderer,))["status"] == "metadata_compatible"
    for field, value, expected in [
        ("software_version", "0.4.9", "incompatible_renderer"),
        ("software_version", "1.0.0", "incompatible_renderer"),
        ("software_version", "unknown", "renderer_version_unknown"),
        ("status", "offline", "renderer_unavailable"),
        ("compatible", False, "renderer_protocol_incompatible"),
    ]:
        result = evaluate(
            plugin,
            renderers=(renderer, renderer | {field: value, "renderer_id": "office"}),
        )
        assert expected in codes(result)
        assert result["reasons"][0]["renderer_id"] == "office"


def test_selects_older_compatible_release_and_reports_newer_blocked(plugin):
    newer = release_at(plugin, "2.0.0")
    newer["requires"]["sdk"] = ">=4,<5"
    plugin["releases"].append(newer)
    result = annotated(plugin)
    compatibility = result["plugins"][0]["compatibility"]
    assert compatibility["latest_compatible_version"] == "1.0.0"
    assert compatibility["latest_available_version"] == "2.0.0"
    assert codes(compatibility["newer_blocked_release"]) == {"incompatible_sdk"}
    assert result["trust"] == {"status": "unverified"}
    assert result["installation_allowed"] is False
    assert result["automatic_updates_enabled"] is False
    assert compatibility["installation_allowed"] is False
    assert "compatibility" not in plugin


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"channel": "preview", "version": "3.0rc1"}, "channel_not_selected"),
        ({"withdrawn": True}, "release_withdrawn"),
        ({"published_at": "2027-01-01T00:00:00Z"}, "release_not_yet_published"),
    ],
)
def test_unavailable_releases_are_not_candidates(plugin, change, reason):
    newer = release_at(plugin, change.get("version", "2.0.0"))
    newer.update(change)
    plugin["releases"].append(newer)
    result = annotated(plugin)["plugins"][0]["compatibility"]
    assert result["latest_compatible_version"] == "1.0.0"
    assert result["latest_available_version"] == "1.0.0"
    assert result["newer_blocked_release"] is None
    assert reason in codes(result["releases"][0])


def test_version_ordering_is_numeric_not_lexical(plugin):
    for version in ("1.9", "1.10"):
        plugin["releases"].append(release_at(plugin, version))
    assert (
        annotated(plugin)["plugins"][0]["compatibility"]["latest_compatible_version"]
        == "1.10"
    )


def test_bundle_and_empty_releases_have_no_independent_candidate(plugin):
    plugin["delivery"] = "bundle"
    result = annotated(plugin)["plugins"][0]["compatibility"]
    assert result["status"] == "bundle_managed"
    assert result["latest_compatible_version"] is None
    plugin.update(delivery="independent", releases=[])
    assert annotated(plugin)["plugins"][0]["compatibility"]["status"] == "no_releases"


@pytest.fixture
def host_probe(monkeypatch):
    detect_host.cache_clear()
    monkeypatch.setattr(module.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        module.platform,
        "machine",
        lambda: pytest.fail("Kernel architecture is not userspace architecture"),
    )
    monkeypatch.setattr(
        module.platform,
        "freedesktop_os_release",
        lambda: {"ID": "debian", "VERSION_ID": "13"},
    )
    monkeypatch.setattr(module.metadata, "version", lambda _: "3.5.0")
    monkeypatch.setattr(module, "get_version", lambda: "5.3.0")
    yield
    detect_host.cache_clear()


@pytest.mark.parametrize(
    "native,bits,expected",
    [
        ("amd64", 64, "x86_64"),
        ("arm64", 64, "aarch64"),
        ("armhf", 32, "armv7l"),
        ("i386", 32, "x86"),
        ("arm64", 32, None),
        ("amd64", 32, None),
        ("all", 64, None),
        ("amd64\ni386", 64, None),
        ("mystery", 64, None),
    ],
)
def test_native_userspace_probe_is_bounded_cached_and_not_kernel_based(
    host_probe, monkeypatch, native, bits, expected
):
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        assert command == ["/usr/bin/dpkg", "--print-architecture"]
        assert kwargs["timeout"] == 2
        assert kwargs.get("shell", False) is False
        return SimpleNamespace(stdout=native + "\n")

    monkeypatch.setattr(module.subprocess, "run", run)
    monkeypatch.setattr(module.sys, "maxsize", 2 ** (bits - 1) - 1)
    assert detect_host().architecture == expected
    assert detect_host().package_format == ("deb" if expected else None)
    assert len(calls) == 1


def test_rpm_probe_reads_installed_package_not_build_macro(host_probe, monkeypatch):
    monkeypatch.setattr(
        module.platform,
        "freedesktop_os_release",
        lambda: {"ID": "fedora", "VERSION_ID": "44"},
    )

    def run(command, **kwargs):
        assert command == ["/usr/bin/rpm", "-q", "--queryformat", "%{ARCH}\n", "rpm"]
        return SimpleNamespace(stdout="aarch64\n")

    monkeypatch.setattr(module.subprocess, "run", run)
    monkeypatch.setattr(module.sys, "maxsize", 2**63 - 1)
    assert detect_host().architecture == "aarch64"
    assert detect_host().package_format == "rpm"


@pytest.mark.parametrize(
    "error",
    [
        FileNotFoundError(),
        subprocess.TimeoutExpired("dpkg", 2),
        subprocess.CalledProcessError(1, "dpkg"),
    ],
)
def test_probe_failure_is_unknown_not_installable(host_probe, monkeypatch, error):
    def run(*args, **kwargs):
        raise error

    monkeypatch.setattr(module.subprocess, "run", run)
    assert detect_host().architecture is None
    assert detect_host().package_format is None


def test_id_like_is_not_an_allowlist(host_probe, monkeypatch):
    monkeypatch.setattr(
        module.platform,
        "freedesktop_os_release",
        lambda: {"ID": "derivative", "ID_LIKE": "debian", "VERSION_ID": "13"},
    )
    monkeypatch.setattr(
        module.subprocess, "run", lambda *a, **k: pytest.fail("No known native backend")
    )
    assert detect_host().package_format is None


def test_non_linux_hosts_do_not_probe_native_managers(host_probe, monkeypatch):
    monkeypatch.setattr(module.platform, "system", lambda: "Windows")
    monkeypatch.setattr(
        module.subprocess, "run", lambda *a, **k: pytest.fail("Not a Linux host")
    )
    assert detect_host().platform == "windows"
    assert detect_host().architecture is None
