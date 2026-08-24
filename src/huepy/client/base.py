"""The Hue client: one object exposing every resource handler.

The client is async-only. Use it as an async context manager, which opens the
HTTP session and loads the id-to-name lookup:

    async with Hue(bridge_ip="192.168.1.100") as hue:
        for light in await hue.light.get_all():
            print(hue.get_name(light.id), light.is_on)
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Any, Self

from pydantic import ValidationError

from huepy._version import package_version
from huepy.client.http import HueHttpClient
from huepy.client.protocol import Transport
from huepy.config import HueConfig, default_config_path
from huepy.exceptions import AuthenticationError
from huepy.models import AnyResource, HueResponse
from huepy.models.event import HueEvent, parse_events
from huepy.resources import (
    Bridge,
    BridgeHome,
    Button,
    Contact,
    Device,
    DevicePower,
    GroupedLight,
    GroupedLightLevel,
    GroupedMotion,
    Light,
    LightLevel,
    Motion,
    RelativeRotary,
    Room,
    Scene,
    ServiceGroup,
    Temperature,
    ZigbeeConnectivity,
    Zone,
)
from huepy.utils.naming import build_name_map

if TYPE_CHECKING:
    from huepy.state import HueState

logger = logging.getLogger(__name__)

UNKNOWN_NAME = "Unknown"


class Hue:
    """Async client for one Philips Hue bridge.

    Attributes:
        config: The resolved connection settings.
        http: The underlying HTTP client, or None before :meth:`start`.
        light: Handler for individual lights.
        light_group: Handler for grouped-light services.
        light_level: Handler for ambient light-level sensors.
        light_level_group: Handler for aggregated light-level services.
        room: Handler for rooms.
        zone: Handler for zones.
        scene: Handler for scenes.
        device: Handler for physical devices.
        device_power: Handler for battery services.
        bridge: Handler for the bridge resource.
        bridge_home: Handler for the top-level home.
        service_group: Handler for named service groups.
        motion: Handler for motion sensors.
        motion_group: Handler for aggregated motion services.
        temperature: Handler for temperature sensors.
        button: Handler for switch and dimmer buttons.
        contact: Handler for contact sensors.
        relative_rotary: Handler for relative rotary input services.
        zigbee_connectivity: Handler for Zigbee reachability services.

    """

    def __init__(
        self,
        bridge_ip: str = "",
        app_key: str | None = None,
        config_path: str | Path | None = None,
        *,
        verify_ssl: bool = False,
    ) -> None:
        """Initialise the client without contacting the bridge.

        Args:
            bridge_ip: Bridge address; falls back to ``HUE_BRIDGE_IP``.
            app_key: Application key; falls back to ``HUE_APP_KEY`` then the
                config file.
            config_path: Where settings are stored; defaults to
                ``$XDG_CONFIG_HOME/huepy/config.json``, or
                ``HUE_CONFIG_PATH`` when set.
            verify_ssl: Whether to verify the bridge's TLS certificate.

        """
        self.config: HueConfig = HueConfig(
            bridge_ip=bridge_ip,
            app_key=app_key,
            config_path=(
                Path(config_path) if config_path is not None else default_config_path()
            ),
            verify_ssl=verify_ssl,
        )
        self._http: Transport | None = None
        self._names: dict[str, str] = {}
        self._event_streams: set[AsyncGenerator[Any]] = set()

        self.light: Light = Light(self)
        self.light_group: GroupedLight = GroupedLight(self)
        self.light_level: LightLevel = LightLevel(self)
        self.light_level_group: GroupedLightLevel = GroupedLightLevel(self)
        self.room: Room = Room(self)
        self.zone: Zone = Zone(self)
        self.scene: Scene = Scene(self)
        self.device: Device = Device(self)
        self.device_power: DevicePower = DevicePower(self)
        self.bridge: Bridge = Bridge(self)
        self.bridge_home: BridgeHome = BridgeHome(self)
        self.service_group: ServiceGroup = ServiceGroup(self)
        self.motion: Motion = Motion(self)
        self.motion_group: GroupedMotion = GroupedMotion(self)
        self.temperature: Temperature = Temperature(self)
        self.button: Button = Button(self)
        self.contact: Contact = Contact(self)
        self.relative_rotary: RelativeRotary = RelativeRotary(self)
        self.zigbee_connectivity: ZigbeeConnectivity = ZigbeeConnectivity(self)

        # Plural aliases for the same handler objects, not copies. The singular
        # names are the raw, id-based surface; the plural ones read naturally
        # for discovery -- `await hue.rooms["Kitchen"]`. Only the handlers whose
        # resources carry a display name get one.
        self.lights: Light = self.light
        self.rooms: Room = self.room
        self.zones: Zone = self.zone
        self.scenes: Scene = self.scene
        self.devices: Device = self.device

    @property
    def http(self) -> Transport:
        """The open HTTP client.

        Raises:
            RuntimeError: If the client has not been started.

        """
        if self._http is None:
            msg = "Client not initialized"
            raise RuntimeError(msg)
        return self._http

    @property
    def names(self) -> dict[str, str]:
        """The id-to-display-name lookup loaded by :meth:`start`."""
        return self._names

    async def __aenter__(self) -> Self:
        """Open the session and load the name lookup."""
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Close the session."""
        await self.close()

    async def start(self, *, load_names: bool = True) -> None:
        """Open the HTTP session and populate the name lookup.

        The lookup is skipped when no application key is available, because
        every request it makes would be rejected. That is what keeps
        :meth:`authenticate` reachable through the client itself: connecting
        to an unkeyed bridge to ask for a key is the one case where there is
        nothing to look up yet.

        Args:
            load_names: Whether to populate the id-to-name lookup. Pass False
                to connect without the five requests it costs; ``get_name``
                then returns ``"Unknown"`` until :meth:`refresh_names` is
                called.

        """
        self._http = await HueHttpClient(self.config).__aenter__()
        logger.info(
            "huepy v%s connected to %s", package_version(), self.config.bridge_ip
        )
        if not load_names:
            return
        if not self.config.app_key:
            logger.debug("No application key yet, so the name lookup is skipped.")
            return
        try:
            _ = await self.refresh_names()
        except BaseException:
            # Otherwise a failure here strands the open aiohttp session, since
            # __aexit__ never runs for an __aenter__ that raised.
            await self.close()
            raise

    async def close(self) -> None:
        """Close the event stream and HTTP session, if they are open.

        Breaking out of :meth:`get_event_stream` leaves its generator
        suspended, still holding the streaming response. Finalising it here
        releases that socket instead of leaving it to the garbage collector.
        """
        streams = tuple(self._event_streams)
        self._event_streams.clear()
        errors = [
            result
            for result in await asyncio.gather(
                *(stream.aclose() for stream in streams),
                return_exceptions=True,
            )
            if isinstance(result, BaseException)
        ]
        http, self._http = self._http, None
        if http is not None:
            try:
                await http.__aexit__(None, None, None)
            except BaseException as exc:  # noqa: BLE001 - cleanup every resource first
                errors.append(exc)
        if errors:
            raise errors[0]

    async def refresh_names(self) -> dict[str, str]:
        """Reload the id-to-display-name lookup from the bridge.

        Every named resource type is fetched concurrently, so a zone or
        scene id resolves to its name just as a light id does.

        Returns:
            The refreshed mapping of resource id to display name.

        """
        # gather, not TaskGroup: any failure here means the bridge is
        # unreachable, and callers should see that error directly rather than
        # having to unwrap an ExceptionGroup.
        devices, lights, rooms, zones, scenes = await asyncio.gather(
            self.device.get_all(),
            self.light.get_all(),
            self.room.get_all(),
            self.zone.get_all(),
            self.scene.get_all(),
        )
        self._names = build_name_map(devices, lights, rooms, zones, scenes)
        return self._names

    def get_name(self, resource_id: str) -> str:
        """Look up a resource's display name.

        Args:
            resource_id: The id of a device, light or room.

        Returns:
            The display name, or ``"Unknown"`` if the id is not in the lookup.

        """
        return self._names.get(resource_id, UNKNOWN_NAME)

    async def snapshot(self) -> list[AnyResource]:
        """Fetch the bridge's aggregate-visible resource graph in one request."""
        response = HueResponse[AnyResource].model_validate(
            await self.http.get("/clip/v2/resource")
        )
        response.raise_for_errors()
        return [resource.bind(self, resource.type) for resource in response.data]

    def state(self) -> HueState:
        """Create an opt-in, event-updated view of the bridge state."""
        from huepy.state import HueState  # noqa: PLC0415 - keeps imports acyclic

        return HueState(self)

    def ensure_authenticated(self) -> None:
        """Check that an application key is available.

        Raises:
            AuthenticationError: If no key was supplied, found in the
                environment, or stored in the config file.

        """
        if not self.config.app_key:
            msg = (
                f"No Hue application key available. Press the bridge link button "
                f"and call `await hue.authenticate()` to obtain one, or set "
                f"HUE_APP_KEY. Looked in: {self.config.config_path}"
            )
            raise AuthenticationError(msg)

    async def authenticate(
        self,
        app_name: str = "huepy",
        timeout: int = 60,  # noqa: ASYNC109 - link-button budget, not a cancel scope
    ) -> str:
        """Obtain and store an application key from the bridge.

        The bridge's link button must be pressed while this runs.

        Args:
            app_name: The device type recorded on the bridge.
            timeout: How long to wait for the button press, in seconds.

        Returns:
            The newly issued application key.

        Raises:
            AuthenticationError: If the bridge refuses or the timeout expires.

        """
        return await self.http.authenticate(app_name, timeout)

    async def get_event_stream(self) -> AsyncGenerator[HueEvent]:
        """Yield events pushed by the bridge, parsed into models.

        For the raw decoded payloads instead, use
        ``hue.http.subscribe_events()``.

        Yields:
            Each event the bridge reports.

        Raises:
            AuthenticationError: If no application key is available.

        """
        self.ensure_authenticated()
        stream = self.http.subscribe_event_frames()
        self._event_streams.add(stream)
        try:
            async for frame in stream:
                # A stream meant to run for weeks must not die on one
                # malformed event, so a payload that will not parse is
                # dropped with a warning rather than raised through.
                try:
                    events = parse_events(frame.events)
                except ValidationError:
                    logger.warning("Discarding unparseable event: %r", frame.events)
                    continue
                for event in events:
                    event.sse_id = frame.event_id
                    yield event
        finally:
            self._event_streams.discard(stream)
            await stream.aclose()
