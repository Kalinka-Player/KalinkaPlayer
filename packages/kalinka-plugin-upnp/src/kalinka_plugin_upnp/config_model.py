"""Configuration exposed in Kalinka's UPnP input settings."""

import ipaddress
import socket
from typing import ClassVar

from kalinka_plugin_sdk.module_config import ModuleConfig
from pydantic import Field, field_validator


class UpnpConfig(ModuleConfig):
    """Discovery identity and the interface serving the UPnP controller."""

    __module_icon__: ClassVar[str] = "cast"
    __module_icon_color__: ClassVar[str] = "#3974A8"

    name: str = Field(default="upnp", title="UPnP", frozen=True, exclude=True)
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
        description="Name shown in UPnP apps. Playback uses the renderer selected in Kalinka.",
        json_schema_extra={"importance": "simple"},
    )
    interface_address: str = Field(
        default="",
        title="Network interface address",
        description="Local IPv4 address for discovery and control. Leave empty to use the default network interface.",
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
