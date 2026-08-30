"""Models for sensor services: motion, temperature, button, contact."""

from pydantic import AwareDatetime, Field

from huepy.models.common import CommandResult, HueModel, HueResource, Metadata


class ToggleableSensor(HueResource):
    """A sensor service the bridge can switch on or off as a detector.

    The verbs are ``enable``/``disable``, not ``turn_on``/``turn_off``: a sensor
    is switched on as a detector, not powered like a light. ``enabled`` lives
    here so every toggleable sensor reports and controls it the same way.
    """

    enabled: bool = True

    async def enable(self) -> CommandResult:
        """Enable this sensor so it reports again.

        Returns:
            A CommandResult containing the bridge references affected.

        Raises:
            DetachedResourceError: If this sensor is not bound to a client.
            HueResponseError: If the bridge rejects the change.

        """
        return await self.update({"enabled": True})

    async def disable(self) -> CommandResult:
        """Disable this sensor, so it stops reporting until re-enabled.

        Returns:
            A CommandResult containing the bridge references affected.

        Raises:
            DetachedResourceError: If this sensor is not bound to a client.
            HueResponseError: If the bridge rejects the change.

        """
        return await self.update({"enabled": False})


class MotionReport(HueModel):
    """The most recent motion transition."""

    changed: AwareDatetime | None = None
    motion: bool | None = None


class MotionReading(HueModel):
    """Current motion state plus the timestamp of the last change."""

    motion: bool | None = None
    motion_valid: bool | None = None
    motion_report: MotionReport | None = None


class Sensitivity(HueModel):
    """A motion sensor's sensitivity setting and its permitted maximum."""

    status: str | None = None
    sensitivity: int | None = None
    sensitivity_max: int = 4


class Motion(ToggleableSensor):
    """A motion sensor service."""

    motion: MotionReading | None = None
    sensitivity: Sensitivity = Sensitivity()

    @property
    def motion_detected(self) -> bool:
        """Whether motion is currently detected."""
        return self.motion is not None and bool(self.motion.motion)

    @property
    def last_motion(self) -> str:
        """Timestamp of the last motion transition, or an empty string."""
        if self.motion is None or self.motion.motion_report is None:
            return ""
        changed = self.motion.motion_report.changed
        return changed.isoformat().replace("+00:00", "Z") if changed is not None else ""

    # Setting sensitivity stays on the ``hue.api.motions`` handler, not here: it
    # writes a field only a single motion sensor has, and ``GroupedMotion`` (an
    # aggregate) and ``CameraMotion`` subclass ``Motion`` for its shape -- a
    # self-acting method would leak onto them and PUT sensitivity to a service
    # that rejects it.


class GroupedMotion(Motion):
    """An aggregate of several motion sensors."""


class TemperatureReport(HueModel):
    """The most recent temperature reading."""

    changed: AwareDatetime | None = None
    temperature: float | None = None


class TemperatureReading(HueModel):
    """Current temperature in degrees Celsius."""

    temperature: float | None = None
    temperature_valid: bool | None = None
    temperature_report: TemperatureReport | None = None


class Temperature(ToggleableSensor):
    """A temperature sensor service."""

    temperature: TemperatureReading | None = None

    @property
    def celsius(self) -> float | None:
        """Current temperature in degrees Celsius, or None if unavailable."""
        return self.temperature.temperature if self.temperature is not None else None


class ButtonReport(HueModel):
    """The most recent button event."""

    updated: AwareDatetime | None = None
    event: str | None = None


class ButtonReading(HueModel):
    """Current button state and its most recent timestamped event."""

    button_report: ButtonReport | None = None
    event_values: list[str] = Field(default_factory=list)
    last_event: str | None = None
    repeat_interval: int | None = None


class Button(HueResource):
    """A single button on a switch or dimmer."""

    metadata: Metadata = Metadata()
    button: ButtonReading | None = None

    @property
    def last_event(self) -> str | None:
        """The most recent button event, e.g. ``initial_press``."""
        if self.button is None:
            return None
        if self.button.last_event is not None:
            return self.button.last_event
        report = self.button.button_report
        return report.event if report is not None else None


class ContactReport(HueModel):
    """The most recent contact-sensor transition."""

    changed: AwareDatetime | None = None
    state: str | None = None


class Contact(ToggleableSensor):
    """A contact sensor, e.g. on a door or window."""

    contact_report: ContactReport | None = None

    @property
    def is_contact(self) -> bool:
        """Whether the contact is currently closed."""
        return (
            self.contact_report is not None and self.contact_report.state == "contact"
        )


class RelativeRotaryRotation(HueModel):
    """One measured rotary movement."""

    direction: str = ""
    steps: int = 0
    duration: int = 0


class RelativeRotaryEvent(HueModel):
    """A rotary action and the movement it describes."""

    action: str = ""
    rotation: RelativeRotaryRotation = RelativeRotaryRotation()


class RelativeRotaryReport(RelativeRotaryEvent):
    """A timestamped rotary action from current bridge firmware."""

    updated: AwareDatetime | None = None


class RelativeRotaryReading(HueModel):
    """Current rotary state, including the deprecated legacy event shape."""

    rotary_report: RelativeRotaryReport | None = None
    last_event: RelativeRotaryEvent | None = None

    @property
    def value(self) -> RelativeRotaryReport | RelativeRotaryEvent | None:
        """Prefer the timestamped report, falling back to the legacy event."""
        return self.rotary_report or self.last_event


class RelativeRotary(HueResource):
    """A relative rotary input service, such as a Hue Tap Dial."""

    relative_rotary: RelativeRotaryReading | None = None
