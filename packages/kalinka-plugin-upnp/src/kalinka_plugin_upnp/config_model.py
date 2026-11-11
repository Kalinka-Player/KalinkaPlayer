"""Configuration exposed in Kalinka's UPnP input settings."""

import ipaddress
import socket
from pathlib import Path
from typing import ClassVar

from kalinka_plugin_sdk.module_config import ModuleConfig
from kalinka_plugin_sdk import ModuleIconFiles
from pydantic import Field, field_validator


class UpnpConfig(ModuleConfig):
    """Discovery identity and the interface serving the UPnP controller."""

    __module_icon__: ClassVar[str] = "cast"
    __module_icon_color__: ClassVar[str] = "#3974A8"
    __module_icon_files__: ClassVar[ModuleIconFiles] = ModuleIconFiles(
        small=Path(__file__).with_name("icons") / "small.svg",
        standard=Path(__file__).with_name("icons") / "standard.svg",
        monochrome=True,
    )

    name: str = Field(
        default="upnp",
        title="UPnP",
        description=(
            "Play from UPnP apps through the selected renderer."
        ),
        frozen=True,
        exclude=True,
    )
    enabled: bool = Field(
        default=False,
        title="Enable UPnP renderer",
        json_schema_extra={"importance": "simple"},
    )
    device_name: str = Field(
        default_factory=lambda: f"Kalinka ({socket.gethostname()})",
        min_length=1,
        max_length=100,
        title="UPnP device name",
        description="Name shown in UPnP apps.",
        json_schema_extra={"importance": "simple"},
    )
    interface_address: str = Field(
        default="",
        title="Network interface address",
        description=(
            "Enter a local IPv4 address for UPnP, or leave empty to use the "
            "default interface."
        ),
    )
    port: int = Field(default=49152, ge=1024, le=65535, title="UPnP HTTP port")

    @field_validator("interface_address")
    @classmethod
    def valid_address(cls, value):
        if value:
            address = ipaddress.IPv4Address(value)
            if (
                address.is_unspecified
                or address.is_multicast
                or int(address) == 0xFFFFFFFF
            ):
                raise ValueError("Choose a local unicast IPv4 address")
        return value
