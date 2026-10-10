import asyncio
import json
import logging
import random
import socket
import time
import urllib.parse
from collections import deque
from typing import Any, Dict, Optional

import httpx
from kalinka_plugin_sdk.datamodel import (
    EntityId,
    PlaybackState,
    PlayerStateEnum,
    VolumeBackend,
)
from kalinka_plugin_sdk.ext_device_events import (
    DevicePowerStateChangedEvent,
    ExtDeviceState,
    VolumeChangedEvent,
)
import ifaddr
from ssdpy import SSDPClient

from kalinka_plugin_sdk.api import EventEmitter, EventListener, ReplayEvent
from kalinka_plugin_sdk.events import PlayQueueEvent, PlayQueueEventType, PlayQueueState
from kalinka_plugin_sdk.events import PlaybackStateChangedEvent
from kalinka_plugin_sdk.ext_device import (
    DeviceVolume,
    ExternalOutputDevice,
    SupportedFunction,
)
from .config_model import KalinkaPluginMusiccastConfig

logger = logging.getLogger(__name__.split(".")[-1])


def is_port_available(port):
    """Check if a port is available for binding on the local machine"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(1)

    try:
        # Try to bind to the specified port on local interface
        # Use '' or '0.0.0.0' to bind to all local interfaces
        sock.bind(("", port))
        return True
    except socket.error:
        return False
    finally:
        # Close the socket
        sock.close()


def find_available_port(start_range=49152, end_range=65535):
    # We expect that we will find an available port within 100 tries
    for _ in range(100):
        port = random.randint(start_range, end_range)

        logger.debug(f"Checking port {port}")
        if is_port_available(port):
            return port

        logger.debug(f"Port {port} is not available")

    # If no available port is found, return None
    logger.error(f"Could not find available port in range {start_range}-{end_range}")
    return None


def get_network_interfaces():
    """Get all active network interfaces with their IP addresses"""

    interfaces = []
    for adapter in ifaddr.get_adapters():
        for ip in adapter.ips:
            # ifaddr gives an IPv4 address as a string, an IPv6 one as a tuple.
            if (
                isinstance(ip.ip, str)
                and not ip.ip.startswith("127.")
                and ip.ip != "0.0.0.0"
            ):
                interfaces.append((adapter.name, ip.ip))
                logger.debug(f"Found interface {adapter.name}: {ip.ip}")

    # Final fallback: use default interface
    if not interfaces:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("8.8.8.8", 80))
                local_ip = s.getsockname()[0]
                interfaces.append(("default", local_ip))
                logger.info(f"Using default interface: {local_ip}")
        except Exception:
            logger.warning("Could not detect any network interfaces")

    return interfaces


def discover_musiccast_devices(
    iface: str, timeout_seconds=10
) -> Optional[Dict[str, Any]]:
    """
    Discover MusicCast devices using SSDP via the ssdpy library.
    Much more compact and reliable than manual SSDP implementation.
    """
    # Kept at debug — when the device is offline we retry on a slow loop and
    # the worker only logs at info on state transitions to avoid spamming.
    logger.debug("Starting SSDP MusicCast device discovery...")

    # Create SSDP client with specified timeout
    client = SSDPClient(iface=iface.encode("utf-8"), timeout=timeout_seconds)

    # Search for MediaRenderer devices (per MusicCast specification)
    logger.debug("Sending SSDP M-SEARCH for MediaRenderer devices...")
    responses = client.m_search(
        "urn:schemas-upnp-org:device:MediaRenderer:1", mx=timeout_seconds
    )

    logger.debug(f"Received {len(responses)} SSDP responses")

    # Process each response to find MusicCast devices
    for response in responses:
        logger.debug(f"Processing SSDP response: {response}")

        # Extract location URL for device description
        location = response.get("location")
        if not location:
            logger.debug("No location header in SSDP response")
            continue

        # Parse device IP from response source
        device_ip = None
        if "source" in response:
            # ssdpy includes source IP in some versions
            device_ip = response["source"][0]
        else:
            # Extract from location URL as fallback
            parsed_url = urllib.parse.urlparse(location)
            device_ip = parsed_url.hostname

        if not device_ip:
            logger.debug("Could not determine device IP")
            continue

        logger.debug(f"Fetching device description from: {location}")

        # Fetch and parse device description XML
        try:
            http_response = httpx.get(location, timeout=5)
            if http_response.status_code != 200:
                logger.debug(
                    f"Failed to fetch device description: HTTP {http_response.status_code}"
                )
                continue

            device_desc_xml = http_response.text
            logger.debug(f"Device description XML:\n{device_desc_xml}")

            # Parse XML to check for Yamaha MusicCast device
            device_info = parse_device_description(device_desc_xml, device_ip)
            if device_info:
                return device_info

        except Exception as e:
            logger.debug(f"Error fetching device description from {location}: {e}")
            continue

    logger.debug("No MusicCast devices found via SSDP discovery")
    return None


def parse_device_description(
    xml_content: str, device_ip: str
) -> Optional[Dict[str, Any]]:
    """
    Parse device description XML to verify it's a Yamaha MusicCast device
    and extract necessary information per specification.
    """
    import xml.etree.ElementTree as ET

    try:
        root = ET.fromstring(xml_content)

        # Define namespaces
        namespaces = {
            "upnp": "urn:schemas-upnp-org:device-1-0",
            "yamaha": "urn:schemas-yamaha-com:device-1-0",
        }

        # Find the device element
        device_elem = root.find(".//upnp:device", namespaces)
        if device_elem is None:
            logger.debug("No device element found in XML")
            return None

        # Check manufacturer - must be "Yamaha Corporation"
        manufacturer = device_elem.find("upnp:manufacturer", namespaces)
        if manufacturer is None or manufacturer.text != "Yamaha Corporation":
            logger.debug(
                f"Not a Yamaha device, manufacturer: {manufacturer.text if manufacturer is not None else 'None'}"
            )
            return None

        # Check for Yamaha X_device tag
        x_device = root.find(".//yamaha:X_device", namespaces)
        if x_device is None:
            logger.debug("No yamaha:X_device tag found")
            return None

        # Extract device information
        model_name = device_elem.find("upnp:modelName", namespaces)
        model_desc = device_elem.find("upnp:modelDescription", namespaces)
        friendly_name = device_elem.find("upnp:friendlyName", namespaces)
        serial_number = device_elem.find("upnp:serialNumber", namespaces)
        udn = device_elem.find("upnp:UDN", namespaces)

        # Extract Yamaha-specific information
        url_base = x_device.find("yamaha:X_URLBase", namespaces)

        # Find the YXC control URL
        yxc_control_url = None
        service_list = x_device.find("yamaha:X_serviceList", namespaces)
        if service_list is not None:
            for service in service_list.findall("yamaha:X_service", namespaces):
                spec_type = service.find("yamaha:X_specType", namespaces)
                if (
                    spec_type is not None
                    and spec_type.text
                    and "YamahaExtendedControl" in spec_type.text
                ):
                    yxc_url_elem = service.find("yamaha:X_yxcControlURL", namespaces)
                    if yxc_url_elem is not None:
                        yxc_control_url = yxc_url_elem.text
                        break

        if not yxc_control_url:
            logger.debug("No YamahaExtendedControl URL found")
            return None

        # Extract base URL and port
        base_url: str = (
            url_base.text
            if (url_base is not None and url_base.text is not None)
            else f"http://{device_ip}:80/"
        )

        # Verify this is actually a MusicCast device by testing the API
        api_base_url = safe_urljoin(base_url, yxc_control_url)
        device_details = verify_musiccast_api(api_base_url)

        if not device_details:
            logger.debug("Device does not respond to MusicCast API")
            return None

        logger.debug(
            f"Found MusicCast device: {device_details['model_name']} at {base_url}"
        )

        return {
            "api_base_url": api_base_url,
            "yxc_control_url": yxc_control_url,
            "friendly_name": friendly_name.text if friendly_name is not None else None,
            "serial_number": serial_number.text if serial_number is not None else None,
            "udn": udn.text if udn is not None else None,
            **device_details,
        }

    except ET.ParseError as e:
        logger.debug(f"XML parsing error: {e}")
        return None
    except Exception as e:
        logger.debug(f"Error parsing device description: {e}")
        return None


def safe_urljoin(base: str, path: str) -> str:
    """
    Safely join a base URL with a path, handling cases where both base and path
    have slashes to avoid double slashes or missing path components.
    """
    # Ensure base ends with /
    if not base.endswith("/"):
        base += "/"

    # Remove leading / from path to avoid urljoin treating it as absolute
    if path.startswith("/"):
        path = path[1:]

    return urllib.parse.urljoin(base, path)


def verify_musiccast_api(api_base_url: str) -> Optional[Dict[str, Any]]:
    """
    Verify the device responds to MusicCast Extended Control API
    and get device information.
    """
    try:
        # Test the getDeviceInfo endpoint
        url = safe_urljoin(api_base_url, "/system/getDeviceInfo")
        response = httpx.get(url, timeout=3)

        if response.status_code == 200:
            data = response.json()
            if data.get("response_code") == 0 and "model_name" in data:
                return {
                    "model_name": data.get("model_name"),
                    "device_id": data.get("device_id"),
                    "system_id": data.get("system_id"),
                    "version": data.get("system_version"),
                    "api_version": data.get("api_version"),
                }
    except Exception as e:
        logger.debug(f"API verification failed for {api_base_url}: {e}")

    return None


def _track_id(state: Optional[PlaybackState]) -> Optional[EntityId]:
    if state is None or state.current_track is None:
        return None
    return state.current_track.id


class KalinkaPluginMusiccastDevice(ExternalOutputDevice):
    # First few connection attempts use these short delays at INFO level so
    # an offline-at-startup case is visible. After that the worker drops to
    # DEBUG and retries every _REDISCOVERY_INTERVAL_SEC indefinitely.
    _QUICK_RETRY_INTERVALS_SEC = (2, 5, 10)
    _REDISCOVERY_INTERVAL_SEC = 60
    _APP_NAME = "MusicCast/1.0(Linux)"
    # The amplifier answers setVolume in a few ms but applies it far slower;
    # anything faster queues inside it and plays out after the user stops.
    _VOLUME_SEND_INTERVAL_SEC = 0.05
    # An unchanged level is never echoed, and UDP can drop one.
    _ECHO_TIMEOUT_SEC = 1.0

    def __init__(
        self,
        config: KalinkaPluginMusiccastConfig,
        event_emitter: EventEmitter,
        listener: EventListener[PlayQueueEventType, PlayQueueEvent, PlayQueueState],
    ):
        self.event_emitter = event_emitter
        self.listener = listener
        self.connected_input = config.connected_input
        self.zone_name = config.zone_name
        self.volume_step_to_db = config.volume_step_to_db
        self.auto_volume = config.auto_volume_correction
        self.discovery_timeout = config.discovery_timeout
        self.session = httpx.AsyncClient(timeout=5)
        # The amplifier refuses connections when asked several things at once.
        self._request_lock = asyncio.Lock()
        self._volume_target: Optional[int] = None
        self._volume_sender: Optional[asyncio.Task] = None
        # Levels sent but not yet echoed, oldest first.
        self._unconfirmed: deque[int] = deque()
        self._volume_wake = asyncio.Event()
        self.ready = False

        self.config = config
        self.base_url = None
        self.tasks = []
        self._discovery_task = None
        # Cached so transitions to/from unreachable can be dispatched only
        # on actual state changes (no event spam every retry).
        self._device_power_on: bool = False
        self._pending_power_on: bool = False
        # Placeholder volume used before discovery completes and after the
        # device disappears. supported=False keeps the UI from offering
        # volume controls until we actually know the device is reachable.
        self.volume = DeviceVolume(
            max_volume=0,
            current_volume=0,
            volume_gain=0,
            supported=False,
            backend=VolumeBackend.HARDWARE,
        )

        # Worker tasks may be spawned before get_ready() completes (when
        # discovery is in flight). They reference shutdown_event,
        # _volume_changed_event, and udp_port from their first line, so
        # initialise them up front — otherwise the workers raise
        # AttributeError immediately and asyncio swallows the exception
        # because nobody awaits the dead tasks until terminate(). The
        # symptom is silent: no [udp] logs ever appear despite the rest of
        # the plugin (set_volume etc.) working through the request path.
        self.shutdown_event = asyncio.Event()
        self.udp_port: Optional[int] = None
        self._volume_changed_event = asyncio.Event()
        self._playback_state: Optional[PlaybackState] = None

    async def get_ready(self):
        """Probe the device, refresh state, and dispatch events.

        The bus is pre-seeded by start() with an `unavailable` state so any
        client that connects before the device responds sees the correct
        UI. Once we successfully read getStatus we dispatch events to flip
        subscribers to the live state.
        """
        status = await self._get_status()
        self.volume = DeviceVolume(
            max_volume=status["max_volume"],
            current_volume=status["volume"],
            volume_gain=0,
            supported=True,
            backend=VolumeBackend.HARDWARE,
        )
        # The gain is gone from the level read back, so the next PLAYING is news.
        self._playback_state = None
        logger.debug(
            f"[volume] init from getStatus: current={self.volume.current_volume} "
            f"max={self.volume.max_volume}"
        )

        power_on_now = (
            status["power"] == "on" and status["input"] == self.connected_input
        )
        self.ready = True
        was_power_on = self._device_power_on
        self._device_power_on = power_on_now

        # Always emit so subscribers see supported=True even on the first
        # successful connection (the seeded initial state had supported=False).
        self.event_emitter.dispatch(VolumeChangedEvent(volume=self.volume))
        # Initial standby is already the seeded state, not a new power-off.
        # Dispatching it during first playback makes automation stop the source.
        if power_on_now != was_power_on:
            self.event_emitter.dispatch(
                DevicePowerStateChangedEvent(power_on=power_on_now)
            )
        if self._pending_power_on:
            await self.power_on()

    async def run_discovery(self) -> bool:
        """Run SSDP discovery. Returns True if a device was found and
        get_ready() succeeded."""
        interfaces = get_network_interfaces()
        if not interfaces:
            logger.debug("No network interfaces found for discovery")
            return False

        for iface_name, iface_ip in interfaces:
            logger.debug(f"Running discovery on interface {iface_name} ({iface_ip})")
            # Blocking SSDP + sync HTTP; off the loop or every retry cycle
            # freezes the whole server for discovery_timeout seconds.
            device_info = await asyncio.to_thread(
                discover_musiccast_devices,
                iface=iface_name,
                timeout_seconds=self.config.discovery_timeout,
            )
            if device_info:
                logger.debug(f"Device control URL: {device_info['api_base_url']}")
                self.base_url = device_info["api_base_url"]
                try:
                    await self.get_ready()
                except Exception as e:
                    logger.debug(f"get_ready() failed after discovery: {e}")
                    return False
                return True

        logger.debug("MusicCast device discovery failed on all interfaces")
        return False

    async def _connect_once(self) -> bool:
        """Single connection attempt. Uses the configured address if set,
        otherwise falls back to SSDP."""
        if self.config.device_addr and self.config.device_addr.strip():
            if not self.base_url:
                yxc_control_url = "/YamahaExtendedControl/v1/"
                self.base_url = (
                    f"http://{self.config.device_addr}:{self.config.device_port}"
                    f"{yxc_control_url}"
                )
            try:
                await self.get_ready()
                return True
            except (httpx.ConnectError, httpx.TimeoutException, ConnectionError) as e:
                logger.debug(f"Configured-device connection failed: {e}")
                return False
            except Exception as e:
                logger.debug(f"Configured-device get_ready failed: {e}")
                return False

        try:
            return await self.run_discovery()
        except Exception as e:
            logger.debug(f"Discovery error: {e}")
            return False

    async def start(self):
        """Start the MusicCast device and initialize tasks."""
        logger.info("Starting MusicCast device...")

        if self.config.device_addr and self.config.device_addr.strip():
            logger.info(
                f"Using configured MusicCast device: "
                f"{self.config.device_addr}:{self.config.device_port}"
            )

        # Allocate the UDP listener port up-front so the event-loop worker
        # can bind as soon as get_ready() flips self.ready to True.
        self.poweroff_timer = None
        self.udp_port = find_available_port()
        if self.udp_port is None:
            raise Exception("Could not find available UDP port")
        logger.info(f"Using UDP port {self.udp_port}")

        # Seed the bus with an `unavailable` state. If discovery fails for
        # a while, any client that connects mid-retry sees
        # `volume.supported=False` and `power_on=False` immediately — they
        # don't observe the stale defaults from the bus's bootstrap state.
        self.event_emitter.set_initial_state(
            ExtDeviceState(power_on=False, volume=self.volume)
        )

        # Always go through the retry loop. If the device is reachable the
        # first attempt resolves immediately; if not, the loop keeps trying
        # without blocking start() or the rest of the plugin.
        self._discovery_task = asyncio.create_task(self._discovery_worker())

        self.tasks.append(asyncio.create_task(self._event_loop()))
        self.tasks.append(asyncio.create_task(self._timer_loop()))
        self.tasks.append(asyncio.create_task(self._event_sender()))
        self.tasks.append(asyncio.create_task(self._playback_state_listener()))

    async def terminate(self):
        """Gracefully shutdown all tasks and close connections"""
        logger.info("Terminating MusicCast device...")
        # Always signal shutdown so the discovery retry loop and the workers
        # (some of which spin on `not self.ready: sleep(1)` and never see
        # cancellation propagate through the sleep cleanly) exit promptly.
        self.shutdown_event.set()

        # Cancel discovery task if running
        if self._discovery_task and not self._discovery_task.done():
            self._discovery_task.cancel()
            try:
                await self._discovery_task
            except asyncio.CancelledError:
                pass

        tasks = list(self.tasks)
        if self._volume_sender is not None:
            tasks.append(self._volume_sender)

        # Cancel all tasks
        for task in tasks:
            if not task.done():
                task.cancel()

        # Wait for all tasks to complete
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        # Close HTTP session
        if hasattr(self, "session"):
            await self.session.aclose()

    def __del__(self):
        """Ensure clean shutdown when object is destroyed"""
        # Note: Cannot call async methods in __del__, cleanup should be handled explicitly

    def _db_to_device_units(self, db):
        return round(db / self.volume_step_to_db)

    # This event sender runs in its own task
    # and used to throttle volume change notifications to once per second
    async def _event_sender(self):
        logger.info("[task] event_sender started")
        last_sent_volume = None
        last_sent_at = 0.0
        debounce_sec = 0.10
        min_interval_sec = 0.00  # set to 0.10 to cap at 10 Hz
        # Use the instance-level event so signals from set_volume /
        # _handle_event / _timer_loop reach us regardless of restart timing.
        volume_changed = self._volume_changed_event

        try:
            while not self.shutdown_event.is_set():
                try:
                    # Wait for volume change with timeout to allow checking shutdown_event
                    await asyncio.wait_for(volume_changed.wait(), timeout=0.5)
                except asyncio.TimeoutError:
                    continue

                volume_changed.clear()

                logger.debug(
                    f"Volume changed event received: {self.volume.current_volume}"
                )
                # Debounce: wait for quiet
                try:
                    while True:
                        await asyncio.wait_for(
                            volume_changed.wait(), timeout=debounce_sec
                        )
                        volume_changed.clear()
                except asyncio.TimeoutError:
                    pass

                target = self.volume.current_volume

                # Throttle: ensure at least min_interval between sends
                if min_interval_sec > 0:
                    now = time.monotonic()
                    remaining = (last_sent_at + min_interval_sec) - now
                    if remaining > 0:
                        # During throttle wait, keep coalescing new changes
                        try:
                            await asyncio.wait_for(
                                volume_changed.wait(), timeout=remaining
                            )
                            # new change arrived; restart loop to re-debounce
                            volume_changed.clear()
                            continue
                        except asyncio.TimeoutError:
                            pass

                if target != last_sent_volume:
                    logger.debug(f"[volume] emitting VolumeChanged={target}")
                    self.event_emitter.dispatch(
                        VolumeChangedEvent(
                            volume=DeviceVolume(
                                max_volume=self.volume.max_volume,
                                current_volume=target,
                                volume_gain=self.volume.volume_gain,
                                backend=self.volume.backend,
                            )
                        )
                    )
                    last_sent_volume = target
                    last_sent_at = time.monotonic()
        except asyncio.CancelledError:
            logger.info("[task] event_sender cancelled")
            raise
        except Exception as e:
            logger.error(f"[task] event_sender died: {e!r}", exc_info=True)
            raise

    async def _playback_state_listener(self):
        """Listen to playback state changes and call appropriate handlers"""
        logger.info("[task] playback_state_listener started")
        try:
            async with self.listener.stream(
                [PlayQueueEventType.PlaybackStateChanged]
            ) as stream:  # type: ignore
                async for item in stream:
                    # Skip replay events as they are just initial state
                    if isinstance(item, ReplayEvent):
                        logger.debug(
                            f"Received replay event with state: {item.state.playback_state}"
                        )
                        continue

                    if isinstance(item, PlaybackStateChangedEvent):
                        await self._on_playback_state(item.state)
        except asyncio.CancelledError:
            logger.info("[task] playback_state_listener cancelled")
            raise
        except Exception as e:
            logger.error(
                f"[task] playback_state_listener died: {e!r}", exc_info=True
            )
            raise

    async def _on_playback_state(self, state: PlaybackState) -> None:
        """Act on a change of state, not on every report of one: the same
        state is published again whenever position, format or track moves."""
        previous, self._playback_state = self._playback_state, state
        was = previous.state if previous is not None else None
        logger.debug("Playback state changed to: %s", state.state)
        if state.state == PlayerStateEnum.PLAYING:
            if was != PlayerStateEnum.PLAYING:
                logger.info("Playback started, calling _on_playing")
                await self._on_playing(state)
            elif _track_id(previous) != _track_id(state):
                # ReplayGain is per track, and a gapless change stays PLAYING.
                await self._on_playing(state)
        elif state.state == PlayerStateEnum.STOPPED and was != PlayerStateEnum.STOPPED:
            logger.info("Playback stopped, calling _on_stopped")
            await self._on_stopped()

    async def _timer_loop(self):
        """Timer loop that periodically polls device status.

        Two responsibilities:
          1. Refresh the X-AppPort UDP subscription so the receiver keeps
             pushing change events to us.
          2. Resync `self.volume.current_volume` from the polled status as a
             defence-in-depth fallback for missed UDP pushes. Without this,
             external volume changes (front-panel knob, MusicCast app) leave
             the cached value stale until the next UDP echo — which YXC may
             never send if the subscription has lapsed or packets were
             dropped. A stale cache makes hardware-key volume-up on the
             phone snap the receiver to `cached + 1` regardless of where it
             physically is.
        """
        logger.info("[task] timer_loop started")
        try:
            while not self.shutdown_event.is_set():
                # Wait for discovery to finish (self.ready / udp_port set)
                # before issuing requests. Otherwise base_url is None and
                # _get_status raises before any [udp] log can fire.
                if not self.ready or self.udp_port is None:
                    await asyncio.sleep(1)
                    continue
                try:
                    logger.debug(
                        f"[udp] refreshing subscription via getStatus "
                        f"(X-AppPort={self.udp_port})"
                    )
                    status = await self._get_status()

                    polled_volume = status.get("volume")
                    if isinstance(polled_volume, int):
                        self._on_reported_volume(polled_volume, "poll resync")

                    await asyncio.sleep(60)
                except (httpx.ConnectError, httpx.TimeoutException, ConnectionError):
                    # Network error — rediscover_device() is idempotent and
                    # emits the unreachable transition exactly once, so we
                    # don't log here. The loop will park on `not self.ready`
                    # until the retry worker recovers the connection.
                    await self.rediscover_device()
                except Exception as e:
                    logger.error(f"Timer loop error: {e}")
                    await asyncio.sleep(10)
        except asyncio.CancelledError:
            logger.info("[task] timer_loop cancelled")
            raise
        except Exception as e:
            logger.error(f"[task] timer_loop died: {e!r}", exc_info=True)
            raise

    async def _event_loop(self):
        """Event loop that listens for UDP events from MusicCast device"""
        logger.info("[task] event_loop started")
        try:
            while not self.shutdown_event.is_set():
                # Wait for discovery to finish (self.ready / udp_port set)
                # before binding. Without this gate the worker would crash
                # on a None udp_port the very first iteration.
                if not self.ready or self.udp_port is None:
                    await asyncio.sleep(1)
                    continue
                udp_socket = None
                try:
                    udp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    # Set socket non-blocking for async operation
                    udp_socket.setblocking(False)

                    # Bind to local interface, not remote device address
                    udp_socket.bind(("", self.udp_port))
                    loop = asyncio.get_event_loop()
                    device_addr = urllib.parse.urlparse(self.base_url).hostname
                    logger.info(
                        f"[udp] listening on 0.0.0.0:{self.udp_port}, "
                        f"expecting packets from device_addr={device_addr}"
                    )

                    while not self.shutdown_event.is_set():
                        try:
                            data, client_address = await loop.sock_recvfrom(udp_socket, 4096)
                        except OSError as e:
                            logger.error(f"Socket error in event loop: {e}")
                            break

                        logger.debug(
                            f"[udp] packet rx from {client_address[0]}:{client_address[1]} "
                            f"len={len(data)}"
                        )

                        # Validate that the event came from the expected device
                        if client_address[0] != device_addr:
                            logger.warning(
                                f"[udp] dropping packet — source {client_address[0]} "
                                f"!= expected {device_addr}"
                            )
                            continue

                        try:
                            event_json = json.loads(data.decode("utf-8"))
                            logger.debug(f"[udp] parsed event: {event_json}")
                            await self._handle_event(event_json)
                        except (json.JSONDecodeError, UnicodeDecodeError) as e:
                            logger.warning(f"[udp] failed to decode payload: {e}")

                except socket.error as e:
                    logger.error(f"Failed to create/bind UDP socket: {e}")
                    await asyncio.sleep(10)
                except Exception as e:
                    logger.error(f"Unexpected exception in event loop: {e}")
                    await asyncio.sleep(10)
                finally:
                    if udp_socket is not None:
                        try:
                            udp_socket.close()
                        except:
                            pass
        except asyncio.CancelledError:
            logger.info("[task] event_loop cancelled")
            raise
        except Exception as e:
            logger.error(f"[task] event_loop died: {e!r}", exc_info=True)
            raise

    async def _handle_event(self, event_json):
        """Handle incoming MusicCast events from the device"""
        # MusicCast events can contain multiple zones (main, zone2, zone3, etc.)
        # Each zone can have different event data
        logger.debug(f"Received MusicCast event: {event_json}")

        if self.zone_name not in event_json:
            logger.debug(f"No event data for configured zone: {self.zone_name}")
            return

        zone_state = event_json.get(self.zone_name)

        # Handle volume changes
        if "volume" in zone_state:
            new_volume = zone_state["volume"]
            if isinstance(new_volume, int):
                self._on_reported_volume(new_volume, "UDP push")

        # Handle power state changes
        power_state = zone_state.get("power")
        input_state = zone_state.get("input")

        if power_state == "standby":
            logger.info("Device entered standby mode")
            if self._device_power_on:
                self._device_power_on = False
                self.event_emitter.dispatch(DevicePowerStateChangedEvent(power_on=False))
            return

        if input_state is not None and input_state != self.connected_input:
            logger.info(f"Input changed from {self.connected_input} to {input_state}")
            if self._device_power_on:
                self._device_power_on = False
                self.event_emitter.dispatch(DevicePowerStateChangedEvent(power_on=False))
            return

        # Device is on and input matches (or input not reported) — track effective-on state
        if power_state == "on" and (
            input_state is None or input_state == self.connected_input
        ):
            if not self._device_power_on:
                self._device_power_on = True
                self.event_emitter.dispatch(DevicePowerStateChangedEvent(power_on=True))

        # Handle mute state if needed
        if "mute" in zone_state:
            mute_state = zone_state["mute"]
            logger.debug(f"Mute state: {mute_state}")

        # Handle sleep timer if present
        if "sleep" in zone_state:
            sleep_time = zone_state["sleep"]
            logger.debug(f"Sleep timer: {sleep_time}")

        # Handle status_updated flag
        if zone_state.get("status_updated", False):
            logger.debug("Status update event received")

    async def _on_stopped(self):
        await self._reset_replaygain()

    async def _reset_replaygain(self):
        if self.volume.volume_gain != 0:
            await self.set_volume(self.volume.current_volume - self.volume.volume_gain)
            self.volume.volume_gain = 0

    async def _on_playing(self, state: PlaybackState):
        # ReplayGain
        if state.current_track is None or state.current_track.replaygain_gain is None:
            await self._reset_replaygain()
            return
        if (
            self.auto_volume is True
            and state.current_track is not None
            and state.current_track.replaygain_gain is not None
        ):
            device_gain_units = self._db_to_device_units(
                state.current_track.replaygain_gain
            )
            if self.volume.volume_gain == device_gain_units:
                return

            new_volume = (
                self.volume.current_volume - self.volume.volume_gain + device_gain_units
            )
            self.volume.volume_gain = device_gain_units
            await self.set_volume(new_volume)
            logger.info(f"Loudness correction applied: {device_gain_units}")

    async def _get_status(self):
        response = await self._request_musiccast(f"/{self.zone_name}/getStatus")
        if response["response_code"] != 0:
            logger.warning(
                "MusicCast returned error code %d", response["response_code"]
            )

        return response

    async def _set_input(self):
        await self._request_musiccast(
            f"/{self.zone_name}/setInput?input={self.connected_input}"
        )

    def _event_headers(self) -> dict[str, str]:
        # On every request, as Yamaha's clients do: the device takes its event
        # receiver from these, and any request renews the subscription.
        if self.udp_port is None:
            return {}
        return {"X-AppName": self._APP_NAME, "X-AppPort": str(self.udp_port)}

    async def _request_musiccast(self, endpoint):
        try:
            if self.base_url is None:
                raise Exception("MusicCast device not initialized")

            async with self._request_lock:
                response = await self.session.get(
                    safe_urljoin(self.base_url, endpoint),
                    headers=self._event_headers(),
                    timeout=5,
                )
            if response.status_code != 200:
                raise Exception(f"MusicCast returned {response.status_code}")

            return response.json()
        except (httpx.ConnectError, httpx.TimeoutException, ConnectionError) as e:
            # rediscover_device() is idempotent — it only logs / dispatches
            # on the first transition to unreachable. Keep this at debug so
            # repeated failures during the retry loop don't fill the log.
            logger.debug(f"Network error accessing MusicCast device: {e}")
            await self.rediscover_device()
            raise
        except Exception as e:
            logger.error(f"MusicCast request failed for {endpoint}: {e}")
            raise

    def _mark_unavailable(self) -> None:
        """Signal device-unreachable to subscribers.

        volume.supported=False tells the UI that volume control isn't
        available. Power is left alone: an unanswered request is not a
        power-off, and a power-off stops playback. get_ready() reports the
        real power state once the device answers again. Only emits on the
        transition to avoid spamming the bus during retry loops.

        Volume queued or sent on the lost connection is dropped, and the
        sender woken to exit: replayed after reconnecting, it would overwrite
        the level get_ready() reads back.
        """
        self._volume_target = None
        self._unconfirmed.clear()
        self._volume_wake.set()
        if self.volume.supported:
            self.volume = DeviceVolume(
                max_volume=self.volume.max_volume,
                current_volume=self.volume.current_volume,
                volume_gain=self.volume.volume_gain,
                supported=False,
                backend=self.volume.backend,
            )
            self.event_emitter.dispatch(VolumeChangedEvent(volume=self.volume))

    async def rediscover_device(self):
        """Mark device unreachable and ensure the retry loop is running.

        Idempotent: callers in the network-error paths can hammer this
        without piling up tasks or re-dispatching state events.
        """
        if self.ready:
            logger.info("MusicCast device unreachable, switching to retry loop")
            self.ready = False
            self._mark_unavailable()

        if not self._discovery_task or self._discovery_task.done():
            self._discovery_task = asyncio.create_task(self._discovery_worker())

    async def _discovery_worker(self):
        """Retry connection until the device is reachable or we're shutting
        down. First few attempts log at INFO so an offline-at-startup case
        is visible; after that we drop to DEBUG and retry every
        _REDISCOVERY_INTERVAL_SEC to avoid spamming the log.
        """
        attempt = 0
        while not self.shutdown_event.is_set():
            attempt += 1
            is_quick = attempt <= len(self._QUICK_RETRY_INTERVALS_SEC)
            log = logger.info if is_quick else logger.debug

            log(f"MusicCast connection attempt {attempt}")
            try:
                if await self._connect_once():
                    logger.info(
                        f"MusicCast device available (attempt {attempt})"
                    )
                    return
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log(f"Connection attempt {attempt} failed: {e}")

            wait = (
                self._QUICK_RETRY_INTERVALS_SEC[attempt - 1]
                if is_quick
                else self._REDISCOVERY_INTERVAL_SEC
            )
            log(f"Retrying MusicCast connection in {wait}s")
            try:
                await asyncio.wait_for(self.shutdown_event.wait(), timeout=wait)
                return  # shutdown set during the wait
            except asyncio.TimeoutError:
                pass

    async def get_volume(self) -> DeviceVolume:
        if not self.ready:
            return DeviceVolume(
                max_volume=0,
                current_volume=0,
                volume_gain=0,
                supported=False,
                backend=VolumeBackend.HARDWARE,
            )

        return self.volume

    async def set_volume(self, volume: int) -> None:
        """Returns once the level is queued, before the amplifier has it.
        Levels are sent at most every _VOLUME_SEND_INTERVAL_SEC, and only the
        newest queued one: a burst never leaves a backlog of stale levels."""
        if not self.ready:
            return

        clamped = max(0, min(volume, self.volume.max_volume))
        logger.debug(
            f"[volume] set_volume requested={volume} clamped={clamped} "
            f"cache_before={self.volume.current_volume}"
        )
        self._volume_target = clamped
        self._volume_wake.set()
        if self._volume_sender is None or self._volume_sender.done():
            self._volume_sender = asyncio.create_task(self._send_volume())
        self._cache_volume(clamped, "set_volume optimistic update")

    async def _send_volume(self) -> None:
        while True:
            if self._volume_target is not None:
                target, self._volume_target = self._volume_target, None
                self._unconfirmed.append(target)
                logger.debug(f"[volume] sending setVolume={target}")
                try:
                    await self._request_musiccast(
                        f"/{self.zone_name}/setVolume?volume={target}"
                    )
                except Exception:
                    self._volume_target = None
                    self._unconfirmed.clear()
                    await self.rediscover_device()
                    return
                await asyncio.sleep(self._VOLUME_SEND_INTERVAL_SEC)
            elif not self._unconfirmed:
                return
            else:
                # A new level wakes this to send it; so does the last echo.
                self._volume_wake.clear()
                try:
                    await asyncio.wait_for(
                        self._volume_wake.wait(), timeout=self._ECHO_TIMEOUT_SEC
                    )
                except asyncio.TimeoutError:
                    logger.debug(
                        f"[volume] no echo for {list(self._unconfirmed)}; reconciling"
                    )
                    self._unconfirmed.clear()
                    await self._reconcile_volume()

    def _on_reported_volume(self, level: int, source: str) -> None:
        """A level the amplifier reports: an echo of ours, or a change made
        elsewhere (knob, another app).

        The amplifier echoes our sends in order, so an unconfirmed level
        confirms itself and every send before it. While any send is still
        unconfirmed or queued, any other level is older than our newest one
        and is dropped rather than shown to clients.
        """
        if level in self._unconfirmed:
            while self._unconfirmed.popleft() != level:
                pass
            if not self._unconfirmed:
                self._volume_wake.set()
            return
        if self._unconfirmed or self._volume_target is not None:
            logger.debug(f"[volume] {source}: dropped {level}, superseded")
            return
        self._cache_volume(level, source)

    async def _reconcile_volume(self) -> None:
        try:
            status = await self._get_status()
        except Exception:
            return  # Already logged; a network error has started rediscovery.
        level = status.get("volume")
        if isinstance(level, int):
            self._on_reported_volume(level, "reconcile")

    def _cache_volume(self, level: int, reason: str) -> None:
        if level == self.volume.current_volume:
            return
        logger.debug(f"[volume] {reason}: {self.volume.current_volume} -> {level}")
        self.volume.current_volume = level
        self._volume_changed_event.set()

    async def power_on(self) -> None:
        if not self.ready:
            # Playback can begin before discovery completes. Apply its power
            # request when the first successful status probe makes us ready.
            self._pending_power_on = True
            return

        if await self.is_power_on():
            self._pending_power_on = False
            return
        await self._request_musiccast(f"/{self.zone_name}/setPower?power=on")
        await self._set_input()
        self._pending_power_on = False
        if not self._device_power_on:
            self._device_power_on = True
            self.event_emitter.dispatch(DevicePowerStateChangedEvent(power_on=True))

    async def power_off(self) -> None:
        self._pending_power_on = False
        if not self.ready:
            return
        await self._request_musiccast(f"/{self.zone_name}/setPower?power=standby")

    async def is_power_on(self) -> bool:
        if not self.ready:
            return False

        status = await self._get_status()

        return status["power"] == "on" and status["input"] == self.connected_input

    def supported_functions(self) -> list[SupportedFunction]:
        return [
            SupportedFunction.GET_VOLUME,
            SupportedFunction.SET_VOLUME,
            SupportedFunction.POWER_ON,
            SupportedFunction.IS_POWER_ON,
            SupportedFunction.POWER_OFF,
        ]
