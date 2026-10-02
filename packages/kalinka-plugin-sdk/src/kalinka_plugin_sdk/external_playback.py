"""SDK 3.7: metadata and arbitration for a plugin with its own audio engine.

Unlike DirectPlayback this never opens a renderer or decodes audio. The
plugin must stop its engine before on_revoked returns, including when its
network control connection has failed. Callbacks must finish within three
seconds and must not acquire/release a hold from on_revoked (the arbiter is
waiting for it). Reporting/releasing an ended session cannot revive it.
"""

from typing import Protocol

from .datamodel import PlaybackState
from .direct_playback import RevokeReason, TransportRequest


class ExternalPlaybackListener(Protocol):
    async def on_command(self, request: TransportRequest) -> None: ...

    async def on_revoked(self, reason: RevokeReason) -> None:
        """Stop external audio before returning; called once per revocation."""
        ...


class ExternalPlaybackSession(Protocol):
    @property
    def active(self) -> bool: ...

    def report(self, state: PlaybackState) -> None:
        """Publish metadata/progress. Positions are milliseconds.

        The server stamps receipt with its monotonic clock. Raises HoldEnded
        after revocation. Unknown audio details should remain absent.
        """
        ...

    async def release(self) -> None:
        """Give up ownership after external audio has stopped. Idempotent."""
        ...


class ExternalPlayback(Protocol):
    async def acquire(
        self, title: str, listener: ExternalPlaybackListener
    ) -> ExternalPlaybackSession:
        """Stop the previous source and hold playback without a renderer.

        Renderer selection changes revoke the hold; external audio cannot
        migrate to another Kalinka renderer.
        """
        ...
