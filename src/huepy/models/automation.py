"""Models for automation and presence resources.

Behaviour scripts and their instances are the v2 successor to the v1 rule
engine: a script is a template the bridge ships, and an instance is one
configured, running copy of it. Geolocation and geofence clients give those
automations a sense of place and presence.
"""

from typing import Any

from pydantic import Field

from huepy.models.common import HueModel, HueResource, NamedResource


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


class SunToday(HueModel):
    """The sunrise and sunset the bridge computed for today."""

    sunset_time: str | None = None
    day_type: str | None = None


class Geolocation(HueResource):
    """Where the bridge believes it is, used by sun-based automations."""

    is_configured: bool = False
    sun_today: SunToday | None = None


class GeofenceClient(HueResource):
    """A phone or app whose presence drives home/away automations.

    Its display name is a top-level ``name`` here, not ``metadata.name`` as on
    most named resources, so it is not a :class:`NamedResource`.
    """

    name: str | None = None
    is_at_home: bool | None = None
