"""The report reads the server's answers and the run's artifacts; these are
the shapes it must keep reading correctly."""

from sddbench import report
from sddbench.retrieval import MISS


def test_settings_are_read_from_the_dotted_values_map():
    config = {
        "schema_version": "abc",
        "values": {
            "input_modules.localfiles.ai_search.enabled": True,
            "input_modules.localfiles.ai_search.max_results": 50,
            "input_modules.localfiles.ai_search.mood.enabled": False,
            "input_modules.localfiles.ai_search.unlisted": "ignored",
        },
    }
    settings = report._settings(config)
    assert settings["input_modules.localfiles.ai_search.enabled"] is True
    assert settings["input_modules.localfiles.ai_search.max_results"] == 50
    assert "input_modules.localfiles.ai_search.unlisted" not in settings


def test_settings_survive_a_config_without_the_values_wrapper():
    assert report._settings({"base_config.search.ai_suggestions_limit": 20}) == {
        "base_config.search.ai_suggestions_limit": 20
    }


def test_a_missed_rank_prints_as_out_of_range():
    assert report._rank(None) == "—"
    assert report._rank(3) == "3"
    assert report._rank(MISS) == "> 50"


def test_hms_reads_as_a_duration():
    assert report._hms(42) == "42s"
    assert report._hms(125) == "2m 5s"
    assert report._hms(3725) == "1h 2m 5s"
