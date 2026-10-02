"""Verify a bounded installed-file manifest without importing plugin code.

The caller MUST authenticate the expected manifest digest independently. A
payload match is not catalog authorization: native ownership, import resolution,
runtime/dependency integrity and fresh independent-worker evidence are separate
requirements. Production callers must run under the trusted worker, not plugins.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
from typing import Annotated, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_PAYLOAD_BYTES = 2 * 1024 * 1024 * 1024
MAX_ENTRIES = 20000
SHA256 = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Root = Literal["python", "native", "wheels"]


def relative_path(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or len(value) > 512
        or path.is_absolute()
        or str(path) != value
        or "\\" in value
        or any(part in {".", ".."} for part in value.split("/"))
        or len(path.parts) > 32
        or any(ord(c) < 32 for c in value)
    ):
        raise ValueError("Expected a bounded, canonical relative path")
    return value


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class PayloadScope(_Record):
    root: Root
    path: str
    kind: Literal["directory", "file"]

    _path = field_validator("path")(relative_path)

    def contains(self, root: str, path: str) -> bool:
        return self.root == root and (
            path == self.path
            or (self.kind == "directory" and path.startswith(self.path + "/"))
        )


class PayloadFile(_Record):
    root: Root
    path: str
    sha256: SHA256
    size_bytes: int = Field(ge=0, le=MAX_PAYLOAD_BYTES)

    _path = field_validator("path")(relative_path)


class PayloadManifest(_Record):
    schema_version: Literal[1]
    artifact_sha256: SHA256
    distribution: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=200)
    version: str = Field(min_length=1, max_length=100)
    entry_point_name: str = Field(min_length=1, max_length=200)
    entry_point_value: str = Field(min_length=1, max_length=500)
    scopes: tuple[PayloadScope, ...] = Field(min_length=1, max_length=100)
    files: tuple[PayloadFile, ...] = Field(min_length=1, max_length=MAX_ENTRIES)

    @field_validator("scopes", "files", mode="before")
    @classmethod
    def freeze_records(cls, value):
        # Freeze nested collections too; the checked manifest must not change
        # between digest validation, coverage validation and filesystem use.
        return tuple(value) if isinstance(value, list) else value

    @field_validator("schema_version", mode="before")
    @classmethod
    def strict_schema_version(cls, value):
        if type(value) is not int:
            raise ValueError("Schema version must be an integer")
        return value

    @model_validator(mode="after")
    def check_coverage(self) -> PayloadManifest:
        keys = [(file.root, file.path) for file in self.files]
        if len(set(keys)) != len(keys):
            raise ValueError("Duplicate payload path")
        if sum(file.size_bytes for file in self.files) > MAX_PAYLOAD_BYTES:
            raise ValueError("Payload exceeds verification limit")
        for index, scope in enumerate(self.scopes):
            for other in self.scopes[index + 1 :]:
                if scope.contains(other.root, other.path) or other.contains(
                    scope.root, scope.path
                ):
                    raise ValueError("Overlapping payload scopes")
            if not any(scope.contains(*key) for key in keys):
                raise ValueError("Empty payload scope")
        for key in keys:
            if sum(scope.contains(*key) for scope in self.scopes) != 1:
                raise ValueError("Payload file must have exactly one owner scope")
        return self


def parse_manifest(data: bytes, *, sha256: str, size_bytes: int) -> PayloadManifest:
    """Check bytes against a trusted reference, then reject ambiguous JSON."""
    if not 0 < size_bytes <= MAX_MANIFEST_BYTES or len(data) != size_bytes:
        raise ValueError("Invalid manifest length")
    if hashlib.sha256(data).hexdigest() != sha256:
        raise ValueError("Manifest digest mismatch")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate manifest key")
            result[key] = value
        return result

    return PayloadManifest.model_validate(json.loads(data, object_pairs_hook=unique))


class PayloadResult(_Record):
    status: Literal["matched", "mismatch", "unverifiable"]
    reason: str
    checked_files: int = 0


class _Refused(Exception):
    def __init__(self, reason: str, *, mismatch: bool = False):
        self.reason = reason
        self.mismatch = mismatch


def _safe_permissions(info: os.stat_result, owner_uid: int) -> None:
    if info.st_uid != owner_uid or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise _Refused("unsafe_permissions")


def _stamp(info: os.stat_result) -> tuple:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _open_directory(path: Path) -> int:
    """Open every component no-follow; never resolve a catalog symlink."""
    if not path.is_absolute() or ".." in path.parts:
        raise _Refused("invalid_root")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            child = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
            )
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def verify_payload(
    manifest: PayloadManifest, roots: Mapping[str, Path], *, owner_uid: int = 0
) -> PayloadResult:
    """Read-only Linux verifier, deliberately conservative about generated files.

    No blanket exclusions: extra .pyc/metadata files need authenticated adapter
    rules, not an implicit allowlist. Symlinks, devices, hardlinks and writable
    code are refused. Supplied roots and owner_uid are trusted deployment inputs,
    never values accepted from an HTTP client or the installed plugin.
    """
    if not hasattr(os, "O_NOFOLLOW") or os.open not in os.supports_dir_fd:
        return PayloadResult(
            status="unverifiable", reason="unsupported_verifier_platform"
        )
    expected = {(file.root, file.path): file for file in manifest.files}
    seen: set[tuple[str, str]] = set()
    checked = 0
    visited = 0
    root_fds: dict[str, int] = {}

    def inspect(parent: int, name: str, root: str, path: str, depth: int = 0):
        nonlocal checked, visited
        visited += 1
        if visited > MAX_ENTRIES * 2 or depth > 32:
            raise _Refused("verification_limit")
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if stat.S_ISLNK(info.st_mode):
            raise _Refused("symlink_not_supported")
        if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
            raise _Refused("special_file_not_supported")
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        if stat.S_ISDIR(info.st_mode):
            flags |= os.O_DIRECTORY
        fd = os.open(name, flags, dir_fd=parent)
        try:
            before = os.fstat(fd)
            if _stamp(info) != _stamp(before):
                raise _Refused("installation_changed")
            _safe_permissions(before, owner_uid)
            if stat.S_ISDIR(before.st_mode):
                names = os.listdir(fd)
                if len(names) > MAX_ENTRIES * 2:
                    raise _Refused("verification_limit")
                for child in names:
                    inspect(fd, child, root, path + "/" + child, depth + 1)
            else:
                record = expected.get((root, path))
                if record is None:
                    raise _Refused("unexpected_file", mismatch=True)
                if before.st_nlink != 1:
                    raise _Refused("hardlink_not_supported")
                if before.st_size != record.size_bytes:
                    raise _Refused("file_size_mismatch", mismatch=True)
                digest = hashlib.sha256()
                remaining = record.size_bytes
                while remaining:
                    chunk = os.read(fd, min(1024 * 1024, remaining))
                    if not chunk:
                        raise _Refused("installation_changed")
                    digest.update(chunk)
                    remaining -= len(chunk)
                if os.read(fd, 1):
                    raise _Refused("installation_changed")
                if digest.hexdigest() != record.sha256:
                    raise _Refused("file_digest_mismatch", mismatch=True)
                seen.add((root, path))
                checked += 1
            if _stamp(before) != _stamp(os.fstat(fd)):
                raise _Refused("installation_changed")
        finally:
            os.close(fd)

    try:
        for root in {scope.root for scope in manifest.scopes}:
            if root not in roots:
                raise _Refused("missing_verification_root")
            root_fds[root] = _open_directory(roots[root])
            _safe_permissions(os.fstat(root_fds[root]), owner_uid)
        for scope in manifest.scopes:
            parent = os.dup(root_fds[scope.root])
            try:
                parts = PurePosixPath(scope.path).parts
                for part in parts[:-1]:
                    child = os.open(
                        part,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                        dir_fd=parent,
                    )
                    os.close(parent)
                    parent = child
                    _safe_permissions(os.fstat(parent), owner_uid)
                info = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
                if scope.kind == "directory" and not stat.S_ISDIR(info.st_mode):
                    raise _Refused("scope_type_mismatch", mismatch=True)
                if scope.kind == "file" and not stat.S_ISREG(info.st_mode):
                    raise _Refused("scope_type_mismatch", mismatch=True)
                inspect(parent, parts[-1], scope.root, scope.path)
            finally:
                os.close(parent)
        if seen != set(expected):
            raise _Refused("missing_file", mismatch=True)
        return PayloadResult(
            status="matched", reason="payload_matched", checked_files=checked
        )
    except _Refused as error:
        return PayloadResult(
            status="mismatch" if error.mismatch else "unverifiable",
            reason=error.reason,
            checked_files=checked,
        )
    except FileNotFoundError:
        return PayloadResult(
            status="mismatch", reason="missing_file", checked_files=checked
        )
    except (OSError, ValueError):
        return PayloadResult(
            status="unverifiable",
            reason="unsafe_or_unreadable_path",
            checked_files=checked,
        )
    finally:
        for fd in root_fds.values():
            os.close(fd)
