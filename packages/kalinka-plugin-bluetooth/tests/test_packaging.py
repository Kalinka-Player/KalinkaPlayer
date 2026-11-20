from pathlib import Path

PACKAGING = Path(__file__).parents[1] / "packaging"


def settings(path: Path, section: str) -> list[tuple[str, str]]:
    current, found = None, []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line.startswith("["):
            current = line.strip("[]")
        elif line and not line.startswith("#") and current == section:
            key, _, value = line.partition("=")
            found.append((key, value))
    return found


def test_receiver_never_runs_without_kalinka():
    unit = settings(PACKAGING / "bluealsa-dropin.conf", "Unit")
    assert ("BindsTo", "kalinka.service") in unit
    assert ("After", "kalinka.service") in unit


def test_kalinka_starts_the_receiver():
    unit = settings(PACKAGING / "kalinka-dropin.conf", "Unit")
    assert ("Wants", "bluealsa.service") in unit


def test_receiver_offers_only_the_a2dp_sink_profile():
    service = settings(PACKAGING / "bluealsa-dropin.conf", "Service")
    starts = [value for key, value in service if key == "ExecStart"]
    assert starts == ["", "/usr/lib/kalinka-plugin-bluetooth/start-bluealsa"]
    script = (PACKAGING / "start-bluealsa").read_text()
    assert script.count("--profile=") == 1
    assert '"$receiver" --profile=a2dp-sink\n' in script


def test_plugin_ships_no_unit_of_its_own_for_org_bluealsa():
    assert not list(PACKAGING.glob("*.service"))
