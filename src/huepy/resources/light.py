"""Handlers for light and light-level resources."""

from abc import ABC, abstractmethod
from typing import Any, ClassVar

from huepy.models import common as common_models
from huepy.models import light as light_models
from huepy.models.common import ResourceIdentifier, ResourceType
from huepy.models.state import build_light_payload
from huepy.resources.base import BaseResource, NamedResourceHandler


class DimmableMixin(ABC):
    """Shared on/off, brightness, colour and colour-temperature commands.

    Behaviour only: this mixin has no state and no constructor, so it is always
    combined with the handler base that has one -- ``BaseResource`` for an
    unnamed resource, ``NamedResourceHandler`` for a named one. Staying free of
    a base is what lets :class:`Light` be both dimmable and name-addressable
    without inheriting the same handler down two paths.
    """

    @abstractmethod
    async def update(
        self,
        resource_id: str,
        data: dict[str, Any],
    ) -> list[ResourceIdentifier]:
        """Apply a partial update; supplied by the handler base.

        Args:
            resource_id: The resource's id.
            data: The fields to change, in the bridge's payload shape.

        Returns:
            References to the resources the bridge reports as updated.

        """

    async def turn_on(self, resource_id: str) -> list[ResourceIdentifier]:
        """Switch the resource on.

        Args:
            resource_id: The light or group id.

        Returns:
            References to the updated resources.

        """
        return await self.update(resource_id, build_light_payload(on=True))

    async def turn_off(self, resource_id: str) -> list[ResourceIdentifier]:
        """Switch the resource off.

        Args:
            resource_id: The light or group id.

        Returns:
            References to the updated resources.

        """
        return await self.update(resource_id, build_light_payload(on=False))

    async def set_brightness(
        self,
        resource_id: str,
        brightness: float,
    ) -> list[ResourceIdentifier]:
        """Set brightness, clamped to 0-100.

        Args:
            resource_id: The light or group id.
            brightness: Target brightness percentage.

        Returns:
            References to the updated resources.

        """
        return await self.update(
            resource_id,
            build_light_payload(brightness=brightness),
        )

    async def set_color(
        self,
        resource_id: str,
        x: float,
        y: float,
    ) -> list[ResourceIdentifier]:
        """Set colour from CIE xy coordinates.

        Args:
            resource_id: The light or group id.
            x: CIE x coordinate.
            y: CIE y coordinate.

        Returns:
            References to the updated resources.

        """
        return await self.update(resource_id, build_light_payload(xy=(x, y)))

    async def set_color_temperature(
        self,
        resource_id: str,
        mirek: int,
    ) -> list[ResourceIdentifier]:
        """Set colour temperature in mirek.

        Args:
            resource_id: The light or group id.
            mirek: Colour temperature; lower is cooler.

        Returns:
            References to the updated resources.

        """
        return await self.update(
            resource_id,
            build_light_payload(mirek=mirek),
        )


class Light(NamedResourceHandler[light_models.Light], DimmableMixin):
    """Handler for individual lights.

    Lights carry a `metadata.name`, so they can be looked up by it:
    ``await hue.lights["Desk lamp"]``.
    """

    resource_type: ClassVar[ResourceType] = ResourceType.LIGHT
    model: ClassVar[type[common_models.HueModel]] = light_models.Light

    async def get_lights_on(self) -> list[light_models.Light]:
        """Fetch every light that is currently switched on.

        Returns:
            The lights whose power state is on.

        """
        return [light for light in await self.get_all() if light.is_on]

    async def get_service_ids_on(self) -> list[str]:
        """Ids of the light *services* that are currently on.

        Returns:
            One id per light that is on.

        """
        return [light.id for light in await self.get_lights_on()]

    async def get_device_ids_on(self) -> list[str]:
        """Ids of the *devices* owning the lights that are currently on.

        Returns:
            One device id per light that is on and reports an owner.

        """
        return [
            light.owner.rid
            for light in await self.get_lights_on()
            if light.owner is not None
        ]


class GroupedLight(BaseResource[light_models.GroupedLight], DimmableMixin):
    """Handler for the aggregate light service of a room or zone."""

    resource_type: ClassVar[ResourceType] = ResourceType.GROUPED_LIGHT
    model: ClassVar[type[common_models.HueModel]] = light_models.GroupedLight


class LightLevel(BaseResource[light_models.LightLevel]):
    """Handler for ambient light-level sensors."""

    resource_type: ClassVar[ResourceType] = ResourceType.LIGHT_LEVEL
    model: ClassVar[type[common_models.HueModel]] = light_models.LightLevel


class GroupedLightLevel(BaseResource[light_models.GroupedLightLevel]):
    """Handler for aggregated light-level services."""

    resource_type: ClassVar[ResourceType] = ResourceType.GROUPED_LIGHT_LEVEL
    model: ClassVar[type[common_models.HueModel]] = light_models.GroupedLightLevel
