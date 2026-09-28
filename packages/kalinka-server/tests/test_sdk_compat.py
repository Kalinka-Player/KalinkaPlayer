"""Tests for the SDK-compatibility guards (:mod:`kalinka_server.sdk_compat`).

The startup guard reads the server's own ``kalinka-plugin-sdk`` requirement
and the installed SDK version from package metadata. These tests patch those
two metadata lookups so the behaviour can be exercised without installing real
packages. The per-plugin check reads a plugin's ``REQUIRES_SDK``.
"""

from importlib.metadata import PackageNotFoundError

import pytest

from kalinka_server import sdk_compat
from kalinka_server.sdk_compat import (
    IncompatibleSDKError,
    check_sdk_compatibility,
    plugin_sdk_mismatch,
)


def _setup(monkeypatch, *, server_requires, sdk_version):
    """Patch the ``requires`` / ``dist_version`` lookups sdk_compat uses.

    Each argument is either a value to return or an ``Exception`` to raise.
    """

    def fake_requires(_dist):
        if isinstance(server_requires, BaseException):
            raise server_requires
        return server_requires

    def fake_version(_dist):
        if isinstance(sdk_version, BaseException):
            raise sdk_version
        return sdk_version

    monkeypatch.setattr(sdk_compat, "requires", fake_requires)
    monkeypatch.setattr(sdk_compat, "dist_version", fake_version)


def test_compatible_version_passes(monkeypatch):
    _setup(monkeypatch, server_requires=["kalinka-plugin-sdk>=1,<2"], sdk_version="1.0.0")
    check_sdk_compatibility()  # does not raise


def test_compatible_minor_within_major_passes(monkeypatch):
    # A backwards-compatible minor bump still satisfies >=1,<2.
    _setup(monkeypatch, server_requires=["kalinka-plugin-sdk>=1,<2"], sdk_version="1.7.3")
    check_sdk_compatibility()  # does not raise


def test_prerelease_dev_version_accepted(monkeypatch):
    # Local setuptools_scm dev builds (e.g. 1.0.1.dev5+...) must be accepted.
    _setup(
        monkeypatch,
        server_requires=["kalinka-plugin-sdk>=1,<2"],
        sdk_version="1.0.1.dev5+g829e38c",
    )
    check_sdk_compatibility()  # does not raise


def test_incompatible_major_raises(monkeypatch):
    _setup(monkeypatch, server_requires=["kalinka-plugin-sdk>=1,<2"], sdk_version="2.0.0")
    with pytest.raises(IncompatibleSDKError):
        check_sdk_compatibility()


def test_below_floor_raises(monkeypatch):
    _setup(monkeypatch, server_requires=["kalinka-plugin-sdk>=1.5,<2"], sdk_version="1.2.0")
    with pytest.raises(IncompatibleSDKError):
        check_sdk_compatibility()


def test_missing_sdk_raises(monkeypatch):
    _setup(
        monkeypatch,
        server_requires=["kalinka-plugin-sdk>=1,<2"],
        sdk_version=PackageNotFoundError("kalinka-plugin-sdk"),
    )
    with pytest.raises(IncompatibleSDKError):
        check_sdk_compatibility()


def test_uninstalled_source_checkout_is_noop(monkeypatch):
    # Server not installed as a dist (unbuilt source tree): skip rather than
    # block startup, even if the SDK version would otherwise be incompatible.
    _setup(
        monkeypatch,
        server_requires=PackageNotFoundError("kalinka-server"),
        sdk_version="9.9.9",
    )
    check_sdk_compatibility()  # does not raise


def test_marker_gated_requirement_is_ignored(monkeypatch):
    # An SDK requirement gated by an extra marker (e.g. a dev extra) is not the
    # runtime requirement, so it is ignored and the guard is a no-op.
    _setup(
        monkeypatch,
        server_requires=['kalinka-plugin-sdk>=1,<2; extra == "dev"'],
        sdk_version="5.0.0",
    )
    check_sdk_compatibility()  # does not raise


def test_non_sdk_requirements_skipped(monkeypatch):
    # Other entries in the requires() list must be ignored without error.
    _setup(
        monkeypatch,
        server_requires=["fastapi>=0.100.0", "kalinka-plugin-sdk>=1,<2"],
        sdk_version="1.5.0",
    )
    check_sdk_compatibility()  # does not raise


def _plugin_requiring(requires_sdk):
    return type("_Plugin", (), {"REQUIRES_SDK": requires_sdk})


@pytest.mark.parametrize("sdk_version", ["3.0.0", "3.4.0", "3.5.0.dev2+g829e38c"])
def test_a_plugin_runs_on_any_sdk_its_requirement_admits(sdk_version):
    assert plugin_sdk_mismatch(_plugin_requiring(">=3,<4"), sdk_version) is None


@pytest.mark.parametrize("requires_sdk", [">=2,<3", ">=4,<5", ">=3.5,<4"])
def test_a_plugin_whose_requirement_excludes_the_sdk_may_not_run(requires_sdk):
    reason = plugin_sdk_mismatch(_plugin_requiring(requires_sdk), "3.4.0")

    assert reason is not None
    assert f"kalinka-plugin-sdk{requires_sdk}" in reason
    assert "3.4.0" in reason


@pytest.mark.parametrize("requires_sdk", ["1.0", "", 3])
def test_a_requirement_that_is_not_a_specifier_counts_against_the_plugin(
    requires_sdk,
):
    assert plugin_sdk_mismatch(_plugin_requiring(requires_sdk), "3.4.0") is not None


def test_a_plugin_declaring_no_requirement_may_not_run():
    assert plugin_sdk_mismatch(type("_Plugin", (), {}), "3.4.0") is not None
