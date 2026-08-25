"""Continuously maintained, event-folded Hue bridge state."""

from __future__ import annotations

import asyncio
import copy
import logging
import weakref
from collections import defaultdict, deque
from collections.abc import AsyncGenerator, Callable, Coroutine, Mapping
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
    StateNotStartedError,
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
    ChangeContext,
    ChangeKind,
    Resync,
    ResyncReason,
)
from huepy.state.subscribe import (
    ChangeFilter,
    ChangeHandler,
    ResyncHandler,
    Subscription,
)
from huepy.utils.naming import build_name_map

logger = logging.getLogger(__name__)

DEFAULT_SUBSCRIBER_SIZE: Final = 4096
# How long close() lets registered handlers finish the closed stream.
DISPATCH_DRAIN_TIMEOUT: Final = 5.0
FADE_REPORT_ALLOWANCE: Final = timedelta(seconds=25)
WRITE_MATCH_WINDOW: Final = timedelta(seconds=25)
_TIMESTAMP: TypeAdapter[datetime] = TypeAdapter(AwareDatetime)
_BARRIER = object()
_CLOSED = object()
UNKNOWN_NAME: Final = "Unknown"
_MIN_BUFFER_SIZE = 2
_RESOURCE_PATH_PARTS = 5
_MATCH_TOLERANCE = 0.1
_MATCH_TOLERANCES: Final = MappingProxyType(
    {
        # Measured: a commanded brightness of 20.0 echoes back as 20.16. The bridge
        # stores brightness as 254 levels, so its own grid is ~0.4 apart and a
        # tolerance below that made `command_echo` unreachable for any brightness
        # -- every transition echo was misfiled as a physical `reported` value.
        # Per attribute rather than one global number, because the same constant
        # guards `xy`, whose whole range is 0..1.
        "brightness": 0.5,
        "mirek": 1.0,
    }
)
_EVENT_SOURCE_STOPPED_MESSAGE = "Event connection source stopped"


@dataclass(frozen=True)
class _Disconnect:
    error: BaseException | None = None


@dataclass(frozen=True)
class _Registration[HandlerT]:
    """One handler and the filter that decides what reaches it."""

    handler: HandlerT
    filter: ChangeFilter


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
        self._name_map: dict[str, str] | None = None
        self._change_handlers: list[_Registration[ChangeHandler]] = []
        self._resync_handlers: list[_Registration[ResyncHandler]] = []
        self._dispatch_task: asyncio.Task[None] | None = None
        self._subscribers: set[_Subscriber] = set()
        self._task: asyncio.Task[None] | None = None
        # Separate from `_task`, which close() clears: a closed graph still
        # holds what it observed, and refusing to read it back would be a
        # regression on a view that was always last-reported state anyway.
        self._started = False
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
    def tracking(self) -> bool:
        """Whether observation has been started on this state."""
        return self._task is not None

    def _ensure_started(self) -> None:
        """Refuse to serve a graph that has never been filled.

        A stopped state holds no resources, so an unguarded read would answer
        "no lights" when the truth is "not tracking yet" -- the one hazard of
        exposing ``hue.state`` from construction. A *closed* state still holds
        what it last observed and stays readable; only the never-started case
        raises.

        Raises:
            StateNotStartedError: If observation has never started.

        """
        if not self._started:
            msg = (
                "Local state is not being tracked. Construct the client with "
                "Hue(state=True), or enter `async with hue.state`."
            )
            raise StateNotStartedError(msg)

    @property
    def resources(self) -> list[AnyResource]:
        """Fresh bound copies of every aggregate-visible resource.

        Raises:
            StateNotStartedError: If observation has never started.

        """
        self._ensure_started()
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
        self._started = True
        try:
            await self._ready
        except BaseException:
            await self.close()
            raise
        # After the handshake, so handlers registered before start see a graph
        # that can already answer `name_of` for their filters.
        self._ensure_dispatching()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        """Stop observation and release subscribers."""
        await self.close()

    async def close(self) -> None:
        """Stop observation and close every subscriber iterator.

        Handler dispatch is stopped *last*, after ``_CLOSED`` is broadcast, so
        registered handlers receive the same queued tail that ``changes()``
        consumers do instead of being cut off mid-shutdown.
        """
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
        await self._stop_dispatch()

    async def _stop_dispatch(self) -> None:
        """Let the handler reader finish the closed stream, then make sure.

        Gathered with ``return_exceptions``: a terminal observer failure is
        re-raised into the dispatch task by ``_drain``, so the task ends with
        an exception rather than cancelled. Awaiting it bare would propagate
        that out of ``close()`` and abandon the rest of the shutdown.
        """
        dispatch, self._dispatch_task = self._dispatch_task, None
        if dispatch is None:
            return
        _, pending = await asyncio.wait({dispatch}, timeout=DISPATCH_DRAIN_TIMEOUT)
        if pending:
            # A handler that never returns must not hold the client open.
            logger.warning("State handlers did not finish; cancelling dispatch")
            _ = dispatch.cancel()
        _ = await asyncio.gather(dispatch, return_exceptions=True)

    def by_id(self, resource_id: str) -> AnyResource | None:
        """Return a fresh bound resource by id.

        Raises:
            StateNotStartedError: If observation has never started.

        """
        self._ensure_started()
        raw = self._raw.get(resource_id)
        return self._public(raw) if raw is not None else None

    def list[ModelT: HueResource](self, model: type[ModelT]) -> list[ModelT]:
        """Return fresh bound copies of every resource matching a model type."""
        return [resource for resource in self.resources if isinstance(resource, model)]

    def name_of(self, resource_id: str) -> str:
        """Resolve a resource or its owner to a human-facing name.

        Raises:
            StateNotStartedError: If observation has never started.

        """
        return self._names_map().get(resource_id, UNKNOWN_NAME)

    def _invalidate_names(self, raw_state: dict[str, dict[str, Any]]) -> None:
        """Drop the memoised name map when the *live* graph changed.

        Called at each mutation rather than once per frame. ``_fold_resource``
        suspends on a point fetch for an unknown id, and a rebuild during that
        window would cache a half-folded graph that later mutations in the same
        frame no longer invalidate -- serving a renamed light under its old
        name until some unrelated frame arrived, and writing that stale name
        into recorded history. Reconnect reconciliation folds a *copy*, which
        must not disturb the live map; hence the identity check.
        """
        if raw_state is self._raw:
            self._name_map = None

    def _names_map(self) -> dict[str, str]:
        """Build the id-to-name map once per graph revision.

        Building it walks :attr:`resources`, which deep-copies and revalidates
        every resource in the graph -- 186 on the measured bridge. Rebuilding
        per call made enriching one change with a name and a room cost two full
        graph re-parses, so a scene recall paid it once per light.
        """
        cached = self._name_map
        if cached is None:
            cached = build_name_map(self.list(NamedResource))
            self._name_map = cached
        return cached

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

    def changes(
        self,
        *,
        maxsize: int = DEFAULT_SUBSCRIBER_SIZE,
    ) -> AsyncGenerator[StateItem]:
        """Yield an isolated stream of changes and continuity markers.

        Deliberately not an ``async def``: the subscriber is registered when
        this is *called*, not when the returned iterator is first advanced. An
        async generator body does not run until its first ``__anext__``, so
        registering there dropped everything published between building the
        iterator and starting to consume it.

        Args:
            maxsize: Bounded newest-wins buffer depth for this subscriber.

        Returns:
            An async iterator of :class:`Change` and :class:`Resync` records.

        Raises:
            RuntimeError: If observation is not running.

        """
        return self._track(self._subscribe(maxsize))

    def on_change(
        self,
        handler: ChangeHandler,
        /,
        *,
        name: str | None = None,
        model: type[HueResource] | None = None,
        resource_id: str | None = None,
        kind: ChangeKind | None = None,
    ) -> Subscription:
        """Call ``handler`` for every change matching every supplied filter.

        The handler may be a plain function or a coroutine function. It can be
        registered before observation starts, which is why ``hue.state`` exists
        from construction. Continuity markers never arrive here -- register
        :meth:`on_resync` for those.

        A handler that raises is logged and skipped; one bad handler must not
        stop a process meant to run for weeks. All handlers share one reader,
        so a slow handler delays the other handlers but never the fold loop;
        use :meth:`watch` in your own task when you need isolation.

        Args:
            handler: Called with each matching :class:`Change`.
            name: Display name of the resource, matched case-insensitively.
            model: Concrete resource model, matched against the resource after
                the change, or before it for a delete.
            resource_id: Exact resource id.
            kind: Only ``UPDATE``, ``ADD`` or ``DELETE``.

        Returns:
            A :class:`Subscription` that unregisters on ``cancel()`` or on
            exit when used as a context manager.

        """
        registration = _Registration(
            handler, ChangeFilter(name, model, resource_id, kind)
        )
        self._change_handlers.append(registration)
        self._ensure_dispatching()
        return Subscription(lambda: self._discard(self._change_handlers, registration))

    def on_resync(self, handler: ResyncHandler, /) -> Subscription:
        """Call ``handler`` for every continuity marker.

        Args:
            handler: Called with each :class:`Resync`.

        Returns:
            A :class:`Subscription` that unregisters on ``cancel()``.

        """
        registration = _Registration(handler, ChangeFilter())
        self._resync_handlers.append(registration)
        self._ensure_dispatching()
        return Subscription(lambda: self._discard(self._resync_handlers, registration))

    def watch(
        self,
        *,
        name: str | None = None,
        model: type[HueResource] | None = None,
        resource_id: str | None = None,
        kind: ChangeKind | None = None,
        maxsize: int = DEFAULT_SUBSCRIBER_SIZE,
    ) -> AsyncGenerator[Change]:
        """Yield matching changes only, discarding continuity markers.

        Each discarded marker is logged at WARNING first: silently dropping a
        gap would be the one thing this layer refuses to do. Use
        :meth:`changes` when the gaps matter.

        Args:
            name: Display name of the resource, matched case-insensitively.
            model: Concrete resource model.
            resource_id: Exact resource id.
            kind: Only ``UPDATE``, ``ADD`` or ``DELETE``.
            maxsize: Bounded newest-wins buffer depth for this subscriber.

        Returns:
            An async iterator of matching :class:`Change` records.

        Raises:
            RuntimeError: If observation is not running.

        """
        subscriber = self._subscribe(maxsize)
        return self._track(
            subscriber,
            self._watch(ChangeFilter(name, model, resource_id, kind), subscriber),
        )

    def _name_for(self, change: Change) -> str:
        """Resolve the display name a change refers to, deletes included.

        A DELETE has already been folded out of the graph by the time anyone
        sees the record, so the live lookup answers ``"Unknown"``. The record
        still carries what the resource was, and without this every delete
        would be filtered out by a `name=` handler and written to history
        nameless.
        """
        name = self.name_of(change.resource_id)
        if name != UNKNOWN_NAME:
            return name
        # `after` first, like `ChangeFilter.matches`: a rename enriched after
        # its resource left the graph should carry the name it changed *to*.
        resource = change.after or change.before
        if isinstance(resource, NamedResource) and resource.name:
            return resource.name
        if resource is not None and resource.owner is not None:
            return self.name_of(resource.owner.rid)
        return name

    def _room_for(self, change: Change) -> Room | None:
        """Resolve the room a change refers to, deletes included."""
        room = self.room_of(change.resource_id)
        if room is not None or self.by_id(change.resource_id) is not None:
            # Only fall back when the resource is actually gone. While it is
            # still in the graph `room_of` already resolved through its owning
            # device, so a second pass could not answer differently -- and each
            # pass deep-copies and revalidates the whole graph.
            return room
        resource = change.after or change.before
        if resource is None or resource.owner is None:
            return None
        # The service is gone but the device that owned it usually is not.
        return self.room_of(resource.owner.rid)

    def describe(self, change: Change) -> ChangeContext:
        """Resolve the display name and containing room for one change.

        Args:
            change: The record to resolve topology for.

        Returns:
            A :class:`ChangeContext` pairing the change with its name and room.

        Raises:
            StateNotStartedError: If observation has never started.

        """
        return ChangeContext(
            change=change,
            name=self._name_for(change),
            room=self._room_for(change),
        )

    async def _watch(
        self,
        change_filter: ChangeFilter,
        subscriber: _Subscriber,
    ) -> AsyncGenerator[Change]:
        """Yield the changes from one subscriber that pass ``change_filter``."""
        async for item in self._drain(subscriber):
            if isinstance(item, Resync):
                logger.warning(
                    "watch() discarded a %s marker (%s..%s); changes() sees gaps",
                    item.reason,
                    item.gap_started,
                    item.gap_ended,
                )
                continue
            if change_filter.matches(item, self._name_for):
                yield item

    def _discard[HandlerT](
        self,
        registrations: list[_Registration[HandlerT]],
        registration: _Registration[HandlerT],
    ) -> None:
        """Remove one registration, tolerating a state that already closed.

        Stops the shared reader once nothing is listening. Left running, it
        would keep taking a deep copy of every change into a queue whose
        consumer matches nothing, for the life of a client that no longer has
        any callbacks.
        """
        with suppress(ValueError):
            registrations.remove(registration)
        if self._change_handlers or self._resync_handlers:
            return
        dispatch, self._dispatch_task = self._dispatch_task, None
        if dispatch is None or dispatch is asyncio.current_task():
            # A handler cancelling its own subscription -- the one-shot idiom --
            # is running *inside* this task. Cancelling here would deliver the
            # CancelledError into that handler at its next await. The loop
            # checks for an empty roster after each item and retires itself.
            return
        # `_drain`'s finally deregisters the subscriber on cancellation.
        _ = dispatch.cancel()

    def _ensure_dispatching(self) -> None:
        """Start the shared handler reader once there is something to feed.

        Handlers may be registered before observation starts, so this is called
        both on registration and at startup; whichever happens second wins.
        """
        if self._dispatch_task is not None and not self._dispatch_task.done():
            return
        if self._task is None or self._task.done():
            return
        if not (self._change_handlers or self._resync_handlers):
            return
        subscriber = self._subscribe()
        self._dispatch_task = asyncio.create_task(
            self._dispatch(subscriber), name="huepy-state-dispatch"
        )

    async def _dispatch(self, subscriber: _Subscriber) -> None:
        """Feed every registered handler from one shared subscriber.

        One subscriber rather than one per handler, because lag is reported by
        inserting a marker into *that* subscriber's queue: with per-handler
        queues a lagging change handler would generate a marker it is defined
        never to receive, and its own data loss would vanish silently.
        """
        try:
            # `aclosing`, because the roster check below returns while the
            # generator is suspended: without it the subscriber would be
            # deregistered only when the asyncgen finalizer got round to it.
            async with aclosing(self._drain(subscriber)) as stream:
                await self._dispatch_items(stream)
        except asyncio.CancelledError:
            raise
        except Exception:
            # A terminal observer failure is re-raised into this task by
            # `_drain`. `changes()` consumers see it; handlers cannot, so
            # without this the callbacks simply stop firing and nothing says
            # why. `ensure_healthy()` re-raises it on demand.
            logger.exception("State stream stopped; handlers will receive nothing")

    async def _dispatch_items(self, stream: AsyncGenerator[StateItem]) -> None:
        """Offer each item to every handler whose filter accepts it."""
        async for item in stream:
            if isinstance(item, Resync):
                for registration in tuple(self._resync_handlers):
                    await self._invoke(registration.handler, item)
            else:
                for registration in tuple(self._change_handlers):
                    if registration.filter.matches(item, self._name_for):
                        await self._invoke(registration.handler, item)
            if not (self._change_handlers or self._resync_handlers):
                # The last subscription went away, from inside a handler.
                return

    @staticmethod
    async def _invoke[RecordT](
        handler: Callable[[RecordT], Coroutine[Any, Any, None] | None],
        record: RecordT,
    ) -> None:
        """Call one handler, awaiting it only when it returned a coroutine."""
        try:
            result = handler(record)
            if result is not None:
                await result
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("State handler %r failed", handler)

    def _subscribe(self, maxsize: int = DEFAULT_SUBSCRIBER_SIZE) -> _Subscriber:
        """Register a bounded subscriber synchronously.

        Raises:
            RuntimeError: If observation is not running.

        """
        if self._task is None or self._task.done():
            if self._terminal_error is not None:
                raise self._terminal_error
            msg = "HueState is not running"
            raise RuntimeError(msg)
        subscriber = _Subscriber(maxsize)
        self._subscribers.add(subscriber)
        return subscriber

    def _track[ItemT](
        self,
        subscriber: _Subscriber,
        stream: AsyncGenerator[ItemT] | None = None,
    ) -> AsyncGenerator[ItemT]:
        """Tie a subscriber's lifetime to the iterator handed to the caller.

        Registration happens eagerly so no change is missed, but an iterator
        that is never advanced never runs its body -- and `aclose()` on an
        unstarted async generator does not run it either. Without this, a
        `changes()` result that is built and dropped would leave a subscriber
        registered for the life of the process, taking a deep copy of every
        change forever.
        """
        generator = cast(
            "AsyncGenerator[ItemT]",
            self._drain(subscriber) if stream is None else stream,
        )
        _ = weakref.finalize(generator, self._subscribers.discard, subscriber)
        return generator

    async def _drain(self, subscriber: _Subscriber) -> AsyncGenerator[StateItem]:
        """Yield one registered subscriber's items until the stream closes."""
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
            self._name_map = None
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
            self._name_map = None
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
            self._invalidate_names(raw_state)
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
            self._invalidate_names(raw_state)
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
        self._invalidate_names(raw_state)
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
        if not _values_close(value, actual[key], key):
            return False
    return compared


def _values_close(expected: object, actual: object, key: str = "") -> bool:
    if isinstance(expected, dict) and isinstance(actual, dict):
        expected_dict = cast("dict[object, object]", expected)
        actual_dict = cast("dict[object, object]", actual)
        return all(
            inner in actual_dict
            and _values_close(value, actual_dict[inner], str(inner))
            for inner, value in expected_dict.items()
        )
    if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        tolerance = _MATCH_TOLERANCES.get(key, _MATCH_TOLERANCE)
        return abs(float(expected) - float(actual)) <= tolerance
    return expected == actual
