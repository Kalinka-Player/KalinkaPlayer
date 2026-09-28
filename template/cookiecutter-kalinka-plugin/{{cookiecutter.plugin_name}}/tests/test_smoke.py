"""
Pytest smoke tests for {{ cookiecutter.plugin_name }}
"""

import asyncio
import inspect
import logging
import pytest
from importlib.metadata import entry_points
from unittest.mock import Mock

from kalinka_plugin_sdk import __version__ as sdk_version
from kalinka_plugin_sdk.api import EventListener
{%- if cookiecutter.plugin_type == "input_module" %}
from kalinka_plugin_sdk.api import PlayQueueController
from kalinka_plugin_sdk.inputmodule import InputModule
from kalinka_plugin_sdk.plugin import InputPluginContext, cast_plugin_interface
{%- else %}
from kalinka_plugin_sdk.api import EventEmitter
from kalinka_plugin_sdk.ext_device import ExternalOutputDevice
from kalinka_plugin_sdk.plugin import OutputDevicePluginContext, cast_plugin_interface
{%- endif %}


@pytest.mark.smoke
def test_entry_point_visible():
    """Test that the plugin entry point is discoverable"""
    eps = entry_points(group="kalinka.plugins")
    # In editable mode, ensure it's discoverable after `pip install -e .`
    assert any(
        ep.name == "{{ cookiecutter.plugin_id }}" for ep in eps
    ), "Plugin entry point '{{ cookiecutter.plugin_id }}' not found in kalinka.plugins group"

    for ep in eps:
        if ep.name == "{{ cookiecutter.plugin_id }}":
            plugin = ep.load()
            assert plugin is not None
            assert hasattr(plugin, "PLUGIN_ID")
            assert plugin.PLUGIN_ID == "{{ cookiecutter.name }}"
            assert hasattr(plugin, "REQUIRES_SDK")
            assert hasattr(plugin, "CONFIG_MODEL")
            obj = plugin()
            assert hasattr(obj, "setup")
            assert hasattr(obj, "shutdown")

            config = plugin.CONFIG_MODEL()
            assert config is not None

            break


@pytest.mark.smoke
def test_imports():
    """Test that all plugin modules can be imported"""
    # These imports should not raise ImportError
    from {{ cookiecutter.plugin_id }}.config_model import {{ cookiecutter.plugin_class_prefix }}Config
    from {{ cookiecutter.plugin_id }}.module_setup import KalinkaPlugin{{ cookiecutter.plugin_class_prefix }}
{%- if cookiecutter.plugin_type == "input_module" %}
    from {{ cookiecutter.plugin_id }}.{{ cookiecutter.plugin_id }}_input_module import {{ cookiecutter.plugin_class_prefix }}InputModule
{%- else %}
    from {{ cookiecutter.plugin_id }}.{{ cookiecutter.plugin_id }}_device import {{ cookiecutter.plugin_class_prefix }}Device
{%- endif %}

    # Verify the imported classes/functions exist
    assert {{ cookiecutter.plugin_class_prefix }}Config is not None
    assert KalinkaPlugin{{ cookiecutter.plugin_class_prefix }} is not None
{%- if cookiecutter.plugin_type == "input_module" %}
    assert {{ cookiecutter.plugin_class_prefix }}InputModule is not None
{%- else %}
    assert {{ cookiecutter.plugin_class_prefix }}Device is not None
{%- endif %}


def _context(config):
    """A context like the one the server hands the plugin's setup()"""
{%- if cookiecutter.plugin_type == "input_module" %}
    return InputPluginContext(
        logger=logging.getLogger("{{ cookiecutter.name }}"),
        plugin_id="{{ cookiecutter.name }}",
        sdk_version=sdk_version,
        config=config,
        listener=Mock(spec=EventListener),
        playqueue=Mock(spec=PlayQueueController),
    )
{%- else %}
    return OutputDevicePluginContext(
        logger=logging.getLogger("{{ cookiecutter.name }}"),
        plugin_id="{{ cookiecutter.name }}",
        sdk_version=sdk_version,
        config=config,
        listener=Mock(spec=EventListener),
        emitter=Mock(spec=EventEmitter),
    )
{%- endif %}


@pytest.mark.smoke
def test_setup_and_shutdown_as_the_server_runs_them(caplog):
    """The server awaits setup() and shutdown() and logs through a standard logging.Logger"""
    from {{ cookiecutter.plugin_id }}.module_setup import KalinkaPlugin{{ cookiecutter.plugin_class_prefix }}

    # At DEBUG every log call is formatted, so a malformed one fails here
    caplog.set_level(logging.DEBUG, logger="{{ cookiecutter.name }}")
    plugin = KalinkaPlugin{{ cookiecutter.plugin_class_prefix }}()
    context = _context(plugin.CONFIG_MODEL(enabled=True))

    async def run():
        await plugin.setup(context)
        interface = cast_plugin_interface(plugin)
        await plugin.shutdown()
        return interface

    interface = asyncio.run(run())

    assert isinstance(interface, {% if cookiecutter.plugin_type == "input_module" %}InputModule{% else %}ExternalOutputDevice{% endif %})


@pytest.mark.smoke
def test_config_model_instantiation():
    """Test that config model can be instantiated with correct defaults"""
    from {{ cookiecutter.plugin_id }}.config_model import {{ cookiecutter.plugin_class_prefix }}Config

    config = {{ cookiecutter.plugin_class_prefix }}Config()

    assert config.name == "{{ cookiecutter.plugin_id }}"
    # Check that the config name title matches plugin display name
    assert config.__class__.model_fields["name"].title == "{{ cookiecutter.plugin_display_name }}"
    # Enabled field should be boolean and should have a default value
    assert isinstance(config.enabled, bool)
    # Without the simple tag the enable toggle drops off the simple settings page
    assert config.__class__.model_fields["enabled"].json_schema_extra["importance"] == "simple"


def test_config_model_validation():
    """Test that config model validation works"""
    from {{ cookiecutter.plugin_id }}.config_model import {{ cookiecutter.plugin_class_prefix }}Config

    # Test with valid data
    config = {{ cookiecutter.plugin_class_prefix }}Config(enabled=True)
    assert config.enabled is True
    assert config.name == "{{ cookiecutter.plugin_id }}"

    # Test with custom name (frozen=True prevents modification after creation, not during init)
    config_with_name = {{ cookiecutter.plugin_class_prefix }}Config(name="different_name", enabled=True)
    assert config_with_name.name == "different_name"
    assert config_with_name.enabled is True


def test_config_model_frozen_field():
    """Test that name field is frozen after creation"""
    from {{ cookiecutter.plugin_id }}.config_model import {{ cookiecutter.plugin_class_prefix }}Config
    import pytest

    config = {{ cookiecutter.plugin_class_prefix }}Config()

    # Attempting to modify the frozen 'name' field should raise an exception
    with pytest.raises(Exception):  # Should raise ValidationError or similar
        config.name = "modified_name"
{%- if cookiecutter.plugin_type == "input_module" %}


@pytest.mark.smoke
def test_input_module_instantiation():
    """Test that input module can be instantiated"""
    from {{ cookiecutter.plugin_id }}.config_model import {{ cookiecutter.plugin_class_prefix }}Config
    from {{ cookiecutter.plugin_id }}.{{ cookiecutter.plugin_id }}_input_module import {{ cookiecutter.plugin_class_prefix }}InputModule

    config = {{ cookiecutter.plugin_class_prefix }}Config()
    module = {{ cookiecutter.plugin_class_prefix }}InputModule(config)

    assert module is not None


@pytest.mark.smoke
def test_input_module_name():
    """Test that input module returns correct name"""
    from {{ cookiecutter.plugin_id }}.config_model import {{ cookiecutter.plugin_class_prefix }}Config
    from {{ cookiecutter.plugin_id }}.{{ cookiecutter.plugin_id }}_input_module import {{ cookiecutter.plugin_class_prefix }}InputModule

    config = {{ cookiecutter.plugin_class_prefix }}Config()
    module = {{ cookiecutter.plugin_class_prefix }}InputModule(config)

    assert module.module_name() == "{{ cookiecutter.plugin_display_name }}"
{%- else %}


@pytest.mark.smoke
def test_device_instantiation():
    """Test that device can be instantiated"""
    from {{ cookiecutter.plugin_id }}.config_model import {{ cookiecutter.plugin_class_prefix }}Config
    from {{ cookiecutter.plugin_id }}.{{ cookiecutter.plugin_id }}_device import {{ cookiecutter.plugin_class_prefix }}Device

    config = {{ cookiecutter.plugin_class_prefix }}Config()
    device = {{ cookiecutter.plugin_class_prefix }}Device(config, Mock(spec=EventEmitter))

    assert device is not None


@pytest.mark.smoke
def test_device_supported_functions():
    """Test that device returns correct supported functions"""
    from {{ cookiecutter.plugin_id }}.config_model import {{ cookiecutter.plugin_class_prefix }}Config
    from {{ cookiecutter.plugin_id }}.{{ cookiecutter.plugin_id }}_device import {{ cookiecutter.plugin_class_prefix }}Device

    config = {{ cookiecutter.plugin_class_prefix }}Config()
    device = {{ cookiecutter.plugin_class_prefix }}Device(config, Mock(spec=EventEmitter))

    functions = device.supported_functions()
    assert isinstance(functions, list), "supported_functions() should return a list"
{%- endif %}


def test_module_setup_constants():
    """Test that module setup has required constants"""
    from {{ cookiecutter.plugin_id }}.module_setup import KalinkaPlugin{{ cookiecutter.plugin_class_prefix }}

    plugin_class = KalinkaPlugin{{ cookiecutter.plugin_class_prefix }}
    assert plugin_class.REQUIRES_SDK == "{{ cookiecutter.sdk_version_constraint }}"
    assert plugin_class.PLUGIN_ID == "{{ cookiecutter.name }}"
    assert plugin_class.CONFIG_MODEL is not None

    # Test instantiation
    plugin_instance = plugin_class()
    assert hasattr(plugin_instance, "setup")
    assert hasattr(plugin_instance, "shutdown")
    assert callable(plugin_instance.setup)
    assert callable(plugin_instance.shutdown)
{%- if cookiecutter.plugin_type == "input_module" %}


@pytest.mark.parametrize(
    "method_name",
    [
        "module_name",
        "search",
        "browse",
        "get_track_info",
        "list_favorite",
        "get_favorite_ids",
        "add_to_favorite",
        "remove_from_favorite",
        "list_filter_values",
        "get",
        "playlist_user_list",
        "playlist_create",
        "playlist_update",
        "playlist_delete",
        "playlist_add_tracks",
        "playlist_remove_tracks",
        "get_resource_path",
    ],
)
def test_input_module_has_required_methods(method_name):
    """Test that input module has all required interface methods, async where the SDK's are"""
    from {{ cookiecutter.plugin_id }}.config_model import {{ cookiecutter.plugin_class_prefix }}Config
    from {{ cookiecutter.plugin_id }}.{{ cookiecutter.plugin_id }}_input_module import {{ cookiecutter.plugin_class_prefix }}InputModule

    config = {{ cookiecutter.plugin_class_prefix }}Config()
    module = {{ cookiecutter.plugin_class_prefix }}InputModule(config)

    assert hasattr(module, method_name), f"Missing required method: {method_name}"
    assert callable(
        getattr(module, method_name)
    ), f"Method {method_name} is not callable"
    assert inspect.iscoroutinefunction(
        getattr(module, method_name)
    ) == inspect.iscoroutinefunction(
        getattr(InputModule, method_name)
    ), f"Method {method_name} must be async exactly when InputModule's is"
{%- else %}


@pytest.mark.parametrize(
    "method_name",
    [
        "get_volume",
        "set_volume",
        "power_on",
        "is_power_on",
        "power_off",
        "supported_functions",
    ],
)
def test_device_has_required_methods(method_name):
    """Test that device has all required interface methods, async where the SDK's are"""
    from {{ cookiecutter.plugin_id }}.config_model import {{ cookiecutter.plugin_class_prefix }}Config
    from {{ cookiecutter.plugin_id }}.{{ cookiecutter.plugin_id }}_device import {{ cookiecutter.plugin_class_prefix }}Device

    config = {{ cookiecutter.plugin_class_prefix }}Config()
    device = {{ cookiecutter.plugin_class_prefix }}Device(config, Mock(spec=EventEmitter))

    assert hasattr(device, method_name), f"Missing required method: {method_name}"
    assert callable(
        getattr(device, method_name)
    ), f"Method {method_name} is not callable"
    assert inspect.iscoroutinefunction(
        getattr(device, method_name)
    ) == inspect.iscoroutinefunction(
        getattr(ExternalOutputDevice, method_name)
    ), f"Method {method_name} must be async exactly when ExternalOutputDevice's is"
{%- endif %}
