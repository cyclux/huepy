"""Resource handlers, one per v2 CLIP resource type."""

from huepy.resources.automation import (
    BehaviorInstance,
    BehaviorScript,
    GeofenceClient,
    Geolocation,
)
from huepy.resources.base import BaseResource, NamedResourceHandler
from huepy.resources.connectivity import (
    WifiConnectivity,
    ZgpConnectivity,
    ZigbeeConnectivity,
)
from huepy.resources.device import Bridge, Device
from huepy.resources.entertainment import Entertainment, EntertainmentConfiguration
from huepy.resources.group import (
    BridgeHome,
    GroupedLightResolver,
    Room,
    Scene,
    ServiceGroup,
    SmartScene,
    Zone,
)
from huepy.resources.light import (
    DimmableMixin,
    GroupedLight,
    GroupedLightLevel,
    Light,
    LightLevel,
)
from huepy.resources.management import DeviceSoftwareUpdate, ZigbeeDeviceDiscovery
from huepy.resources.security import (
    CameraMotion,
    Homekit,
    Matter,
    MatterFabric,
    Tamper,
)
from huepy.resources.sensor import (
    Button,
    Contact,
    DevicePower,
    GroupedMotion,
    Motion,
    RelativeRotary,
    Temperature,
    ToggleableSensorMixin,
)

__all__ = [
    "BaseResource",
    "BehaviorInstance",
    "BehaviorScript",
    "Bridge",
    "BridgeHome",
    "Button",
    "CameraMotion",
    "Contact",
    "Device",
    "DevicePower",
    "DeviceSoftwareUpdate",
    "DimmableMixin",
    "Entertainment",
    "EntertainmentConfiguration",
    "GeofenceClient",
    "Geolocation",
    "GroupedLight",
    "GroupedLightLevel",
    "GroupedLightResolver",
    "GroupedMotion",
    "Homekit",
    "Light",
    "LightLevel",
    "Matter",
    "MatterFabric",
    "Motion",
    "NamedResourceHandler",
    "RelativeRotary",
    "Room",
    "Scene",
    "ServiceGroup",
    "SmartScene",
    "Tamper",
    "Temperature",
    "ToggleableSensorMixin",
    "WifiConnectivity",
    "ZgpConnectivity",
    "ZigbeeConnectivity",
    "ZigbeeDeviceDiscovery",
    "Zone",
]
