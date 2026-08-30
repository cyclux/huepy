"""Handlers for smart-home integrations and Hue Secure sensors."""

from typing import ClassVar

from huepy.models import common as common_models
from huepy.models import security as security_models
from huepy.models.common import ResourceIdentifier, ResourceType
from huepy.resources.base import BaseResource
from huepy.resources.sensor import ToggleableSensorMixin


class Homekit(BaseResource[security_models.Homekit]):
    """Handler for the bridge's HomeKit service."""

    resource_type: ClassVar[ResourceType] = ResourceType.HOMEKIT
    model: ClassVar[type[common_models.HueModel]] = security_models.Homekit

    async def reset(self, resource_id: str) -> list[ResourceIdentifier]:
        """Reset the bridge's HomeKit pairing.

        Args:
            resource_id: The HomeKit service id.

        Returns:
            References to the updated resources.

        """
        return await self.update(resource_id, {"action": "homekit_reset"})


class Matter(BaseResource[security_models.Matter]):
    """Handler for the bridge's Matter service."""

    resource_type: ClassVar[ResourceType] = ResourceType.MATTER
    model: ClassVar[type[common_models.HueModel]] = security_models.Matter

    async def reset(self, resource_id: str) -> list[ResourceIdentifier]:
        """Reset the bridge's Matter commissioning.

        Args:
            resource_id: The Matter service id.

        Returns:
            References to the updated resources.

        """
        return await self.update(resource_id, {"action": "matter_reset"})


class MatterFabric(BaseResource[security_models.MatterFabric]):
    """Handler for commissioned Matter fabrics.

    A fabric can be listed and deleted -- to decommission it -- but not edited.
    """

    resource_type: ClassVar[ResourceType] = ResourceType.MATTER_FABRIC
    model: ClassVar[type[common_models.HueModel]] = security_models.MatterFabric


class Tamper(BaseResource[security_models.Tamper]):
    """Handler for a sensor's tamper-detection service (read-only)."""

    resource_type: ClassVar[ResourceType] = ResourceType.TAMPER
    model: ClassVar[type[common_models.HueModel]] = security_models.Tamper


class CameraMotion(ToggleableSensorMixin[security_models.CameraMotion]):
    """Handler for a camera's motion-detection service.

    Enable or disable it with :meth:`enable` and :meth:`disable`, like a motion
    sensor.
    """

    resource_type: ClassVar[ResourceType] = ResourceType.CAMERA_MOTION
    model: ClassVar[type[common_models.HueModel]] = security_models.CameraMotion
