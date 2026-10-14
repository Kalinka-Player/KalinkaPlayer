"""The allow-list a plugin's package ships (SDK 3.12).

The install helper reads it from /opt/kalinka/allowed_packages and installs
nothing it does not name, so every plugin must write it the same way.
"""

import json

from kalinka_plugin_sdk import (
    OptionalPackageSpec,
    optional_packages_manifest,
    print_optional_packages_manifest,
)

PACKAGES = {
    "onnxruntime": OptionalPackageSpec(
        pip_spec="onnxruntime>=1.17",
        description="d",
        architectures=("x86_64", "aarch64"),
    ),
    "essentia-tensorflow": OptionalPackageSpec(
        pip_spec="essentia-tensorflow==2.1b6",
        description="e",
        import_name="essentia",
    ),
}


def test_the_manifest_names_the_plugin_and_each_package_on_its_terms():
    assert optional_packages_manifest("jamendo", PACKAGES) == {
        "plugin": "jamendo",
        "schema": 1,
        "packages": {
            "onnxruntime": {
                "pip_spec": "onnxruntime>=1.17",
                "description": "d",
                "import_name": "",
                "architectures": ["x86_64", "aarch64"],
            },
            "essentia-tensorflow": {
                "pip_spec": "essentia-tensorflow==2.1b6",
                "description": "e",
                "import_name": "essentia",
                "architectures": [],
            },
        },
    }


def test_the_printed_manifest_reads_back_as_the_specs_it_was_written_from(capsys):
    print_optional_packages_manifest("jamendo", PACKAGES)

    printed = json.loads(capsys.readouterr().out)

    assert printed == optional_packages_manifest("jamendo", PACKAGES)
    assert {
        key: OptionalPackageSpec(**entry) for key, entry in printed["packages"].items()
    } == PACKAGES
