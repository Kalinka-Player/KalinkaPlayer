"""What a log export says about each HDMI port and the sink plugged into it."""

from kalinka_server.hdmi_ports import describe_edid, hdmi_ports

HDMI_VSDB = (3, b"\x03\x0c\x00\x10\x00")
STEREO_PCM = (1, b"\x09\x7f\x07")


def _base(name, extensions=0):
    block = bytearray(128)
    block[:8] = b"\x00\xff\xff\xff\xff\xff\xff\x00"
    block[54:59] = b"\x00\x00\x00\xfc\x00"
    block[59:72] = (name.encode() + b"\n").ljust(13, b" ")
    block[126] = extensions
    return bytes(block)


def _cta(*data_blocks, basic_audio=False):
    collection = b"".join(
        bytes([tag << 5 | len(payload)]) + payload for tag, payload in data_blocks
    )
    block = bytearray(128)
    block[:4] = bytes([0x02, 3, 4 + len(collection), 0x40 if basic_audio else 0])
    block[4 : 4 + len(collection)] = collection
    return bytes(block)


def test_a_receiver_declaring_hdmi_and_audio():
    edid = _base("RMC-1", 1) + _cta(HDMI_VSDB, STEREO_PCM)

    assert describe_edid(edid) == {"sink": "RMC-1", "hdmi": True, "audio": True}


def test_a_dvi_screen_takes_neither_hdmi_nor_audio():
    assert describe_edid(_base("OLD SCREEN")) == {
        "sink": "OLD SCREEN",
        "hdmi": False,
        "audio": False,
    }


def test_audio_is_declared_by_a_flag_or_by_its_own_block():
    assert describe_edid(_base("TV", 1) + _cta(HDMI_VSDB))["audio"] is False
    assert (
        describe_edid(_base("TV", 1) + _cta(HDMI_VSDB, basic_audio=True))["audio"]
        is True
    )
    assert describe_edid(_base("TV", 1) + _cta(STEREO_PCM))["audio"] is True


def test_only_the_hdmi_licensing_block_makes_a_sink_hdmi():
    forum_block = (3, b"\xd8\x5d\xc4\x01\x78")

    assert (
        describe_edid(_base("TV", 1) + _cta(forum_block, STEREO_PCM))["hdmi"] is False
    )


def test_each_port_says_whether_a_sink_is_plugged_in_and_what_it_takes(tmp_path):
    plugged = tmp_path / "card1-HDMI-A-1"
    plugged.mkdir()
    (plugged / "status").write_text("connected\n")
    (plugged / "enabled").write_text("enabled\n")
    (plugged / "edid").write_bytes(_base("RMC-1", 1) + _cta(HDMI_VSDB, STEREO_PCM))
    empty = tmp_path / "card1-HDMI-A-2"
    empty.mkdir()
    (empty / "status").write_text("disconnected\n")
    (empty / "enabled").write_text("disabled\n")
    (empty / "edid").write_bytes(b"")
    (tmp_path / "card1-Writeback-1").mkdir()
    (tmp_path / "card0").mkdir()

    assert hdmi_ports(str(tmp_path)) == [
        {
            "connector": "card1-HDMI-A-1",
            "status": "connected",
            "enabled": "enabled",
            "edid_bytes": 256,
            "sink": "RMC-1",
            "hdmi": True,
            "audio": True,
        },
        {
            "connector": "card1-HDMI-A-2",
            "status": "disconnected",
            "enabled": "disabled",
            "edid_bytes": 0,
        },
    ]


def test_a_machine_without_drm_has_no_ports(tmp_path):
    assert hdmi_ports(str(tmp_path / "missing")) == []
