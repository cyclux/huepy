"""Models for bridge and device network-connectivity services."""

from huepy.models.common import HueModel, HueResource


class ZigbeeChannel(HueModel):
    """The bridge's configured Zigbee channel and configuration state."""

    value: str = "unknown"
    status: str = "unknown"


class ZigbeeConnectivity(HueResource):
    """Reachability of one Zigbee device or of the bridge network itself."""

    status: str = ""
    mac_address: str | None = None
    channel: ZigbeeChannel | None = None
    extended_pan_id: str | None = None

    @property
    def is_connected(self) -> bool:
        """Whether the bridge currently reports this service as connected."""
        return self.status == "connected"
