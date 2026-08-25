"""Handlers for physical devices and the bridge itself."""

from typing import ClassVar

from huepy.models import common as common_models
from huepy.models import device as device_models
from huepy.models.common import ResourceType
from huepy.resources.base import BaseResource, NamedResourceHandler


class Device(NamedResourceHandler[device_models.Device]):
    """Handler for physical Hue devices.

    Devices carry a `metadata.name`, so they can be looked up by it:
    ``await hue.devices.get("Hue play 1")``.
    """

    resource_type: ClassVar[ResourceType] = ResourceType.DEVICE
    model: ClassVar[type[common_models.HueModel]] = device_models.Device


class Bridge(BaseResource[device_models.Bridge]):
    """Handler for the bridge resource."""

    resource_type: ClassVar[ResourceType] = ResourceType.BRIDGE
    model: ClassVar[type[common_models.HueModel]] = device_models.Bridge
