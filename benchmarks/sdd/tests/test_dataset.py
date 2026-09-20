"""Preparation: the identity the benchmark assumes, and the tags it removes."""



from sddbench import dataset


def test_track_id_matches_the_one_the_server_will_mint():
    from kalinka_plugin_localfiles.utils.id_generator import generate_track_id

    path = "/library/0123456789abcdef.mp3"
    assert dataset.kalinka_track_id(path) == generate_track_id(path)


def test_opaque_name_is_stable_and_says_nothing():
    name = dataset.opaque_name("1004034")
    assert name == dataset.opaque_name("1004034")
    assert "1004034" not in name
    assert name.endswith(".mp3") and len(name) == 40 + 4


def _id3v2(size: int, footer: bool = False) -> bytes:
    flags = 0x10 if footer else 0
    syncsafe = bytes(
        ((size >> 21) & 0x7F, (size >> 14) & 0x7F, (size >> 7) & 0x7F, size & 0x7F)
    )
    return b"ID3" + b"\x04\x00" + bytes([flags]) + syncsafe


def test_id3v2_length_reads_the_syncsafe_size():
    assert dataset._id3v2_length(_id3v2(5824)) == 10 + 5824
    assert dataset._id3v2_length(_id3v2(100, footer=True)) == 10 + 100 + 10


def test_strip_containers_removes_every_tag_byte(tmp_path):
    audio = b"\xff\xfb" + b"audio-frames" * 10
    path = tmp_path / "t.mp3"
    path.write_bytes(_id3v2(16) + b"\x00" * 16 + audio + b"TAG" + b"\x00" * 125)
    dataset._strip_containers(path)
    assert path.read_bytes() == audio


def test_strip_containers_leaves_an_untagged_file_alone(tmp_path):
    audio = b"\xff\xfb" + b"frames" * 40
    path = tmp_path / "t.mp3"
    path.write_bytes(audio)
    dataset._strip_containers(path)
    assert path.read_bytes() == audio


def test_caption_stats_describe_the_set():
    captions = [
        dataset.Caption("1", "t1", "a quiet piano piece", True),
        dataset.Caption("2", "t1", "piano, soft, slow", False),
        dataset.Caption("3", "t2", "loud drum’n bass", True),
    ]
    stats = dataset.caption_stats(captions)
    assert stats["captions"] == 3
    assert stats["tracks"] == 2
    assert stats["captions_per_track"] == {1: 1, 2: 1}
    assert stats["valid_subset_captions"] == 2
    assert stats["non_ascii_captions"] == 1


def test_strip_containers_handles_a_file_shorter_than_a_trailer(tmp_path):
    path = tmp_path / "t.mp3"
    path.write_bytes(b"\xff\xfb short")
    dataset._strip_containers(path)
    assert path.read_bytes() == b"\xff\xfb short"
