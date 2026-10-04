"""A valid public catalog: one independent plugin with one Debian release."""


def catalog_document() -> dict:
    return {
        "schema_version": 1,
        "catalog_id": "kalinka",
        "revision": "test-public-revision",
        "plugins": [
            {
                "schema_version": 1,
                "id": "demo",
                "name": "Demo input",
                "description": "Example input source",
                "distribution": "kalinka-plugin-demo",
                "entry_point": "kalinka_plugin_demo",
                "creator": {"name": "Creator", "url": "https://example.org/creator"},
                "maintainers": [
                    {"name": "Maintainer", "url": "https://example.org/team"}
                ],
                "license": "MIT",
                "source": {
                    "repository": "https://example.org/source",
                    "subdirectory": "",
                },
                "type": "input_module",
                "tier": "unofficial",
                "maturity": "experimental",
                "categories": ["radio"],
                "delivery": "independent",
                "releases": [
                    {
                        "version": "1.0.0",
                        "channel": "stable",
                        "published_at": "2026-10-01T00:00:00Z",
                        "source_tag": "v1.0.0",
                        "source_commit": "c" * 40,
                        "release_notes": "https://example.org/releases/v1.0.0",
                        "requires": {
                            "server": ">=5.3,<6",
                            "sdk": ">=3.5,<4",
                            "python": ">=3.11",
                            "platforms": ["linux"],
                            "architectures": ["all"],
                            "capabilities": [],
                            "notes": [],
                        },
                        "data_rollback": "manual",
                        "withdrawn": False,
                        "artifacts": [
                            {
                                "format": "deb",
                                "filename": "kalinka-plugin-demo_1.0.0_all.deb",
                                "platform": "linux",
                                "architectures": ["all"],
                                "package": {
                                    "name": "kalinka-plugin-demo",
                                    "version": "1.0.0",
                                    "architecture": "all",
                                },
                                "targets": [{"id": "debian", "versions": ["13"]}],
                                "url": "https://example.org/releases/v1.0.0/kalinka-plugin-demo_1.0.0_all.deb",
                                "sha256": "a" * 64,
                                "size_bytes": 1024,
                            }
                        ],
                    }
                ],
            }
        ],
    }
