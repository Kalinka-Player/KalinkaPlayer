"""Import-free discovery and fail-closed catalog identity reconciliation.

AuthenticatedCatalog and WorkerEvidence are internal trust-boundary inputs, NOT
HTTP request models. A future catalog/worker adapter must authenticate them and
check current installed bytes. Constructing either object is not authentication.
No production adapter is enabled yet; the default service always fails closed.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
from importlib import metadata
import json
from typing import Callable, Iterable, Literal

from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

MAX_DISTRIBUTIONS = 10000
MAX_METADATA_TEXT = 65536


@dataclass(frozen=True)
class EntryPoint:
    name: str
    value: str


@dataclass(frozen=True)
class Installation:
    distribution: str
    version: str
    entry_points: tuple[EntryPoint, ...]
    location: str = field(repr=False)
    origin: Literal["manual", "catalog", "bundle", "editable", "unknown"] = "unknown"
    metadata_error: bool = False

    @property
    def installation_id(self) -> str:
        # A lookup fingerprint, NOT a digest of installed code or proof of trust.
        data = [
            self.distribution,
            self.version,
            self.location,
            sorted((ep.name, ep.value) for ep in self.entry_points),
        ]
        return hashlib.sha256(
            json.dumps(data, separators=(",", ":")).encode()
        ).hexdigest()


@dataclass(frozen=True)
class Discovery:
    installations: tuple[Installation, ...]
    complete: bool


def discover_plugins(distributions: Iterable | None = None) -> Discovery:
    """Read metadata only: no EntryPoint.load(), plugin imports or setup calls.

    Metadata and locations are untrusted hints. Local paths, direct URLs and
    credentials never leave the service's public projection.
    """
    found = []
    complete = True
    try:
        for index, dist in enumerate(
            metadata.distributions() if distributions is None else distributions
        ):
            if index >= MAX_DISTRIBUTIONS:
                complete = False
                break
            try:
                eps = tuple(
                    EntryPoint(ep.name, ep.value)
                    for ep in dist.entry_points
                    if ep.group == "kalinka.plugins"
                )
                if not eps:
                    continue
                error = False
                name = dist.metadata.get("Name", "")
                version = dist.version or ""
                if not name or not version or len(name) > 200 or len(version) > 100:
                    error = True
                if len(eps) > 100 or any(
                    len(ep.name) > 200 or len(ep.value) > 500 for ep in eps
                ):
                    complete = False
                    continue
                origin = "unknown"
                try:
                    direct = dist.read_text("direct_url.json")
                    if direct:
                        if len(direct) > MAX_METADATA_TEXT:
                            raise ValueError("metadata limit")
                        direct_record = json.loads(direct)
                        if direct_record.get("dir_info", {}).get("editable") is True:
                            origin = "editable"
                except (ValueError, AttributeError, TypeError, OSError):
                    error = True
                found.append(
                    Installation(
                        distribution=canonicalize_name(name[:200]),
                        version=version[:100],
                        entry_points=eps,
                        location=str(dist.locate_file("")),
                        origin=origin,
                        metadata_error=error,
                    )
                )
            except Exception:
                # Broken distributions must not turn a partial scan into a
                # confident identity decision for the remaining distributions.
                complete = False
    except Exception:
        complete = False
    return Discovery(
        tuple(
            sorted(found, key=lambda item: (item.distribution, item.installation_id))
        ),
        complete,
    )


@dataclass(frozen=True)
class ReleaseIdentity:
    version: str
    artifact_sha256: str
    payload_sha256: str | None


@dataclass(frozen=True)
class CatalogPlugin:
    plugin_id: str
    distribution: str
    entry_point: str
    delivery: Literal["bundle", "independent"]
    releases: tuple[ReleaseIdentity, ...]


@dataclass(frozen=True)
class AuthenticatedCatalog:
    """Output of a future authenticated catalog adapter, never raw JSON."""

    catalog_id: str
    revision: str
    expires_at: datetime
    plugins: tuple[CatalogPlugin, ...]


@dataclass(frozen=True)
class WorkerEvidence:
    """Fresh result of independent worker checks, including native ownership
    and runtime resolution. A PayloadResult alone cannot produce this result.
    """

    installation_id: str
    catalog_id: str
    catalog_revision: str
    plugin_id: str
    version: str
    entry_point_value: str
    artifact_sha256: str
    payload_sha256: str
    verified_at: datetime
    expires_at: datetime
    status: Literal["verified", "modified", "unverified"] = "verified"


def _fresh(expires_at: datetime, now: datetime) -> bool:
    return expires_at.tzinfo is not None and expires_at > now


def reconcile(
    discovery: Discovery,
    catalog: AuthenticatedCatalog | None,
    evidence: tuple[WorkerEvidence, ...] = (),
    *,
    now: datetime | None = None,
) -> dict:
    """Classify identities; no network, persistence, imports or install action.

    Update-check eligibility is separate from automatic installation permission.
    Even with a verified identity this implementation enables no mutations.
    """
    now = now or datetime.now(timezone.utc)
    fresh = catalog is not None and _fresh(catalog.expires_at, now)
    status = "authenticated" if fresh else ("expired" if catalog else "unavailable")
    name_counts = Counter(item.distribution for item in discovery.installations)
    ep_counts = Counter(
        ep.name for item in discovery.installations for ep in item.entry_points
    )
    rows = []
    for item in discovery.installations:
        row = {
            "installation_id": item.installation_id,
            "distribution": item.distribution,
            "version": item.version,
            "entry_points": [
                {"name": ep.name, "value": ep.value} for ep in item.entry_points
            ],
            "origin": item.origin,
            "catalog_match": "unverified",
            "catalog_binding": None,
            "management": "unmanaged",
            "verification": {"status": "unverified", "reason": "catalog_unavailable"},
            "update_policy": "notify",
            "update_check_eligible": False,
            "automatic_update_allowed": False,
            "automatic_update_blockers": ["installer_unavailable"],
        }

        def refuse(reason: str, match: str = "unverified") -> None:
            row["catalog_match"] = match
            row["verification"] = {"status": match, "reason": reason}

        if not discovery.complete:
            refuse("discovery_incomplete")
        elif item.metadata_error:
            refuse("invalid_installed_metadata")
        elif name_counts[item.distribution] != 1 or any(
            ep_counts[ep.name] != 1 for ep in item.entry_points
        ):
            refuse("duplicate_installed_identity", "ambiguous")
        elif item.origin == "editable":
            refuse("editable_installation")
        elif not fresh:
            refuse("catalog_expired" if catalog else "catalog_unavailable")
        else:
            assert catalog is not None
            candidates = [
                entry
                for entry in catalog.plugins
                if canonicalize_name(entry.distribution) == item.distribution
            ]
            if not candidates:
                refuse("not_in_catalog", "unregistered")
            elif len(candidates) != 1:
                refuse("ambiguous_catalog_identity", "ambiguous")
            else:
                candidate = candidates[0]
                eps = [
                    ep for ep in item.entry_points if ep.name == candidate.entry_point
                ]
                try:
                    version = Version(item.version)
                    releases = [
                        release
                        for release in candidate.releases
                        if Version(release.version) == version
                    ]
                except InvalidVersion:
                    releases = []
                if len(eps) != 1 or len(item.entry_points) != 1:
                    refuse("entry_point_mismatch")
                elif not releases:
                    refuse("release_not_in_catalog")
                elif not any(release.payload_sha256 for release in releases):
                    refuse("payload_manifest_missing")
                else:
                    proofs = [
                        proof
                        for proof in evidence
                        if proof.installation_id == item.installation_id
                    ]
                    if len(proofs) != 1:
                        refuse("independent_verification_required")
                    else:
                        proof = proofs[0]
                        identity_matches = (
                            proof.catalog_id == catalog.catalog_id
                            and proof.catalog_revision == catalog.revision
                            and proof.plugin_id == candidate.plugin_id
                            and proof.version == item.version
                            and proof.entry_point_value == eps[0].value
                            and any(
                                release.artifact_sha256 == proof.artifact_sha256
                                and release.payload_sha256 == proof.payload_sha256
                                for release in releases
                            )
                        )
                        if not identity_matches:
                            refuse("verification_identity_mismatch")
                        elif (
                            not _fresh(proof.expires_at, now)
                            or proof.verified_at.tzinfo is None
                            or not proof.verified_at <= now < proof.expires_at
                            or (proof.expires_at - proof.verified_at).total_seconds()
                            > 300
                        ):
                            refuse("verification_expired")
                        elif proof.status != "verified":
                            refuse(
                                (
                                    "installed_payload_changed"
                                    if proof.status == "modified"
                                    else "independent_verification_required"
                                ),
                                proof.status,
                            )
                        else:
                            row.update(
                                {
                                    "catalog_match": "verified",
                                    "catalog_binding": {
                                        "catalog_id": catalog.catalog_id,
                                        "plugin_id": candidate.plugin_id,
                                        "catalog_revision": catalog.revision,
                                        "artifact_sha256": proof.artifact_sha256,
                                        "payload_sha256": proof.payload_sha256,
                                    },
                                    "management": (
                                        "bundle"
                                        if candidate.delivery == "bundle"
                                        else "managed"
                                    ),
                                    "verification": {
                                        "status": "verified",
                                        "reason": "verified_installed_payload",
                                        "verified_at": proof.verified_at.isoformat(),
                                    },
                                    "update_check_eligible": candidate.delivery
                                    == "independent",
                                }
                            )
        rows.append(row)
    return {
        "catalog": {
            "status": status,
            "reason": (
                None
                if fresh
                else (
                    "catalog_expired"
                    if catalog
                    else "signature_verification_not_configured"
                )
            ),
        },
        "discovery_complete": discovery.complete,
        "plugins": rows,
    }


CAPABILITIES = {
    "inventory": True,
    "catalog_browsing": True,
    "catalog_verification": False,
    "independent_verification": False,
    "update_checks": False,
    "installation": False,
    "automatic_updates": False,
}


class PluginInventory:
    """Production read-only service. No untrusted catalog/evidence input exists.

    Future adapters can call reconcile after catalog signature and worker
    verification. PublicCatalog is a separate browsing cache: its unsigned data
    never creates bindings here. Neither can a writable JSON receipt or a name.
    """

    def __init__(self, discover: Callable[[], Discovery] = discover_plugins):
        self._discover = discover

    def snapshot(self) -> dict:
        result = reconcile(self._discover(), None)
        result["capabilities"] = dict(CAPABILITIES)
        return result
