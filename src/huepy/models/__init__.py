"""Pydantic models for the Hue v2 CLIP API.

Every resource method returns one of these instead of a bare dict, so field
access is checked and discoverable. Models tolerate unknown fields, which
keeps them working across bridge firmware updates.
"""

from collections.abc import Mapping
from types import MappingProxyType
from typing import Annotated, cast

from pydantic import Discriminator, Tag, TypeAdapter

from huepy.models.automation import (
    BehaviorInstance,
    BehaviorScript,
    GeofenceClient,
    Geolocation,
    SunToday,
)
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
from huepy.models.connectivity import (
    WifiConnectivity,
    ZgpConnectivity,
    ZigbeeChannel,
    ZigbeeConnectivity,
)
from huepy.models.device import (
    Bridge,
    Device,
    DevicePower,
    PowerState,
    ProductData,
    TimeZone,
)
from huepy.models.entertainment import (
    Entertainment,
    EntertainmentChannel,
    EntertainmentConfiguration,
    StreamProxy,
)
from huepy.models.event import (
    EventResource,
    EventType,
    HueEvent,
    parse_events,
)
from huepy.models.group import (
    BridgeHome,
    GroupState,
    RecallAction,
    ResourceGroup,
    Room,
    Scene,
    SceneAction,
    SceneStatus,
    ServiceGroup,
    SmartScene,
    SmartSceneActiveTimeslot,
    SmartSceneStartTime,
    SmartSceneTime,
    SmartSceneTimeslot,
    SmartSceneWeekTimeslot,
    WeekDay,
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
    Signal,
    Signaling,
    TimedEffect,
    TimedEffects,
)
from huepy.models.management import (
    AutoInstall,
    DeviceSoftwareUpdate,
    ZigbeeDeviceDiscovery,
)
from huepy.models.security import (
    CameraMotion,
    FabricData,
    Homekit,
    Matter,
    MatterFabric,
    Tamper,
    TamperReport,
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
from huepy.models.state import (
    build_effect_payload,
    build_light_payload,
    build_powerup_payload,
    build_scene_recall,
)

UNKNOWN_RESOURCE_TAG = "_unknown"


_RESOURCE_MODELS = {
    "behavior_instance": BehaviorInstance,
    "behavior_script": BehaviorScript,
    "bridge": Bridge,
    "bridge_home": BridgeHome,
    "button": Button,
    "camera_motion": CameraMotion,
    "contact": Contact,
    "device": Device,
    "device_power": DevicePower,
    "device_software_update": DeviceSoftwareUpdate,
    "entertainment": Entertainment,
    "entertainment_configuration": EntertainmentConfiguration,
    "geofence_client": GeofenceClient,
    "geolocation": Geolocation,
    "grouped_light": GroupedLight,
    "grouped_light_level": GroupedLightLevel,
    "grouped_motion": GroupedMotion,
    "homekit": Homekit,
    "light": Light,
    "light_level": LightLevel,
    "matter": Matter,
    "matter_fabric": MatterFabric,
    "motion": Motion,
    "relative_rotary": RelativeRotary,
    "room": Room,
    "scene": Scene,
    "service_group": ServiceGroup,
    "smart_scene": SmartScene,
    "tamper": Tamper,
    "temperature": Temperature,
    "wifi_connectivity": WifiConnectivity,
    "zone": Zone,
    "zgp_connectivity": ZgpConnectivity,
    "zigbee_connectivity": ZigbeeConnectivity,
    "zigbee_device_discovery": ZigbeeDeviceDiscovery,
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
    Annotated[BehaviorInstance, Tag("behavior_instance")]
    | Annotated[BehaviorScript, Tag("behavior_script")]
    | Annotated[Bridge, Tag("bridge")]
    | Annotated[BridgeHome, Tag("bridge_home")]
    | Annotated[Button, Tag("button")]
    | Annotated[CameraMotion, Tag("camera_motion")]
    | Annotated[Contact, Tag("contact")]
    | Annotated[Device, Tag("device")]
    | Annotated[DevicePower, Tag("device_power")]
    | Annotated[DeviceSoftwareUpdate, Tag("device_software_update")]
    | Annotated[Entertainment, Tag("entertainment")]
    | Annotated[EntertainmentConfiguration, Tag("entertainment_configuration")]
    | Annotated[GeofenceClient, Tag("geofence_client")]
    | Annotated[Geolocation, Tag("geolocation")]
    | Annotated[GroupedLight, Tag("grouped_light")]
    | Annotated[GroupedLightLevel, Tag("grouped_light_level")]
    | Annotated[GroupedMotion, Tag("grouped_motion")]
    | Annotated[Homekit, Tag("homekit")]
    | Annotated[Light, Tag("light")]
    | Annotated[LightLevel, Tag("light_level")]
    | Annotated[Matter, Tag("matter")]
    | Annotated[MatterFabric, Tag("matter_fabric")]
    | Annotated[Motion, Tag("motion")]
    | Annotated[RelativeRotary, Tag("relative_rotary")]
    | Annotated[Room, Tag("room")]
    | Annotated[Scene, Tag("scene")]
    | Annotated[ServiceGroup, Tag("service_group")]
    | Annotated[SmartScene, Tag("smart_scene")]
    | Annotated[Tamper, Tag("tamper")]
    | Annotated[Temperature, Tag("temperature")]
    | Annotated[WifiConnectivity, Tag("wifi_connectivity")]
    | Annotated[Zone, Tag("zone")]
    | Annotated[ZgpConnectivity, Tag("zgp_connectivity")]
    | Annotated[ZigbeeConnectivity, Tag("zigbee_connectivity")]
    | Annotated[ZigbeeDeviceDiscovery, Tag("zigbee_device_discovery")]
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
    "AutoInstall",
    "BehaviorInstance",
    "BehaviorScript",
    "Bridge",
    "BridgeHome",
    "Button",
    "ButtonReading",
    "ButtonReport",
    "CameraMotion",
    "Color",
    "ColorGamut",
    "ColorTemperature",
    "ColorXY",
    "CommandResult",
    "Contact",
    "ContactReport",
    "Device",
    "DevicePower",
    "DeviceSoftwareUpdate",
    "Dimming",
    "Effect",
    "Effects",
    "Entertainment",
    "EntertainmentChannel",
    "EntertainmentConfiguration",
    "EventResource",
    "EventType",
    "FabricData",
    "GeofenceClient",
    "Geolocation",
    "Gradient",
    "GradientPoint",
    "GroupState",
    "GroupedColor",
    "GroupedLight",
    "GroupedLightLevel",
    "GroupedMotion",
    "Homekit",
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
    "Matter",
    "MatterFabric",
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
    "RecallAction",
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
    "Signal",
    "Signaling",
    "SmartScene",
    "SmartSceneActiveTimeslot",
    "SmartSceneStartTime",
    "SmartSceneTime",
    "SmartSceneTimeslot",
    "SmartSceneWeekTimeslot",
    "StreamProxy",
    "SunToday",
    "Tamper",
    "TamperReport",
    "Temperature",
    "TemperatureReading",
    "TemperatureReport",
    "TimeZone",
    "TimedEffect",
    "TimedEffects",
    "WeekDay",
    "WifiConnectivity",
    "ZgpConnectivity",
    "ZigbeeChannel",
    "ZigbeeConnectivity",
    "ZigbeeDeviceDiscovery",
    "Zone",
    "build_effect_payload",
    "build_light_payload",
    "build_powerup_payload",
    "build_scene_recall",
    "parse_events",
    "parse_resource",
    "unwrap",
    "unwrap_one",
]
