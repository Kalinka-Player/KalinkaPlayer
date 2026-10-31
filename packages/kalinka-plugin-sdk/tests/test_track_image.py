from kalinka_plugin_sdk.datamodel import (
    Album,
    CoverImage,
    EntityId,
    EntityType,
    Track,
)


def _track(album_image=None, image=None):
    return Track(
        id=EntityId(id="song", type=EntityType.TRACK, source="test"),
        title="Stagger Lee",
        duration=315,
        album=Album(
            id=EntityId(id="album", type=EntityType.ALBUM, source="test"),
            title="Murder Ballads",
            image=album_image,
        ),
        image=image,
    )


def test_a_track_from_an_older_module_has_no_image_of_its_own():
    stored = _track().model_dump_json(exclude={"image"})
    assert Track.model_validate_json(stored).image is None


def test_a_tracks_own_cover_survives_a_round_trip_apart_from_its_albums():
    track = _track(CoverImage(small="/album.jpg"), CoverImage(small="/track.jpg"))

    again = Track.model_validate_json(track.model_dump_json())

    assert again.image.small == "/track.jpg"
    assert again.album.image.small == "/album.jpg"
