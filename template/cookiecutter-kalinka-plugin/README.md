# Kalinka Plugin Cookiecutter Template

This is a cookiecutter template for creating new Kalinka Music Player plugins. It provides a complete project structure with all necessary files, configuration, and boilerplate code for both input module and device plugins.

## Prerequisites

Before using this template, ensure you have:

- Python 3.10+
- [cookiecutter](https://cookiecutter.readthedocs.io/) installed
- Git (for version management)
- Basic understanding of Kalinka Plugin SDK

### Installing Cookiecutter

```bash
pip install cookiecutter
```

## Using the Template

### 1. Generate a New Plugin

Navigate to where you want to create your new plugin and run:

```bash
cookiecutter /path/to/cookiecutter-kalinka-plugin
```

Or if you're in the same directory as the template:

```bash
cookiecutter cookiecutter-kalinka-plugin
```

### 2. Template Variables

When you run cookiecutter, you'll be prompted to provide values for the following variables:

- **name**: Your plugin's core name (e.g., "myawesome", "musicbox", "localfiles")
  - Used as base for: package naming, folder names, git tags
  - Format: Simple name without prefixes (lowercase, no hyphens or spaces)
  - Example: "musicbox" becomes "kalinka-plugin-musicbox"

- **plugin_name**: Full plugin name (auto-generated from name)
  - Format: "kalinka-plugin-{name}"
  - Example: "kalinka-plugin-musicbox"

- **plugin_id**: Python package identifier (auto-generated from name)
  - Format: "kalinka_plugin_{name}" (snake_case)
  - Example: "kalinka_plugin_musicbox"

- **plugin_class_prefix**: Class name prefix (auto-generated from name)
  - Format: PascalCase (no spaces or hyphens)
  - Example: "Musicbox" becomes "MusicboxPlugin"

- **plugin_display_name**: Human-readable name (auto-generated from name)
  - Format: Title Case
  - Example: "Musicbox"

- **plugin_description**: Brief description of your plugin's functionality

- **plugin_type**: Choose between:
  - `input_module`: For music streaming services, local file browsers, etc.
  - `device`: For audio output devices, external players, etc.

- **author_name**: Your name

- **author_email**: Your email address

- **version**: Initial version (default: "1.0.0")

- **python_version**: Minimum Python version (default: "3.10")

- **sdk_version_constraint**: SDK version constraint (default: ">=3,<4")

- **license**: Choose from:
  - GPL-3.0-or-later (default)
  - MIT
  - Apache-2.0
  - BSD-3-Clause

### 3. Example Session

```bash
$ cookiecutter cookiecutter-kalinka-plugin
name [myawesome]: musicbox
plugin_name [kalinka-plugin-musicbox]: 
plugin_id [kalinka_plugin_musicbox]: 
plugin_class_prefix [Musicbox]: 
plugin_display_name [Musicbox]: 
plugin_description [A Kalinka music player plugin]: Music streaming service integration for Kalinka
plugin_type [input_module]: 
author_name [Your Name]: John Doe
author_email [your.email@example.com]: john@example.com
version [1.0.0]: 
python_version [3.10]: 
sdk_version_constraint [>=3,<4]: 
license [GPL-3.0-or-later]: MIT
```

## Generated Project Structure

After generation, you'll have a complete plugin project:

```
kalinka-plugin-musicbox/
├── README.md                    # Project documentation
├── pyproject.toml              # Python package configuration
├── src/
│   └── kalinka_plugin_musicbox/ # Main Python package
│       ├── __init__.py         # Package initialization with plugin class import
│       ├── _version.py         # Auto-generated version file
│       ├── config_model.py     # Plugin configuration schema
│       ├── module_setup.py     # Plugin class definition and entry point
│       └── musicbox_input_module.py  # Input module (or device) implementation
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

## Next Steps After Generation

### 1. Initialize Git Repository

```bash
cd your-new-plugin
git init
git add .
git commit -m "Initial commit from cookiecutter template"
```

### 2. Implement Your Plugin Logic

**Note**: For detailed API documentation, parameter specifications, and implementation examples, refer to the [kalinka-plugin-sdk documentation](../kalinka-plugin-sdk/README.md).

#### For Input Module Plugins:
Edit `src/your_plugin/your_plugin_input_module.py` and implement:
- `module_name()` - Return the display name of your module
- `search()` - Search for tracks, albums, artists, playlists
- `browse()` - Browse music catalogs and collections
- `get_track_info()` - Get detailed track information for playback
- `list_favorite()` - List user favorites (tracks, albums, etc.)
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
- `get_resource_path()` - Get URLs for cover art and other resources

#### For Device Plugins:
Edit `src/your_plugin/your_plugin_device.py` and implement:
- `get_volume()` - Get current device volume
- `set_volume()` - Set device volume
- `power_on()` - Turn device on
- `is_power_on()` - Check if device is powered on
- `power_off()` - Turn device off
- `supported_functions()` - Return list of supported device functions

### 3. Add Configuration Fields

Edit `src/your_plugin/config_model.py` to add plugin-specific configuration:

```python
class YourPluginConfig(ModuleConfig):
    name: str = Field(default="your_plugin", frozen=True, exclude=True)
    enabled: bool = Field(default=False, title="Module Enabled")
    
    # Add your custom fields
    api_key: str = Field(default="", title="API Key", description="Your service API key")
    server_url: str = Field(default="https://api.example.com", title="Server URL")
    timeout: int = Field(default=30, title="Request Timeout (seconds)")
```

### 4. Update Module Setup

Edit `src/your_plugin/module_setup.py` to add any initialization logic:

```python
class KalinkaPluginYourPlugin(InputModulePlugin):  # or OutputDevicePlugin
    REQUIRES_SDK = ">=2.0,<3"
    PLUGIN_ID = "your_plugin"
    CONFIG_MODEL = YourPluginConfig

    def __init__(self):
        self.interface = None  # or self._device = None for devices

    def setup(self, context: PluginContext) -> None:
        """Entry point used by Kalinka"""
        config = YourPluginConfig(**context.config.model_dump())
        
        # Add any initialization logic here
        if not config.api_key:
            context.logger.warning("No API key configured")
        
        self.interface = YourPluginInputModule(config)  # or YourPluginDevice
        context.logger.info("plugin_setup", plugin=self.PLUGIN_ID, version=context.sdk_version)

    def shutdown(self) -> None:
        """Clean up resources"""
        self.interface = None  # or self._device = None
```

### 5. Add Tests

Extend `tests/test_smoke.py` with your own tests:

```python
def test_custom_functionality():
    """Test your plugin's custom functionality"""
    config = YourPluginConfig(api_key="test-key")
    module = YourPluginInputModule(config)
    
    # Add your tests here
    assert module.is_configured()
```

### 6. Build and Test

```bash
# Install in development mode
pip install -e .

# Run tests
pytest tests/

# Build wheel
./scripts/build_wheel.sh

# Build Debian package (optional)
./scripts/build_deb.sh
```

### 7. Version Management

The template uses setuptools_scm for automatic versioning:

```bash
# Create a release
git tag kalinka-plugin-{name}-v1.0.0
git push origin kalinka-plugin-{name}-v1.0.0

# Development versions are automatically generated
```

## Template Features

### Conditional Content

The template includes conditional content based on plugin type:
- Input module plugins get input module implementation stubs
- Device plugins get device implementation stubs
- Tests are customized for the chosen plugin type

### Automatic Naming

Many values are automatically derived from your plugin name:
- Plugin names get "kalinka-plugin-" prefix automatically
- Package names are converted to "kalinka_plugin_{name}" format
- Class names are converted to PascalCase
- Display names are converted to Title Case

### Complete Build System

The generated project includes:
- Python packaging with setuptools
- Class-based entry points for plugin discovery (`kalinka_plugin_name = "package:PluginClass"`)
- Automatic version management with setuptools_scm
- Debian packaging support
- Test framework setup
- Development dependency management

### Documentation

Each generated project includes:
- Comprehensive README with usage instructions
- Inline code documentation
- Build and installation instructions

## Best Practices

### Plugin Naming
- Use descriptive, simple names (e.g., "myawesome", "localfiles", "musiccast")
- Avoid prefixes like "kalinka-plugin-" (these are added automatically)
- Use lowercase, no hyphens or spaces in the base name
- Include the service/device name if applicable

### Development
- Start with the smoke tests to ensure basic structure works
- Implement one method at a time and test incrementally
- Use structured logging with the provided context logger
- Handle errors gracefully and provide meaningful messages

### Configuration
- Add validation to your configuration model
- Provide sensible defaults
- Document all configuration options
- Mark sensitive fields appropriately

### Testing
- Write tests for your core functionality
- Test error conditions and edge cases
- Use pytest fixtures for common setup
- Mock external services during testing

## Troubleshooting

### Common Issues

**Template not found**: Ensure you're using the correct path to the template directory.

**Permission errors**: Make sure you have write permissions in the target directory.

**Missing dependencies**: Install cookiecutter and ensure Python 3.10+ is available.

**Build failures**: Check that all template variables were properly substituted and that there are no syntax errors in generated files.

### Getting Help

- Check the Kalinka Plugin SDK documentation
- Review existing plugins for examples
- Ask for help in the Kalinka community

## Contributing to the Template

If you find issues with the template or want to add features:

1. Test your changes with different variable combinations
2. Ensure both plugin types (input_module and device) work correctly
3. Update this documentation as needed
4. Test the generated projects can be built and installed

The template is designed to be a solid starting point for plugin development while remaining flexible enough for various use cases.