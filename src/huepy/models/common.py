"""Shared building blocks for the Hue v2 CLIP resource models.

Every model here is tolerant of unknown fields: bridge firmware updates add
keys over time, and a strict model would turn a harmless addition into a
parse failure for every caller.

This module also holds the `{errors, data}` envelope every v2 CLIP response is
wrapped in. The bridge reports failures *inside* successful response bodies: a
request can come back with a populated ``errors`` array. Routing every response
through :func:`unwrap` is what stops such a failure being reported to the
caller as success.

A resource parsed by a handler is *bound*: it carries the client that fetched
it, so it can act on itself without the caller keeping a ``(handler, id)``
pair around. A model built by hand is detached and raises
:class:`~huepy.exceptions.DetachedResourceError` on any command.

Typical usage example:

    light = await hue.api.lights.get(light_id)
    if light.dimming is not None:
        print(light.dimming.brightness)
"""

import logging
from enum import StrEnum
from typing import TYPE_CHECKING, Any, ClassVar, Self

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

from huepy.exceptions import (
    ADVISORY_ERROR_CODES,
    DetachedResourceError,
    HueResponseError,
)

if TYPE_CHECKING:
    from huepy.client.protocol import HueClient

RESOURCE_ROOT = "/clip/v2/resource"
"""Path prefix every addressable v2 resource lives under."""


logger = logging.getLogger(__name__)


class HueModel(BaseModel):
    """Base for every Hue payload model.

    Unknown fields are preserved rather than rejected, so a firmware update
    that adds a key cannot break parsing.
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(
        extra="allow", populate_by_name=True
    )


class HueErrorDetail(HueModel):
    """A single error entry returned in a response body.

    Attributes:
        description: The human-readable message.
        error_code: The bridge's machine-readable code, e.g.
            ``communication_error``. Empty when the bridge sends none.

    """

    description: str = ""
    error_code: str = ""


class HueResponse[ModelT: HueModel](HueModel):
    """A parsed v2 response: zero or more errors, zero or more resources."""

    errors: list[HueErrorDetail] = Field(default_factory=list)
    data: list[ModelT] = Field(default_factory=list)

    def raise_for_errors(self) -> None:
        """Raise unless every error was advisory and something still changed.

        The bridge uses ``errors[]`` for two different things, and only
        ``error_code`` tells them apart:

        * anything in :data:`~huepy.exceptions.ADVISORY_ERROR_CODES` -- the
          command was accepted, but a caveat applies: a flaky radio, or an
          attribute that will not land because the light is off. Raising here
          would discard a command the bridge took, and one unreachable bulb
          would break every call that touches it. Logged, not raised.
        * anything else -- the request itself was wrong, e.g. setting colour
          temperature on a light that has none. The bridge still lists the
          resource in ``data``, so these must raise on their own account or
          they vanish silently. That silent-success bug is why this method
          exists.

        Classification is per error, so a blocking code alongside an advisory
        one still raises.

        An advisory error that changed nothing at all still raises: nothing
        was accepted, so there is no success to preserve.

        Outright rejections never reach here -- the bridge answers those with
        4xx, which the transport turns into
        :class:`~huepy.exceptions.HueAPIError`.

        Raises:
            HueResponseError: If any error was not advisory, or if nothing
                was changed.

        """
        if not self.errors:
            return
        blocking = [
            error
            for error in self.errors
            if error.error_code not in ADVISORY_ERROR_CODES
        ]
        for error in self.errors:
            if error not in blocking:
                logger.warning(
                    "Bridge accepted the command with a caveat (%s): %s",
                    error.error_code,
                    error.description,
                )
        if blocking:
            raise HueResponseError([error.description for error in blocking])
        if not self.data:
            raise HueResponseError([error.description for error in self.errors])


def unwrap[ModelT: HueModel](
    payload: object,
    model: type[ModelT],
) -> list[ModelT]:
    """Validate a raw response body into a list of models.

    Args:
        payload: The decoded JSON body returned by the bridge.
        model: The model class each ``data`` entry should be parsed into.

    Returns:
        The parsed resources, which may be an empty list.

    Raises:
        HueResponseError: If the bridge reported errors in the body.

    """
    response = HueResponse[model].model_validate(payload)
    response.raise_for_errors()
    return response.data


def unwrap_one[ModelT: HueModel](
    payload: object,
    model: type[ModelT],
) -> ModelT:
    """Validate a raw response body expected to hold exactly one resource.

    Args:
        payload: The decoded JSON body returned by the bridge.
        model: The model class the single ``data`` entry should be parsed into.

    Returns:
        The single parsed resource.

    Raises:
        HueResponseError: If the bridge reported errors, or returned no resource.

    """
    resources = unwrap(payload, model)
    if not resources:
        raise HueResponseError(["Bridge returned no resource"])
    return resources[0]


class ResourceType(StrEnum):
    """The `rtype` values used by the v2 CLIP API."""

    BEHAVIOR_INSTANCE = "behavior_instance"
    BEHAVIOR_SCRIPT = "behavior_script"
    BRIDGE = "bridge"
    BRIDGE_HOME = "bridge_home"
    BUTTON = "button"
    CAMERA_MOTION = "camera_motion"
    CONTACT = "contact"
    DEVICE = "device"
    DEVICE_POWER = "device_power"
    DEVICE_SOFTWARE_UPDATE = "device_software_update"
    ENTERTAINMENT = "entertainment"
    ENTERTAINMENT_CONFIGURATION = "entertainment_configuration"
    GEOFENCE_CLIENT = "geofence_client"
    GEOLOCATION = "geolocation"
    GROUPED_LIGHT = "grouped_light"
    GROUPED_LIGHT_LEVEL = "grouped_light_level"
    GROUPED_MOTION = "grouped_motion"
    HOMEKIT = "homekit"
    LIGHT = "light"
    LIGHT_LEVEL = "light_level"
    MATTER = "matter"
    MATTER_FABRIC = "matter_fabric"
    MOTION = "motion"
    RELATIVE_ROTARY = "relative_rotary"
    ROOM = "room"
    SCENE = "scene"
    SERVICE_GROUP = "service_group"
    SMART_SCENE = "smart_scene"
    TAMPER = "tamper"
    TEMPERATURE = "temperature"
    ZONE = "zone"
    ZGP_CONNECTIVITY = "zgp_connectivity"
    ZIGBEE_CONNECTIVITY = "zigbee_connectivity"
    ZIGBEE_DEVICE_DISCOVERY = "zigbee_device_discovery"
    WIFI_CONNECTIVITY = "wifi_connectivity"


class ResourceIdentifier(HueModel):
    """A reference to another resource.

    Used by the ``owner``, ``children`` and ``services`` fields.
    """

    rid: str
    rtype: str


class CommandResult(HueModel):
    """High-level outcome of a bridge mutation."""

    sent: bool = True
    resources: tuple[ResourceIdentifier, ...] = ()

    @classmethod
    def from_resources(
        cls,
        resources: list[ResourceIdentifier],
        *,
        sent: bool = True,
    ) -> "CommandResult":
        """Build a result from bridge resource references."""
        return cls(sent=sent, resources=tuple(resources))


class Metadata(HueModel):
    """Human-facing naming attached to most resources."""

    name: str = ""
    archetype: str | None = None


class On(HueModel):
    """Power state."""

    on: bool


class Dimming(HueModel):
    """Brightness as a percentage of the light's usable range."""

    brightness: float
    min_dim_level: float | None = None


class MirekSchema(HueModel):
    """The colour-temperature range a light accepts."""

    mirek_minimum: int | None = None
    mirek_maximum: int | None = None


class ColorTemperature(HueModel):
    """Colour temperature in mirek (reciprocal megakelvin)."""

    mirek: int | None = None
    mirek_valid: bool | None = None
    mirek_schema: MirekSchema | None = None


class ColorXY(HueModel):
    """A point in CIE xy colour space."""

    x: float
    y: float


class ColorGamut(HueModel):
    """The triangle of colours a light can physically reproduce.

    Each corner is a primary; any xy point outside the triangle is rounded to
    the nearest reachable one by the bridge, so callers that care about
    accuracy clamp to this first.
    """

    red: ColorXY
    green: ColorXY
    blue: ColorXY


class Color(HueModel):
    """Colour, expressed as CIE xy plus the light's reachable gamut."""

    xy: ColorXY
    gamut: ColorGamut | None = None
    gamut_type: str | None = None


class HueResource(HueModel):
    """Fields common to every addressable v2 resource.

    A resource handler binds every resource it parses to itself, so the model
    can issue its own commands. The binding lives in private attributes, which
    pydantic excludes from both validation and serialisation -- models stay
    pure data on the wire.
    """

    id: str
    type: str = ""
    id_v1: str | None = None
    owner: ResourceIdentifier | None = None

    _hue: "HueClient | None" = PrivateAttr(default=None)
    _rtype: str = PrivateAttr(default="")

    @property
    def device_id(self) -> str | None:
        """The id of the owning device, when this resource is a device service."""
        return self.owner.rid if self.owner is not None else None

    def bind(self, hue: "HueClient", rtype: str = "") -> Self:
        """Attach the client this resource issues its own commands through.

        Args:
            hue: The client that fetched this resource.
            rtype: The v2 resource type to address it under. Falls back to the
                model's own ``type`` field when empty.

        Returns:
            This same instance, so the call can be chained onto a parse.

        """
        self._hue = hue
        self._rtype = rtype or self.type
        return self

    @property
    def is_bound(self) -> bool:
        """Whether this resource carries a client and can issue commands."""
        return self._hue is not None

    @property
    def _client(self) -> "HueClient":
        """The client this resource was fetched by.

        Returns:
            The bound client.

        Raises:
            DetachedResourceError: If the resource was built by hand and so
                has no client to talk to.

        """
        if self._hue is None:
            msg = (
                f"{type(self).__name__} {self.id!r} is not bound to a client, "
                f"so it cannot issue commands. Fetch it via "
                f"hue.<resource>.get(...) to get a bound one."
            )
            raise DetachedResourceError(msg)
        return self._hue

    @property
    def _path(self) -> str:
        """The bridge endpoint addressing this resource."""
        return f"{RESOURCE_ROOT}/{self._rtype}/{self.id}"

    async def _put(self, path: str, data: dict[str, Any]) -> CommandResult:
        """Write a payload to an arbitrary path through the bound client.

        Args:
            path: The endpoint to write to. Usually :attr:`_path`, but a room
                sends light commands to its grouped_light service instead.
            data: The fields to change, in the bridge's payload shape.

        Returns:
            A CommandResult containing the bridge references affected.

        Raises:
            DetachedResourceError: If this resource is not bound to a client.
            HueResponseError: If the bridge rejects the write.

        """
        payload = await self._client.http.put(path, data)
        return CommandResult.from_resources(unwrap(payload, ResourceIdentifier))

    async def update(self, data: dict[str, Any]) -> CommandResult:
        """Apply a partial update to this resource.

        Args:
            data: The fields to change, in the bridge's payload shape.

        Returns:
            A CommandResult containing the bridge references affected.

        Raises:
            DetachedResourceError: If this resource is not bound to a client.
            HueResponseError: If the bridge rejects the update.

        """
        return await self._put(self._path, data)

    async def delete(self) -> CommandResult:
        """Delete this resource from the bridge.

        Returns:
            A CommandResult containing the bridge references deleted.

        Raises:
            DetachedResourceError: If this resource is not bound to a client.
            HueResponseError: If the bridge rejects the deletion.

        """
        payload = await self._client.http.delete(self._path)
        if payload is None:
            return CommandResult()
        return CommandResult.from_resources(unwrap(payload, ResourceIdentifier))

    async def refresh(self) -> Self:
        """Re-fetch this resource and return the fresh copy.

        The instance you called this on is left untouched: a model is a
        snapshot of what the bridge reported, and silently mutating one under
        a caller holding a reference to it would be a surprise.

        Returns:
            A newly parsed, newly bound instance carrying the current state.

        Raises:
            DetachedResourceError: If this resource is not bound to a client.
            HueResponseError: If the bridge no longer has this resource.

        """
        client = self._client
        payload = await client.http.get(self._path)
        return unwrap_one(payload, type(self)).bind(client, self._rtype)


class NamedResource(HueResource):
    """A resource that carries a `metadata.name`."""

    metadata: Metadata = Metadata()

    @property
    def name(self) -> str:
        """The resource's display name, or an empty string if unnamed."""
        return self.metadata.name
