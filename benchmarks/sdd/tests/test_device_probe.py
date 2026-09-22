"""The device probe: its accounting must add up, and it must not confuse a
hot cache with a cold one or a tmpfs with a disk."""

import io
import time
import types
from pathlib import Path

import pytest

import device_probe as probe


def test_the_stream_charges_reads_and_seeks_to_io():
    buckets = probe.Buckets()
    stream = probe.TimedStream(io.BytesIO(b"0123456789"), buckets)

    assert stream.read(4) == b"0123"
    assert stream.seek(0) == 0
    assert stream.read() == b"0123456789"

    assert buckets.io_bytes == 14
    assert buckets.io_s > 0.0


def test_the_stream_looks_like_a_readable_seekable_file():
    stream = probe.TimedStream(io.BytesIO(b"abc"), probe.Buckets())

    assert stream.seekable() and stream.readable()
    stream.read(2)
    assert stream.tell() == 2


def test_resetting_clears_every_bucket():
    buckets = probe.Buckets(io_s=1.0, io_bytes=2, decode_s=3.0, infer_s=4.0)

    buckets.reset()

    assert (buckets.io_s, buckets.io_bytes, buckets.decode_s, buckets.infer_s) == (
        0.0,
        0,
        0.0,
        0.0,
    )


def _split(**overrides) -> probe.Split:
    fields = dict(
        round=1, location="usb", cache="cold", track="a.mp3", total_s=8.0,
        io_s=0.1, decode_s=0.3, infer_s=7.0, other_s=0.6, read_bytes=10,
        temp_c=70.0, clock_mhz=1800.0,
    )
    fields.update(overrides)
    return probe.Split(**fields)


def test_the_median_split_is_taken_per_bucket():
    summary = probe.median_split(
        [_split(total_s=7.0, infer_s=6.0), _split(total_s=9.0, infer_s=8.0)]
    )

    assert summary["total"] == 8.0
    assert summary["infer"] == 7.0


def test_drift_compares_the_first_round_with_the_last():
    splits = [
        _split(round=1, total_s=7.5, temp_c=60.0),
        _split(round=2, total_s=8.5, temp_c=75.0),
        _split(round=3, total_s=9.5, temp_c=84.0),
    ]

    first, last, cool, hot = probe.drift(splits)

    assert (first, last) == (7.5, 9.5)
    assert (cool, hot) == (60.0, 84.0)


def test_a_location_can_be_labelled_or_named_after_its_folder():
    parsed = probe.locations(["usb=/mnt/usb/Test", "/home/envel/music"])

    assert parsed[0] == ("usb", Path("/mnt/usb/Test"))
    assert parsed[1] == ("music", Path("/home/envel/music"))


def test_mp3s_win_but_other_audio_is_still_found(tmp_path: Path):
    (tmp_path / "b.mp3").touch()
    (tmp_path / "a.flac").touch()

    assert probe.audio_in(tmp_path) == [tmp_path / "b.mp3"]

    (tmp_path / "b.mp3").unlink()
    assert probe.audio_in(tmp_path) == [tmp_path / "a.flac"]


def test_the_mount_report_names_the_filesystem(tmp_path: Path):
    reported = probe.mount_of(tmp_path)

    assert "(" in reported and ")" in reported


def test_eviction_leaves_the_file_readable(tmp_path: Path):
    path = tmp_path / "a.mp3"
    path.write_bytes(b"x" * 4096)

    probe.evict(path)

    assert path.read_bytes() == b"x" * 4096


def test_the_csv_holds_one_row_per_measurement(tmp_path: Path):
    out = tmp_path / "device.csv"

    probe.write_csv(out, [_split(round=1), _split(round=2, cache="warm")])

    lines = out.read_text().strip().splitlines()
    assert lines[0].startswith("round,location,cache,track,total_s")
    assert len(lines) == 3
    assert lines[2].startswith("2,usb,warm,a.mp3")


def test_the_probe_needs_the_plugin_not_the_repository():
    """It is copied to the device on its own, so nothing here may import the
    benchmark package."""
    source = Path(probe.__file__).read_text()

    assert "sddbench" not in source
    assert "from kalinka_plugin_localfiles" in source


def test_the_firmware_clock_is_preferred_over_what_linux_asked_for(monkeypatch):
    """A throttled Pi keeps reporting 1800 MHz in sysfs; the firmware does not."""
    monkeypatch.setattr(probe, "vcgencmd", lambda *_: "frequency(48)=1531406208")

    assert probe.clock_mhz() == pytest.approx(1531.4, abs=0.1)


def test_the_clock_falls_back_to_sysfs_off_a_pi(monkeypatch):
    monkeypatch.setattr(probe, "vcgencmd", lambda *_: "")

    assert probe.clock_mhz() >= 0.0


def test_a_clock_reading_is_parsed_from_either_source():
    assert probe.parse_clock("frequency(48)=1800404352") == pytest.approx(1800.4, abs=0.1)
    assert probe.parse_clock("1800000\n") == pytest.approx(1800.0)
    assert probe.parse_clock("nonsense") == 0.0


def test_vcgencmd_is_quiet_where_there_is_no_pi_firmware(monkeypatch):
    monkeypatch.setattr(
        probe.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError)
    )

    assert probe.vcgencmd("get_throttled") == ""
    assert probe.throttling() == ""


def test_the_state_line_names_the_throttling_word_when_there_is_one(monkeypatch):
    monkeypatch.setattr(probe, "temperature_c", lambda: 84.2)
    monkeypatch.setattr(probe, "clock_mhz", lambda: 1531.0)
    monkeypatch.setattr(probe, "throttling", lambda: "0xe0008")

    assert probe.state() == "84.2C, 1531 MHz, throttled=0xe0008"


def fake_clap():
    """A stand-in for the plugin module: a fragment loader and a session that
    each spend a known amount of time, and a model that drives them the way
    the real one does."""
    module = types.SimpleNamespace()

    def read_fragment(*_args):
        time.sleep(0.01)
        return [0.0]

    module._read_fragment = read_fragment

    class Session:
        def run(self, *_args, **_kwargs):
            time.sleep(0.02)
            return [[0.0]]

    class Model:
        def __init__(self, model_dir):
            self.is_audio_loaded = False

        def load_audio(self):
            self.is_audio_loaded = True
            self._audio_session = Session()

        def _session_options(self):
            return types.SimpleNamespace(intra_op_num_threads=2)

        def get_audio_embedding(self, stream):
            stream.read(16)
            module._read_fragment(stream, 0.0, 48_000)
            self._audio_session.run(None, {})
            return [1.0]

    module.ClapOnnxModel = Model
    return module


@pytest.fixture
def track(tmp_path: Path) -> Path:
    path = tmp_path / "00a1196ae8bd.mp3"
    path.write_bytes(b"x" * 8192)
    return path


def test_each_stage_is_charged_to_its_own_bucket(track: Path):
    with probe.Probe(Path("/models"), clap=fake_clap()) as measured:
        split = measured.embed(track, "warm", 1, "here")

    assert split.decode_s >= 0.01
    assert split.infer_s >= 0.02
    assert split.read_bytes == 16
    assert split.total_s >= split.decode_s + split.infer_s + split.io_s
    assert split.other_s == pytest.approx(
        split.total_s - split.decode_s - split.infer_s - split.io_s, abs=1e-9
    )


def test_the_probe_puts_the_fragment_loader_back(track: Path):
    clap = fake_clap()
    original = clap._read_fragment

    with probe.Probe(Path("/models"), clap=clap) as measured:
        assert clap._read_fragment is not original
        measured.embed(track, "warm", 1, "here")

    assert clap._read_fragment is original


def test_a_second_probe_does_not_report_through_the_first(track: Path):
    """Two probes in a row — a thread sweep is exactly that — must each
    measure their own decoding rather than the first one's."""
    clap = fake_clap()
    with probe.Probe(Path("/models"), clap=clap) as first:
        first.embed(track, "warm", 1, "here")
    with probe.Probe(Path("/models"), clap=clap) as second:
        split = second.embed(track, "warm", 2, "here")

    assert split.decode_s >= 0.01


def test_a_swept_thread_count_reaches_the_session(track: Path):
    with probe.Probe(Path("/models"), intra_op=3, clap=fake_clap()) as measured:
        assert measured.intra_op == 3

    with probe.Probe(Path("/models"), clap=fake_clap()) as measured:
        assert measured.intra_op == 2


def test_the_sweep_measures_the_shipped_thread_count_even_when_unasked(
    monkeypatch, capsys
):
    """The ratio column reads "vs shipped", so the shipped value has to be
    one of the measurements — on a board whose shipped count is not in the
    list the operator typed, a ratio against the first value would invert
    the conclusion."""
    seen: list[int] = []

    class _Stub:
        def __init__(self, model_dir, intra_op=None):
            seen.append(intra_op)
            self._intra_op = intra_op

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def embed(self, track, cache, number, label):
            return types.SimpleNamespace(total_s=float(self._intra_op))

    monkeypatch.setattr(probe, "Probe", _Stub)
    probe.sweep(Path("/models"), Path("probe.mp3"), [1, 2], shipped=6, rounds=1)

    assert seen == [1, 2, 6]
    printed = capsys.readouterr().out
    assert "0.17x" in printed  # 1 thread against the shipped 6, not against itself
    assert "1.00x" in printed and "(shipped)" in printed
