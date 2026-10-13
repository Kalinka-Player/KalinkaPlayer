import asyncio
import logging
from typing import ClassVar, Optional

from kalinka_plugin_sdk import ModuleHealthState, ModuleState, OptionalPackageSpec
from kalinka_plugin_sdk.plugin import InputModulePlugin, InputPluginContext
from kalinka_plugin_sdk.inputmodule import InputModule

from .config_model import JamendoConfig
from .jamendo import JamendoInputModule, get_client
from .mood_search import JamendoMoodIndex
from .optional_packages import OPTIONAL_PACKAGES

logger = logging.getLogger(__name__.split(".")[-1])


class KalinkaPluginJamendo(InputModulePlugin):
    # 3 replaced the genre API with the filter contract this module
    # declares; 3.12 lets the shared embedder name the runtime it lacks.
    # Keep in sync with pyproject's kalinka-plugin-sdk pin.
    REQUIRES_SDK = ">=3.12,<4"
    PLUGIN_ID = "jamendo"
    CONFIG_MODEL = JamendoConfig
    OPTIONAL_PACKAGES: ClassVar[dict[str, OptionalPackageSpec]] = OPTIONAL_PACKAGES

    def __init__(self):
        self.interface: Optional[InputModule] = None
        self._client = None
        # Captured at setup so get_state() can read the current config.
        self._context: Optional[InputPluginContext] = None
        self._provision_task: Optional[asyncio.Task] = None

    def get_interface(self) -> Optional[InputModule]:
        return self.interface

    async def setup(self, context: InputPluginContext) -> None:
        self._context = context
        config = JamendoConfig(**context.config.model_dump())
        logger.info("Setting up Jamendo input module")
        self._client = await get_client(config)
        mood_index = None
        if config.ai_search_enabled:
            mood_index = JamendoMoodIndex(
                config.ai_index_path,
                config.ai_index_url or None,
                context.embedder,
            )
            # Provision assets now (download the index, warm the shared
            # embedder) rather than blocking the first search. available() is
            # concurrency-safe, so a search that arrives mid-download just
            # awaits the same provisioning.
            self._provision_task = asyncio.create_task(mood_index.available())
        self.interface = JamendoInputModule(config, self._client, mood_index)

    async def get_state(self) -> ModuleState:
        """Surface the "no client_id configured" case as an ERROR, and a
        mood search without its runtime as a WARNING.

        Without a client_id every Jamendo request returns empty, which is
        indistinguishable from "no results" in the UI. Reporting it here lets
        the modules page tell the user to set their key instead of silently
        showing an empty catalog. Mood search without its runtime would be
        just as silent, so its packages are reported for the modules page to
        offer.
        """
        client_id = ""
        if self._context is not None:
            client_id = getattr(self._context.config, "client_id", "") or ""

        if not client_id:
            return ModuleState(
                state=ModuleHealthState.ERROR,
                message=(
                    "No Jamendo client_id configured. Create a free application "
                    "at [Jamendo Dev Portal](https://devportal.jamendo.com) and paste its Client ID "
                    "into this module's settings."
                ),
            )
        missing = self._missing_packages()
        if missing:
            return ModuleState(
                state=ModuleHealthState.WARNING,
                message=(
                    "Mood search unavailable. Missing package(s): "
                    + ", ".join(f"`{m}`" for m in missing)
                    + ". Use **Restart with install** in the modules page to "
                    "fetch them."
                ),
                missing_packages=missing,
            )
        return ModuleState(state=ModuleHealthState.READY)

    async def required_packages(self) -> list[str]:
        """Packages mood search needs under the current config, which may
        hold changes the user staged since setup."""
        return self._missing_packages()

    def _missing_packages(self) -> list[str]:
        """Optional packages mood search needs but cannot import, as the
        shared embedder names its runtime. None without a client_id:
        nothing on Jamendo works then, so a box that never set Jamendo up
        never installs its runtime."""
        if self._context is None:
            return []
        config = self._context.config
        if not getattr(config, "client_id", "") or not getattr(
            config, "ai_search_enabled", False
        ):
            return []
        # A server before SDK 3.12 installed the runtime with itself.
        missing_runtime = getattr(self._context.embedder, "missing_runtime", None)
        if missing_runtime is None:
            return []
        lacking = set(missing_runtime())
        return [
            key
            for key, spec in OPTIONAL_PACKAGES.items()
            if (spec.import_name or key) in lacking
        ]

    async def shutdown(self) -> None:
        logger.info("Shutting down Jamendo input module")
        if self._provision_task is not None:
            self._provision_task.cancel()
        if self._client is not None:
            await self._client.aclose()
