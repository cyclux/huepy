"""Models for physical devices and their power state."""

from typing import Any

from pydantic import Field

from huepy.models.common import (
    CommandResult,
    HueModel,
    HueResource,
    NamedResource,
    ResourceIdentifier,
)
from huepy.models.state import MILLISECONDS_PER_SECOND


class ProductData(HueModel):
    """Manufacturer information reported by a device."""

    model_id: str | None = None
    manufacturer_name: str | None = None
    product_name: str | None = None
    product_archetype: str | None = None
    certified: bool | None = None
    software_version: str | None = None


class Device(NamedResource):
    """A physical Hue device, which exposes one or more services."""

    product_data: ProductData = ProductData()
    services: list[ResourceIdentifier] = Field(default_factory=list)

    def service_id(self, rtype: str) -> str | None:
        """Return the id of this device's service of the given type, if any."""
        return next((s.rid for s in self.services if s.rtype == rtype), None)

    async def identify(self, *, duration: float | None = None) -> CommandResult:
        """Ask the device to identify itself, e.g. by blinking its lights.

        Args:
            duration: How long to identify for, in seconds. Left to the device
                when omitted.

        Returns:
            A CommandResult containing the bridge references affected.

        Raises:
            DetachedResourceError: If this device is not bound to a client.
            HueResponseError: If the device does not support identify.

        """
        action: dict[str, Any] = {"action": "identify"}
        if duration is not None:
            action["duration"] = int(duration * MILLISECONDS_PER_SECOND)
        return await self.update({"identify": action})

    async def usertest(self, *, enabled: bool) -> CommandResult:
        """Turn the device's user-test mode on or off.

        Args:
            enabled: Whether user test is on. In user test the device signals
                its presence, e.g. a motion sensor flashes on each detection.

        Returns:
            A CommandResult containing the bridge references affected.

        Raises:
            DetachedResourceError: If this device is not bound to a client.
            HueResponseError: If the device does not support user test.

        """
        return await self.update({"usertest": {"usertest": enabled}})


class PowerState(HueModel):
    """Battery status of a battery-powered device."""

    battery_state: str | None = None
    battery_level: int | None = None


class DevicePower(HueResource):
    """The battery service of a device."""

    power_state: PowerState = PowerState()

    @property
    def battery_level(self) -> int | None:
        """Remaining battery percentage, or None if not reported."""
        return self.power_state.battery_level


class TimeZone(HueModel):
    """The bridge's configured time zone."""

    time_zone: str | None = None


class Bridge(HueResource):
    """The Hue bridge itself."""

    bridge_id: str | None = None
    time_zone: TimeZone = TimeZone()

    async def set_timezone(self, time_zone: str) -> CommandResult:
        """Set the bridge's time zone.

        Args:
            time_zone: An IANA time-zone name, e.g. ``"Europe/Berlin"``.

        Returns:
            A CommandResult containing the bridge references affected.

        Raises:
            DetachedResourceError: If this bridge is not bound to a client.
            HueResponseError: If the bridge rejects the change.

        """
        return await self.update({"time_zone": {"time_zone": time_zone}})
