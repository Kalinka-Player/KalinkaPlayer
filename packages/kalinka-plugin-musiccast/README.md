# Kalinka Plugin Kalinka MusicCast Plugin

A plugin for Kalinka to support Yamaha MusicCast device for volume control and automatic power on / off and input switch

## Overview

This plugin was generated from the Kalinka Plugin cookiecutter template and provides a device implementation for the Kalinka Music Player system.

## Plugin Information

- **Plugin Name**: Kalinka MusicCast Plugin
- **Plugin ID**: `musiccast`
- **Plugin Type**: device
- **Author**: Dmitry Savin <envelsavinds@gmail.com>
- **License**: GPL-3.0-or-later
- **Version**: 1.0.0

## Plugin Structure

```
kalinka-plugin-kalinka-plugin-musiccast/
├── README.md                    # This file
├── pyproject.toml              # Python package configuration
├── src/
│   └── musiccast/
│       ├── __init__.py         # Package initialization
│       ├── _version.py         # Version information
│       ├── config_model.py     # Plugin configuration schema
│       ├── module_setup.py     # Plugin entry point and setup
│       └── musiccast_device.py       # Device implementation
├── debian/                     # Debian packaging files
│   ├── control.in             # Package metadata template
│   ├── postinst              # Post-installation script
│   ├── prerm                 # Pre-removal script
│   └── rules                 # Build rules
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

class KalinkaPluginMusiccastConfig(ModuleConfig):
    name: str = Field(default="musiccast", frozen=True, exclude=True)
    enabled: bool = Field(default=False, title="Module Enabled")
    # Add your plugin-specific configuration fields here
```

### 2. Module Setup (`module_setup.py`)
The main entry point that Kalinka calls to initialize your plugin:

```python
from kalinka_plugin_sdk.api import PluginContext
from kalinka_plugin_sdk.ext_device import ExternalOutputDevice

def setup(cfg: KalinkaPluginMusiccastConfig, ctx: PluginContext) -> ExternalOutputDevice:
    """Entry point used by Kalinka"""
    ctx.logger.info("plugin_setup", plugin="musiccast")
    return KalinkaPluginMusiccastDevice(cfg)
```
### 3. Device Implementation (`musiccast_device.py`)
Implements the `ExternalOutputDevice` interface with all required methods for audio output:

- `play()` - Start playing a track
- `pause()` - Pause playback
- `resume()` - Resume playback
- `stop()` - Stop playback
- `set_volume()` - Control volume
- `seek()` - Seek to position
- And other device control methods

## Building

### Prerequisites
- Python 3.11+
- `kalinka-plugin-sdk` package
- Build tools: `python3-build`, `setuptools`, `setuptools-scm`, `wheel`
- For Debian packaging: `dpkg-dev`
- Git repository with proper tags for version detection

### Version Management
This plugin uses **setuptools_scm** for automatic version detection:
- **Release builds**: A single `kalinka-vX.Y.Z` tag versions this plugin together with the server and the other first-party plugins (the SDK is versioned independently by its own SemVer, no tag)
- **Development builds**: commits after the latest `kalinka-v*` tag produce a dev version from `git describe`, e.g. `0.1.1.dev4+g1a2b3c4.d20260608`
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
1. Install the wheel to `/usr/share/kalinka/plugins/`
2. Use post-install script to install into Kalinka's venv
3. Restart Kalinka service if available

## Installation

### From Wheel
```bash
# Install into Kalinka's venv
/opt/kalinka/venv/bin/pip install kalinka-plugin-kalinka-plugin-musiccast-*.whl
```

### From Debian Package
```bash
sudo dpkg -i kalinka-plugin-kalinka-plugin-musiccast_*_all.deb
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
Use the context logger for structured logging:

```python
ctx.logger.info("message", key="value")
```

### Error Handling
Implement proper error handling for network/API failures and provide meaningful error messages.

## License

This plugin is released under the GPL-3.0-or-later license.

## Support

For questions about this plugin or plugin development in general:
- Check the Kalinka Plugin SDK documentation
- Review other plugins in the ecosystem
- Consult the main Kalinka project documentation
