"""Models for resources that group other resources: rooms, zones, homes, scenes."""

from typing import Any, override

from pydantic import AwareDatetime, Field

from huepy.models.common import (
    RESOURCE_ROOT,
    HueModel,
    HueResource,
    NamedResource,
    ResourceIdentifier,
    ResourceType,
)
from huepy.models.light import LightCommands


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

    async def activate(self) -> list[ResourceIdentifier]:
        """Recall this scene, applying it to the room or zone it belongs to.

        Returns:
            References to the resources the bridge reports as updated.

        Raises:
            DetachedResourceError: If this scene is not bound to a client.
            HueResponseError: If the bridge rejects the recall.

        """
        return await self.update({"recall": {"action": "active"}})
