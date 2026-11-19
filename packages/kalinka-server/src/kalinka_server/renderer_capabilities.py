"""What a renderer says it can do, as its Hello and CapabilitiesChanged carry it.

Only part of it is a plugin's business: ``output`` is what DirectPlayback tells
a plugin the output can play. The rest stays with the server.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from kalinka_plugin_sdk.direct_playback import OutputCapabilities


@dataclass(frozen=True)
class DspCapabilities:
    """What the renderer's DSP is built to do, whatever it is set to."""

    dsd_to_pcm: bool = False
    #: The SignalStep kinds the renderer can report.
    steps: tuple[str, ...] = ()
    max_output_channels: int = 0


@dataclass(frozen=True)
class RendererCapabilities:
    """One renderer's capabilities, replaced whole whenever it restates them.

    ``dsp`` is None from a renderer too old to describe its DSP.
    """

    output: OutputCapabilities
    dsp: Optional[DspCapabilities] = None
