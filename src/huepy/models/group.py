"""Models for resources that group other resources: rooms, zones, homes, scenes."""

import asyncio
from dataclasses import dataclass
from typing import Any, override

from pydantic import AwareDatetime, Field

from huepy.models.common import (
    RESOURCE_ROOT,
    CommandResult,
    HueModel,
    HueResource,
    NamedResource,
    ResourceIdentifier,
    ResourceType,
    unwrap,
)
from huepy.models.light import Light, LightCommands, LightState


@dataclass(frozen=True)
class GroupState:
    """A restorable snapshot of every light in one room or zone.

    Attributes:
        group_id: The room or zone the snapshot was taken from. Restoring it
            onto a different group is refused.
        lights: One captured state per light, in the order they were read.

    """

    group_id: str
    lights: tuple[LightState, ...]


class ResourceGroup(NamedResource, LightCommands):
    """Common shape of a room or zone: named, with children and services.

    Rooms and zones do not accept light commands directly: each owns a
    `grouped_light` service that does. The reference to that service already
    arrived in ``services``, so a bound group resolves it from memory and
    performs a light command in exactly one request.
    """

    children: list[ResourceIdentifier] = Field(default_factory=list)
    services: list[ResourceIdentifier] = Field(default_factory=list)

    def service_id(self, rtype: ResourceType | str) -> str | None:
        """Return the id of this group's service of the given type, if it has one.

        Args:
            rtype: The service type to look for, e.g. ``ResourceType.GROUPED_LIGHT``.

        Returns:
            The service's rid, or None when the group exposes no such service.

        """
        wanted = str(rtype)
        return next((s.rid for s in self.services if s.rtype == wanted), None)

    def contains_device(self, device_id: str) -> bool:
        """Whether the given device is a direct child of this group."""
        return any(
            child.rid == device_id and child.rtype == ResourceType.DEVICE
            for child in self.children
        )

    def contains_light(self, light: Light) -> bool:
        """Whether this group's children resolve to ``light``.

        One rule covers both group kinds: a room's children are the devices
        that own their light services, while a zone's children are the light
        services themselves. Testing the light's own id *and* its owner's
        matches whichever of the two this group uses, and the two id spaces
        do not overlap, so neither can match by accident.

        Args:
            light: The light to test for membership.

        Returns:
            True when the light belongs to this room or zone.

        """
        child_ids = {child.rid for child in self.children}
        return light.id in child_ids or (
            light.owner is not None and light.owner.rid in child_ids
        )

    async def lights(self) -> list[Light]:
        """Fetch this group's own lights.

        A group's ``children`` are references, not lights, so answering "which
        lights are in this room?" always meant a fetch plus a join. One request
        lists every light; the join is :meth:`contains_light`.

        Returns:
            The bound lights belonging to this group, empty when it has none.

        Raises:
            DetachedResourceError: If this resource is not bound to a client.
            HueResponseError: If the bridge reports a blocking error.

        """
        client = self._client
        payload = await client.http.get(f"{RESOURCE_ROOT}/{ResourceType.LIGHT}")
        return [
            light.bind(client, ResourceType.LIGHT)
            for light in unwrap(payload, Light)
            if self.contains_light(light)
        ]

    async def capture(self) -> GroupState:
        """Capture the state of every light in this group, ready to restore.

        Returns:
            A snapshot tied to this group's id.

        Raises:
            DetachedResourceError: If this resource is not bound to a client.

        """
        return GroupState(
            group_id=self.id,
            lights=tuple(light.capture() for light in await self.lights()),
        )

    async def restore(
        self,
        state: GroupState,
        *,
        transition: float | None = None,
    ) -> list[CommandResult]:
        """Restore a snapshot captured from this same group.

        One request per light rather than one for the group, which is the one
        place a group command is worth splitting: a group's ``grouped_light``
        reports no aggregate colour temperature, so restoring through it
        silently drops the colour temperature and leaves the room the wrong
        colour. The requests are issued concurrently.

        A light that has left the group since the capture is skipped rather
        than resurrected.

        Args:
            state: A snapshot from :meth:`capture` on this same group.
            transition: Fade duration in seconds, applied to every light.

        Returns:
            One CommandResult per light restored.

        Raises:
            ValueError: If the snapshot came from a different group.
            DetachedResourceError: If this resource is not bound to a client.
            HueResponseError: If the bridge rejects a write.

        """
        if state.group_id != self.id:
            msg = f"state belongs to group {state.group_id}, not {self.id}"
            raise ValueError(msg)
        present = {light.id: light for light in await self.lights()}
        # Fanned out concurrently, but each PUT targets /light, so the transport
        # rate limiter spaces their starts and a large room cannot flood the
        # bridge -- the throughput budget is enforced there, not here.
        return list(
            await asyncio.gather(
                *(
                    present[captured.light_id].restore(captured, transition=transition)
                    for captured in state.lights
                    if captured.light_id in present
                )
            )
        )

    @override
    def _command_path(self) -> str:
        """Route light commands through this group's grouped_light service.

        Returns:
            The endpoint of the group's grouped_light service.

        Raises:
            ValueError: If the group exposes no grouped_light service.

        """
        service_id = self.service_id(ResourceType.GROUPED_LIGHT)
        if service_id is None:
            rtype = self._rtype or self.type or "group"
            msg = f"No grouped_light service found for {rtype} {self.id}"
            raise ValueError(msg)
        return f"{RESOURCE_ROOT}/{ResourceType.GROUPED_LIGHT}/{service_id}"


class Room(ResourceGroup):
    """A room: a group of devices in one physical space."""


class Zone(ResourceGroup):
    """A zone: an arbitrary group of light services."""


class BridgeHome(HueResource):
    """The top-level home grouping every room and unassigned device."""

    children: list[ResourceIdentifier] = Field(default_factory=list)
    services: list[ResourceIdentifier] = Field(default_factory=list)


class ServiceGroup(NamedResource):
    """A named group of arbitrary services."""

    children: list[ResourceIdentifier] = Field(default_factory=list)
    services: list[ResourceIdentifier] = Field(default_factory=list)


class SceneAction(HueModel):
    """One resource target and its stored scene action."""

    target: ResourceIdentifier | None = None
    action: dict[str, Any] = Field(default_factory=dict)


class SceneStatus(HueModel):
    """The bridge-reported activation state of a scene."""

    active: str | None = None
    last_recall: AwareDatetime | None = None


class Scene(NamedResource):
    """A stored lighting state for a room or zone."""

    group: ResourceIdentifier | None = None
    speed: float | None = None
    auto_dynamic: bool | None = None
    actions: list[SceneAction] = Field(default_factory=list)
    status: SceneStatus | None = None

    async def activate(self) -> CommandResult:
        """Recall this scene, applying it to the room or zone it belongs to.

        Returns:
            A CommandResult containing the bridge references affected.

        Raises:
            DetachedResourceError: If this scene is not bound to a client.
            HueResponseError: If the bridge rejects the recall.

        """
        return await self.update({"recall": {"action": "active"}})
