from typing import ClassVar
from pydantic import Field
from kalinka_plugin_sdk.module_config import ModuleConfig


class DummydeviceConfig(ModuleConfig):
    __module_icon__: ClassVar[str] = "speaker_outlined"

    name: str = Field(
        default="dummydevice",
        title="Dummy device",
        description="Stand-in connected device for development.",
        frozen=True,
        exclude=True,
    )
    enabled: bool = Field(
        default=False,
        title="Module enabled",
        json_schema_extra={"importance": "simple"},
    )
