from kalinka_plugin_sdk.api import EventEmitter
from kalinka_plugin_sdk.ext_device import ExternalOutputDevice, SupportedFunction, DeviceVolume
from kalinka_plugin_sdk.ext_device_events import ExtDeviceEvent, ExtDeviceState

from .config_model import {{ cookiecutter.plugin_class_prefix }}Config


class {{ cookiecutter.plugin_class_prefix }}Device(ExternalOutputDevice):
    """
    Tells Kalinka the device's volume and power state through ``emitter``:
    ``set_initial_state()`` once the device is reached, then ``dispatch()``
    a ``VolumeChangedEvent`` or ``DevicePowerStateChangedEvent`` on each change.
    """

    def __init__(
        self,
        config: {{ cookiecutter.plugin_class_prefix }}Config,
        emitter: EventEmitter[ExtDeviceEvent, ExtDeviceState],
    ):
        self.config = config
        self.emitter = emitter

    async def get_volume(self) -> DeviceVolume:
        """Get current volume level"""
        raise NotImplementedError

    async def set_volume(self, volume: int) -> None:
        """Set volume (0 to 100)"""
        raise NotImplementedError

    async def power_on(self) -> None:
        """Power on the device"""
        raise NotImplementedError

    async def is_power_on(self) -> bool:
        """Check if device is powered on"""
        raise NotImplementedError

    async def power_off(self) -> None:
        """Power off the device"""
        raise NotImplementedError

    def supported_functions(self) -> list[SupportedFunction]:
        """Return list of supported functions"""
        return [
            SupportedFunction.GET_VOLUME,
            SupportedFunction.SET_VOLUME,
            SupportedFunction.POWER_ON,
            SupportedFunction.IS_POWER_ON,
            SupportedFunction.POWER_OFF,
        ]
