"""Resource handlers, one per v2 CLIP resource type."""

from huepy.resources.base import BaseResource, NamedResourceHandler
from huepy.resources.device import Bridge, Device
from huepy.resources.group import (
    BridgeHome,
    GroupedLightControlMixin,
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
    "GroupedLightControlMixin",
    "GroupedLightLevel",
    "GroupedMotion",
    "Light",
    "LightLevel",
    "Motion",
    "NamedResourceHandler",
    "Room",
    "Scene",
    "ServiceGroup",
    "Temperature",
    "ToggleableSensorMixin",
    "Zone",
]
