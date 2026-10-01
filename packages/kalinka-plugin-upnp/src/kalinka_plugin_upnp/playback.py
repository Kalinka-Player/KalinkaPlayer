"""Serialize UPnP and Kalinka controls over an exclusive renderer session."""

import asyncio
import logging
import time
from collections import deque
from collections.abc import Callable

from kalinka_plugin_sdk.datamodel import DeviceVolume, PlaybackState, PlayerStateEnum
from kalinka_plugin_sdk.direct_playback import (
    DirectPlayback,
    HoldEnded,
    OutputUnavailable,
    TransportKind,
)

from .media import Media, UpnpError

logger = logging.getLogger(__name__)


class SessionListener:
    """Fence late callbacks from an old hold without blocking the SDK listener."""

    def __init__(self, playback: "Playback"):
        self.playback = playback

    def on_state(self, state: PlaybackState):
        if self.playback.listener is self:
            self.playback.update_state(state)

    def on_volume(self, volume: DeviceVolume):
        if self.playback.listener is self:
            self.playback.update_volume(volume)

    def on_command(self, request):
        if self.playback.listener is self:
            self.playback.enqueue(self, request.kind.value, request.position_ms)

    def on_finished(self):
        if self.playback.listener is self:
            self.playback.enqueue(self, "finished", None)

    def on_next_started(self, track):
        if self.playback.listener is self:
            self.playback.next_started(track)

    def on_revoked(self, reason):
        if self.playback.listener is self:
            self.playback.hold = self.playback.listener = None
            self.playback.next_uri = None
            self.playback.forward.clear()
            self.playback.queued_media.clear()
            self.playback.scheduled_next = None
            self.playback.state = (
                "STOPPED" if self.playback.current else "NO_MEDIA_PRESENT"
            )
            self.playback.changed("AVTransport")


class Playback:
    """One UPnP transport; acquiring output requires an explicit Play command."""

    def __init__(self, direct: DirectPlayback, changed: Callable[[str], None]):
        self.direct, self.changed = direct, changed
        self.current: Media | None = None
        self.next_uri: Media | None = None
        self.history: deque[Media] = deque(maxlen=32)
        self.forward: deque[Media] = deque(maxlen=32)
        self.queued_media: deque[Media] = deque(maxlen=32)
        self.scheduled_next: Media | None = None
        self.hold = self.listener = None
        self.state = "NO_MEDIA_PRESENT"
        self.status = "OK"
        self.position_ms = 0
        self.position_timestamp_ns = 0
        self.duration_ms = 0
        self.volume = 0
        self.volume_supported = False
        self.muted = False
        self.restore_volume = 0
        self.lock = asyncio.Lock()
        self.commands = asyncio.Queue(maxsize=32)
        self.worker = None
        self.closed = False

    def start(self):
        self.worker = asyncio.create_task(self._run_commands())

    @property
    def active(self):
        return self.hold is not None and self.hold.active

    @property
    def next(self):
        return self.forward[-1] if self.forward else self.next_uri

    @property
    def previous(self):
        return self.history[-1] if self.history else None

    @property
    def actions(self):
        if not self.current:
            return ""
        actions = ["Play", "Stop", "Seek"]
        if self.active and self.state in ("PLAYING", "TRANSITIONING"):
            actions.append("Pause")
        if self.next:
            actions.append("Next")
        if self.previous:
            actions.append("Previous")
        return ",".join(actions)

    def enqueue(self, listener, kind, position):
        try:
            self.commands.put_nowait((listener, kind, position, self.current))
        except asyncio.QueueFull:
            if kind == "finished":
                self.commands.get_nowait()
                self.commands.put_nowait((listener, kind, position, self.current))
                return
            logger.warning("UPnP transport command queue is full")

    async def _run_commands(self):
        while True:
            listener, kind, position, media = await self.commands.get()
            try:
                async with self.lock:
                    if self.listener is not listener or not self.active:
                        continue
                    if kind in ("finished", "queue_next") and media is not self.current:
                        continue
                    async with asyncio.timeout(10):
                        if kind == "finished":
                            if self.next:
                                await self._next()
                            else:
                                await self._stop()
                        elif kind == TransportKind.PAUSE.value:
                            await self._pause()
                        elif kind == TransportKind.RESUME.value:
                            await self._play()
                        elif kind == TransportKind.SEEK.value:
                            await self._seek(position)
                        elif kind == TransportKind.NEXT.value:
                            await self._next()
                        elif kind == TransportKind.PREV.value:
                            await self._previous()
                        elif kind == "queue_next":
                            await self._queue_next()
            except UpnpError as exc:
                logger.info("UPnP %s unavailable: %s", kind, exc.description)
            except (HoldEnded, OutputUnavailable, TimeoutError):
                self.status = "ERROR_OCCURRED"
                self.changed("AVTransport")
            except Exception as exc:  # noqa: BLE001 - upstream errors may contain credentials
                logger.error("UPnP transport control failed (%s)", type(exc).__name__)
                self.status = "ERROR_OCCURRED"
                self.changed("AVTransport")

    async def command(self, name, *args):
        async with self.lock:
            if self.closed:
                raise UpnpError(501, "Receiver is shutting down")
            try:
                return await getattr(self, f"_{name}")(*args)
            except (HoldEnded, OutputUnavailable):
                self.status = "ERROR_OCCURRED"
                self.changed("AVTransport")
                raise UpnpError(501, "Renderer unavailable") from None

    async def _set_uri(self, media):
        self.next_uri = None
        self.forward.clear()
        if media is None:
            await self._release()
            self.history.clear()
            self.current = None
            self.duration_ms = 0
            self._stopped()
        else:
            if self.current and self.current.uri != media.uri:
                self.history.append(self.current)
            await self._replace(media)

    async def _set_next(self, media):
        self.next_uri = media
        self.forward.clear()
        await self._queue_next()
        self.changed("AVTransport")

    async def _queue_next(self):
        if not self.active or self.state == "STOPPED":
            return
        media = self.next
        if media is self.scheduled_next:
            return
        if media is not None:
            self.queued_media.append(media)
            await self.hold.set_next(media.source, media.track)
        else:
            await self.hold.set_next(None)
        self.scheduled_next = media

    def next_started(self, track):
        media = next(
            (media for media in reversed(self.queued_media) if media.track == track),
            None,
        )
        if media is None:
            return
        if self.current:
            self.history.append(self.current)
        if media is self.next:
            self._consume_next()
        if media is self.scheduled_next:
            self.scheduled_next = None
        self.current = media
        self.position_ms, self.duration_ms = 0, media.duration_ms
        self.state, self.status = "TRANSITIONING", "OK"
        self.changed("AVTransport")
        self.enqueue(self.listener, "queue_next", None)

    async def _play(self):
        if self.current is None:
            raise UpnpError(702, "No contents")
        if self.active and self.state in ("PLAYING", "TRANSITIONING"):
            return
        if self.active and self.state == "PAUSED_PLAYBACK":
            await self.hold.resume()
        else:
            if not self.active:
                listener = SessionListener(self)
                self.listener = listener
                acquiring = asyncio.create_task(self.direct.acquire("UPnP", listener))
                try:
                    hold = await asyncio.shield(acquiring)
                except asyncio.CancelledError:
                    self.listener = None
                    try:
                        hold = await acquiring
                    except Exception:  # noqa: BLE001 - acquisition failed during cancellation
                        logger.debug("UPnP acquisition failed during cancellation")
                    else:
                        await hold.release()
                    raise
                except BaseException:
                    if self.listener is listener:
                        self.listener = None
                    raise
                if self.listener is not listener or not hold.active:
                    await hold.release()
                    raise HoldEnded("UPnP hold ended while opening")
                self.hold = hold
            try:
                self.queued_media.clear()
                self.scheduled_next = None
                await self.hold.play(
                    self.current.source,
                    self.current.track,
                    start_offset_ms=self.position_ms,
                )
            except BaseException:
                await self._release()
                raise
        self.state, self.status = "TRANSITIONING", "OK"
        await self._queue_next()
        self.changed("AVTransport")

    async def _pause(self):
        if not self.active or self.state not in (
            "PLAYING",
            "TRANSITIONING",
            "PAUSED_PLAYBACK",
        ):
            raise UpnpError(701, "Transition not available")
        await self.hold.pause()
        self.position_ms = self.position_now_ms()
        self.state = "PAUSED_PLAYBACK"
        self.changed("AVTransport")

    async def _stop(self):
        self.queued_media.clear()
        self.scheduled_next = None
        if self.active:
            await self.hold.stop()
        self._stopped()

    def _stopped(self):
        self.state = "STOPPED" if self.current else "NO_MEDIA_PRESENT"
        self.position_ms = 0
        self.status = "OK"
        self.changed("AVTransport")

    async def _release(self):
        hold, self.hold, self.listener = self.hold, None, None
        self.queued_media.clear()
        self.scheduled_next = None
        self._stopped()
        if hold is not None:
            await hold.release()

    async def _seek(self, position_ms):
        if not self.current:
            raise UpnpError(701, "Transition not available")
        if (
            position_ms is None
            or position_ms < 0
            or (self.duration_ms and position_ms > self.duration_ms)
        ):
            raise UpnpError(711, "Illegal seek target")
        if self.active and self.state != "STOPPED":
            await self.hold.seek(position_ms)
        self.position_ms = position_ms
        self.position_timestamp_ns = time.monotonic_ns()
        self.changed("AVTransport")

    async def _next(self):
        if self.next is None:
            raise UpnpError(711, "The UPnP controller has not supplied a next track")
        media = self.next
        if self.current:
            self.history.append(self.current)
        self._consume_next()
        await self._replace(media)

    def _consume_next(self):
        if self.forward:
            self.forward.pop()
        else:
            self.next_uri = None

    async def _previous(self):
        if self.previous is None:
            raise UpnpError(711, "No previous UPnP track is available")
        media = self.history.pop()
        if self.current:
            self.forward.append(self.current)
        await self._replace(media)

    async def _replace(self, media):
        was_running = self.active and self.state in (
            "PLAYING",
            "TRANSITIONING",
            "PAUSED_PLAYBACK",
        )
        was_paused = self.state == "PAUSED_PLAYBACK"
        self.current = media
        self.position_ms, self.state = 0, "STOPPED"
        self.duration_ms = media.duration_ms
        self.status = "OK"
        if was_running:
            await self._play()
            if was_paused:
                await self._pause()
        self.changed("AVTransport")

    async def _set_volume(self, percent):
        if not self.active:
            raise UpnpError(701, "Volume requires an active UPnP session")
        hold = self.hold
        volume = await hold.get_volume()
        if volume is None or not volume.supported or volume.max_volume <= 0:
            raise UpnpError(501, "Volume control unavailable")
        await hold.set_volume(percent)
        self.volume, self.muted = percent, False
        self.changed("RenderingControl")

    async def _set_mute(self, muted):
        if muted == self.muted:
            return
        restore = self.volume if muted else self.restore_volume
        await self._set_volume(0 if muted else restore)
        self.restore_volume, self.muted = restore, muted
        self.changed("RenderingControl")

    def update_state(self, state):
        if (
            state.current_track
            and self.current
            and state.current_track.id != self.current.track.id
        ):
            return
        states = {
            PlayerStateEnum.PLAYING: "PLAYING",
            PlayerStateEnum.PAUSED: "PAUSED_PLAYBACK",
            PlayerStateEnum.BUFFERING: "TRANSITIONING",
            PlayerStateEnum.STOPPED: "STOPPED",
            PlayerStateEnum.ERROR: "STOPPED",
        }
        if state.state in states:
            self.state = states[state.state]
            self.status = (
                "ERROR_OCCURRED" if state.state == PlayerStateEnum.ERROR else "OK"
            )
        if state.position is not None:
            self.position_ms = max(0, state.position)
            self.position_timestamp_ns = state.timestamp_ns or time.monotonic_ns()
        if state.audio_info and state.audio_info.duration_ms > 0:
            self.duration_ms = state.audio_info.duration_ms
        self.changed("AVTransport")

    def position_now_ms(self):
        position = self.position_ms
        if self.state == "PLAYING" and self.position_timestamp_ns:
            position += max(
                0, (time.monotonic_ns() - self.position_timestamp_ns) // 1_000_000
            )
        return min(position, self.duration_ms) if self.duration_ms else position

    def update_volume(self, volume):
        self.volume_supported = volume.supported and volume.max_volume > 0
        if self.volume_supported:
            self.volume = min(
                100, max(0, round(volume.current_volume * 100 / volume.max_volume))
            )
            if self.volume:
                self.muted = False
        self.changed("RenderingControl")

    async def close(self):
        self.closed = True
        if self.worker:
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
        async with self.lock:
            await self._release()
