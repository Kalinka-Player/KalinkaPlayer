import asyncio

import pytest
from pydantic import ValidationError

from kalinka_plugin_sdk import ContinuationRequest, InputModule, ModuleCapability


class _Plain(InputModule):
    def module_name(self) -> str:
        return "plain"


def test_request_defaults_to_one_track_with_nothing_to_avoid():
    request = ContinuationRequest(seeds=["a", "b"])

    assert request.seeds == ["a", "b"]
    assert request.avoid == []
    assert request.exclude == []
    assert request.count == 1


def test_request_refuses_a_count_below_one():
    with pytest.raises(ValidationError):
        ContinuationRequest(seeds=["a"], count=0)


def test_request_is_frozen():
    request = ContinuationRequest(seeds=["a"])

    with pytest.raises(ValidationError):
        request.count = 5


def test_default_module_proposes_nothing_and_does_not_report_continuation():
    module = _Plain()

    assert asyncio.run(module.continue_queue(ContinuationRequest(seeds=["a"]))) == []
    assert ModuleCapability.CONTINUATION not in module.capabilities()


def test_capability_value_is_the_wire_name():
    assert ModuleCapability.CONTINUATION.value == "continuation"
