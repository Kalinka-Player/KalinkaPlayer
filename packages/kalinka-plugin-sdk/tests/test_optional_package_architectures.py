"""Where an optional package can be installed (SDK 3.12)."""

import sys

import pytest

from kalinka_plugin_sdk import OptionalPackageSpec, optional_packages


@pytest.mark.parametrize(
    "kernel, python_bits, architecture",
    [
        ("x86_64", 64, "x86_64"),
        ("aarch64", 64, "aarch64"),
        # 32-bit Raspberry Pi OS on a Pi 4 or 5 runs a 64-bit kernel.
        ("aarch64", 32, "armv7l"),
        ("armv8l", 32, "armv7l"),
        ("armv7l", 32, "armv7l"),
        ("x86_64", 32, "i686"),
    ],
)
def test_the_wheel_architecture_is_the_interpreters(
    monkeypatch, kernel, python_bits, architecture
):
    monkeypatch.setattr(optional_packages.platform, "machine", lambda: kernel)
    monkeypatch.setattr(sys, "maxsize", 2 ** (python_bits - 1) - 1)

    assert optional_packages.wheel_architecture() == architecture


def test_a_package_declaring_no_architectures_has_wheels_for_every_one():
    spec = OptionalPackageSpec(pip_spec="tokenizers>=0.15", description="d")

    assert spec.has_wheels_for("armv7l")
    assert spec.has_wheels_for("x86_64")


def test_a_package_has_wheels_only_for_the_architectures_it_declares():
    spec = OptionalPackageSpec(
        pip_spec="onnxruntime>=1.17",
        description="d",
        architectures=("x86_64", "aarch64"),
    )

    assert spec.has_wheels_for("aarch64")
    assert not spec.has_wheels_for("armv7l")
