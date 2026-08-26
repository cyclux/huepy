"""Handlers for bridge-side device management: pairing and firmware."""

from typing import Any, ClassVar

from huepy.models import common as common_models
from huepy.models import management as management_models
from huepy.models.common import ResourceIdentifier, ResourceType
from huepy.resources.base import BaseResource


class ZigbeeDeviceDiscovery(BaseResource[management_models.ZigbeeDeviceDiscovery]):
    """Handler for the device-pairing service -- how new lights are added."""

    resource_type: ClassVar[ResourceType] = ResourceType.ZIGBEE_DEVICE_DISCOVERY
    model: ClassVar[type[common_models.HueModel]] = (
        management_models.ZigbeeDeviceDiscovery
    )

    async def search(
        self,
        resource_id: str,
        *,
        install_codes: list[str] | None = None,
        channels: list[int] | None = None,
    ) -> list[ResourceIdentifier]:
        """Start searching for new Zigbee devices to pair.

        Args:
            resource_id: The discovery service id.
            install_codes: Install codes for devices that require one.
            channels: Zigbee channels to search, if not the defaults.

        Returns:
            References to the updated resources.

        """
        return await self._search("search", resource_id, install_codes, channels)

    async def search_with_default_link_key(
        self,
        resource_id: str,
        *,
        install_codes: list[str] | None = None,
        channels: list[int] | None = None,
    ) -> list[ResourceIdentifier]:
        """Search for devices, allowing the well-known default link key.

        Some older or third-party Zigbee devices only join with the default
        link key; this permits that, at a small security cost.

        Args:
            resource_id: The discovery service id.
            install_codes: Install codes for devices that require one.
            channels: Zigbee channels to search, if not the defaults.

        Returns:
            References to the updated resources.

        """
        return await self._search(
            "search_allow_default_link_key", resource_id, install_codes, channels
        )

    async def _search(
        self,
        action_type: str,
        resource_id: str,
        install_codes: list[str] | None,
        channels: list[int] | None,
    ) -> list[ResourceIdentifier]:
        """Issue one device-search action, omitting empty parameters."""
        action: dict[str, Any] = {"action_type": action_type}
        if install_codes is not None:
            action["search_codes"] = install_codes
        if channels is not None:
            action["search_channels"] = channels
        return await self.update(resource_id, {"action": action})


class DeviceSoftwareUpdate(BaseResource[management_models.DeviceSoftwareUpdate]):
    """Handler for a device's firmware-update service."""

    resource_type: ClassVar[ResourceType] = ResourceType.DEVICE_SOFTWARE_UPDATE
    model: ClassVar[type[common_models.HueModel]] = (
        management_models.DeviceSoftwareUpdate
    )

    async def install(self, resource_id: str) -> list[ResourceIdentifier]:
        """Install a firmware update that is ready to apply.

        Args:
            resource_id: The software-update service id.

        Returns:
            References to the updated resources.

        """
        return await self.update(resource_id, {"state": "ready_to_install"})

    async def set_auto_install(
        self,
        resource_id: str,
        *,
        on: bool,
        update_time: str | None = None,
    ) -> list[ResourceIdentifier]:
        """Configure whether and when the device installs updates on its own.

        Args:
            resource_id: The software-update service id.
            on: Whether automatic installation is enabled.
            update_time: The local time of day to install at, e.g. ``"03:00:00"``.

        Returns:
            References to the updated resources.

        """
        auto_install: dict[str, Any] = {"on": on}
        if update_time is not None:
            auto_install["update_time"] = update_time
        return await self.update(resource_id, {"auto_install": auto_install})
