"""Bundled category artwork served through the module's resource endpoint."""

from pathlib import Path

from kalinka_plugin_sdk.datamodel import CoverImage

ART_DIRECTORY = Path(__file__).with_name("catalog_artwork")
ART_CATEGORIES = ("files", "albums", "artists", "tracks", "recent", "playlists")
ART_PREFIX = "catalog-art/"
_VERSIONS = {category: 2 if category == "artists" else 1 for category in ART_CATEGORIES}
_FILES = {
    f"{category}-v{version}-{size}.jpg"
    for category, current in _VERSIONS.items()
    for version in range(1, current + 1)
    for size in ("thumbnail", "small", "large")
}


def catalog_image(category: str) -> CoverImage:
    """Versioned artwork URLs, independent of the library and its cover cache."""
    if category not in ART_CATEGORIES:
        raise ValueError(f"Unknown catalog artwork: {category}")
    base = f"/resource/{ART_PREFIX}{category}-v{_VERSIONS[category]}"
    return CoverImage(
        thumbnail=f"{base}-thumbnail.jpg",
        small=f"{base}-small.jpg",
        large=f"{base}-large.jpg",
        aspect_ratio=3.0,
    )


def catalog_art_path(resource: str) -> str | None:
    """Resolve only public artwork files, never other package resources."""
    if not resource.startswith(ART_PREFIX):
        return None
    filename = resource.removeprefix(ART_PREFIX)
    if filename not in _FILES:
        return None
    path = ART_DIRECTORY / filename
    return str(path) if path.is_file() else None
