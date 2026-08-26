"""Handlers for network-connectivity services."""

from typing import ClassVar

from huepy.models import common as common_models
from huepy.models import connectivity as connectivity_models
from huepy.models.common import ResourceType
from huepy.resources.base import BaseResource


class ZigbeeConnectivity(BaseResource[connectivity_models.ZigbeeConnectivity]):
    """Handler for Zigbee reachability services."""

    resource_type: ClassVar[ResourceType] = ResourceType.ZIGBEE_CONNECTIVITY
    model: ClassVar[type[common_models.HueModel]] = (
        connectivity_models.ZigbeeConnectivity
    )


class ZgpConnectivity(BaseResource[connectivity_models.ZgpConnectivity]):
    """Handler for Zigbee Green Power reachability services."""

    resource_type: ClassVar[ResourceType] = ResourceType.ZGP_CONNECTIVITY
    model: ClassVar[type[common_models.HueModel]] = connectivity_models.ZgpConnectivity


class WifiConnectivity(BaseResource[connectivity_models.WifiConnectivity]):
    """Handler for Wi-Fi reachability services."""

    resource_type: ClassVar[ResourceType] = ResourceType.WIFI_CONNECTIVITY
    model: ClassVar[type[common_models.HueModel]] = connectivity_models.WifiConnectivity
