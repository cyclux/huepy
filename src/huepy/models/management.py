"""Models for bridge-side device management: pairing and firmware updates.

``ZigbeeDeviceDiscovery`` is how new devices are searched for and paired -- the
only route to adding a light through the v2 API. ``DeviceSoftwareUpdate`` tracks
and controls a device's firmware.
"""

from typing import Any

from pydantic import Field

from huepy.models.common import HueModel, HueResource


class ZigbeeDeviceDiscovery(HueResource):
    """The bridge's device-pairing service for one owner.

    ``status`` is ``"ready"`` when idle or ``"active"`` while a search is
    running; :meth:`ZigbeeDeviceDiscovery.search` on the handler starts one.
    """

    status: str | None = None
    action_values: list[str] = Field(default_factory=list)

    @property
    def is_searching(self) -> bool:
        """Whether a device search is currently running."""
        return self.status == "active"


class AutoInstall(HueModel):
    """When a device installs firmware updates on its own."""

    on: bool | None = None
    update_time: str | None = None


class DeviceSoftwareUpdate(HueResource):
    """The firmware-update state of a device.

    ``state`` moves through ``no_update`` -> ``update_data_ready`` ->
    ``ready_to_install`` -> ``installing`` as an update becomes available and is
    applied.
    """

    state: str | None = None
    auto_install: AutoInstall | None = None
    problems: list[dict[str, Any]] = Field(default_factory=list)
