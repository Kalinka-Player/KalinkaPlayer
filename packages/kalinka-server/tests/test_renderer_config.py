import asyncio

import pytest

from kalinka_server.renderer_config import RendererConfigService, snapshot_to_dict
from kalinka_server.renderer_proto import renderer_pb2 as pb
from kalinka_server.renderer_registry import RendererRegistry, RendererUnavailable

RENDERER_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


@pytest.mark.parametrize("details", ["", "Extra setup information."])
def test_detailed_help_survives_the_renderer_protocol(details):
    snapshot = pb.ConfigSnapshot()
    field = snapshot.sections.add().fields.add()
    field.path = "output.device"
    field.description = "Choose an output."
    field.help_details = details
    received = pb.ConfigSnapshot.FromString(snapshot.SerializeToString())

    exposed = snapshot_to_dict(received)["sections"][0]["fields"][0]

    assert exposed["description"] == "Choose an output."
    assert exposed["help_details"] == details


class FakeWs:
    """Renderer end of the config plane, answering in line like the real one."""

    def __init__(self, service=None):
        self.service = service
        self.requests = 0
        self.updates: list[dict] = []
        self.answer = True
        # False stands in for a renderer built before the tier existed.
        self.declare_importance = True
        # As does this, for one built before a field could say what it accepts.
        self.declare_bounds = True
        self.device_options = ["default", "hw:CARD=sofhdadsp,DEV=0"]
        self.values = {"output.driver": "alsa", "output.device": "default"}
        self._next_id = 0

    def next_message_id(self) -> int:
        self._next_id += 1
        return self._next_id

    def _snapshot(self) -> pb.ConfigSnapshot:
        snapshot = pb.ConfigSnapshot()
        snapshot.config_version = f"v{len(self.device_options)}"
        section = snapshot.sections.add()
        section.path = "output"
        section.title = "Output"
        device = section.fields.add()
        device.path = "output.device"
        device.title = "Device"
        device.type = pb.CONFIG_FIELD_TYPE_ENUM
        device.value = self.values["output.device"]
        device.default_value = "default"
        device.apply = pb.APPLY_COST_INTERRUPTS_PLAYBACK
        if self.declare_importance:
            device.importance = pb.CONFIG_IMPORTANCE_SIMPLE
        for name in self.device_options:
            option = device.options.add()
            option.value = name
            option.label = name
        latency = section.fields.add()
        latency.path = "output.latency_ms"
        latency.type = pb.CONFIG_FIELD_TYPE_INT
        latency.value = "160"
        latency.apply = pb.APPLY_COST_INTERRUPTS_PLAYBACK
        if self.declare_importance:
            latency.importance = pb.CONFIG_IMPORTANCE_EXPERT
        if self.declare_bounds:
            latency.unit = "ms"
            latency.widget = pb.CONFIG_WIDGET_SLIDER
            latency.range.min = 20
            latency.range.max = 1000
            latency.range.step = 10
        return snapshot

    async def send_config_request(self, message_id) -> None:
        self.requests += 1
        if self.answer:
            self.service.handle_reply(RENDERER_ID, self, message_id, self._snapshot())

    async def send_config_update(self, message_id, changes) -> None:
        self.updates.append(dict(changes))
        if not self.answer:
            return
        result = pb.ConfigResult()
        for path, value in changes.items():
            outcome = result.outcomes.add()
            outcome.path = path
            if path in self.values and value in self.device_options:
                self.values[path] = value
                outcome.applied = True
                outcome.value = value
                result.effect = pb.APPLY_COST_INTERRUPTS_PLAYBACK
            else:
                outcome.value = self.values.get(path, "")
                outcome.error = "not one of the offered options"
        result.config_version = f"v{len(self.device_options)}"
        self.service.handle_reply(RENDERER_ID, self, message_id, result)

    async def replace(self):
        pass


def make_service(timeout_s=5.0):
    registry = RendererRegistry(offline_timeout_s=30.0)
    service = RendererConfigService(registry, timeout_s=timeout_s)
    ws = FakeWs(service)
    registry.register(
        renderer_id=RENDERER_ID,
        instance_id="inst-1",
        friendly_name="Test Renderer",
        software_version="0.1.0",
        kind="native",
        platform={},
        session=ws,
    )
    return registry, service, ws


@pytest.mark.asyncio
async def test_config_arrives_as_schema_and_values_together():
    registry, service, ws = make_service()

    config = await service.get(RENDERER_ID)

    assert config["config_version"] == "v2"
    field = config["sections"][0]["fields"][0]
    assert field["path"] == "output.device"
    assert field["type"] == "enum"
    assert field["value"] == "default"
    assert field["apply"] == "interrupts_playback"
    assert [o["value"] for o in field["options"]] == [
        "default",
        "hw:CARD=sofhdadsp,DEV=0",
    ]


@pytest.mark.asyncio
async def test_the_renderer_says_which_page_each_field_belongs_on():
    registry, service, ws = make_service()

    config = await service.get(RENDERER_ID)

    fields = {f["path"]: f["importance"] for f in config["sections"][0]["fields"]}
    assert fields == {"output.device": "simple", "output.latency_ms": "expert"}


@pytest.mark.asyncio
async def test_a_renderer_that_predates_the_tier_keeps_its_page():
    registry, service, ws = make_service()
    ws.declare_importance = False

    config = await service.get(RENDERER_ID)

    assert [f["importance"] for f in config["sections"][0]["fields"]] == [
        "simple",
        "simple",
    ]


@pytest.mark.asyncio
async def test_a_bounded_field_carries_what_it_accepts():
    registry, service, ws = make_service()

    config = await service.get(RENDERER_ID)

    fields = {f["path"]: f for f in config["sections"][0]["fields"]}
    latency = fields["output.latency_ms"]
    assert latency["range"] == {"min": 20, "max": 1000, "step": 10}
    assert latency["unit"] == "ms"
    assert latency["widget"] == "slider"


@pytest.mark.asyncio
async def test_a_field_with_no_bounds_says_so_rather_than_inventing_them():
    registry, service, ws = make_service()
    ws.declare_bounds = False

    config = await service.get(RENDERER_ID)

    for field in config["sections"][0]["fields"]:
        assert field["range"] is None, field["path"]
        assert field["unit"] == ""
        assert field["widget"] == ""


@pytest.mark.asyncio
async def test_the_device_list_is_as_fresh_as_the_request():
    registry, service, ws = make_service()
    await service.get(RENDERER_ID)

    ws.device_options.append("hw:CARD=USB,DEV=0")  # a DAC gets plugged in
    config = await service.get(RENDERER_ID)

    assert config["config_version"] == "v3"
    assert len(config["sections"][0]["fields"][0]["options"]) == 3


@pytest.mark.asyncio
async def test_an_update_reports_what_took_effect():
    registry, service, ws = make_service()

    result = await service.update(
        RENDERER_ID, {"output.device": "hw:CARD=sofhdadsp,DEV=0"}
    )

    assert ws.updates == [{"output.device": "hw:CARD=sofhdadsp,DEV=0"}]
    assert result["effect"] == "interrupts_playback"
    assert result["outcomes"] == [
        {
            "path": "output.device",
            "applied": True,
            "value": "hw:CARD=sofhdadsp,DEV=0",
            "error": "",
        }
    ]


@pytest.mark.asyncio
async def test_a_refused_value_leaves_the_old_one_in_the_answer():
    registry, service, ws = make_service()

    result = await service.update(RENDERER_ID, {"output.device": "hw:CARD=gone"})

    assert result["outcomes"][0]["applied"] is False
    assert result["outcomes"][0]["error"] == "not one of the offered options"
    assert result["outcomes"][0]["value"] == "default"


@pytest.mark.asyncio
async def test_config_needs_a_connected_renderer():
    registry, service, ws = make_service()
    registry.disconnect(RENDERER_ID, ws, clean=False)

    with pytest.raises(RendererUnavailable):
        await service.get(RENDERER_ID)
    with pytest.raises(RendererUnavailable):
        await service.get("nobody-by-that-id")


@pytest.mark.asyncio
async def test_a_disconnect_fails_the_request_in_flight():
    registry, service, ws = make_service()
    ws.answer = False

    task = asyncio.create_task(service.get(RENDERER_ID))
    await asyncio.sleep(0)
    service.handle_disconnect(RENDERER_ID, ws)

    with pytest.raises(RendererUnavailable):
        await task


@pytest.mark.asyncio
async def test_a_silent_renderer_times_out():
    registry, service, ws = make_service(timeout_s=0.05)
    ws.answer = False

    with pytest.raises(asyncio.TimeoutError):
        await service.get(RENDERER_ID)


@pytest.mark.asyncio
async def test_a_reply_nobody_awaits_is_dropped():
    registry, service, ws = make_service()

    service.handle_reply(RENDERER_ID, ws, 999, pb.ConfigSnapshot())  # no raise

    config = await service.get(RENDERER_ID)
    assert config["config_version"] == "v2"
