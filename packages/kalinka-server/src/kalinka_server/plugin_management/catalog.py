"""Anonymous, bounded public-catalog browsing. This module grants no trust.

The bundled schema is copied from the hub's schemas/plugin.schema.json (v1).
Never load a schema, plugin code, artifact, or verification key from this feed.
In particular, validated JSON is NOT an inventory.AuthenticatedCatalog.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
from importlib.resources import files
import json
import os
import random
import time
from typing import Callable
from urllib.parse import unquote, urlsplit

import httpx
from jsonschema import Draft202012Validator, FormatChecker, ValidationError
from packaging.specifiers import SpecifierSet
from packaging.version import Version

CATALOG_ID = "kalinka"
DEFAULT_BASE_URL = (
    "https://raw.githubusercontent.com/Kalinka-Player/kalinka-plugins/main/"
)
BASE_URL_ENV = "KALINKA_PLUGIN_CATALOG_BASE_URL"
MAX_CATALOG_BYTES = 2 * 1024 * 1024
REFRESH_SECONDS = 3600
STALE_SECONDS = 2 * REFRESH_SECONDS
REQUEST_DEADLINE_SECONDS = 30
FLAG_POLL_SECONDS = 5

_SCHEMA = json.loads(files(__package__).joinpath("plugin.schema.json").read_text())
_VALIDATOR = Draft202012Validator(_SCHEMA, format_checker=FormatChecker())


class CatalogError(ValueError):
    """A safe, fixed error code; never expose upstream bodies or exceptions."""


def _https_url(value: str) -> None:
    parts = urlsplit(value)
    if (
        len(value) > 2048
        or parts.scheme != "https"
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.port not in (None, 443)
        or parts.query
        or parts.fragment
        or "\\" in value
        or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value)
        or any(part in {".", ".."} for part in unquote(parts.path).split("/"))
    ):
        raise ValueError("Invalid public HTTPS URL")


def catalog_url(base_url: str) -> str:
    """Only deployment configuration chooses the origin; HTTP clients cannot."""
    _https_url(base_url)
    url = base_url.rstrip("/") + "/catalog.json"
    _https_url(url)
    return url


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise CatalogError("invalid_catalog")
        result[key] = value
    return result


def _reject_constant(value):
    raise CatalogError("invalid_catalog")


def _check_tree(value, depth=0):
    if depth > 32:
        raise CatalogError("invalid_catalog")
    if isinstance(value, dict):
        for key, child in value.items():
            if len(key) > 200:
                raise CatalogError("invalid_catalog")
            _check_tree(child, depth + 1)
    elif isinstance(value, list):
        if len(value) > 1000:
            raise CatalogError("invalid_catalog")
        for child in value:
            _check_tree(child, depth + 1)
    elif isinstance(value, str):
        if len(value) > 4096:
            raise CatalogError("invalid_catalog")
        # JSON accepts escaped lone surrogates, but the REST UTF-8 encoder does
        # not. Refuse them before replacing a cache that clients can display.
        value.encode("utf-8")


def _check_urls(value):
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"url", "repository", "release_notes"}:
                _https_url(child)
            else:
                _check_urls(child)
    elif isinstance(value, list):
        for child in value:
            _check_urls(child)


def _check_all(values):
    if "all" in values and len(values) != 1:
        raise CatalogError("invalid_catalog")


def parse_public_catalog(data: bytes) -> dict:
    """Validate a bounded v1 display document, not executable install inputs.

    Full artifact/header consistency and host compatibility belong to the
    publication/installer adapters. The public API reports those as unevaluated.
    """
    if len(data) > MAX_CATALOG_BYTES:
        raise CatalogError("response_too_large")
    try:
        catalog = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_unique_pairs,
            parse_constant=_reject_constant,
        )
        _check_tree(catalog)
        if not isinstance(catalog, dict) or set(catalog) != {
            "schema_version",
            "catalog_id",
            "revision",
            "plugins",
        }:
            raise CatalogError("invalid_catalog")
        if type(catalog["schema_version"]) is not int or catalog["schema_version"] != 1:
            raise CatalogError("unsupported_schema_version")
        if catalog["catalog_id"] != CATALOG_ID:
            raise CatalogError("unexpected_catalog_id")
        revision = catalog["revision"]
        if (
            not isinstance(revision, str)
            or not 1 <= len(revision) <= 200
            or not revision.isprintable()
        ):
            raise CatalogError("invalid_catalog")
        plugins = catalog["plugins"]
        if not isinstance(plugins, list) or not 1 <= len(plugins) <= 256:
            raise CatalogError("invalid_catalog")
        identities = {key: set() for key in ("id", "distribution", "entry_point")}
        for plugin in plugins:
            _VALIDATOR.validate(plugin)
            if type(plugin["schema_version"]) is not int:
                raise CatalogError("invalid_catalog")
            _check_urls(plugin)
            for key, known in identities.items():
                if plugin[key] in known:
                    raise CatalogError("invalid_catalog")
                known.add(plugin[key])
            versions = set()
            if len(plugin["releases"]) > 200:
                raise CatalogError("invalid_catalog")
            for release in plugin["releases"]:
                # jsonschema's date-time checker has optional dependencies;
                # require a parseable, timezone-aware timestamp independently.
                if datetime.fromisoformat(release["published_at"]).tzinfo is None:
                    raise CatalogError("invalid_catalog")
                version = Version(release["version"])
                if (
                    version in versions
                    or version.local
                    or (release["channel"] == "stable" and version.is_prerelease)
                ):
                    raise CatalogError("invalid_catalog")
                versions.add(version)
                requires = release["requires"]
                for component in ("server", "sdk", "python", "renderer"):
                    if component in requires and not str(
                        SpecifierSet(requires[component])
                    ):
                        raise CatalogError("invalid_catalog")
                if "rollback_versions" in release and not str(
                    SpecifierSet(release["rollback_versions"])
                ):
                    raise CatalogError("invalid_catalog")
                _check_all(requires["architectures"])
                _check_all(requires["platforms"])
                for artifact in release["artifacts"]:
                    _check_all(artifact["architectures"])
        return catalog
    except CatalogError:
        raise
    except (ValueError, TypeError, KeyError, RecursionError, ValidationError):
        raise CatalogError("invalid_catalog") from None


def _safe_header(value: str | None) -> str | None:
    if value and len(value) <= 512 and all(32 <= ord(char) < 127 for char in value):
        return value
    return None


class PublicCatalog:
    """Lifecycle-owned in-memory cache, separate from verified installed state.

    No credentials, cookies, redirect following, or URLs supplied by API callers.
    A new client per hourly request avoids retaining upstream cookies. Deployment
    configuration is trusted; only its explicit HTTPS origin is contacted.
    """

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        monotonic: Callable[[], float] = time.monotonic,
        enabled: Callable[[], bool] = lambda: True,
    ):
        self._enabled = enabled
        self._url = None
        self._disabled = not base_url
        self._configuration_error = None
        if base_url:
            try:
                self._url = catalog_url(base_url)
            except ValueError:
                self._configuration_error = "invalid_configuration"
        self._transport = transport
        self._clock = clock
        self._monotonic = monotonic
        self._lock = asyncio.Lock()
        self._document = None
        self._digest = None
        self._etag = None
        self._modified = None
        self._last_attempt = None
        self._last_success = None
        self._last_success_tick = None
        self._error = None
        self._generation = 0

    @classmethod
    def from_environment(
        cls, *, enabled: Callable[[], bool] = lambda: True
    ) -> PublicCatalog:
        # Empty is an explicit opt-out. Do not read a GitHub token.
        return cls(os.environ.get(BASE_URL_ENV, DEFAULT_BASE_URL), enabled=enabled)

    async def refresh(self) -> None:
        """Coalesce concurrent refreshes and preserve last valid display data."""
        if self._url is None or not self._enabled():
            return
        generation = self._generation
        async with self._lock:
            if generation != self._generation or not self._enabled():
                return
            self._last_attempt = self._clock().isoformat()
            try:
                async with asyncio.timeout(REQUEST_DEADLINE_SECONDS):
                    document, digest, etag, modified = await self._fetch()
                if not self._enabled():
                    return
                # Publish one validated generation with no intervening await.
                self._document, self._digest = document, digest
                self._etag, self._modified = etag, modified
                self._last_success = self._clock().isoformat()
                self._last_success_tick = self._monotonic()
                self._error = None
            except CatalogError as exc:
                self._error = str(exc)
            except (TimeoutError, httpx.TimeoutException):
                self._error = "timeout"
            except httpx.HTTPError:
                self._error = "network_error"
            finally:
                self._generation += 1

    async def _fetch(self) -> tuple:
        headers = {
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "User-Agent": "Kalinka-Plugin-Catalog/1",
        }
        if self._etag:
            headers["If-None-Match"] = self._etag
        if self._modified:
            headers["If-Modified-Since"] = self._modified
        async with httpx.AsyncClient(
            timeout=10,
            follow_redirects=False,
            trust_env=False,
            transport=self._transport,
        ) as client:
            async with client.stream("GET", self._url, headers=headers) as response:
                if response.status_code == 304:
                    if self._document is None or not (self._etag or self._modified):
                        raise CatalogError("unexpected_not_modified")
                    return (
                        self._document,
                        self._digest,
                        _safe_header(response.headers.get("etag")) or self._etag,
                        _safe_header(response.headers.get("last-modified"))
                        or self._modified,
                    )
                if response.is_redirect:
                    raise CatalogError("redirect_refused")
                if response.status_code != 200:
                    raise CatalogError(f"http_status_{response.status_code}")
                if (
                    response.headers.get("content-encoding", "identity").lower()
                    != "identity"
                ):
                    raise CatalogError("unsupported_encoding")
                content_type = (
                    response.headers.get("content-type", "")
                    .split(";", 1)[0]
                    .strip()
                    .lower()
                )
                # raw.githubusercontent.com serves JSON as text/plain.
                if content_type not in {
                    "application/json",
                    "text/plain",
                    "application/octet-stream",
                }:
                    raise CatalogError("invalid_content_type")
                length = response.headers.get("content-length")
                if length is not None:
                    if not length.isascii() or not length.isdigit() or len(length) > 12:
                        raise CatalogError("invalid_content_length")
                    if int(length) > MAX_CATALOG_BYTES:
                        raise CatalogError("response_too_large")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > MAX_CATALOG_BYTES:
                        raise CatalogError("response_too_large")
                    body.extend(chunk)
                if length is not None and int(length) != len(body):
                    raise CatalogError("invalid_content_length")
                document = parse_public_catalog(bytes(body))
                return (
                    document,
                    hashlib.sha256(body).hexdigest(),
                    _safe_header(response.headers.get("etag")),
                    _safe_header(response.headers.get("last-modified")),
                )

    async def run(self) -> None:
        """Startup refresh, hourly jitter, bounded exponential failure backoff."""
        if self._url is None:
            return
        failures = 0
        next_refresh = 0.0
        was_enabled = False
        while True:
            enabled = self._enabled()
            if enabled and (not was_enabled or self._monotonic() >= next_refresh):
                await self.refresh()
                failures = min(failures + 1, 7) if self._error else 0
                delay = (
                    min(60 * 2 ** (failures - 1), REFRESH_SECONDS)
                    if failures
                    else REFRESH_SECONDS
                )
                next_refresh = self._monotonic() + delay * random.uniform(0.9, 1.1)
            was_enabled = enabled
            # Observe live config writes without contacting the network while
            # disabled or waiting for the hourly refresh window.
            await asyncio.sleep(FLAG_POLL_SECONDS)

    def snapshot(self) -> dict:
        """Never perform network I/O or expose shared mutable cache objects."""
        enabled = self._enabled()
        stale = self._document is not None and (
            self._error is not None
            or self._monotonic() - self._last_success_tick >= STALE_SECONDS
        )
        status = (
            "disabled"
            if self._disabled or not enabled
            else (
                "unavailable"
                if self._document is None
                else "stale" if stale else "available"
            )
        )
        return {
            "status": status,
            "catalog_id": CATALOG_ID,
            "source_url": self._url,
            "revision": self._document["revision"] if self._document else None,
            "content_sha256": self._digest,  # Content fingerprint, not authentication.
            "last_attempt_at": self._last_attempt,
            "last_successful_check": self._last_success,
            "refreshing": self._lock.locked(),
            "error": self._configuration_error or self._error,
            "trust": {
                "status": "unverified",
                "reason": "signature_verification_not_configured",
                "transport": "https",
            },
            "compatibility": {"status": "not_evaluated"},
            "installation_allowed": False,
            "automatic_updates_enabled": False,
            "plugins": (
                deepcopy(self._document["plugins"])
                if self._document and enabled
                else []
            ),
        }
