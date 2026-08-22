"""Models for physical devices and their power state."""

from pydantic import Field

from huepy.models.common import (
    HueModel,
    HueResource,
    NamedResource,
    ResourceIdentifier,
)


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
