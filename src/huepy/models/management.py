"""Models for bridge-side device management: pairing and firmware updates.

``ZigbeeDeviceDiscovery`` is how new devices are searched for and paired -- the
only route to adding a light through the v2 API. ``DeviceSoftwareUpdate`` tracks
and controls a device's firmware.
"""

from typing import Any

from pydantic import Field

from huepy.models.common import CommandResult, HueModel, HueResource


class ZigbeeDeviceDiscovery(HueResource):
    """The bridge's device-pairing service for one owner.

    ``status`` is ``"ready"`` when idle or ``"active"`` while a search is
    running; :meth:`search` starts one.
    """

    status: str | None = None
    action_values: list[str] = Field(default_factory=list)

    @property
    def is_searching(self) -> bool:
        """Whether a device search is currently running."""
        return self.status == "active"

    async def search(
        self,
        *,
        install_codes: list[str] | None = None,
        channels: list[int] | None = None,
    ) -> CommandResult:
        """Start searching for new Zigbee devices to pair.

        Args:
            install_codes: Install codes for devices that require one.
            channels: Zigbee channels to search, if not the defaults.

        Returns:
            A CommandResult containing the bridge references affected.

        Raises:
            DetachedResourceError: If this service is not bound to a client.
            HueResponseError: If the bridge rejects the search.

        """
        return await self._search("search", install_codes, channels)

    async def search_with_default_link_key(
        self,
        *,
        install_codes: list[str] | None = None,
        channels: list[int] | None = None,
    ) -> CommandResult:
        """Search for devices, allowing the well-known default link key.

        Some older or third-party Zigbee devices only join with the default
        link key; this permits that, at a small security cost.

        Args:
            install_codes: Install codes for devices that require one.
            channels: Zigbee channels to search, if not the defaults.

        Returns:
            A CommandResult containing the bridge references affected.

        Raises:
            DetachedResourceError: If this service is not bound to a client.
            HueResponseError: If the bridge rejects the search.

        """
        return await self._search(
            "search_allow_default_link_key", install_codes, channels
        )

    async def _search(
        self,
        action_type: str,
        install_codes: list[str] | None,
        channels: list[int] | None,
    ) -> CommandResult:
        """Issue one device-search action, omitting empty parameters."""
        action: dict[str, Any] = {"action_type": action_type}
        if install_codes is not None:
            action["search_codes"] = install_codes
        if channels is not None:
            action["search_channels"] = channels
        return await self.update({"action": action})


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

    async def install(self) -> CommandResult:
        """Install a firmware update that is ready to apply.

        Returns:
            A CommandResult containing the bridge references affected.

        Raises:
            DetachedResourceError: If this service is not bound to a client.
            HueResponseError: If no update is ready, or the bridge rejects it.

        """
        return await self.update({"state": "ready_to_install"})

    async def set_auto_install(
        self,
        *,
        on: bool,
        update_time: str | None = None,
    ) -> CommandResult:
        """Configure whether and when the device installs updates on its own.

        Args:
            on: Whether automatic installation is enabled.
            update_time: The local time of day to install at, e.g. ``"03:00:00"``.

        Returns:
            A CommandResult containing the bridge references affected.

        Raises:
            DetachedResourceError: If this service is not bound to a client.
            HueResponseError: If the bridge rejects the change.

        """
        auto_install: dict[str, Any] = {"on": on}
        if update_time is not None:
            auto_install["update_time"] = update_time
        return await self.update({"auto_install": auto_install})
