"""Pydantic models for the Hue v2 CLIP API.

Every resource method returns one of these instead of a bare dict, so field
access is checked and discoverable. Models tolerate unknown fields, which
keeps them working across bridge firmware updates.
"""

from collections.abc import Mapping
from types import MappingProxyType
from typing import Annotated, cast

from pydantic import Discriminator, Tag, TypeAdapter

from huepy.models.common import (
    Color,
    ColorGamut,
    ColorTemperature,
    ColorXY,
    CommandResult,
    Dimming,
    HueErrorDetail,
    HueModel,
    HueResource,
    HueResponse,
    Metadata,
    MirekSchema,
    NamedResource,
    On,
    ResourceIdentifier,
    ResourceType,
    unwrap,
    unwrap_one,
)
from huepy.models.connectivity import ZigbeeChannel, ZigbeeConnectivity
from huepy.models.device import (
    Bridge,
    Device,
    DevicePower,
    PowerState,
    ProductData,
    TimeZone,
)
from huepy.models.event import (
    EventResource,
    EventType,
    HueEvent,
    parse_events,
)
from huepy.models.group import (
    BridgeHome,
    ResourceGroup,
    Room,
    Scene,
    SceneAction,
    SceneStatus,
    ServiceGroup,
    Zone,
)
from huepy.models.light import (
    Alert,
    Effect,
    Effects,
    Gradient,
    GradientPoint,
    GroupedColor,
    GroupedLight,
    GroupedLightLevel,
    Light,
    LightCommands,
    LightLevel,
    LightLevelReading,
    LightLevelReport,
    LightState,
    Powerup,
    Signaling,
    TimedEffects,
)
from huepy.models.sensor import (
    Button,
    ButtonReading,
    ButtonReport,
    Contact,
    ContactReport,
    GroupedMotion,
    Motion,
    MotionReading,
    MotionReport,
    RelativeRotary,
    RelativeRotaryEvent,
    RelativeRotaryReading,
    RelativeRotaryReport,
    RelativeRotaryRotation,
    Sensitivity,
    Temperature,
    TemperatureReading,
    TemperatureReport,
)
from huepy.models.state import build_light_payload

UNKNOWN_RESOURCE_TAG = "_unknown"


_RESOURCE_MODELS = {
    "bridge": Bridge,
    "bridge_home": BridgeHome,
    "button": Button,
    "contact": Contact,
    "device": Device,
    "device_power": DevicePower,
    "grouped_light": GroupedLight,
    "grouped_light_level": GroupedLightLevel,
    "grouped_motion": GroupedMotion,
    "light": Light,
    "light_level": LightLevel,
    "motion": Motion,
    "relative_rotary": RelativeRotary,
    "room": Room,
    "scene": Scene,
    "service_group": ServiceGroup,
    "temperature": Temperature,
    "zone": Zone,
    "zigbee_connectivity": ZigbeeConnectivity,
}
RESOURCE_MODELS = MappingProxyType(_RESOURCE_MODELS)
"""Modelled bridge resource types and their concrete pydantic classes."""


def _resource_tag(value: object) -> str:
    """Return the callable-discriminator tag for a resource payload or model."""
    raw = (
        cast("Mapping[str, object]", value).get("type", "")
        if isinstance(value, Mapping)
        else getattr(value, "type", "")
    )
    return (
        raw if isinstance(raw, str) and raw in RESOURCE_MODELS else UNKNOWN_RESOURCE_TAG
    )


AnyResource = Annotated[
    Annotated[Bridge, Tag("bridge")]
    | Annotated[BridgeHome, Tag("bridge_home")]
    | Annotated[Button, Tag("button")]
    | Annotated[Contact, Tag("contact")]
    | Annotated[Device, Tag("device")]
    | Annotated[DevicePower, Tag("device_power")]
    | Annotated[GroupedLight, Tag("grouped_light")]
    | Annotated[GroupedLightLevel, Tag("grouped_light_level")]
    | Annotated[GroupedMotion, Tag("grouped_motion")]
    | Annotated[Light, Tag("light")]
    | Annotated[LightLevel, Tag("light_level")]
    | Annotated[Motion, Tag("motion")]
    | Annotated[RelativeRotary, Tag("relative_rotary")]
    | Annotated[Room, Tag("room")]
    | Annotated[Scene, Tag("scene")]
    | Annotated[ServiceGroup, Tag("service_group")]
    | Annotated[Temperature, Tag("temperature")]
    | Annotated[Zone, Tag("zone")]
    | Annotated[ZigbeeConnectivity, Tag("zigbee_connectivity")]
    | Annotated[HueResource, Tag(UNKNOWN_RESOURCE_TAG)],
    Discriminator(_resource_tag),
]
RESOURCE_LIST = TypeAdapter(list[AnyResource])
_RESOURCE: TypeAdapter[AnyResource] = TypeAdapter(AnyResource)


def parse_resource(payload: object) -> AnyResource:
    """Parse one known or future bridge resource into its best model."""
    return _RESOURCE.validate_python(payload)


__all__ = [
    "RESOURCE_LIST",
    "RESOURCE_MODELS",
    "Alert",
    "AnyResource",
    "Bridge",
    "BridgeHome",
    "Button",
    "ButtonReading",
    "ButtonReport",
    "Color",
    "ColorGamut",
    "ColorTemperature",
    "ColorXY",
    "CommandResult",
    "Contact",
    "ContactReport",
    "Device",
    "DevicePower",
    "Dimming",
    "Effect",
    "Effects",
    "EventResource",
    "EventType",
    "Gradient",
    "GradientPoint",
    "GroupedColor",
    "GroupedLight",
    "GroupedLightLevel",
    "GroupedMotion",
    "HueErrorDetail",
    "HueEvent",
    "HueModel",
    "HueResource",
    "HueResponse",
    "Light",
    "LightCommands",
    "LightLevel",
    "LightLevelReading",
    "LightLevelReport",
    "LightState",
    "Metadata",
    "MirekSchema",
    "Motion",
    "MotionReading",
    "MotionReport",
    "NamedResource",
    "On",
    "PowerState",
    "Powerup",
    "ProductData",
    "RelativeRotary",
    "RelativeRotaryEvent",
    "RelativeRotaryReading",
    "RelativeRotaryReport",
    "RelativeRotaryRotation",
    "ResourceGroup",
    "ResourceIdentifier",
    "ResourceType",
    "Room",
    "Scene",
    "SceneAction",
    "SceneStatus",
    "Sensitivity",
    "ServiceGroup",
    "Signaling",
    "Temperature",
    "TemperatureReading",
    "TemperatureReport",
    "TimeZone",
    "TimedEffects",
    "ZigbeeChannel",
    "ZigbeeConnectivity",
    "Zone",
    "build_light_payload",
    "parse_events",
    "parse_resource",
    "unwrap",
    "unwrap_one",
]
