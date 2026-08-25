"""Continuously maintained, event-folded Hue bridge state."""

from __future__ import annotations

import asyncio
import copy
import logging
from collections import defaultdict, deque
from collections.abc import AsyncGenerator, Callable, Mapping
from contextlib import aclosing, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import Any, Final, Never, Protocol, Self, cast, final
from uuid import UUID

from pydantic import AwareDatetime, TypeAdapter, ValidationError

from huepy.client.protocol import EventConnection, HueClient, PendingWrite, SSEFrame
from huepy.exceptions import (
    AmbiguousResourceError,
    BridgeConnectionError,
    HueAPIError,
    HueResponseError,
    ResourceNotFoundError,
)
from huepy.models import (
    AnyResource,
    Device,
    HueResponse,
    Light,
    NamedResource,
    Room,
    Scene,
    Zone,
    parse_resource,
)
from huepy.models.common import HueResource
from huepy.models.event import EventType, HueEvent
from huepy.state.records import (
    ActiveFade,
    Change,
    ChangeKind,
    Resync,
    ResyncReason,
)
from huepy.utils.naming import build_name_map

logger = logging.getLogger(__name__)

DEFAULT_SUBSCRIBER_SIZE: Final = 4096
FADE_REPORT_ALLOWANCE: Final = timedelta(seconds=25)
WRITE_MATCH_WINDOW: Final = timedelta(seconds=25)
_TIMESTAMP: TypeAdapter[datetime] = TypeAdapter(AwareDatetime)
_BARRIER = object()
_CLOSED = object()
_MIN_BUFFER_SIZE = 2
_RESOURCE_PATH_PARTS = 5
_MATCH_TOLERANCE = 0.1
_EVENT_SOURCE_STOPPED_MESSAGE = "Event connection source stopped"


@dataclass(frozen=True)
class _Disconnect:
    error: BaseException | None = None


@dataclass
class _Command:
    write: PendingWrite
    resource_ids: frozenset[str]
    target: dict[str, Any]
    transition_ends_at: datetime | None
    done: asyncio.Event = field(default_factory=asyncio.Event)
    echoed_resources: set[str] = field(default_factory=set)


StateItem = Change | Resync
_BufferItem = StateItem | BaseException | object


def _item_start(item: StateItem) -> datetime:
    return item.received_at if isinstance(item, Change) else item.gap_started


def _item_end(item: StateItem) -> datetime:
    return item.received_at if isinstance(item, Change) else item.gap_ended


@final
class _Subscriber:
    """A bounded newest-wins buffer with explicit loss signalling."""

    def __init__(self, maxsize: int) -> None:
        if maxsize < _MIN_BUFFER_SIZE:
            msg = "changes maxsize must be at least 2"
            raise ValueError(msg)
        self.maxsize = maxsize
        self.items: deque[_BufferItem] = deque()
        self.condition = asyncio.Condition()
        self._lag_started: datetime | None = None
        self._lag_dropped = 0

    async def put(self, item: _BufferItem) -> None:
        async with self.condition:
            if len(self.items) >= self.maxsize:
                self._make_room(item)
            self.items.append(item)
            self.condition.notify()

    def _make_room(self, incoming: _BufferItem) -> None:
        existing_marker = next(
            (
                queued
                for queued in self.items
                if isinstance(queued, Resync) and queued.reason is ResyncReason.LAGGED
            ),
            None,
        )
        if existing_marker is not None:
            self.items.remove(existing_marker)
        while len(self.items) > self.maxsize - 2:
            dropped = self.items.popleft()
            if isinstance(dropped, (Change, Resync)):
                self._lag_started = self._lag_started or _item_start(dropped)
                self._lag_dropped += 1
        now = (
            _item_end(incoming)
            if isinstance(incoming, (Change, Resync))
            else datetime.now(UTC)
        )
        self.items.append(
            Resync(
                reason=ResyncReason.LAGGED,
                gap_started=self._lag_started or now,
                gap_ended=now,
                dropped=self._lag_dropped,
            )
        )

    async def get(self) -> _BufferItem:
        async with self.condition:
            _ = await self.condition.wait_for(lambda: bool(self.items))
            item = self.items.popleft()
            if isinstance(item, Resync) and item.reason is ResyncReason.LAGGED:
                self._lag_started = None
                self._lag_dropped = 0
            return item


@final
class StateView[ModelT: HueResource]:
    """A synchronous typed view over one category of canonical state."""

    def __init__(self, state: HueState, model: type[ModelT]) -> None:
        """Bind the view to one state and concrete resource model."""
        self._state = state
        self._model = model

    def by_id(self, resource_id: str) -> ModelT | None:
        """Return a resource of this view's type by id, when present."""
        resource = self._state.by_id(resource_id)
        return resource if isinstance(resource, self._model) else None

    def list(self) -> list[ModelT]:
        """Return all resources in this view."""
        return self._state.list(self._model)

    def get(self, name: str) -> ModelT:
        """Return the unique case-insensitive display-name match."""
        wanted = name.strip().casefold()
        resources = self.list()
        matches = [
            resource
            for resource in resources
            if wanted
            and isinstance(resource, NamedResource)
            and bool(resource.name)
            and resource.name.strip().casefold() == wanted
        ]
        if not matches:
            known = sorted(
                resource.name
                for resource in resources
                if isinstance(resource, NamedResource) and resource.name
            )
            raise ResourceNotFoundError(name, known)
        if len(matches) > 1:
            raise AmbiguousResourceError(name, [resource.id for resource in matches])
        return matches[0]

    def names(self) -> list[str]:
        """Return the sorted non-empty names in this view."""
        return sorted(
            resource.name
            for resource in self.list()
            if isinstance(resource, NamedResource) and resource.name
        )


class StateClient(HueClient, Protocol):
    """Client capabilities required by :class:`HueState`."""

    async def snapshot(self) -> list[AnyResource]:
        """Fetch the aggregate-visible resource graph."""
        ...


@final
class HueState:
    """An opt-in, last-reported view of one Hue bridge resource graph."""

    def __init__(self, hue: StateClient) -> None:
        """Create a stopped state view composed over an open Hue client."""
        self._hue = hue
        self._raw: dict[str, dict[str, Any]] = {}
        self._subscribers: set[_Subscriber] = set()
        self._task: asyncio.Task[None] | None = None
        self._ready: asyncio.Future[None] | None = None
        self._connected = False
        self._last_frame_at: datetime | None = None
        self._opened_at: datetime | None = None
        self._unsubscribe_write: Callable[[], None] | None = None
        self._commands: dict[UUID, _Command] = {}
        self._fades: dict[str, ActiveFade] = {}
        self._publish_tasks: set[asyncio.Task[None]] = set()
        self._reader_tasks: set[asyncio.Task[None]] = set()
        self._publication_tail: asyncio.Task[None] | None = None
        self._terminal_error: BaseException | None = None

        self.lights = StateView(self, Light)
        self.rooms = StateView(self, Room)
        self.zones = StateView(self, Zone)
        self.scenes = StateView(self, Scene)
        self.devices = StateView(self, Device)

    @property
    def connected(self) -> bool:
        """Whether the stream is active and startup/reconciliation is complete."""
        return self._connected

    @property
    def resources(self) -> list[AnyResource]:
        """Fresh bound copies of every aggregate-visible resource."""
        return [self._public(raw) for raw in self._raw.values()]

    def ensure_healthy(self) -> None:
        """Raise the terminal observer failure instead of serving stale state."""
        if self._terminal_error is not None:
            raise self._terminal_error

    def ensure_resolver_healthy(self) -> None:
        """Require a continuous live index before resolving a command target."""
        self.ensure_healthy()
        if not self._connected:
            msg = "Live resource index is reconnecting; name resolution is unavailable"
            raise BridgeConnectionError(msg)

    @property
    def fading(self) -> Mapping[str, ActiveFade]:
        """Current locally issued fades, keyed by affected resource id."""
        now = datetime.now(UTC)
        expired = [
            resource_id
            for resource_id, fade in self._fades.items()
            if fade.unreliable_until <= now
        ]
        for resource_id in expired:
            del self._fades[resource_id]
        self._prune_commands(now)
        return MappingProxyType(
            {
                resource_id: fade.model_copy(deep=True)
                for resource_id, fade in self._fades.items()
            }
        )

    async def __aenter__(self) -> Self:
        """Start observation and wait for the atomic startup handshake."""
        if self._task is not None:
            msg = "HueState is already running"
            raise RuntimeError(msg)
        loop = asyncio.get_running_loop()
        self._commands.clear()
        self._fades.clear()
        self._publication_tail = None
        self._terminal_error = None
        self._ready = loop.create_future()
        self._unsubscribe_write = self._hue.http.add_write_observer(self._observe_write)
        self._task = asyncio.create_task(self._run(), name="huepy-state")
        try:
            await self._ready
        except BaseException:
            await self.close()
            raise
        return self

    async def __aexit__(self, *_exc: object) -> None:
        """Stop observation and release subscribers."""
        await self.close()

    async def close(self) -> None:
        """Stop observation and close every subscriber iterator."""
        if self._unsubscribe_write is not None:
            self._unsubscribe_write()
            self._unsubscribe_write = None
        task, self._task = self._task, None
        if task is not None:
            _ = task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        for publish_task in tuple(self._publish_tasks):
            _ = publish_task.cancel()
        if self._publish_tasks:
            _ = await asyncio.gather(*self._publish_tasks, return_exceptions=True)
        self._connected = False
        await self._broadcast(_CLOSED)

    def by_id(self, resource_id: str) -> AnyResource | None:
        """Return a fresh bound resource by id."""
        raw = self._raw.get(resource_id)
        return self._public(raw) if raw is not None else None

    def list[ModelT: HueResource](self, model: type[ModelT]) -> list[ModelT]:
        """Return fresh bound copies of every resource matching a model type."""
        return [resource for resource in self.resources if isinstance(resource, model)]

    def name_of(self, resource_id: str) -> str:
        """Resolve a resource or its owner to a human-facing name."""
        return build_name_map(self.list(NamedResource)).get(resource_id, "Unknown")

    def device_of(self, resource_id: str) -> Device | None:
        """Return the physical device owning a resource, when resolvable."""
        resource = self.by_id(resource_id)
        if isinstance(resource, Device):
            return resource
        owner = resource.owner.rid if resource is not None and resource.owner else None
        candidate = self.by_id(owner) if owner is not None else None
        return candidate if isinstance(candidate, Device) else None

    def room_of(self, resource_id: str) -> Room | None:
        """Return the room containing a resource or device."""
        resource = self.by_id(resource_id)
        owner_id = (
            resource.owner.rid
            if resource is not None and resource.owner is not None
            else None
        )
        direct_owner = self.by_id(owner_id) if owner_id is not None else None
        if isinstance(direct_owner, Room):
            return direct_owner
        device = self.device_of(resource_id)
        device_id = device.id if device is not None else resource_id
        return next(
            (
                room
                for room in self.rooms.list()
                if any(child.rid == device_id for child in room.children)
            ),
            None,
        )

    def zones_of(self, resource_id: str) -> list[Zone]:
        """Return every zone containing a resource or its device services."""
        device = self.device_of(resource_id)
        candidates = {resource_id}
        if device is not None:
            candidates.add(device.id)
            candidates.update(service.rid for service in device.services)
        return [
            zone
            for zone in self.zones.list()
            if any(child.rid in candidates for child in zone.children)
        ]

    def lights_in(self, group: Room | Zone) -> list[Light]:
        """Resolve the lights belonging to a room or zone, skipping holes."""
        child_ids = {child.rid for child in group.children}
        if isinstance(group, Room):
            return [
                light
                for light in self.lights.list()
                if light.owner is not None and light.owner.rid in child_ids
            ]
        return [light for light in self.lights.list() if light.id in child_ids]

    async def changes(
        self,
        *,
        maxsize: int = DEFAULT_SUBSCRIBER_SIZE,
    ) -> AsyncGenerator[StateItem]:
        """Yield an isolated stream of changes and continuity markers."""
        if self._task is None or self._task.done():
            if self._terminal_error is not None:
                raise self._terminal_error
            msg = "HueState is not running"
            raise RuntimeError(msg)
        subscriber = _Subscriber(maxsize)
        self._subscribers.add(subscriber)
        try:
            while True:
                item = await subscriber.get()
                if item is _CLOSED:
                    return
                if isinstance(item, BaseException):
                    raise item
                yield cast("StateItem", item)
        finally:
            self._subscribers.discard(subscriber)

    async def _run(self) -> None:
        ready = self._ready
        first = True
        gap_started: datetime | None = None
        try:
            connections = self._hue.http.event_connections(max_retries=None)
            async with aclosing(connections):
                async for connection in connections:
                    self._opened_at = connection.opened_at
                    self._connected = False
                    await self._run_connection(
                        connection,
                        startup=first,
                        gap_started=gap_started,
                    )
                    first = False
                    gap_started = self._last_frame_at or connection.opened_at
            _event_source_stopped()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - propagate terminal to subscribers
            self._connected = False
            self._terminal_error = exc
            if ready is not None and not ready.done():
                ready.set_exception(exc)
            else:
                await self._publish_item(exc)
        finally:
            self._connected = False

    async def _run_connection(
        self,
        connection: EventConnection,
        *,
        startup: bool,
        gap_started: datetime | None,
    ) -> None:
        consumer = asyncio.create_task(
            self._consume_connection(
                connection,
                startup=startup,
                gap_started=gap_started,
            )
        )
        try:
            await consumer
        finally:
            _ = consumer.cancel()
            with suppress(asyncio.CancelledError):
                await consumer
            readers = tuple(self._reader_tasks)
            for reader in readers:
                _ = reader.cancel()
            if readers:
                _ = await asyncio.gather(*readers, return_exceptions=True)

    async def _consume_connection(  # noqa: C901, PLR0912
        self,
        connection: EventConnection,
        *,
        startup: bool,
        gap_started: datetime | None,
    ) -> None:
        queue: asyncio.Queue[SSEFrame | object | _Disconnect] = asyncio.Queue()
        pump = asyncio.create_task(self._pump(connection, queue))
        self._reader_tasks.add(pump)
        pump.add_done_callback(self._reader_tasks.discard)
        # The connection is already open. Give its reader one scheduling turn
        # before a possibly in-memory snapshot completes synchronously.
        await asyncio.sleep(0)
        snapshot = await self._snapshot_with_retry()
        await queue.put(_BARRIER)
        buffered: list[SSEFrame] = []
        disconnect: _Disconnect | None = None
        while True:
            item = await queue.get()
            if item is _BARRIER:
                break
            if isinstance(item, _Disconnect):
                disconnect = item
                continue
            buffered.append(cast("SSEFrame", item))

        if (
            disconnect is not None
            and _is_terminal_disconnect(disconnect)
            and disconnect.error is not None
        ):
            raise disconnect.error

        snapshot_raw = {
            resource.id: self._resource_raw(resource) for resource in snapshot
        }
        if startup:
            self._raw = snapshot_raw
            for frame in buffered:
                await self._fold_frame(self._raw, frame, publish=False)
            self._connected = disconnect is None
            ready = self._ready
            if ready is not None and not ready.done():
                ready.set_result(None)
        else:
            historical = copy.deepcopy(self._raw)
            for frame in buffered:
                await self._fold_frame(historical, frame, publish=True)
            gap_end = datetime.now(UTC)
            await self._publish_item(
                Resync(
                    reason=ResyncReason.RECONNECT,
                    gap_started=gap_started or self._opened_at or gap_end,
                    gap_ended=gap_end,
                )
            )
            await self._publish_diff(historical, snapshot_raw, gap_end)
            self._raw = snapshot_raw
            for frame in buffered:
                await self._fold_frame(self._raw, frame, publish=False)
            self._connected = disconnect is None

        if disconnect is not None:
            await pump
            return

        while True:
            item = await queue.get()
            if isinstance(item, _Disconnect):
                self._connected = False
                await pump
                if _is_terminal_disconnect(item) and item.error is not None:
                    raise item.error
                return
            if item is _BARRIER:
                continue
            await self._fold_frame(self._raw, cast("SSEFrame", item), publish=True)

    async def _pump(
        self,
        connection: EventConnection,
        queue: asyncio.Queue[SSEFrame | object | _Disconnect],
    ) -> None:
        try:
            async for frame in connection.frames:
                await queue.put(frame)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - preserve disconnect boundary
            await queue.put(_Disconnect(exc))
        else:
            await queue.put(_Disconnect())

    async def _snapshot_with_retry(self) -> list[AnyResource]:
        retry = 0
        while True:
            try:
                return await self._hue.snapshot()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - reconnect until cancelled
                retry += 1
                delay = min(2 ** (retry - 1), 60)
                logger.warning(
                    "State snapshot failed (%s); retrying in %ss", exc, delay
                )
                await asyncio.sleep(delay)

    async def _fold_frame(  # noqa: C901, PLR0912 - tolerant shape dispatch
        self,
        raw_state: dict[str, dict[str, Any]],
        frame: SSEFrame,
        *,
        publish: bool,
    ) -> None:
        self._last_frame_at = frame.received_at
        for raw_event in frame.events:
            try:
                event = HueEvent.model_validate(raw_event)
            except ValidationError:
                if publish:
                    await self._inconsistent(frame, raw_event)
                continue
            event_type = event.event_type
            if event_type is None or event_type not in {
                EventType.UPDATE,
                EventType.ADD,
                EventType.DELETE,
            }:
                if publish:
                    await self._inconsistent(frame, raw_event)
                continue
            grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
            data = raw_event.get("data")
            if not isinstance(data, list):
                if publish:
                    await self._inconsistent(frame, raw_event)
                continue
            malformed_entry = False
            for entry in cast("list[object]", data):
                if not isinstance(entry, dict):
                    malformed_entry = True
                    continue
                typed_entry = cast("dict[str, Any]", entry)
                resource_id = typed_entry.get("id")
                if not isinstance(resource_id, str) or not resource_id:
                    malformed_entry = True
                    continue
                grouped[resource_id].append(typed_entry)
            if malformed_entry and publish:
                await self._inconsistent(frame, raw_event)
            for resource_id, entries in grouped.items():
                delta: dict[str, Any] = {}
                for entry in entries:
                    delta = _deep_merge(delta, entry)
                was_missing = resource_id not in raw_state
                try:
                    change = await self._fold_resource(
                        raw_state,
                        event_type,
                        resource_id,
                        delta,
                        event.creationtime,
                        frame,
                    )
                except ValidationError:
                    if publish:
                        await self._inconsistent(frame, raw_event)
                    else:
                        logger.warning("Ignoring invalid startup event: %r", raw_event)
                    continue
                if (
                    publish
                    and change is None
                    and was_missing
                    and event_type is EventType.UPDATE
                ):
                    await self._inconsistent(frame, raw_event)
                if publish and change is not None:
                    await self._publish_change(change)

    async def _fold_resource(  # noqa: PLR0913, PLR0917 - complete fold context
        self,
        raw_state: dict[str, dict[str, Any]],
        event_type: EventType,
        resource_id: str,
        delta: dict[str, Any],
        event_at: datetime | None,
        frame: SSEFrame,
    ) -> Change | None:
        before_raw = raw_state.get(resource_id)
        resource_type = str(
            delta.get(
                "type",
                before_raw.get("type", "") if before_raw else "",
            )
        )
        if event_type is EventType.DELETE:
            if before_raw is None:
                return None
            del raw_state[resource_id]
            return self._make_change(
                ChangeKind.DELETE,
                before_raw,
                None,
                {},
                event_at,
                frame,
                resource_id,
                resource_type,
            )

        if before_raw is None and event_type is EventType.UPDATE:
            fetched = await self._fetch_resource(resource_type, resource_id)
            if fetched is None:
                return None
            after_raw = self._resource_raw(fetched)
            raw_state[resource_id] = after_raw
            return self._make_change(
                ChangeKind.ADD,
                None,
                after_raw,
                delta,
                event_at,
                frame,
                resource_id,
                resource_type,
            )

        after_raw = (
            copy.deepcopy(delta)
            if before_raw is None
            else _deep_merge(before_raw, delta)
        )
        _ = parse_resource(copy.deepcopy(after_raw))
        if before_raw == after_raw:
            return None
        raw_state[resource_id] = after_raw
        return self._make_change(
            ChangeKind.ADD if before_raw is None else ChangeKind.UPDATE,
            before_raw,
            after_raw,
            delta,
            event_at,
            frame,
            resource_id,
            resource_type,
        )

    def _make_change(  # noqa: PLR0913, PLR0917 - persisted record fields
        self,
        kind: ChangeKind,
        before_raw: dict[str, Any] | None,
        after_raw: dict[str, Any] | None,
        delta: dict[str, Any],
        event_at: datetime | None,
        frame: SSEFrame,
        resource_id: str,
        resource_type: str,
    ) -> Change:
        return Change(
            kind=kind,
            observed_at=_observed_at(delta),
            event_at=event_at,
            received_at=frame.received_at,
            event_id=frame.event_id,
            resource_id=resource_id,
            resource_type=resource_type,
            before=self._detached(before_raw),
            after=self._detached(after_raw),
            delta=copy.deepcopy(delta),
        )

    async def _fetch_resource(
        self,
        resource_type: str,
        resource_id: str,
    ) -> AnyResource | None:
        if not resource_type:
            return None
        try:
            response = HueResponse[AnyResource].model_validate(
                await self._hue.http.get(
                    f"/clip/v2/resource/{resource_type}/{resource_id}"
                )
            )
            response.raise_for_errors()
        except HueAPIError as exc:
            if exc.status_code == 404:  # noqa: PLR2004 - HTTP Not Found
                return None
            raise
        except (HueResponseError, ValidationError):
            return None
        if not response.data:
            return None
        fetched = response.data[0]
        if fetched.id != resource_id or fetched.type != resource_type:
            return None
        return fetched

    async def _inconsistent(self, frame: SSEFrame, detail: dict[str, Any]) -> None:
        await self._publish_item(
            Resync(
                reason=ResyncReason.INCONSISTENT,
                gap_started=frame.received_at,
                gap_ended=frame.received_at,
                detail=copy.deepcopy(detail),
            )
        )

    async def _publish_diff(
        self,
        before: dict[str, dict[str, Any]],
        after: dict[str, dict[str, Any]],
        at: datetime,
    ) -> None:
        for resource_id in before.keys() | after.keys():
            old = before.get(resource_id)
            new = after.get(resource_id)
            if old == new:
                continue
            resource_type = str((new or old or {}).get("type", ""))
            change = Change(
                kind=(
                    ChangeKind.ADD
                    if old is None
                    else ChangeKind.DELETE
                    if new is None
                    else ChangeKind.UPDATE
                ),
                received_at=at,
                resource_id=resource_id,
                resource_type=resource_type,
                before=self._detached(old),
                after=self._detached(new),
                delta=_dict_delta(old or {}, new or {}) if new is not None else {},
                resynced=True,
            )
            await self._publish_item(change)

    async def _publish_change(self, change: Change) -> None:
        command, observation = self._matching_command(change)
        annotated = (
            change
            if command is None
            else change.model_copy(
                update={
                    "origin": "self",
                    "command_id": command.write.command_id,
                    "command_confirmed": (
                        True
                        if command.write.status == "accepted"
                        else False
                        if command.write.status in {"unknown", "rejected"}
                        else None
                    ),
                    "observation": observation,
                    "transition_ends_at": command.transition_ends_at,
                },
                deep=True,
            )
        )
        predecessor = self._publication_tail
        must_wait = command is not None and command.write.status == "pending"
        if not must_wait and (predecessor is None or predecessor.done()):
            await self._broadcast(annotated)
            return
        task = asyncio.create_task(
            self._publish_in_order(annotated, command, predecessor)
        )
        self._publication_tail = task
        self._publish_tasks.add(task)
        task.add_done_callback(self._publish_tasks.discard)

    async def _publish_item(self, item: _BufferItem) -> None:
        predecessor = self._publication_tail
        if predecessor is None or predecessor.done():
            await self._broadcast(item)
            return
        task = asyncio.create_task(self._publish_item_in_order(item, predecessor))
        self._publication_tail = task
        self._publish_tasks.add(task)
        task.add_done_callback(self._publish_tasks.discard)

    async def _publish_item_in_order(
        self,
        item: _BufferItem,
        predecessor: asyncio.Task[None],
    ) -> None:
        await predecessor
        await self._broadcast(item)

    async def _publish_in_order(
        self,
        change: Change,
        command: _Command | None,
        predecessor: asyncio.Task[None] | None,
    ) -> None:
        if predecessor is not None:
            await predecessor
        if command is not None and command.write.status == "pending":
            _ = await command.done.wait()
        if command is not None and command.write.status == "rejected":
            change = change.model_copy(
                update={
                    "origin": "unattributed",
                    "command_id": None,
                    "command_confirmed": None,
                    "observation": "reported",
                    "transition_ends_at": None,
                },
                deep=True,
            )
        elif command is not None:
            change = change.model_copy(
                update={"command_confirmed": command.write.status == "accepted"},
                deep=True,
            )
        await self._broadcast(change)

    def _matching_command(
        self,
        change: Change,
    ) -> tuple[_Command | None, str]:
        self._prune_commands(change.received_at)
        candidates = [
            command
            for command in self._commands.values()
            if change.resource_id in command.resource_ids
        ]
        candidates.sort(key=lambda command: command.write.sent_at, reverse=True)
        for command in candidates:
            if change.received_at < command.write.sent_at:
                continue
            if _compatible(command.target, change.delta):
                if (
                    command.transition_ends_at is not None
                    and change.resource_id not in command.echoed_resources
                ):
                    command.echoed_resources.add(change.resource_id)
                    return command, "command_echo"
                return command, "reported"
        fade = self.fading.get(change.resource_id)
        if (
            fade is not None
            and change.received_at >= fade.sent_at
            and not set(change.delta).isdisjoint(fade.target)
        ):
            command = self._commands.get(fade.command_id)
            if command is not None:
                return command, "reported"
        return None, "reported"

    def _prune_commands(self, now: datetime) -> None:
        active_ids = {fade.command_id for fade in self._fades.values()}
        expired = [
            command_id
            for command_id, command in self._commands.items()
            if command_id not in active_ids
            and command.write.sent_at + WRITE_MATCH_WINDOW <= now
        ]
        for command_id in expired:
            del self._commands[command_id]

    def _observe_write(self, write: PendingWrite) -> None:
        command = self._commands.get(write.command_id)
        if write.status == "pending":
            resource_type, resource_id = _parse_resource_path(write.path)
            if resource_id is None:
                return
            resource_ids = self._write_targets(resource_type, resource_id)
            target = {
                key: copy.deepcopy(value)
                for key, value in write.payload.items()
                if key != "dynamics"
            }
            duration = _duration(write.payload)
            ends_at = (
                write.sent_at + timedelta(milliseconds=duration)
                if duration is not None and duration > 0
                else None
            )
            command = _Command(
                write=write,
                resource_ids=frozenset(resource_ids),
                target=target,
                transition_ends_at=ends_at,
            )
            self._commands[write.command_id] = command
            if ends_at is not None:
                for target_id in resource_ids:
                    self._fades[target_id] = ActiveFade(
                        command_id=write.command_id,
                        resource_id=target_id,
                        target=target,
                        sent_at=write.sent_at,
                        ends_at=ends_at,
                        unreliable_until=ends_at + FADE_REPORT_ALLOWANCE,
                    )
            return
        if command is None:
            return
        command.write = write
        command.done.set()
        if write.status == "rejected":
            _ = self._commands.pop(write.command_id, None)
        for resource_id, fade in tuple(self._fades.items()):
            if fade.command_id != write.command_id:
                continue
            if write.status == "rejected":
                del self._fades[resource_id]
            else:
                self._fades[resource_id] = fade.model_copy(
                    update={"confirmed": write.status == "accepted"},
                    deep=True,
                )

    def _write_targets(self, resource_type: str | None, resource_id: str) -> set[str]:
        if resource_type == "light":
            return {resource_id}
        if resource_type != "grouped_light":
            return {resource_id}
        groups = [
            group
            for group in [*self.rooms.list(), *self.zones.list()]
            if group.service_id("grouped_light") == resource_id
        ]
        if not groups:
            return {resource_id}
        return {
            resource_id,
            *(light.id for group in groups for light in self.lights_in(group)),
        }

    async def _broadcast(self, item: _BufferItem) -> None:
        for subscriber in tuple(self._subscribers):
            detached = (
                item.model_copy(deep=True)
                if isinstance(item, (Change, Resync))
                else item
            )
            await subscriber.put(detached)

    def _public(self, raw: dict[str, Any]) -> AnyResource:
        return parse_resource(copy.deepcopy(raw)).bind(
            self._hue, str(raw.get("type", ""))
        )

    @staticmethod
    def _detached(raw: dict[str, Any] | None) -> AnyResource | None:
        return parse_resource(copy.deepcopy(raw)) if raw is not None else None

    @staticmethod
    def _resource_raw(resource: AnyResource) -> dict[str, Any]:
        return resource.model_dump(
            mode="json",
            by_alias=True,
            exclude_none=True,
            exclude_computed_fields=True,
        )


def _deep_merge(base: dict[str, Any], delta: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in delta.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            merged[key] = _deep_merge(
                cast("dict[str, Any]", current), cast("dict[str, Any]", value)
            )
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _event_source_stopped() -> Never:
    """Raise the terminal error for an unexpectedly finite connection source."""
    raise BridgeConnectionError(_EVENT_SOURCE_STOPPED_MESSAGE)


def _is_terminal_disconnect(disconnect: _Disconnect) -> bool:
    """Whether a frame reader ended for a non-network reason."""
    return disconnect.error is not None and not isinstance(
        disconnect.error,
        BridgeConnectionError,
    )


def _dict_delta(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    delta: dict[str, Any] = {}
    for key, value in after.items():
        old = before.get(key, object())
        if isinstance(old, dict) and isinstance(value, dict):
            nested = _dict_delta(
                cast("dict[str, Any]", old), cast("dict[str, Any]", value)
            )
            if nested:
                delta[key] = nested
        elif old != value:
            delta[key] = copy.deepcopy(value)
    return delta


def _observed_at(delta: dict[str, Any]) -> datetime | None:
    stack: list[tuple[str | None, object]] = [(None, delta)]
    while stack:
        parent, value = stack.pop()
        if isinstance(value, dict):
            mapping = cast("dict[str, object]", value)
            if parent == "button" or (
                parent is not None and parent.endswith("_report")
            ):
                for key in ("changed", "updated"):
                    raw = mapping.get(key)
                    if raw is None:
                        continue
                    try:
                        return _TIMESTAMP.validate_python(raw)
                    except ValidationError:
                        continue
            stack.extend(reversed(tuple(mapping.items())))
        elif isinstance(value, list):
            stack.extend(
                (parent, item) for item in reversed(cast("list[object]", value))
            )
    return None


def _parse_resource_path(path: str) -> tuple[str | None, str | None]:
    parts = path.strip("/").split("/")
    if len(parts) < _RESOURCE_PATH_PARTS or parts[:3] != ["clip", "v2", "resource"]:
        return None, None
    return parts[3], parts[4]


def _duration(payload: dict[str, Any]) -> int | None:
    dynamics = payload.get("dynamics")
    if not isinstance(dynamics, dict):
        return None
    duration = cast("dict[str, object]", dynamics).get("duration")
    return int(duration) if isinstance(duration, (int, float)) else None


def _compatible(expected: dict[str, Any], actual: dict[str, Any]) -> bool:
    if not expected:
        return False
    compared = False
    for key, value in expected.items():
        if key not in actual:
            continue
        compared = True
        if not _values_close(value, actual[key]):
            return False
    return compared


def _values_close(expected: object, actual: object) -> bool:
    if isinstance(expected, dict) and isinstance(actual, dict):
        expected_dict = cast("dict[object, object]", expected)
        actual_dict = cast("dict[object, object]", actual)
        return all(
            key in actual_dict and _values_close(value, actual_dict[key])
            for key, value in expected_dict.items()
        )
    if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        return abs(float(expected) - float(actual)) <= _MATCH_TOLERANCE
    return expected == actual
