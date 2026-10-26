"""The user agent the enrichers name themselves with to MusicBrainz, Wikidata,
Deezer and the Cover Art Archive."""

import pytest
from kalinka_server.config_overrides import apply_overrides_with_prefix

from kalinka_plugin_localfiles.config_model import LocalFilesConfig

OWN = "MyBox/2.0 (mailto:me@example.org)"


@pytest.mark.parametrize(
    "saved, loaded",
    [
        (
            "Kalinka/1.0 (https://github.com/madenvel/KalinkaPlayer)",
            LocalFilesConfig().enricher.plugins.user_agent,
        ),
        (OWN, OWN),
    ],
    ids=["former-owner", "own-agent"],
)
def test_a_saved_user_agent_follows_the_repository_move(saved, loaded):
    """Saved before the move, its contact link would lead the services to
    whoever registers the old name."""
    config = LocalFilesConfig()

    apply_overrides_with_prefix(
        config,
        {"input_modules.localfiles.enricher.plugins.user_agent": saved},
        "input_modules.localfiles.",
    )

    assert config.enricher.plugins.user_agent == loaded
