"""Handlers for sensor services: motion, temperature, button, contact, power."""

from typing import ClassVar

from huepy.models import common as common_models
from huepy.models import device as device_models
from huepy.models import sensor as sensor_models
from huepy.models.common import ResourceIdentifier, ResourceType
from huepy.resources.base import BaseResource


class ToggleableSensorMixin[ModelT: common_models.HueModel](BaseResource[ModelT]):
    """Sensors that can be enabled and disabled on the bridge."""

    async def turn_on(self, resource_id: str) -> list[ResourceIdentifier]:
        """Enable the sensor.

        Args:
            resource_id: The sensor service id.

        Returns:
            References to the updated resources.

        """
        return await self.update(resource_id, {"enabled": True})

    async def turn_off(self, resource_id: str) -> list[ResourceIdentifier]:
        """Disable the sensor.

        Args:
            resource_id: The sensor service id.

        Returns:
            References to the updated resources.

        """
        return await self.update(resource_id, {"enabled": False})


class Motion(ToggleableSensorMixin[sensor_models.Motion]):
    """Handler for motion sensors."""

    resource_type: ClassVar[ResourceType] = ResourceType.MOTION
    model: ClassVar[type[common_models.HueModel]] = sensor_models.Motion

    async def set_sensitivity(
        self,
        resource_id: str,
        sensitivity: int,
    ) -> list[ResourceIdentifier]:
        """Set the sensor's motion sensitivity.

        Args:
            resource_id: The sensor service id.
            sensitivity: The new sensitivity, from 0 to the sensor's maximum.

        Returns:
            References to the updated resources.

        Raises:
            TypeError: If ``sensitivity`` is not an integer.
            ValueError: If ``sensitivity`` is negative or above the maximum.

        """
        # Runtime guard for untyped callers; the annotation covers checked ones.
        if not isinstance(sensitivity, int):  # pyright: ignore[reportUnnecessaryIsInstance]
            msg = "Sensitivity must be an integer"
            raise TypeError(msg)  # pyright: ignore[reportUnreachable]
        if sensitivity < 0:
            msg = "Sensitivity cannot be negative"
            raise ValueError(msg)

        sensor = await self.get(resource_id)
        maximum = sensor.sensitivity.sensitivity_max
        if sensitivity > maximum:
            msg = f"Sensitivity {sensitivity} exceeds maximum allowed {maximum}"
            raise ValueError(msg)

        return await self.update(
            resource_id,
            {"sensitivity": {"sensitivity": sensitivity}},
        )

    async def get_motion_state(self, resource_id: str) -> bool:
        """Whether the sensor currently detects motion.

        Args:
            resource_id: The sensor service id.

        Returns:
            True if motion is currently detected.

        """
        return (await self.get(resource_id)).motion_detected

    async def get_last_motion(self, resource_id: str) -> str:
        """Timestamp of the sensor's last motion transition.

        Args:
            resource_id: The sensor service id.

        Returns:
            An ISO 8601 timestamp, or an empty string if none is reported.

        """
        return (await self.get(resource_id)).last_motion


class GroupedMotion(ToggleableSensorMixin[sensor_models.GroupedMotion]):
    """Handler for aggregated motion services."""

    resource_type: ClassVar[ResourceType] = ResourceType.GROUPED_MOTION
    model: ClassVar[type[common_models.HueModel]] = sensor_models.GroupedMotion


class Temperature(ToggleableSensorMixin[sensor_models.Temperature]):
    """Handler for temperature sensors."""

    resource_type: ClassVar[ResourceType] = ResourceType.TEMPERATURE
    model: ClassVar[type[common_models.HueModel]] = sensor_models.Temperature


class Contact(ToggleableSensorMixin[sensor_models.Contact]):
    """Handler for contact sensors."""

    resource_type: ClassVar[ResourceType] = ResourceType.CONTACT
    model: ClassVar[type[common_models.HueModel]] = sensor_models.Contact


class Button(BaseResource[sensor_models.Button]):
    """Handler for switch and dimmer buttons."""

    resource_type: ClassVar[ResourceType] = ResourceType.BUTTON
    model: ClassVar[type[common_models.HueModel]] = sensor_models.Button


class DevicePower(BaseResource[device_models.DevicePower]):
    """Handler for the battery service of battery-powered devices."""

    resource_type: ClassVar[ResourceType] = ResourceType.DEVICE_POWER
    model: ClassVar[type[common_models.HueModel]] = device_models.DevicePower
