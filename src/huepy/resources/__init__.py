"""Resource handlers, one per v2 CLIP resource type."""

from huepy.resources.base import BaseResource, NamedResourceHandler
from huepy.resources.connectivity import ZigbeeConnectivity
from huepy.resources.device import Bridge, Device
from huepy.resources.group import (
    BridgeHome,
    GroupedLightResolver,
    Room,
    Scene,
    ServiceGroup,
    Zone,
)
from huepy.resources.light import (
    DimmableMixin,
    GroupedLight,
    GroupedLightLevel,
    Light,
    LightLevel,
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
    "Bridge",
    "BridgeHome",
    "Button",
    "Contact",
    "Device",
    "DevicePower",
    "DimmableMixin",
    "GroupedLight",
    "GroupedLightLevel",
    "GroupedLightResolver",
    "GroupedMotion",
    "Light",
    "LightLevel",
    "Motion",
    "NamedResourceHandler",
    "RelativeRotary",
    "Room",
    "Scene",
    "ServiceGroup",
    "Temperature",
    "ToggleableSensorMixin",
    "ZigbeeConnectivity",
    "Zone",
]
