"""Handlers for grouping resources: rooms, zones, homes, scenes, service groups."""

from typing import Any, ClassVar

from huepy.models import common as common_models
from huepy.models import group as group_models
from huepy.models.common import ResourceIdentifier, ResourceType
from huepy.models.group import RecallAction
from huepy.models.state import MILLISECONDS_PER_SECOND, build_scene_recall
from huepy.resources.base import BaseResource, NamedResourceHandler
from huepy.resources.light import Light


class GroupedLightResolver[ModelT: group_models.ResourceGroup](
    NamedResourceHandler[ModelT]
):
    """Resolve the `grouped_light` service owned by a room or zone.

    Rooms and zones do not accept light commands directly: each owns a
    `grouped_light` service that does. The low-level API exposes that service
    id explicitly; high-level bound groups route commands without another GET.

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


class Room(GroupedLightResolver[group_models.Room]):
    """Handler for rooms.

    Rooms carry a `metadata.name`, so they can be looked up by it:
    ``await hue.rooms.get("Kitchen")``.
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

        rooms = await self.list()
        return next(
            (room.id for room in rooms if room.contains_device(light.owner.rid)),
            None,
        )


class Zone(GroupedLightResolver[group_models.Zone]):
    """Handler for zones.

    Zones carry a `metadata.name`, so they can be looked up by it:
    ``await hue.zones.get("Downstairs")``.
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
    ``await hue.service_groups.get("Hallway sensors")``.
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
    High-level lookup is available as ``await hue.scenes.get("Movie night")``.
    Duplicate names raise rather than selecting a scene from the wrong room.
    """

    resource_type: ClassVar[ResourceType] = ResourceType.SCENE
    model: ClassVar[type[common_models.HueModel]] = group_models.Scene

    async def create(
        self,
        name: str,
        room_id: str,
        *,
        actions: list[dict[str, Any]] | None = None,
        speed: float | None = None,
        auto_dynamic: bool | None = None,
    ) -> list[ResourceIdentifier]:
        """Create a scene attached to a room.

        Args:
            name: The scene's display name.
            room_id: The room the scene belongs to.
            actions: The per-target scene actions, in the bridge's shape. A
                scene with no actions stores nothing to recall.
            speed: Speed of the dynamic palette, from 0.0 to 1.0.
            auto_dynamic: Whether recalling the scene starts it dynamically.

        Returns:
            A reference to the created scene.

        """
        body: dict[str, Any] = {
            "metadata": {"name": name},
            "group": {"rid": room_id, "rtype": ResourceType.ROOM},
        }
        if actions is not None:
            body["actions"] = actions
        if speed is not None:
            body["speed"] = speed
        if auto_dynamic is not None:
            body["auto_dynamic"] = auto_dynamic
        return await self._create(body)

    async def activate(
        self,
        scene_id: str,
        *,
        action: RecallAction | str = RecallAction.ACTIVE,
        duration: float | None = None,
        brightness: float | None = None,
    ) -> list[ResourceIdentifier]:
        """Recall a scene.

        Args:
            scene_id: The scene's id.
            action: How to recall it: ``RecallAction.ACTIVE`` applies it once,
                ``DYNAMIC_PALETTE`` starts it cycling its palette.
            duration: Transition time into the scene, in seconds.
            brightness: A brightness percentage to override the scene's own.

        Returns:
            References to the updated resources.

        """
        return await self.update(
            scene_id,
            build_scene_recall(str(action), duration=duration, brightness=brightness),
        )


class SmartScene(NamedResourceHandler[group_models.SmartScene]):
    """Handler for smart scenes -- scenes that follow a weekly schedule.

    Smart scenes carry a `metadata.name`, so they can be looked up by it:
    ``await hue.smart_scenes.get("Daily rhythm")``.
    """

    resource_type: ClassVar[ResourceType] = ResourceType.SMART_SCENE
    model: ClassVar[type[common_models.HueModel]] = group_models.SmartScene

    async def create(
        self,
        name: str,
        group_id: str,
        week_timeslots: list[dict[str, Any]],
        *,
        group_rtype: ResourceType | str = ResourceType.ROOM,
        transition_duration: float | None = None,
    ) -> list[ResourceIdentifier]:
        """Create a smart scene on a room or zone.

        Args:
            name: The smart scene's display name.
            group_id: The room or zone it belongs to.
            week_timeslots: The weekly schedule, in the bridge's shape: each
                entry a day's ``timeslots`` (each a ``start_time`` and a scene
                ``target``) and its ``recurrence`` weekdays.
            group_rtype: The kind of group ``group_id`` names, room or zone.
            transition_duration: Fade between timeslots, in seconds.

        Returns:
            A reference to the created smart scene.

        """
        body: dict[str, Any] = {
            "metadata": {"name": name},
            "group": {"rid": group_id, "rtype": str(group_rtype)},
            "week_timeslots": week_timeslots,
        }
        if transition_duration is not None:
            body["transition_duration"] = int(
                transition_duration * MILLISECONDS_PER_SECOND
            )
        return await self._create(body)

    async def activate(self, scene_id: str) -> list[ResourceIdentifier]:
        """Start a smart scene running its daily schedule.

        Args:
            scene_id: The smart scene's id.

        Returns:
            References to the updated resources.

        """
        return await self.update(scene_id, {"recall": {"action": "activate"}})

    async def deactivate(self, scene_id: str) -> list[ResourceIdentifier]:
        """Stop a running smart scene.

        Args:
            scene_id: The smart scene's id.

        Returns:
            References to the updated resources.

        """
        return await self.update(scene_id, {"recall": {"action": "deactivate"}})
