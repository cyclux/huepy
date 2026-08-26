"""Handlers for automation and presence resources."""

from typing import Any, ClassVar

from huepy.models import automation as automation_models
from huepy.models import common as common_models
from huepy.models.common import ResourceIdentifier, ResourceType
from huepy.resources.base import BaseResource, NamedResourceHandler

LATITUDE_MAX = 90.0
LONGITUDE_MAX = 180.0


class BehaviorScript(NamedResourceHandler[automation_models.BehaviorScript]):
    """Handler for behaviour scripts (read-only templates)."""

    resource_type: ClassVar[ResourceType] = ResourceType.BEHAVIOR_SCRIPT
    model: ClassVar[type[common_models.HueModel]] = automation_models.BehaviorScript


class BehaviorInstance(NamedResourceHandler[automation_models.BehaviorInstance]):
    """Handler for behaviour instances -- configured, running automations."""

    resource_type: ClassVar[ResourceType] = ResourceType.BEHAVIOR_INSTANCE
    model: ClassVar[type[common_models.HueModel]] = automation_models.BehaviorInstance

    async def create(
        self,
        script_id: str,
        configuration: dict[str, Any],
        *,
        enabled: bool = True,
        name: str | None = None,
    ) -> list[ResourceIdentifier]:
        """Create and start an automation from a behaviour script.

        Args:
            script_id: The behaviour script to instantiate.
            configuration: The instance configuration, validated by the
                script's ``configuration_schema``.
            enabled: Whether the automation starts enabled.
            name: An optional display name for the instance.

        Returns:
            A reference to the created instance.

        """
        body: dict[str, Any] = {
            "type": str(ResourceType.BEHAVIOR_INSTANCE),
            "script_id": script_id,
            "enabled": enabled,
            "configuration": configuration,
        }
        if name is not None:
            body["metadata"] = {"name": name}
        return await self._create(body)

    async def enable(self, resource_id: str) -> list[ResourceIdentifier]:
        """Enable an automation.

        Args:
            resource_id: The instance id.

        Returns:
            References to the updated resources.

        """
        return await self.update(resource_id, {"enabled": True})

    async def disable(self, resource_id: str) -> list[ResourceIdentifier]:
        """Disable an automation without deleting it.

        Args:
            resource_id: The instance id.

        Returns:
            References to the updated resources.

        """
        return await self.update(resource_id, {"enabled": False})

    async def configure(
        self,
        resource_id: str,
        configuration: dict[str, Any],
    ) -> list[ResourceIdentifier]:
        """Replace an automation's configuration.

        Args:
            resource_id: The instance id.
            configuration: The new configuration, in the script's shape.

        Returns:
            References to the updated resources.

        """
        return await self.update(resource_id, {"configuration": configuration})


class Geolocation(BaseResource[automation_models.Geolocation]):
    """Handler for the bridge's geolocation service."""

    resource_type: ClassVar[ResourceType] = ResourceType.GEOLOCATION
    model: ClassVar[type[common_models.HueModel]] = automation_models.Geolocation

    async def set_location(
        self,
        resource_id: str,
        latitude: float,
        longitude: float,
    ) -> list[ResourceIdentifier]:
        """Set the bridge's location, enabling sun-based automations.

        Args:
            resource_id: The geolocation service id.
            latitude: Degrees north, -90 to 90.
            longitude: Degrees east, -180 to 180.

        Returns:
            References to the updated resources.

        Raises:
            ValueError: If a coordinate is out of range.

        """
        if not -LATITUDE_MAX <= latitude <= LATITUDE_MAX:
            msg = f"latitude must be between -90 and 90, got {latitude}"
            raise ValueError(msg)
        if not -LONGITUDE_MAX <= longitude <= LONGITUDE_MAX:
            msg = f"longitude must be between -180 and 180, got {longitude}"
            raise ValueError(msg)
        return await self.update(
            resource_id,
            {"latitude": latitude, "longitude": longitude},
        )


class GeofenceClient(BaseResource[automation_models.GeofenceClient]):
    """Handler for geofence clients -- the phones driving presence."""

    resource_type: ClassVar[ResourceType] = ResourceType.GEOFENCE_CLIENT
    model: ClassVar[type[common_models.HueModel]] = automation_models.GeofenceClient

    async def create(
        self,
        name: str,
        *,
        is_at_home: bool = False,
    ) -> list[ResourceIdentifier]:
        """Register a geofence client.

        Args:
            name: The client's display name.
            is_at_home: Whether it starts marked as at home.

        Returns:
            A reference to the created client.

        """
        return await self._create({"name": name, "is_at_home": is_at_home})
