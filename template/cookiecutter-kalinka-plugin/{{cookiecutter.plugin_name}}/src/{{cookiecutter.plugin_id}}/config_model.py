from pydantic import Field
from kalinka_plugin_sdk.module_config import ModuleConfig


class {{ cookiecutter.plugin_class_prefix }}Config(ModuleConfig):
    name: str = Field(
        default="{{ cookiecutter.name }}",
        title="{{ cookiecutter.plugin_display_name }}",
        frozen=True,
        exclude=True,
    )
    enabled: bool = Field(
        default=False,
        title="Module enabled",
        json_schema_extra={"importance": "simple"},
    )
