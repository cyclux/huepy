"""Handlers for the entertainment configuration resources."""

from typing import Any, ClassVar

from huepy.models import common as common_models
from huepy.models import entertainment as entertainment_models
from huepy.models.common import ResourceIdentifier, ResourceType
from huepy.resources.base import BaseResource, NamedResourceHandler


class Entertainment(BaseResource[entertainment_models.Entertainment]):
    """Handler for the per-light entertainment service (read-only)."""

    resource_type: ClassVar[ResourceType] = ResourceType.ENTERTAINMENT
    model: ClassVar[type[common_models.HueModel]] = entertainment_models.Entertainment


class EntertainmentConfiguration(
    NamedResourceHandler[entertainment_models.EntertainmentConfiguration]
):
    """Handler for entertainment areas.

    Entertainment configurations carry a `metadata.name`, so they can be looked
    up by it: ``await hue.entertainment_configurations.get("TV")``.
    """

    resource_type: ClassVar[ResourceType] = ResourceType.ENTERTAINMENT_CONFIGURATION
    model: ClassVar[type[common_models.HueModel]] = (
        entertainment_models.EntertainmentConfiguration
    )

    async def start(self, resource_id: str) -> list[ResourceIdentifier]:
        """Start streaming to an entertainment area.

        Args:
            resource_id: The entertainment configuration id.

        Returns:
            References to the updated resources.

        """
        return await self.update(resource_id, {"action": "start"})

    async def stop(self, resource_id: str) -> list[ResourceIdentifier]:
        """Stop streaming to an entertainment area.

        Args:
            resource_id: The entertainment configuration id.

        Returns:
            References to the updated resources.

        """
        return await self.update(resource_id, {"action": "stop"})

    async def create(self, config: dict[str, Any]) -> list[ResourceIdentifier]:
        """Create an entertainment configuration from a raw body.

        Args:
            config: The configuration body, in the bridge's shape.

        Returns:
            A reference to the created configuration.

        """
        return await self._create(config)
