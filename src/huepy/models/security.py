"""Models for the smart-home bridge integrations and Hue Secure sensors.

HomeKit and Matter expose the bridge to Apple Home and the Matter fabric; each
can be reset. ``Tamper`` and ``CameraMotion`` are Hue Secure sensor services.
"""

from pydantic import AwareDatetime, Field

from huepy.models.common import CommandResult, HueModel, HueResource
from huepy.models.sensor import Motion


class Homekit(HueResource):
    """The bridge's HomeKit pairing service, which can be reset."""

    status: str | None = None
    status_values: list[str] = Field(default_factory=list)

    async def reset(self) -> CommandResult:
        """Reset the bridge's HomeKit pairing.

        Returns:
            A CommandResult containing the bridge references affected.

        Raises:
            DetachedResourceError: If this service is not bound to a client.
            HueResponseError: If the bridge rejects the reset.

        """
        return await self.update({"action": "homekit_reset"})


class Matter(HueResource):
    """The bridge's Matter service, which can be reset."""

    max_fabrics: int | None = None
    has_qr_code: bool | None = None

    async def reset(self) -> CommandResult:
        """Reset the bridge's Matter commissioning.

        Returns:
            A CommandResult containing the bridge references affected.

        Raises:
            DetachedResourceError: If this service is not bound to a client.
            HueResponseError: If the bridge rejects the reset.

        """
        return await self.update({"action": "matter_reset"})


class FabricData(HueModel):
    """The label and vendor of one commissioned Matter fabric."""

    label: str | None = None
    vendor_id: int | None = None


class MatterFabric(HueResource):
    """One Matter fabric the bridge has been commissioned into.

    A fabric cannot be edited, only listed and removed.
    """

    status: str | None = None
    creation_time: AwareDatetime | None = None
    fabric_data: FabricData | None = None


class TamperReport(HueModel):
    """One tamper transition a sensor reported."""

    changed: AwareDatetime | None = None
    source: str | None = None
    state: str | None = None


class Tamper(HueResource):
    """A sensor's tamper-detection service."""

    tamper_reports: list[TamperReport] = Field(default_factory=list)

    @property
    def is_tampered(self) -> bool:
        """Whether the most recent tamper report is in the tampered state."""
        return bool(self.tamper_reports) and self.tamper_reports[0].state == "tampered"


class CameraMotion(Motion):
    """A camera's motion-detection service.

    The same shape as a :class:`~huepy.models.sensor.Motion` service, reported
    by a Hue Secure camera instead of a motion sensor.
    """
