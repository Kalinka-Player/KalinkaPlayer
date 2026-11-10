from pydantic import BaseModel, Field

from kalinka_plugin_sdk.module_config import ModuleConfig
from kalinka_server.config_model import KalinkaConfig
from kalinka_server.config_schema_processor import build_presentation


class Feature(BaseModel):
    enabled: bool = Field(
        default=False,
        description="Search by sound.",
        json_schema_extra={
            "importance": "simple",
            "help_details": "The first analysis can take hours.",
        },
    )


class HelpConfig(ModuleConfig):
    name: str = Field(default="help_test", exclude=True)
    interval: int = Field(
        default=15,
        json_schema_extra={
            "importance": "simple",
            "help": "Minutes between scans.",
            "help_details": "Set to 0 to stop scheduled scans.",
        },
    )
    feature: Feature = Field(default_factory=Feature)
    expert_value: int = Field(
        default=1, json_schema_extra={"help_details": "Expert help."}
    )
    legacy_value: int = 2


def test_detailed_help_reaches_regular_nested_and_expert_fields():
    schema = build_presentation(KalinkaConfig(), {"help_test": HelpConfig()}, {})
    module = schema.pages[1].modules[0]
    interval = next(f for f in module.fields if f.path.endswith(".interval"))
    nested = module.sections[0].fields[0]
    expert = {f.path: f for f in schema.expert_fields}

    assert interval.help == "Minutes between scans."
    assert interval.help_details == "Set to 0 to stop scheduled scans."
    assert nested.help == "Search by sound."
    assert nested.help_details == "The first analysis can take hours."
    assert expert[interval.path].help_details == interval.help_details
    assert expert[nested.path].help_details == nested.help_details
    assert expert["input_modules.help_test.expert_value"].help_details == "Expert help."
    assert expert["input_modules.help_test.legacy_value"].help_details is None
    payload = schema.model_dump(mode="json")
    exposed = next(f for f in payload["expert_fields"] if f["path"] == interval.path)
    assert exposed["help_details"] == interval.help_details
