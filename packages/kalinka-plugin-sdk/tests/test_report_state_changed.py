"""A plugin may tell the server its state moved, where the server lets it."""

from kalinka_plugin_sdk import InputPluginContext, OutputDevicePluginContext

COMMON = dict(
    logger=None, plugin_id="jamendo", sdk_version="3.23.0", config=None, listener=None
)


def test_a_server_that_does_not_offer_it_hands_none():
    assert InputPluginContext(**COMMON, playqueue=None).report_state_changed is None
    assert (
        OutputDevicePluginContext(**COMMON, emitter=None).report_state_changed is None
    )


def test_a_plugin_calls_what_the_server_handed_it():
    told = []
    context = InputPluginContext(
        **COMMON, playqueue=None, report_state_changed=lambda: told.append(1)
    )

    context.report_state_changed()

    assert told == [1]
