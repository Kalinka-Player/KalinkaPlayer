"""What the Core keeps about a renderer, shown on the renderer's own page.

A renderer knows its driver, its card and how it applies volume. What its
output is wired into — an amp or a receiver with a volume of its own — it
cannot see, so the Core holds that mapping and answers for it. It rides the
renderer's settings payload, and is written by path like everything else there,
so the page reads as one thing.

Mapping a renderer to a module moves whose volume and power clients drive: the
module takes over while that renderer is the one playing, and switching
renderers switches it with them.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any, Awaitable, Callable, Mapping, Optional

from .renderer_prefs import REPLAYGAIN_MODES, RendererPreferences
from .replaygain import ADJUSTABLE_VOLUME_MODES

logger = logging.getLogger(__name__.split(".")[-1])

SECTION_PATH = "core.output"
DEVICE_MODULE_PATH = "core.output.device_module"
REPLAYGAIN_PATH = "core.output.replaygain"

# The absent mapping: the renderer controls itself.
RENDERER_ITSELF = ""
RENDERER_ITSELF_LABEL = "This renderer"

# What a module is offered as, when it has no title of its own.
Candidates = Callable[[], list[tuple[str, str]]]

REPLAYGAIN_OPTIONS = [
    {
        "value": "disabled",
        "label": "Disabled",
        "description": "Tracks play at the volume you set.",
    },
    {
        "value": "album",
        "label": "Album level",
        "description": (
            "Each album plays at a similar loudness. Quiet and loud songs "
            "within an album stay as they are."
        ),
    },
    {
        "value": "track",
        "label": "Track level",
        "description": (
            "Every track plays at a similar loudness. Good for playlists and "
            "shuffle."
        ),
    },
]


def _enum_field(
    path: str,
    title: str,
    description: str,
    value: str,
    default: str,
    options: list[dict],
    read_only: bool,
) -> dict:
    if read_only:
        # The page shows a read-only field as its value, not as a chosen option.
        value = next(
            (option["label"] for option in options if option["value"] == value),
            value,
        )
    return {
        "path": path,
        "title": title,
        "description": description,
        "type": "enum",
        "value": value,
        "default": default,
        "options": options,
        "apply": "instant",
        "read_only": read_only,
        "importance": "simple",
        "range": None,
        "unit": "",
        "widget": "",
    }


class CoreRendererSettings:
    def __init__(
        self,
        prefs: RendererPreferences,
        candidates: Candidates,
        on_changed: Optional[Callable[[], Awaitable[None]]] = None,
        get_config: Optional[Callable[[str], Awaitable[dict]]] = None,
    ):
        self._prefs = prefs
        # (module id, label) of every module a renderer can be mapped to.
        self._candidates = candidates
        self._on_changed = on_changed
        self._get_config = get_config

    def owns(self, path: str) -> bool:
        return path in (DEVICE_MODULE_PATH, REPLAYGAIN_PATH)

    def section(self, renderer_id: str) -> dict:
        candidates = self._candidates()
        mapped = self._prefs.volume_control(renderer_id) or RENDERER_ITSELF
        options = [
            {
                "value": RENDERER_ITSELF,
                "label": RENDERER_ITSELF_LABEL,
                "description": "The renderer sets its own volume.",
            }
        ] + [
            {"value": module, "label": label, "description": ""}
            for module, label in candidates
        ]
        if mapped and all(module != mapped for module, _ in candidates):
            # Mapped to something not available right now; it must stay
            # selectable or the page could not show what is set.
            options.append(
                {
                    "value": mapped,
                    "label": f"{mapped} (unavailable)",
                    "description": "Selected, but its plugin is not running.",
                }
            )
        return {
            "path": SECTION_PATH,
            "title": "Connected device",
            "description": "",
            "fields": [
                _enum_field(
                    DEVICE_MODULE_PATH,
                    "Volume and power",
                    "The amplifier or receiver this renderer is connected to. "
                    "Volume and power changes then go to that device, and the "
                    "renderer plays at full level from the next track.",
                    mapped,
                    RENDERER_ITSELF,
                    options,
                    read_only=not candidates and not mapped,
                )
            ],
        }

    def merge_into(self, snapshot: dict, renderer_id: str) -> dict:
        """The renderer's payload with the Core's section on the end."""
        section = self.section(renderer_id)
        section["fields"].append(self._replaygain_field(renderer_id, snapshot))
        snapshot.setdefault("sections", []).append(section)
        snapshot["config_version"] = (
            f"{snapshot.get('config_version', '')}.{self.fingerprint()}"
        )
        return snapshot

    def _replaygain_field(self, renderer_id: str, snapshot: dict) -> dict:
        available = self._replaygain_available(renderer_id, snapshot)
        return _enum_field(
            REPLAYGAIN_PATH,
            "ReplayGain",
            "Plays tracks at a similar loudness using their ReplayGain tags. "
            "The volume is moved for each track and set back when playback "
            "stops. Tracks without tags are left as they are. Does not work "
            "with Fixed output, or with a card mixer that does not report its "
            "volume in dB.",
            # Not applied without a volume it can move, whatever is saved.
            self._prefs.replaygain(renderer_id) if available else "disabled",
            "disabled",
            [dict(option) for option in REPLAYGAIN_OPTIONS],
            read_only=not available,
        )

    def split(self, changes: Mapping[str, Any]) -> tuple[dict, dict]:
        """The changes that are ours, and the ones for the renderer."""
        ours = {path: value for path, value in changes.items() if self.owns(path)}
        theirs = {
            path: value for path, value in changes.items() if path not in ours
        }
        return ours, theirs

    def fingerprint(self) -> str:
        """Folds into the renderer's config_version: the options change as
        modules come and go, and a page holding the old shape must know."""
        blob = "replaygain-v1\x1f" + "\x1f".join(
            module for module, _ in self._candidates()
        )
        return hashlib.blake2s(blob.encode(), digest_size=4).hexdigest()

    async def apply(
        self, renderer_id: str, changes: Mapping[str, Any]
    ) -> list[dict]:
        outcomes = []
        for path in sorted(changes, key=lambda path: path != DEVICE_MODULE_PATH):
            value = changes[path]
            outcomes.append(await self._apply_one(renderer_id, path, str(value)))
        return outcomes

    async def _apply_one(self, renderer_id: str, path: str, value: str) -> dict:
        if path == REPLAYGAIN_PATH:
            error = ""
            if value not in REPLAYGAIN_MODES:
                error = "ReplayGain must be disabled, album or track"
            elif value != "disabled" and not self._prefs.volume_control(renderer_id):
                snapshot = (
                    await self._get_config(renderer_id) if self._get_config else {}
                )
                if not self._replaygain_available(renderer_id, snapshot):
                    error = "ReplayGain cannot change this renderer's volume"
            if error:
                return {
                    "path": path,
                    "applied": False,
                    "value": self._prefs.replaygain(renderer_id),
                    "error": error,
                }
            self._prefs.set_replaygain(renderer_id, value)
            if self._on_changed is not None:
                await self._on_changed()
            return {"path": path, "applied": True, "value": value, "error": ""}
        current = self._prefs.volume_control(renderer_id) or RENDERER_ITSELF
        if not self.owns(path):
            return {
                "path": path,
                "applied": False,
                "value": current,
                "error": "no such setting",
            }
        if value != RENDERER_ITSELF and all(
            module != value for module, _ in self._candidates()
        ):
            return {
                "path": path,
                "applied": False,
                "value": current,
                "error": f"'{value}' is not an enabled device module with volume control",
            }
        self._prefs.set_volume_control(
            renderer_id, value if value != RENDERER_ITSELF else None
        )
        logger.info(
            "Renderer %s volume control: %s",
            renderer_id,
            value or "renderer itself",
        )
        if self._on_changed is not None:
            await self._on_changed()
        return {"path": path, "applied": True, "value": value, "error": ""}

    def _replaygain_available(self, renderer_id: str, snapshot: dict) -> bool:
        return bool(self._prefs.volume_control(renderer_id)) or any(
            field.get("path") == "output.volume_mode"
            and field.get("value") in ADJUSTABLE_VOLUME_MODES
            for section in snapshot.get("sections", [])
            for field in section.get("fields", [])
        )
