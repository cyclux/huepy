"""The Hue client: one object exposing every resource handler.

The client is async-only. Use it as an async context manager, which opens the
HTTP session without resource I/O in the default stateless mode:

    async with Hue(bridge_ip="192.168.1.100") as hue:
        for light in await hue.api.lights.list():
            print(hue.get_name(light.id), light.is_on)
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator, Sequence
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Any, Self

from pydantic import ValidationError

from huepy._version import package_version
from huepy.client.api import HueAPI
from huepy.client.http import HueHttpClient
from huepy.client.protocol import Transport
from huepy.collections import (
    DeviceCollection,
    LightCollection,
    RoomCollection,
    SceneCollection,
    ServiceGroupCollection,
    ZoneCollection,
)
from huepy.config import HueConfig, default_config_path
from huepy.exceptions import AuthenticationError
from huepy.models import AnyResource, HueResponse, NamedResource
from huepy.models.event import HueEvent, parse_events
from huepy.utils.naming import build_name_map

if TYPE_CHECKING:
    from huepy.recording import HistorySink, Recorder
    from huepy.state import HueState

logger = logging.getLogger(__name__)

UNKNOWN_NAME = "Unknown"


def _as_sinks(
    record: HistorySink | Sequence[HistorySink] | None,
) -> tuple[HistorySink, ...]:
    """Normalise the `record=` argument to a tuple of sinks.

    `record=SQLiteSink(...)` is the shape people actually type, so the single
    sink is accepted directly. A sink is not a Sequence, which is what makes
    the discrimination safe.
    """
    if record is None:
        return ()
    if isinstance(record, Sequence):
        return tuple(record)
    return (record,)


class Hue:
    """Async client for one Philips Hue bridge.

    Attributes:
        config: The resolved connection settings.
        api: Typed, id-addressed CLIP v2 handlers.
        lights: Human-facing named light collection.
        rooms: Human-facing named room collection.
        zones: Human-facing named zone collection.
        scenes: Human-facing named scene collection.
        devices: Human-facing named device collection.
        service_groups: Human-facing named service-group collection.
        state: The local state graph; observing when ``Hue(state=True)``.
        recorder: The running history recorder, when ``record=`` was given.

    """

    def __init__(  # noqa: PLR0913 - each one is an independent client setting
        self,
        bridge_ip: str = "",
        app_key: str | None = None,
        config_path: str | Path | None = None,
        *,
        verify_ssl: bool = False,
        state: bool = False,
        record: HistorySink | Sequence[HistorySink] | None = None,
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
            state: Whether to maintain the local resource graph in the
                background from the aggregate snapshot and event stream.
            record: One sink, or several, to persist the change history into.
                Implies ``state=True``: a configured sink that never receives a
                row because a second flag was missed is not a useful default.

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
        self._sinks: tuple[HistorySink, ...] = _as_sinks(record)
        self._state_requested: bool = state or bool(self._sinks)
        self._state: HueState | None = None
        self._recorder: Recorder | None = None

        self.api: HueAPI = HueAPI(self)
        self.lights: LightCollection = LightCollection(self, self.api.lights)
        self.rooms: RoomCollection = RoomCollection(self, self.api.rooms)
        self.zones: ZoneCollection = ZoneCollection(self, self.api.zones)
        self.scenes: SceneCollection = SceneCollection(self, self.api.scenes)
        self.devices: DeviceCollection = DeviceCollection(self, self.api.devices)
        self.service_groups: ServiceGroupCollection = ServiceGroupCollection(
            self, self.api.service_groups
        )

    @property
    def state(self) -> HueState:
        """The local state graph, observing when ``Hue(state=True)``.

        Present from construction so handlers and sinks can be registered
        before the stream opens; reads raise
        :class:`~huepy.exceptions.StateNotStartedError` until it is started.
        Built on first access rather than in ``__init__`` because
        ``huepy.state`` imports the client protocol, and deferring the import
        is what keeps the package import graph acyclic.
        """
        if self._state is None:
            # Deferred so a stateless client never imports the state layer,
            # and so the dependency direction stays one-way by construction.
            from huepy.state import HueState  # noqa: PLC0415 - deferred by design

            self._state = HueState(self)
        return self._state

    @property
    def recorder(self) -> Recorder | None:
        """The running history recorder, or None when nothing is recorded."""
        return self._recorder

    @property
    def _tracking_state(self) -> HueState | None:
        """The state graph when it is observing, without building one to ask.

        Reading ``self.state`` would construct one, so the paths that only need
        to *know* whether tracking is on ask here instead.
        """
        state = self._state
        return state if state is not None and state.tracking else None

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
        """The id-to-display-name lookup populated explicitly or by state tracking."""
        if self._tracking_state is not None:
            self._tracking_state.ensure_resolver_healthy()
            return build_name_map(self._tracking_state.list(NamedResource))
        return self._names

    async def __aenter__(self) -> Self:
        """Open the session and, when requested, start tracking local state."""
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

    async def start(self) -> None:
        """Open the HTTP session without eagerly fetching bridge resources."""
        self._http = await HueHttpClient(self.config).__aenter__()
        logger.info(
            "huepy v%s connected to %s", package_version(), self.config.bridge_ip
        )
        if self._state_requested:
            try:
                self.ensure_authenticated()
                _ = await self.state.__aenter__()
                if self._sinks:
                    # Deferred so a client without `record=` never pays for
                    # sqlite3 and concurrent.futures at import time.
                    from huepy.recording import (  # noqa: PLC0415 - deferred by design
                        Recorder,
                    )

                    self._recorder = Recorder(self.state, self._sinks)
                    await self._recorder.start()
            except BaseException:
                await self.close()
                raise

    async def close(self) -> None:
        """Close the event stream and HTTP session, if they are open.

        Breaking out of :meth:`get_event_stream` leaves its generator
        suspended, still holding the streaming response. Finalising it here
        releases that socket instead of leaving it to the garbage collector.
        """
        errors: list[BaseException] = []
        # The state object outlives close() so registrations survive a restart;
        # only its observation is stopped. `tracking` is what makes a second
        # close() a no-op now that the attribute is no longer cleared.
        state = self._tracking_state
        if state is not None:
            try:
                await state.close()
            except BaseException as exc:  # noqa: BLE001 - continue all cleanup
                errors.append(exc)
        # After the state: closing it ends the stream, so the recorder drains
        # what it already holds and flushes on its own. Before the transport,
        # so a sink is never still writing when the session goes away.
        recorder, self._recorder = self._recorder, None
        if recorder is not None:
            try:
                await recorder.close()
            except BaseException as exc:  # noqa: BLE001 - continue all cleanup
                errors.append(exc)
        streams = tuple(self._event_streams)
        self._event_streams.clear()
        errors.extend(
            result
            for result in await asyncio.gather(
                *(stream.aclose() for stream in streams),
                return_exceptions=True,
            )
            if isinstance(result, BaseException)
        )
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

        One aggregate snapshot supplies every named resource and its contained
        service references.

        Returns:
            The refreshed mapping of resource id to display name.

        """
        self._names = build_name_map(
            resource
            for resource in await self.snapshot()
            if isinstance(resource, NamedResource)
        )
        return self._names

    def get_name(self, resource_id: str) -> str:
        """Look up a resource's display name.

        Args:
            resource_id: The id of a device, light or room.

        Returns:
            The display name, or ``"Unknown"`` if the id is not in the lookup.

        """
        if self._tracking_state is not None:
            self._tracking_state.ensure_resolver_healthy()
            return self._tracking_state.name_of(resource_id)
        return self._names.get(resource_id, UNKNOWN_NAME)

    async def snapshot(self) -> list[AnyResource]:
        """Fetch the bridge's aggregate-visible resource graph in one request."""
        response = HueResponse[AnyResource].model_validate(
            await self.http.get("/clip/v2/resource")
        )
        response.raise_for_errors()
        return [resource.bind(self, resource.type) for resource in response.data]

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
