# Kalinka Plugin {{ cookiecutter.plugin_display_name }}

{{ cookiecutter.plugin_description }}

## Overview

This plugin was generated from the Kalinka Plugin cookiecutter template and provides a {{ cookiecutter.plugin_type }} implementation for the Kalinka Music Player system.

## Plugin Information

- **Plugin Name**: {{ cookiecutter.plugin_display_name }}
- **Plugin ID**: `{{ cookiecutter.name }}`
- **Python package**: `{{ cookiecutter.plugin_id }}`
- **Plugin Type**: {{ cookiecutter.plugin_type }}
- **Author**: {{ cookiecutter.author_name }} <{{ cookiecutter.author_email }}>
- **License**: {{ cookiecutter.license }}
- **Version**: {{ cookiecutter.version }}

## Plugin Structure

```
{{ cookiecutter.plugin_name }}/
├── README.md                    # This file
├── pyproject.toml              # Python package configuration
├── src/
│   └── {{ cookiecutter.plugin_id }}/
│       ├── __init__.py         # Package initialization
│       ├── _version.py         # Version information
│       ├── config_model.py     # Plugin configuration schema
│       ├── module_setup.py     # Plugin entry point and setup
{%- if cookiecutter.plugin_type == "input_module" %}
│       └── {{ cookiecutter.plugin_id }}_input_module.py  # Input module implementation
{%- else %}
│       └── {{ cookiecutter.plugin_id }}_device.py       # Device implementation
{%- endif %}
├── debian/                     # Debian packaging files
│   ├── control.in             # Package metadata template
│   ├── prerm                 # Pre-removal script
│   ├── rules                 # Build rules
│   └── triggers              # Restarts Kalinka to pick up the wheel
├── scripts/
│   ├── build_wheel.sh        # Build Python wheel
│   └── build_deb.sh          # Build Debian package
└── tests/
    └── test_smoke.py         # Basic smoke tests
```

## Key Components

### 1. Configuration Model (`config_model.py`)
Defines the configuration schema using Pydantic models that inherit from `ModuleConfig`:

```python
from pydantic import Field
from kalinka_plugin_sdk.module_config import ModuleConfig

class {{ cookiecutter.plugin_class_prefix }}Config(ModuleConfig):
    name: str = Field(default="{{ cookiecutter.name }}", frozen=True, exclude=True)
    enabled: bool = Field(
        default=False,
        title="Module enabled",
        json_schema_extra={"importance": "simple"},
    )
    # Add your plugin-specific configuration fields here
```

### 2. Module Setup (`module_setup.py`)
The main entry point class that Kalinka instantiates for your plugin:

```python
{%- if cookiecutter.plugin_type == "input_module" %}
from typing import Optional

from kalinka_plugin_sdk.inputmodule import InputModule
from kalinka_plugin_sdk.plugin import InputModulePlugin, InputPluginContext

class KalinkaPlugin{{ cookiecutter.plugin_class_prefix }}(InputModulePlugin):
    REQUIRES_SDK = "{{ cookiecutter.sdk_version_constraint }}"
    PLUGIN_ID = "{{ cookiecutter.name }}"
    CONFIG_MODEL = {{ cookiecutter.plugin_class_prefix }}Config

    def __init__(self):
        self.interface = None

    def get_interface(self) -> Optional[InputModule]:
        return self.interface

    async def setup(self, context: InputPluginContext) -> None:
        """Entry point used by Kalinka"""
        self.interface = {{ cookiecutter.plugin_class_prefix }}InputModule(context.config)
        context.logger.info("%s set up (SDK %s)", self.PLUGIN_ID, context.sdk_version)

    async def shutdown(self) -> None:
        """Clean up resources"""
        self.interface = None
{%- else %}
from typing import Optional

from kalinka_plugin_sdk.ext_device import ExternalOutputDevice
from kalinka_plugin_sdk.plugin import OutputDevicePlugin, OutputDevicePluginContext

class KalinkaPlugin{{ cookiecutter.plugin_class_prefix }}(OutputDevicePlugin):
    REQUIRES_SDK = "{{ cookiecutter.sdk_version_constraint }}"
    PLUGIN_ID = "{{ cookiecutter.name }}"
    CONFIG_MODEL = {{ cookiecutter.plugin_class_prefix }}Config

    def __init__(self):
        self._device = None

    def get_interface(self) -> Optional[ExternalOutputDevice]:
        return self._device

    async def setup(self, context: OutputDevicePluginContext) -> None:
        """Entry point used by Kalinka"""
        self._device = {{ cookiecutter.plugin_class_prefix }}Device(context.config, context.emitter)
        context.logger.info("%s set up (SDK %s)", self.PLUGIN_ID, context.sdk_version)

    async def shutdown(self) -> None:
        """Clean up resources"""
        self._device = None
{%- endif %}
```

{%- if cookiecutter.plugin_type == "input_module" %}
### 3. Input Module Implementation (`{{ cookiecutter.plugin_id }}_input_module.py`)
Implements the `InputModule` interface with all required methods for music streaming functionality:

- `module_name()` - Return display name of the module
- `search()` - Search for tracks, albums, artists
- `browse()` - Browse music catalogs
- `get_track_info()` - Get detailed track information
- `list_favorite()` - List user favorites
- `get_favorite_ids()` - Get all favorite IDs
- `add_to_favorite()` - Add items to favorites
- `remove_from_favorite()` - Remove items from favorites
- `list_filter_values()` - List the values of one catalog filter field
- `get()` - Get specific entity by ID
- `playlist_user_list()` - List user playlists
- `playlist_create()` - Create new playlist
- `playlist_update()` - Update playlist metadata
- `playlist_delete()` - Delete playlist
- `playlist_add_tracks()` - Add tracks to playlist
- `playlist_remove_tracks()` - Remove tracks from playlist
- `get_resource_path()` - Get cover art resource URLs
{%- else %}
### 3. Device Implementation (`{{ cookiecutter.plugin_id }}_device.py`)
Implements the `ExternalOutputDevice` interface with all required methods for device control:

- `get_volume()` - Get current device volume
- `set_volume()` - Set device volume (0 to 100)
- `power_on()` - Turn device on
- `is_power_on()` - Check if device is powered on
- `power_off()` - Turn device off
- `supported_functions()` - Return list of supported device functions

The device interface focuses on external audio device control rather than playback control.
Every method but `supported_functions()` is `async`. The device reports its volume and power
state through the `emitter` it is given, including changes made on the device itself.
{%- endif %}

## Building

### Prerequisites
- Python {{ cookiecutter.python_version }}+
- `kalinka-plugin-sdk` package
- Build tools: `python3-build`, `setuptools`, `setuptools-scm`, `wheel`
- For Debian packaging: `dpkg-dev`
- Git repository with proper tags for version detection

### Version Management
This plugin uses **setuptools_scm** for automatic version detection:
- **Release builds**: Tag your release with `{{ cookiecutter.plugin_name }}-v1.2.3` format
- **Development builds**: setuptools_scm automatically generates dev versions like `1.2.4.dev0+gc1e6070.d20250928`
- **Clean releases**: Commit all changes and tag for clean release versions

### Build Python Wheel
```bash
./scripts/build_wheel.sh
```
The script automatically:
- Detects version from git tags using setuptools_scm
- Generates `_version.py` with the detected version
- Builds the wheel with proper version metadata

### Build Debian Package
```bash
./scripts/build_deb.sh
```
The script automatically:
- Builds the wheel first to detect the version
- Generates Debian control files with the correct version
- Creates a `.deb` package ready for installation

The Debian package will:
1. Install the wheel to `/opt/kalinka/wheels/`
2. Activate the `kalinka-server-restart` trigger, so Kalinka restarts and its startup installs the wheel into Kalinka's venv

## Installation

### From Wheel
```bash
# Install into Kalinka's venv
/opt/kalinka/venv/bin/pip install dist/{{ cookiecutter.plugin_id }}-*.whl
```

### From Debian Package
```bash
sudo dpkg -i {{ cookiecutter.plugin_name }}_*_all.deb
```

## Configuration

After installation, the plugin appears in Kalinka's configuration interface where you can:
- Enable/disable the plugin
- Configure plugin-specific settings
- Test the plugin functionality

## Development

### Testing
Run the included smoke tests:

```bash
pytest tests/
```

### Logging
`context.logger` is a standard `logging.Logger` named after the plugin. Pass values as
%-style arguments; it takes no keyword fields:

```python
context.logger.info("Found %d albums for %s", len(albums), query)
```

### Error Handling
Implement proper error handling for network/API failures and provide meaningful error messages.

## License

This plugin is released under the {{ cookiecutter.license }} license.

## Support

For questions about this plugin or plugin development in general:
- Check the Kalinka Plugin SDK documentation
- Review other plugins in the ecosystem
- Consult the main Kalinka project documentation
