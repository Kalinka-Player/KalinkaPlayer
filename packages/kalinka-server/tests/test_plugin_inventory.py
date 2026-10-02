"""Names never confer catalog identity, including for manual installations."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from importlib.metadata import EntryPoint as MetadataEntryPoint
import json

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from kalinka_server.plugin_management.inventory import (
    AuthenticatedCatalog,
    CatalogPlugin,
    Discovery,
    EntryPoint,
    Installation,
    PluginInventory,
    ReleaseIdentity,
    WorkerEvidence,
    discover_plugins,
    reconcile,
)
from kalinka_server.plugin_management.route import register_plugin_routes
from kalinka_server.version import get_rest_api_version

NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)


@pytest.fixture
def identity():
    installation = Installation(
        "kalinka-plugin-demo",
        "1.0.0",
        (EntryPoint("kalinka_plugin_demo", "demo:Plugin"),),
        "/trusted/site-packages",
        origin="manual",
    )
    release = ReleaseIdentity("1.0.0", "a" * 64, "b" * 64)
    plugin = CatalogPlugin(
        "demo", "kalinka-plugin-demo", "kalinka_plugin_demo", "independent", (release,)
    )
    catalog = AuthenticatedCatalog(
        "kalinka", "c" * 40, NOW + timedelta(hours=1), (plugin,)
    )
    proof = WorkerEvidence(
        installation.installation_id,
        "kalinka",
        catalog.revision,
        "demo",
        "1.0.0",
        "demo:Plugin",
        "a" * 64,
        "b" * 64,
        NOW,
        NOW + timedelta(minutes=1),
    )
    return installation, catalog, proof


def row(installation, catalog, evidence=(), *, complete=True):
    return reconcile(Discovery((installation,), complete), catalog, evidence, now=NOW)[
        "plugins"
    ][0]


def test_manual_genuine_plugin_is_bound_without_reinstall(identity):
    installation, catalog, proof = identity
    result = row(installation, catalog, (proof,))
    assert result["origin"] == "manual"
    assert result["catalog_match"] == "verified"
    assert result["management"] == "managed"
    assert result["catalog_binding"]["plugin_id"] == "demo"
    assert result["update_check_eligible"] is True
    # Identity is not permission to install, nor opt-in to automatic updates.
    assert result["update_policy"] == "notify"
    assert result["automatic_update_allowed"] is False


def test_names_and_versions_without_worker_proof_are_not_enough(identity):
    installation, catalog, _ = identity
    result = row(installation, catalog)
    assert result["catalog_binding"] is None
    assert result["verification"]["reason"] == "independent_verification_required"
    assert result["update_check_eligible"] is False


@pytest.mark.parametrize(
    "field,value",
    [
        ("catalog_id", "other"),
        ("catalog_revision", "d" * 40),
        ("plugin_id", "other"),
        ("version", "2.0.0"),
        ("entry_point_value", "impostor:Plugin"),
        ("artifact_sha256", "d" * 64),
        ("payload_sha256", "e" * 64),
        ("installation_id", "f" * 64),
    ],
)
def test_proof_cannot_be_reused_for_another_identity(identity, field, value):
    installation, catalog, proof = identity
    result = row(installation, catalog, (replace(proof, **{field: value}),))
    assert result["catalog_match"] != "verified"
    assert result["update_check_eligible"] is False


@pytest.mark.parametrize(
    "change",
    [
        {"expires_at": NOW},
        {"verified_at": NOW + timedelta(seconds=1)},
        {"expires_at": NOW + timedelta(hours=1)},
        {"verified_at": NOW.replace(tzinfo=None)},
        {"expires_at": NOW.replace(tzinfo=None)},
    ],
)
def test_expired_future_naive_or_unbounded_proof_is_rejected(identity, change):
    installation, catalog, proof = identity
    assert (
        row(installation, catalog, (replace(proof, **change),))["catalog_match"]
        != "verified"
    )


def test_expired_catalog_cannot_use_fresh_receipt(identity):
    installation, catalog, proof = identity
    result = row(installation, replace(catalog, expires_at=NOW), (proof,))
    assert result["verification"]["reason"] == "catalog_expired"
    assert result["update_check_eligible"] is False


def test_payload_mismatch_suspends_identity(identity):
    installation, catalog, proof = identity
    result = row(installation, catalog, (replace(proof, status="modified"),))
    assert result["catalog_match"] == "modified"
    assert result["catalog_binding"] is None
    assert result["update_check_eligible"] is False


def test_other_release_can_be_recognised_after_manual_upgrade(identity):
    installation, catalog, proof = identity
    upgraded = replace(installation, version="1.1.0")
    assert (
        row(upgraded, catalog, (proof,))["verification"]["reason"]
        == "release_not_in_catalog"
    )
    new_release = ReleaseIdentity("1.1.0", "d" * 64, "e" * 64)
    catalog = replace(
        catalog, plugins=(replace(catalog.plugins[0], releases=(new_release,)),)
    )
    proof = replace(
        proof,
        installation_id=upgraded.installation_id,
        version="1.1.0",
        artifact_sha256=new_release.artifact_sha256,
        payload_sha256=new_release.payload_sha256,
    )
    assert row(upgraded, catalog, (proof,))["catalog_match"] == "verified"


@pytest.mark.parametrize(
    "change",
    [{"origin": "editable"}, {"metadata_error": True}, {"version": "not-a-version"}],
)
def test_bad_or_editable_installs_are_not_promoted(identity, change):
    installation, catalog, proof = identity
    assert (
        row(replace(installation, **change), catalog, (proof,))["catalog_match"]
        != "verified"
    )


def test_unregistered_plugin_is_not_promoted(identity):
    installation, catalog, proof = identity
    result = row(installation, replace(catalog, plugins=()), (proof,))
    assert result["catalog_match"] == "unregistered"
    assert result["update_check_eligible"] is False


def test_incomplete_discovery_blocks_verification(identity):
    installation, catalog, proof = identity
    result = row(installation, catalog, (proof,), complete=False)
    assert result["verification"]["reason"] == "discovery_incomplete"


def test_missing_manifest_blocks_verification(identity):
    installation, catalog, proof = identity
    plugin = replace(
        catalog.plugins[0], releases=(ReleaseIdentity("1.0.0", "a" * 64, None),)
    )
    result = row(installation, replace(catalog, plugins=(plugin,)), (proof,))
    assert result["verification"]["reason"] == "payload_manifest_missing"


def test_bundle_identity_does_not_grant_independent_updates(identity):
    installation, catalog, proof = identity
    catalog = replace(
        catalog, plugins=(replace(catalog.plugins[0], delivery="bundle"),)
    )
    result = row(installation, catalog, (proof,))
    assert result["catalog_match"] == "verified"
    assert result["management"] == "bundle"
    assert result["update_check_eligible"] is False


@pytest.mark.parametrize("same_distribution", [True, False])
def test_duplicate_distribution_or_entry_point_blocks_matching(
    identity, same_distribution
):
    installation, catalog, proof = identity
    duplicate = replace(
        installation,
        location="/another/location",
        distribution=installation.distribution if same_distribution else "impostor",
    )
    result = reconcile(
        Discovery((installation, duplicate), True), catalog, (proof,), now=NOW
    )
    assert all(p["catalog_match"] == "ambiguous" for p in result["plugins"])


class FakeDistribution:
    metadata = {"Name": "Kalinka_Plugin_Demo"}
    version = "1.0.0"
    entry_points = [
        MetadataEntryPoint(
            name="kalinka_plugin_demo",
            value="does_not_exist:Plugin",
            group="kalinka.plugins",
        )
    ]

    def read_text(self, name):
        return None

    def locate_file(self, name):
        return "/private/secret/location"


def test_discovery_never_loads_plugin_code(monkeypatch):
    def forbidden(*args):
        pytest.fail("Plugin code must not be imported")

    monkeypatch.setattr(MetadataEntryPoint, "load", forbidden)
    discovery = discover_plugins([FakeDistribution()])
    assert discovery.complete
    assert discovery.installations[0].distribution == "kalinka-plugin-demo"
    assert discovery.installations[0].origin == "unknown"
    result = PluginInventory(lambda: discovery).snapshot()
    assert "/private/secret" not in json.dumps(result)
    assert result["plugins"][0]["catalog_match"] == "unverified"


def test_broken_metadata_marks_discovery_incomplete():
    class Broken:
        @property
        def entry_points(self):
            raise ValueError("broken")

    discovery = discover_plugins([Broken(), FakeDistribution()])
    assert not discovery.complete
    assert len(discovery.installations) == 1


def test_editable_and_malformed_direct_url_are_only_untrusted_hints():
    class Editable(FakeDistribution):
        def read_text(self, name):
            return '{"url":"https://user:secret@host/", "dir_info":{"editable":true}}'

    discovery = discover_plugins([Editable()])
    assert discovery.installations[0].origin == "editable"
    assert "secret@host" not in json.dumps(
        PluginInventory(lambda: discovery).snapshot()
    )

    class Malformed(FakeDistribution):
        def read_text(self, name):
            return "not-json"

    assert discover_plugins([Malformed()]).installations[0].metadata_error


def test_read_only_routes_fail_closed_without_trust():
    app = FastAPI()
    register_plugin_routes(
        app,
        PluginInventory(lambda: discover_plugins([FakeDistribution()])),
        enabled=lambda: True,
    )
    client = TestClient(app)
    response = client.get("/server/plugins")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    inventory = response.json()
    assert inventory["plugins"][0]["catalog_binding"] is None
    assert inventory["capabilities"]["installation"] is False
    assert client.get("/server/plugins/catalog").status_code == 503
    updates = client.get("/server/plugins/updates").json()
    assert updates["status"] == "unavailable"
    assert updates["updates"] == []
    for path in (
        "/server/plugins",
        "/server/plugins/plans",
        "/server/plugins/operations",
    ):
        assert client.post(path, json={"verified": True}).status_code in {404, 405}
    assert get_rest_api_version() == "0.9"
