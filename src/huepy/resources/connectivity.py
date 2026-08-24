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
