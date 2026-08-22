"""Handlers for grouping resources: rooms, zones, homes, scenes, service groups."""

from typing import ClassVar

from huepy.models import common as common_models
from huepy.models import group as group_models
from huepy.models.common import ResourceIdentifier, ResourceType
from huepy.resources.base import BaseResource, NamedResourceHandler
from huepy.resources.light import GroupedLight, Light


class GroupedLightControlMixin[ModelT: group_models.ResourceGroup](
    NamedResourceHandler[ModelT]
):
    """Control commands that act through a group's `grouped_light` service.

    Rooms and zones do not accept light commands directly: each owns a
    `grouped_light` service that does. These helpers resolve that service once
    and forward the command to it.

    Every `ResourceGroup` carries a `metadata.name`, so this builds on
    :class:`~huepy.resources.base.NamedResourceHandler`: a group is always
    reachable by the name a human gave it.
    """

    async def grouped_light_id(self, resource_id: str) -> str:
        """Resolve the group's `grouped_light` service id.

        Args:
            resource_id: The room or zone id.

        Returns:
            The id of the group's grouped_light service.

        Raises:
            ValueError: If the group exposes no grouped_light service.

        """
        group = await self.get(resource_id)
        service_id = group.service_id(ResourceType.GROUPED_LIGHT)
        if service_id is None:
            msg = (
                f"No grouped_light service found for {self.resource_type} {resource_id}"
            )
            raise ValueError(msg)
        return service_id

    async def _grouped_light(self, resource_id: str) -> tuple[GroupedLight, str]:
        """Return the grouped-light handler and service id for this group."""
        return GroupedLight(self.hue), await self.grouped_light_id(resource_id)

    async def turn_on(self, resource_id: str) -> list[ResourceIdentifier]:
        """Switch on every light in the group.

        Args:
            resource_id: The room or zone id.

        Returns:
            References to the updated resources.

        """
        handler, service_id = await self._grouped_light(resource_id)
        return await handler.turn_on(service_id)

    async def turn_off(self, resource_id: str) -> list[ResourceIdentifier]:
        """Switch off every light in the group.

        Args:
            resource_id: The room or zone id.

        Returns:
            References to the updated resources.

        """
        handler, service_id = await self._grouped_light(resource_id)
        return await handler.turn_off(service_id)

    async def set_brightness(
        self,
        resource_id: str,
        brightness: float,
    ) -> list[ResourceIdentifier]:
        """Set brightness for every light in the group.

        Args:
            resource_id: The room or zone id.
            brightness: Target brightness percentage, clamped to 0-100.

        Returns:
            References to the updated resources.

        """
        handler, service_id = await self._grouped_light(resource_id)
        return await handler.set_brightness(service_id, brightness)

    async def set_color(
        self,
        resource_id: str,
        x: float,
        y: float,
    ) -> list[ResourceIdentifier]:
        """Set colour for every light in the group.

        Args:
            resource_id: The room or zone id.
            x: CIE x coordinate.
            y: CIE y coordinate.

        Returns:
            References to the updated resources.

        """
        handler, service_id = await self._grouped_light(resource_id)
        return await handler.set_color(service_id, x, y)

    async def set_color_temperature(
        self,
        resource_id: str,
        mirek: int,
    ) -> list[ResourceIdentifier]:
        """Set colour temperature for every light in the group.

        Args:
            resource_id: The room or zone id.
            mirek: Colour temperature; lower is cooler.

        Returns:
            References to the updated resources.

        """
        handler, service_id = await self._grouped_light(resource_id)
        return await handler.set_color_temperature(service_id, mirek)


class Room(GroupedLightControlMixin[group_models.Room]):
    """Handler for rooms.

    Rooms carry a `metadata.name`, so they can be looked up by it:
    ``await hue.rooms["Kitchen"]``.
    """

    resource_type: ClassVar[ResourceType] = ResourceType.ROOM
    model: ClassVar[type[common_models.HueModel]] = group_models.Room

    async def create(self, name: str, devices: list[str]) -> list[ResourceIdentifier]:
        """Create a room containing the given devices.

        Args:
            name: The room's display name.
            devices: Ids of the devices to place in the room.

        Returns:
            A reference to the created room.

        """
        return await self._create(
            {
                "metadata": {"name": name},
                "children": [
                    {"rid": device_id, "rtype": ResourceType.DEVICE}
                    for device_id in devices
                ],
            },
        )

    async def get_from_light_service_id(self, light_id: str) -> str | None:
        """Find the room a light belongs to.

        Args:
            light_id: The id of the light service.

        Returns:
            The room's id, or None if the light is not assigned to a room.

        """
        light = await Light(self.hue).get(light_id)
        if light.owner is None:
            return None

        rooms = await self.get_all()
        return next(
            (room.id for room in rooms if room.contains_device(light.owner.rid)),
            None,
        )


class Zone(GroupedLightControlMixin[group_models.Zone]):
    """Handler for zones.

    Zones carry a `metadata.name`, so they can be looked up by it:
    ``await hue.zones["Downstairs"]``.
    """

    resource_type: ClassVar[ResourceType] = ResourceType.ZONE
    model: ClassVar[type[common_models.HueModel]] = group_models.Zone

    async def create(
        self,
        name: str,
        services: list[dict[str, str]],
    ) -> list[ResourceIdentifier]:
        """Create a zone from the given light services.

        Args:
            name: The zone's display name.
            services: Service references, each with ``rid`` and ``rtype``.

        Returns:
            A reference to the created zone.

        """
        return await self._create({"metadata": {"name": name}, "children": services})


class BridgeHome(BaseResource[group_models.BridgeHome]):
    """Handler for the top-level home resource."""

    resource_type: ClassVar[ResourceType] = ResourceType.BRIDGE_HOME
    model: ClassVar[type[common_models.HueModel]] = group_models.BridgeHome


class ServiceGroup(NamedResourceHandler[group_models.ServiceGroup]):
    """Handler for named groups of arbitrary services.

    Service groups carry a `metadata.name`, so they can be looked up by it:
    ``await hue.service_group["Hallway sensors"]``.
    """

    resource_type: ClassVar[ResourceType] = ResourceType.SERVICE_GROUP
    model: ClassVar[type[common_models.HueModel]] = group_models.ServiceGroup

    async def create(
        self,
        name: str,
        services: list[dict[str, str]],
        archetype: str = "sensor_group",
    ) -> list[ResourceIdentifier]:
        """Create a service group.

        Args:
            name: The group's display name.
            services: Service references, each with ``rid`` and ``rtype``.
            archetype: The kind of group to create.

        Returns:
            A reference to the created group.

        """
        return await self._create(
            {
                "metadata": {"name": name, "archetype": archetype},
                "children": services,
            },
        )


class Scene(NamedResourceHandler[group_models.Scene]):
    """Handler for scenes.

    Scenes carry a `metadata.name`, so they can be looked up by it:
    ``await hue.scenes["Movie night"]``. Scene names repeat across rooms far
    more often than room names do, and the first match in bridge order wins.
    """

    resource_type: ClassVar[ResourceType] = ResourceType.SCENE
    model: ClassVar[type[common_models.HueModel]] = group_models.Scene

    async def create(self, name: str, room_id: str) -> list[ResourceIdentifier]:
        """Create a scene attached to a room.

        Args:
            name: The scene's display name.
            room_id: The room the scene belongs to.

        Returns:
            A reference to the created scene.

        """
        return await self._create(
            {
                "metadata": {"name": name},
                "group": {"rid": room_id, "rtype": ResourceType.ROOM},
            },
        )

    async def activate(self, scene_id: str) -> list[ResourceIdentifier]:
        """Recall a scene.

        Args:
            scene_id: The scene's id.

        Returns:
            References to the updated resources.

        """
        return await self.update(scene_id, {"recall": {"action": "active"}})
