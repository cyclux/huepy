"""Models for automation and presence resources.

Behaviour scripts and their instances are the v2 successor to the v1 rule
engine: a script is a template the bridge ships, and an instance is one
configured, running copy of it. Geolocation and geofence clients give those
automations a sense of place and presence.
"""

from typing import Any

from pydantic import Field

from huepy.models.common import CommandResult, HueModel, HueResource, NamedResource

LATITUDE_MAX = 90.0
LONGITUDE_MAX = 180.0


class BehaviorScript(NamedResource):
    """A behaviour template the bridge offers, ready to be instantiated.

    Read-only: the v2 API ships these and does not let applications upload their
    own. ``configuration_schema`` describes what an instance must supply.
    """

    description: str = ""
    configuration_schema: dict[str, Any] = Field(default_factory=dict)
    trigger_schema: dict[str, Any] = Field(default_factory=dict)
    state_schema: dict[str, Any] = Field(default_factory=dict)
    version: str | None = None
    supported_features: list[str] = Field(default_factory=list)
    max_number_instances: int | None = None


class BehaviorInstance(NamedResource):
    """One configured, running behaviour: an automation.

    ``configuration`` is validated by its script's ``configuration_schema``, so
    its shape varies by script and is carried as arbitrary JSON.
    """

    script_id: str | None = None
    enabled: bool = False
    state: dict[str, Any] | None = None
    configuration: dict[str, Any] = Field(default_factory=dict)
    dependees: list[dict[str, Any]] = Field(default_factory=list)
    status: str | None = None
    last_error: str | None = None

    async def enable(self) -> CommandResult:
        """Enable this automation.

        Returns:
            A CommandResult containing the bridge references affected.

        Raises:
            DetachedResourceError: If this instance is not bound to a client.
            HueResponseError: If the bridge rejects the change.

        """
        return await self.update({"enabled": True})

    async def disable(self) -> CommandResult:
        """Disable this automation without deleting it.

        Returns:
            A CommandResult containing the bridge references affected.

        Raises:
            DetachedResourceError: If this instance is not bound to a client.
            HueResponseError: If the bridge rejects the change.

        """
        return await self.update({"enabled": False})

    async def configure(self, configuration: dict[str, Any]) -> CommandResult:
        """Replace this automation's configuration.

        Args:
            configuration: The new configuration, validated by the instance's
                script ``configuration_schema``.

        Returns:
            A CommandResult containing the bridge references affected.

        Raises:
            DetachedResourceError: If this instance is not bound to a client.
            HueResponseError: If the bridge rejects the configuration.

        """
        return await self.update({"configuration": configuration})


class SunToday(HueModel):
    """The sunrise and sunset the bridge computed for today."""

    sunset_time: str | None = None
    day_type: str | None = None


class Geolocation(HueResource):
    """Where the bridge believes it is, used by sun-based automations."""

    is_configured: bool = False
    sun_today: SunToday | None = None

    async def set_location(self, latitude: float, longitude: float) -> CommandResult:
        """Set the bridge's location, enabling sun-based automations.

        Args:
            latitude: Degrees north, -90 to 90.
            longitude: Degrees east, -180 to 180.

        Returns:
            A CommandResult containing the bridge references affected.

        Raises:
            DetachedResourceError: If this service is not bound to a client.
            ValueError: If a coordinate is out of range.
            HueResponseError: If the bridge rejects the change.

        """
        if not -LATITUDE_MAX <= latitude <= LATITUDE_MAX:
            msg = f"latitude must be between -90 and 90, got {latitude}"
            raise ValueError(msg)
        if not -LONGITUDE_MAX <= longitude <= LONGITUDE_MAX:
            msg = f"longitude must be between -180 and 180, got {longitude}"
            raise ValueError(msg)
        return await self.update({"latitude": latitude, "longitude": longitude})


class GeofenceClient(HueResource):
    """A phone or app whose presence drives home/away automations.

    Its display name is a top-level ``name`` here, not ``metadata.name`` as on
    most named resources, so it is not a :class:`NamedResource`.
    """

    name: str | None = None
    is_at_home: bool | None = None
