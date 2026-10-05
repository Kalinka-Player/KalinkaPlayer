"""Adversarial payload checks; no package/plugin code is executed."""

import hashlib
import json
import os
from pathlib import Path

from pydantic import ValidationError
import pytest

from kalinka_server.plugin_management.payload import (
    MAX_MANIFEST_BYTES,
    PayloadManifest,
    parse_manifest,
    verify_payload,
)


def digest(data):
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def payload(tmp_path):
    python = tmp_path / "python"
    native = tmp_path / "native"
    files = [
        ("python", "demo/__init__.py", b"raise RuntimeError('never import me')\n"),
        (
            "python",
            "demo-1.0.dist-info/entry_points.txt",
            b"[kalinka.plugins]\ndemo = demo:Plugin\n",
        ),
        ("native", "demo/bridge", b"native executable fixture"),
    ]
    roots = {"python": python, "native": native}
    for root, path, data in files:
        target = roots[root] / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    manifest = PayloadManifest.model_validate(
        {
            "schema_version": 1,
            "artifact_sha256": "a" * 64,
            "distribution": "kalinka-plugin-demo",
            "version": "1.0.0",
            "entry_point_name": "demo",
            "entry_point_value": "demo:Plugin",
            "scopes": [
                {"root": "python", "path": "demo", "kind": "directory"},
                {"root": "python", "path": "demo-1.0.dist-info", "kind": "directory"},
                {"root": "native", "path": "demo", "kind": "directory"},
            ],
            "files": [
                {
                    "root": root,
                    "path": path,
                    "size_bytes": len(data),
                    "sha256": digest(data),
                }
                for root, path, data in files
            ],
        }
    )
    return manifest, roots


def check(payload):
    manifest, roots = payload
    return verify_payload(manifest, roots, owner_uid=os.getuid())


def test_valid_payload_is_only_a_payload_match(payload):
    result = check(payload)
    assert result.status == "matched"
    assert result.checked_files == 3
    assert "verified" not in result.model_dump()


@pytest.mark.parametrize(
    "root,path",
    [
        ("python", "demo/__init__.py"),
        ("native", "demo/bridge"),
        ("python", "demo-1.0.dist-info/entry_points.txt"),
    ],
)
def test_altered_active_code_native_binary_or_entry_points_fail(payload, root, path):
    manifest, roots = payload
    target = roots[root] / path
    target.write_bytes(b"x" * target.stat().st_size)
    result = check(payload)
    assert result.status == "mismatch"
    assert result.reason == "file_digest_mismatch"


def test_wrong_length_fails(payload):
    _, roots = payload
    (roots["native"] / "demo/bridge").write_bytes(b"replacement")
    assert check(payload).reason == "file_size_mismatch"


@pytest.mark.parametrize(
    "name", ["extra.py", "__pycache__/extra.pyc", "extra.pth", "RECORD"]
)
def test_extra_files_are_not_ignored(payload, name):
    _, roots = payload
    extra = roots["python"] / "demo" / name
    extra.parent.mkdir(exist_ok=True)
    extra.write_text("injected")
    assert check(payload).reason == "unexpected_file"


def test_missing_file_fails(payload):
    _, roots = payload
    (roots["python"] / "demo/__init__.py").unlink()
    assert check(payload).reason == "missing_file"


def test_symlink_cannot_substitute_a_file(payload):
    _, roots = payload
    file = roots["native"] / "demo/bridge"
    copied = roots["native"] / "outside"
    copied.write_bytes(file.read_bytes())
    file.unlink()
    file.symlink_to(copied)
    assert check(payload).status != "matched"


def test_parent_directory_symlink_is_refused(payload):
    _, roots = payload
    original = roots["python"] / "demo"
    moved = roots["python"] / "outside"
    original.rename(moved)
    original.symlink_to(moved, target_is_directory=True)
    assert check(payload).status != "matched"


def test_root_symlink_is_refused(payload, tmp_path):
    manifest, roots = payload
    link = tmp_path / "link"
    link.symlink_to(roots["python"], target_is_directory=True)
    roots["python"] = link
    assert check(payload).status != "matched"


def test_fifo_is_refused_without_blocking(payload):
    _, roots = payload
    os.mkfifo(roots["python"] / "demo/pipe")
    assert check(payload).reason == "special_file_not_supported"


def test_hardlinked_payload_is_not_accepted(payload):
    _, roots = payload
    os.link(roots["native"] / "demo/bridge", roots["native"] / "outside")
    assert check(payload).reason == "hardlink_not_supported"


@pytest.mark.parametrize(
    "path",
    [
        "",
        "/absolute",
        "../escape",
        "demo/../escape",
        "demo//file",
        "demo/./file",
        "demo\\file",
        "demo/\u0000file",
    ],
)
def test_traversal_and_ambiguous_paths_are_rejected(payload, path):
    manifest, _ = payload
    data = manifest.model_dump(mode="json")
    data["files"][0]["path"] = path
    with pytest.raises(ValidationError):
        PayloadManifest.model_validate(data)


@pytest.mark.parametrize(
    "mutation",
    ["duplicate_file", "overlap", "unowned", "unknown_field", "unknown_root"],
)
def test_ambiguous_or_unknown_manifest_structure_is_rejected(payload, mutation):
    manifest, _ = payload
    data = manifest.model_dump(mode="json")
    if mutation == "duplicate_file":
        data["files"].append(data["files"][0])
    elif mutation == "overlap":
        data["scopes"].append(data["scopes"][0])
    elif mutation == "unowned":
        data["files"][0]["path"] = "elsewhere/file"
    elif mutation == "unknown_field":
        data["verification_command"] = "anything"
    else:
        data["files"][0]["root"] = "arbitrary"
    with pytest.raises(ValidationError):
        PayloadManifest.model_validate(data)


def test_manifest_bytes_are_checked_against_independent_digest(payload):
    manifest, _ = payload
    data = manifest.model_dump_json().encode()
    assert parse_manifest(data, sha256=digest(data), size_bytes=len(data)) == manifest
    with pytest.raises(ValueError, match="digest"):
        parse_manifest(data, sha256="b" * 64, size_bytes=len(data))
    with pytest.raises(ValueError, match="length"):
        parse_manifest(data, sha256=digest(data), size_bytes=MAX_MANIFEST_BYTES + 1)
    with pytest.raises(ValueError, match="length"):
        parse_manifest(data, sha256=digest(data), size_bytes=len(data) - 1)


def test_duplicate_json_keys_are_rejected():
    data = b'{"schema_version":1,"schema_version":1}'
    with pytest.raises(ValueError, match="Duplicate"):
        parse_manifest(data, sha256=digest(data), size_bytes=len(data))


@pytest.mark.parametrize("path", ["demo", "demo/__init__.py"])
def test_writable_code_or_directory_is_unverifiable(payload, path):
    _, roots = payload
    (roots["python"] / path).chmod(0o777)
    assert check(payload).reason == "unsafe_permissions"


def test_unexpected_owner_is_unverifiable(payload):
    manifest, roots = payload
    assert (
        verify_payload(manifest, roots, owner_uid=os.getuid() + 1).reason
        == "unsafe_permissions"
    )


def test_missing_root_is_unverifiable(payload):
    manifest, roots = payload
    assert (
        verify_payload(
            manifest, {"python": roots["python"]}, owner_uid=os.getuid()
        ).reason
        == "missing_verification_root"
    )


def test_file_scope_checks_a_single_native_file(payload):
    manifest, roots = payload
    data = manifest.model_dump(mode="json")
    data["scopes"][-1] = {"root": "native", "path": "demo/bridge", "kind": "file"}
    result = verify_payload(
        PayloadManifest.model_validate(data), roots, owner_uid=os.getuid()
    )
    assert result.status == "matched"


def test_manifest_collections_cannot_change_after_validation(payload):
    manifest, _ = payload
    assert isinstance(manifest.files, tuple)
    assert isinstance(manifest.scopes, tuple)
    with pytest.raises(ValidationError):
        manifest.files[0].sha256 = "f" * 64


def test_boolean_schema_version_is_not_a_version(payload):
    manifest, _ = payload
    data = manifest.model_dump(mode="json")
    data["schema_version"] = True
    with pytest.raises(ValidationError):
        PayloadManifest.model_validate(data)


def test_mutation_during_read_is_not_a_match(payload, monkeypatch):
    _, roots = payload
    init = roots["python"] / "demo/__init__.py"
    # Installed files predate the check; a coarse clock stamps same-tick writes alike.
    os.utime(init, ns=(0, 0))
    original_read = os.read
    modified = False

    def racing_read(fd, size):
        nonlocal modified
        data = original_read(fd, size)
        if not modified and data.startswith(b"raise RuntimeError"):
            modified = True
            # Change already-read bytes: checking the digest alone would miss
            # this, so inode/stat stability must also be checked.
            init.write_bytes(b"x" * len(data))
        return data

    monkeypatch.setattr(os, "read", racing_read)
    result = check(payload)
    assert modified
    assert result.status != "matched"
    assert result.reason == "installation_changed"
