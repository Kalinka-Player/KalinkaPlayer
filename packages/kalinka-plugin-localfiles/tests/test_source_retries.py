"""One bounded recovery schedule per unavailable source."""

from kalinka_plugin_localfiles.indexer.source_retries import SourceRetries
from kalinka_plugin_localfiles.storage import ChangeKind


def test_outage_delay_grows_caps_and_resets_after_recovery():
    now = [0.0]
    retries = SourceRetries(clock=lambda: now[0])
    for delay in (30, 60, 120, 240, 300, 300, 300):
        assert retries.unavailable("root") == delay
        assert retries.next_retry_in() == delay
        assert not retries.due()
        now[0] += delay
        assert retries.due() == ["root"]
        assert "root" in retries  # Expiry allows a probe, not file reads.
    assert retries.recovered("root") == {(ChangeKind.DIR_ADDED, "root")}
    assert retries.next_retry_in() is None
    assert retries.unavailable("root") == 30


def test_new_events_share_the_existing_deadline_and_recovery_scans_the_whole_root():
    retries = SourceRetries(clock=lambda: 0)
    retries.unavailable("root")
    for _ in range(100):
        assert retries.defer("root", (ChangeKind.FILE_WRITTEN, "root/a.flac"))
        assert retries.defer("root", (ChangeKind.PATH_REMOVED, "root/Album"))
    assert retries.next_retry_in() == 30
    assert not retries.defer("healthy", (ChangeKind.FILE_WRITTEN, "healthy/b.flac"))
    assert retries.recovered("root") == {
        (ChangeKind.DIR_ADDED, "root"),
        (ChangeKind.FILE_WRITTEN, "root/a.flac"),
    }


def test_long_outage_bounds_write_buffer_but_still_owes_a_full_scan():
    retries = SourceRetries()
    retries.unavailable("root")
    for n in range(10010):
        retries.defer("root", (ChangeKind.FILE_WRITTEN, f"root/{n}.flac"))
    changes = retries.recovered("root")
    assert len(changes) == 10001
    assert (ChangeKind.DIR_ADDED, "root") in changes
