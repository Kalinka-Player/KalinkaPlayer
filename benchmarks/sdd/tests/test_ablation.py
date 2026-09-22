"""The ablation: a run is asked under the configuration it is named for."""

from sddbench import instance
from sddbench.clock import Clock
from sddbench.instance import MoodAblation

RUNS = {"mood_on": True, "mood_off": False}


def _applied(monkeypatch) -> list[bool]:
    settings: list[bool] = []
    monkeypatch.setattr(
        instance, "set_mood", lambda server, enabled: settings.append(enabled)
    )
    return settings


def test_fusion_is_turned_back_on_for_the_run_that_asks_for_it(tmp_path, monkeypatch):
    """Runs can be given in any order, and a run that only ever turned fusion
    off would report CLAP-only numbers under the fusion heading."""
    settings = _applied(monkeypatch)
    ablation = MoodAblation(None, Clock(tmp_path / "clock.json"), RUNS)
    for run_name in ("mood_off", "mood_on"):
        ablation.prepare(run_name)
    assert settings == [False, True]


def test_a_setting_the_instance_already_has_costs_no_restart(tmp_path, monkeypatch):
    settings = _applied(monkeypatch)
    clock = Clock(tmp_path / "clock.json")
    ablation = MoodAblation(None, clock, RUNS)
    ablation.prepare("mood_on")
    ablation.prepare("mood_on")
    assert settings == []
    assert clock.spans == {}


def test_each_restart_is_timed_under_its_own_run(tmp_path, monkeypatch):
    _applied(monkeypatch)
    clock = Clock(tmp_path / "clock.json")
    ablation = MoodAblation(None, clock, RUNS)
    ablation.prepare("mood_off")
    ablation.prepare("mood_on")
    assert sorted(clock.spans) == ["ablation_mood_off", "ablation_mood_on"]
