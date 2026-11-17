"""The clearances a local track's next range is served on."""

from kalinka_plugin_localfiles.vouched_tracks import VouchedTrack, VouchedTracks


class _Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def _track(name):
    return VouchedTrack(f"/music/{name}", f"/music/{name}", "audio/x-dsf", (1, 2))


def test_a_clearance_stands_for_its_window_and_no_longer():
    clock = _Clock()
    vouched = VouchedTracks(window_s=120.0, clock=clock)
    vouched.put("a", _track("a"))
    clock.now = 119.9
    assert vouched.get("a") == _track("a")
    clock.now = 120.0
    assert vouched.get("a") is None


def test_clearing_again_starts_the_window_again():
    clock = _Clock()
    vouched = VouchedTracks(window_s=120.0, clock=clock)
    vouched.put("a", _track("a"))
    clock.now = 100.0
    vouched.put("a", _track("a"))
    clock.now = 200.0
    assert vouched.get("a") == _track("a")


def test_past_its_capacity_the_oldest_clearance_goes():
    vouched = VouchedTracks(capacity=2, clock=_Clock())
    for name in ("a", "b", "c"):
        vouched.put(name, _track(name))
    assert vouched.get("a") is None
    assert vouched.get("b") == _track("b")
    assert vouched.get("c") == _track("c")


def test_a_forgotten_clearance_is_gone():
    vouched = VouchedTracks(clock=_Clock())
    vouched.put("a", _track("a"))
    vouched.forget("a")
    assert vouched.get("a") is None
