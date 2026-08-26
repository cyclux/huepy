"""Models for light and light-level resources.

A parsed light is bound to the client that fetched it, so it acts on itself:

    light = await hue.api.lights.get(light_id)
    await light.set(on=True, brightness=40, mirek=400, transition=2)

A colour may be given in whichever spelling suits the caller. A single light
clamps it to the gamut that particular bulb reports, so an RGB value it cannot
reproduce lands on the nearest colour it can, rather than on whatever the
bridge decides to substitute:

    await light.set_rgb((255, 136, 0))
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, cast, override

from pydantic import (
    AwareDatetime,
    Field,
    ValidationInfo,
    computed_field,
    field_validator,
)

from huepy.color import (
    Gamut,
    clamp_to_gamut,
    gamut_for,
    mirek_to_kelvin,
    rgb_to_hex,
    xy_to_rgb,
)
from huepy.models.common import (
    Color,
    ColorGamut,
    ColorTemperature,
    ColorXY,
    CommandResult,
    Dimming,
    HueModel,
    HueResource,
    NamedResource,
    On,
)
from huepy.models.state import (
    MAX_TIMED_EFFECT_MS,
    MILLISECONDS_PER_SECOND,
    build_effect_payload,
    build_light_payload,
    build_powerup_payload,
)

BREATHE = "breathe"
"""The one alert action the v2 API documents: a single slow pulse."""

_MAX_SIGNAL_COLORS = 2
"""A signal carries at most two colours -- one to blink, two to alternate."""

_COLOUR_SIGNALS = frozenset({"on_off_color", "alternating"})
"""The signals that accept colours; the rest reject them."""

_MAX_BRIGHTNESS_DELTA = 100.0
"""Largest relative brightness step the bridge accepts, in percentage points."""

_MAX_MIREK_DELTA = 950
"""Largest relative colour-temperature step the bridge accepts, in mirek."""


@dataclass(frozen=True)
class LightState:
    """A restorable snapshot of one light's active state."""

    light_id: str
    on: bool
    brightness: float | None
    mirek: int | None
    xy: tuple[float, float] | None


def _as_gamut(gamut: ColorGamut) -> Gamut:
    """Convert a bridge-reported gamut into the colour module's form.

    :class:`~huepy.models.common.ColorGamut` carries each primary as a
    :class:`~huepy.models.common.ColorXY` model, because that is the shape the
    bridge sends. :mod:`huepy.color` is deliberately free of any dependency on
    the model layer and works in plain ``(x, y)`` tuples, so the two meet here.

    Args:
        gamut: The gamut as the bridge reported it.

    Returns:
        The same triangle as a :class:`~huepy.color.Gamut`.

    """
    return Gamut(
        red=(gamut.red.x, gamut.red.y),
        green=(gamut.green.x, gamut.green.y),
        blue=(gamut.blue.x, gamut.blue.y),
    )


class Effect(StrEnum):
    """The dynamic effects Hue lights are documented to run.

    Which of these a given bulb supports is reported in
    :attr:`Effects.effect_values`; a candle effect on a white-only bulb is
    rejected by the bridge. The values are offered as an enum so callers get
    completion and a spelling that cannot drift, but the models keep their
    ``str`` fields so a firmware that adds an effect stays parseable.
    """

    NO_EFFECT = "no_effect"
    CANDLE = "candle"
    FIRE = "fire"
    PRISM = "prism"
    SPARKLE = "sparkle"
    OPAL = "opal"
    GLISTEN = "glisten"
    UNDERWATER = "underwater"
    COSMOS = "cosmos"
    SUNBEAM = "sunbeam"
    ENCHANT = "enchant"


class TimedEffect(StrEnum):
    """A dynamic effect that runs for a set duration, then stops.

    ``SUNRISE`` and ``SUNSET`` fade the light the way daylight does over the
    duration given; ``NO_EFFECT`` stops one that is running. Kept a ``StrEnum``
    for the same reason as :class:`Effect`: a firmware that adds one stays
    parseable.
    """

    NO_EFFECT = "no_effect"
    SUNRISE = "sunrise"
    SUNSET = "sunset"


class Signal(StrEnum):
    """The attention signals a light can display.

    Unlike :meth:`Light.alert`, a signal runs for a duration and can alternate
    between colours: ``ON_OFF`` blinks, ``ON_OFF_COLOR`` blinks in a colour,
    and ``ALTERNATING`` cycles between two. ``NO_SIGNAL`` cancels one.
    """

    NO_SIGNAL = "no_signal"
    ON_OFF = "on_off"
    ON_OFF_COLOR = "on_off_color"
    ALTERNATING = "alternating"


class Effects(HueModel):
    """The effect a light is running, and the effects it could run.

    ``status`` stays a plain ``str`` rather than an :class:`Effect` on
    purpose: an effect this library has never heard of would otherwise turn
    a firmware update into a parse failure for the whole light.
    """

    status: str | None = None
    status_values: list[str] = Field(default_factory=list)
    effect_values: list[str] = Field(default_factory=list)


class TimedEffects(Effects):
    """An effect that runs for a fixed time and then stops.

    Identical to :class:`Effects` but for the countdown: ``duration`` is the
    time left in milliseconds, not the time the effect was started with.
    """

    duration: int | None = None


class GradientPoint(HueModel):
    """One colour stop of a gradient."""

    color: Color


class Gradient(HueModel):
    """The colour ramp a gradient strip or lightstrip renders.

    ``points`` holds the stops the light was given, which is at most
    ``points_capable`` of them; the light interpolates between them across
    ``pixel_count`` physical LEDs.
    """

    points: list[GradientPoint] = Field(default_factory=list)
    mode: str | None = None
    points_capable: int | None = None
    pixel_count: int | None = None


class Powerup(HueModel):
    """What a light does when mains power returns.

    Without this, a bulb switched on at the wall comes back at whatever it was
    last set to. A configured powerup makes that deterministic.
    """

    preset: str | None = None
    configured: bool | None = None
    on: On | None = None
    dimming: Dimming | None = None

    @field_validator("on", "dimming", mode="before")
    @classmethod
    def _unwrap_mode(cls, value: object, info: ValidationInfo) -> object:
        """Unwrap the mode envelope the bridge puts around powerup state.

        Powerup is the one place the bridge nests state a second time, as
        ``{"mode": "on", "on": {"on": true}}``. The envelope says only which
        of the presets the field belongs to, which ``preset`` already reports,
        so it is dropped here and the field keeps the same
        :class:`~huepy.models.common.On` and
        :class:`~huepy.models.common.Dimming` shapes as every other resource.

        Args:
            value: The raw value the bridge sent for this field.
            info: Validation context, read for the name of the field being
                validated -- the inner key always repeats it.

        Returns:
            The inner state when the value is mode-wrapped, otherwise the
            value untouched, leaving an unfamiliar shape to the field's own
            validation.

        """
        if not isinstance(value, dict):
            return value
        wrapper = cast("dict[str, object]", value)
        field = info.field_name or ""
        if "mode" in wrapper:
            # A mode of "previous" carries no nested state at all -- the bridge
            # is saying "whatever it was before" -- so there is nothing to
            # parse and the field is left unset. Returning the wrapper here
            # instead would fail the inner model's required fields and take
            # the whole light parse down with it.
            return wrapper.get(field)
        return wrapper


class Alert(HueModel):
    """The alert actions a light accepts.

    An alert is a one-shot attention signal, so the bridge reports only what
    can be asked for, never a current state.
    """

    action_values: list[str] = Field(default_factory=list)


class Signaling(HueModel):
    """The signals a light can display, and the one it is displaying.

    ``status`` is left as a raw mapping: it carries a signal name, an estimated
    end timestamp and the colours in play, and its shape has changed across
    firmware versions.
    """

    status: dict[str, Any] | None = None
    signal_values: list[str] = Field(default_factory=list)


class LightCommands(HueResource):
    """Commands shared by every resource that behaves like a light.

    :meth:`set` composes every attribute into a single PUT; the convenience
    methods are thin wrappers over it, so there is one code path to the
    bridge. Rooms and zones join in by overriding :meth:`_command_path`.
    """

    def _command_path(self) -> str:
        """Return the endpoint light commands are sent to.

        Returns:
            This resource's own path. Rooms and zones override this to address
            their grouped_light service instead.

        """
        return self._path

    def _default_gamut(self) -> Gamut | None:
        """Return the gamut colours are clamped to when the caller names none.

        Returns:
            None. Only a resource that knows which bulb it is can answer this,
            and :class:`Light` overrides it to do so; a group spans bulbs with
            different gamuts, so it has no single triangle to clamp against.

        """
        return None

    async def set(  # noqa: PLR0913 - one PUT carries the whole state
        self,
        *,
        on: bool | None = None,
        brightness: float | None = None,
        xy: tuple[float, float] | None = None,
        mirek: int | None = None,
        rgb: tuple[int, int, int] | None = None,
        hex_color: str | None = None,
        kelvin: int | None = None,
        gamut: Gamut | None = None,
        transition: float | None = None,
        speed: float | None = None,
    ) -> CommandResult:
        """Apply a whole state change in one request.

        Args:
            on: Target power state.
            brightness: Target brightness percentage, clamped to 0-100.
            xy: Target colour as a CIE ``(x, y)`` pair.
            mirek: Target colour temperature; lower is cooler.
            rgb: Target colour as 8-bit ``(red, green, blue)`` channels.
            hex_color: Target colour as ``"#rrggbb"`` or its ``"#rgb"``
                shorthand.
            kelvin: Target colour temperature in kelvin; higher is cooler.
            gamut: The triangle the colour is clamped into. Defaults to the
                one this resource reports, if it reports one at all.
            transition: How long the change should take, in seconds.
            speed: Speed of the active dynamic palette, from 0.0 to 1.0; only
                takes effect while a dynamic scene is running.

        Returns:
            A CommandResult containing the bridge references, or
            ``CommandResult(sent=False)`` when no argument was supplied.

        Raises:
            DetachedResourceError: If this resource is not bound to a client.
            ValueError: If more than one colour or more than one colour
                temperature is given, if a colour is combined with a colour
                temperature, if ``transition`` is negative or longer than
                6,000 seconds, or if ``speed`` is outside 0.0-1.0.
            HueResponseError: If the bridge rejects the change.

        """
        payload = build_light_payload(
            on=on,
            brightness=brightness,
            xy=xy,
            mirek=mirek,
            rgb=rgb,
            hex_color=hex_color,
            kelvin=kelvin,
            gamut=gamut if gamut is not None else self._default_gamut(),
            transition=transition,
            speed=speed,
        )
        if not payload:
            return CommandResult(sent=False)
        return await self._put(self._command_path(), payload)

    async def turn_on(
        self,
        *,
        transition: float | None = None,
    ) -> CommandResult:
        """Switch this resource on.

        Args:
            transition: How long the change should take, in seconds.

        Returns:
            A CommandResult containing the bridge references affected.

        """
        return await self.set(on=True, transition=transition)

    async def turn_off(
        self,
        *,
        transition: float | None = None,
    ) -> CommandResult:
        """Switch this resource off.

        Args:
            transition: How long the change should take, in seconds.

        Returns:
            A CommandResult containing the bridge references affected.

        """
        return await self.set(on=False, transition=transition)

    async def set_brightness(
        self,
        brightness: float,
        *,
        transition: float | None = None,
    ) -> CommandResult:
        """Set brightness, clamped to 0-100.

        Args:
            brightness: Target brightness percentage.
            transition: How long the change should take, in seconds.

        Returns:
            A CommandResult containing the bridge references affected.

        """
        return await self.set(brightness=brightness, transition=transition)

    async def set_color(
        self,
        x: float,
        y: float,
        *,
        transition: float | None = None,
    ) -> CommandResult:
        """Set colour from CIE xy coordinates.

        Args:
            x: CIE x coordinate.
            y: CIE y coordinate.
            transition: How long the change should take, in seconds.

        Returns:
            A CommandResult containing the bridge references affected.

        """
        return await self.set(xy=(x, y), transition=transition)

    async def set_rgb(
        self,
        rgb: tuple[int, int, int],
        *,
        transition: float | None = None,
    ) -> CommandResult:
        """Set colour from 8-bit RGB channels.

        Args:
            rgb: The red, green and blue channels, each in the range 0-255.
            transition: How long the change should take, in seconds.

        Returns:
            A CommandResult containing the bridge references affected.

        Raises:
            ValueError: If any channel falls outside 0-255.

        """
        return await self.set(rgb=rgb, transition=transition)

    async def set_color_temperature(
        self,
        mirek: int,
        *,
        transition: float | None = None,
    ) -> CommandResult:
        """Set colour temperature in mirek.

        Args:
            mirek: Colour temperature; lower is cooler.
            transition: How long the change should take, in seconds.

        Returns:
            A CommandResult containing the bridge references affected.

        """
        return await self.set(mirek=mirek, transition=transition)

    async def set_kelvin(
        self,
        kelvin: int,
        *,
        transition: float | None = None,
    ) -> CommandResult:
        """Set colour temperature in kelvin.

        Args:
            kelvin: Colour temperature in kelvin; higher is cooler. Values
                outside the 2000-6536 K the bridge accepts are clamped to the
                nearest endpoint rather than rejected.
            transition: How long the change should take, in seconds.

        Returns:
            A CommandResult containing the bridge references affected.

        Raises:
            ValueError: If ``kelvin`` is zero or negative.

        """
        return await self.set(kelvin=kelvin, transition=transition)


class Light(LightCommands, NamedResource):
    """A single controllable light.

    Beyond on/off and colour, a light may run an effect, render a gradient,
    signal, or hold a powerup behaviour. Each of those is optional: a plain
    white bulb reports none of them.
    """

    on: On | None = None
    dimming: Dimming | None = None
    color: Color | None = None
    color_temperature: ColorTemperature | None = None
    mode: str | None = None
    effects: Effects | None = None
    timed_effects: TimedEffects | None = None
    gradient: Gradient | None = None
    powerup: Powerup | None = None
    # `alert` is the payload key, but the name is taken by the command of the
    # same name below, so the field is aliased rather than renaming a command
    # that reads well at the call site: `await light.alert()`.
    alert_actions: Alert | None = Field(default=None, alias="alert")
    signaling: Signaling | None = None

    @property
    def is_on(self) -> bool:
        """Whether the light is currently powered on."""
        return self.on is not None and self.on.on

    @property
    def brightness(self) -> float | None:
        """Current brightness percentage, or None if the light cannot dim."""
        return self.dimming.brightness if self.dimming is not None else None

    @property
    def mirek(self) -> int | None:
        """Current colour temperature in mirek, or None if unsupported or unset."""
        if self.color_temperature is None:
            return None
        return self.color_temperature.mirek

    @computed_field
    @property
    def kelvin(self) -> int | None:
        """Current valid colour temperature in kelvin, when available."""
        temperature = self.color_temperature
        if (
            temperature is None
            or not temperature.mirek_valid
            or temperature.mirek is None
        ):
            return None
        return mirek_to_kelvin(temperature.mirek)

    @computed_field
    @property
    def rgb(self) -> tuple[int, int, int] | None:
        """Current colour in RGB, when colour and brightness are available."""
        if self.color is None or self.brightness is None:
            return None
        return xy_to_rgb(
            (self.color.xy.x, self.color.xy.y),
            brightness=self.brightness,
        )

    @computed_field
    @property
    def hex_color(self) -> str | None:
        """Current colour as a hex string, mirroring the ``hex_color=`` setter.

        Named for the argument :meth:`set` takes rather than for the notation,
        so reading a colour and writing it back use the same word.
        """
        rgb = self.rgb
        return None if rgb is None else rgb_to_hex(rgb)

    def capture(self) -> LightState:
        """Capture the valid active state needed to restore this light."""
        temperature = self.color_temperature
        in_ct_mode = temperature is not None and bool(temperature.mirek_valid)
        return LightState(
            light_id=self.id,
            on=self.is_on,
            brightness=self.brightness,
            mirek=temperature.mirek if in_ct_mode and temperature is not None else None,
            xy=(
                (self.color.xy.x, self.color.xy.y)
                if self.color is not None and not in_ct_mode
                else None
            ),
        )

    async def restore(
        self,
        state: LightState,
        *,
        transition: float | None = None,
    ) -> CommandResult:
        """Restore a state captured from this same light."""
        if state.light_id != self.id:
            msg = f"state belongs to light {state.light_id}, not {self.id}"
            raise ValueError(msg)
        return await self.set(
            on=state.on,
            brightness=state.brightness,
            mirek=state.mirek,
            xy=state.xy,
            transition=transition,
        )

    @property
    def effect(self) -> str | None:
        """Name of the effect running now, or None if the light runs none.

        Stays a ``str`` because the bridge may name an effect this library's
        :class:`Effect` enum predates.
        """
        return self.effects.status if self.effects is not None else None

    @property
    def is_gradient(self) -> bool:
        """Whether this light renders a gradient across several colour points."""
        return self.gradient is not None and bool(self.gradient.points)

    @override
    def _default_gamut(self) -> Gamut | None:
        """Return the triangle of colours this particular bulb can reproduce.

        Returns:
            The gamut the light reports, converted for :mod:`huepy.color`.
            Falls back to the standard gamut for the light's ``gamut_type``
            when the corners themselves are absent, and to None when the light
            reports no colour support or a type this library does not know --
            None means "send the colour unclamped", the previous behaviour.

        """
        if self.color is None:
            return None
        if self.color.gamut is not None:
            return _as_gamut(self.color.gamut)
        return gamut_for(self.color.gamut_type)

    async def set_effect(  # noqa: PLR0913 - one PUT carries the effect and its tint
        self,
        effect: Effect | str,
        *,
        xy: tuple[float, float] | None = None,
        rgb: tuple[int, int, int] | None = None,
        hex_color: str | None = None,
        mirek: int | None = None,
        kelvin: int | None = None,
        speed: float | None = None,
    ) -> CommandResult:
        """Start a dynamic effect, or stop the running one.

        Sends the current ``effects_v2`` form, so an effect can be tinted with a
        colour or colour temperature and paced with a speed.

        Args:
            effect: The effect to run, either an :class:`Effect` member or its
                raw name. ``Effect.NO_EFFECT`` stops whatever is running.
            xy: A tint as a CIE ``(x, y)`` pair.
            rgb: A tint as 8-bit ``(red, green, blue)`` channels.
            hex_color: A tint as ``"#rrggbb"`` or its ``"#rgb"`` shorthand.
            mirek: A tint colour temperature in mirek.
            kelvin: A tint colour temperature in kelvin.
            speed: How fast the effect runs, from 0.0 to 1.0.

        Returns:
            A CommandResult containing the bridge references affected.

        Raises:
            DetachedResourceError: If this resource is not bound to a client.
            ValueError: If a colour is combined with a colour temperature,
                ``speed`` is outside 0.0-1.0, or parameters are given for
                ``Effect.NO_EFFECT``.
            HueResponseError: If the light does not support that effect.

        """
        payload = build_effect_payload(
            str(effect),
            xy=xy,
            rgb=rgb,
            hex_color=hex_color,
            mirek=mirek,
            kelvin=kelvin,
            speed=speed,
            gamut=self._default_gamut(),
        )
        return await self._put(self._command_path(), payload)

    async def set_timed_effect(
        self,
        effect: TimedEffect | str,
        *,
        duration: float | None = None,
    ) -> CommandResult:
        """Run a timed effect, such as a sunrise or sunset fade.

        Args:
            effect: The effect to run, a :class:`TimedEffect` member or its raw
                name. ``TimedEffect.NO_EFFECT`` stops one that is running.
            duration: How long the fade lasts, in seconds. Required for a real
                effect; at most six hours.

        Returns:
            A CommandResult containing the bridge references affected.

        Raises:
            DetachedResourceError: If this resource is not bound to a client.
            ValueError: If ``duration`` is negative or longer than six hours.
            HueResponseError: If the light does not support timed effects.

        """
        timed: dict[str, Any] = {"effect": str(effect)}
        if duration is not None:
            if duration < 0:
                msg = f"duration must not be negative, got {duration}"
                raise ValueError(msg)
            milliseconds = int(duration * MILLISECONDS_PER_SECOND)
            if milliseconds > MAX_TIMED_EFFECT_MS:
                msg = "duration must not exceed six hours"
                raise ValueError(msg)
            timed["duration"] = milliseconds
        return await self._put(self._command_path(), {"timed_effects": timed})

    async def set_gradient(
        self,
        colors: list[tuple[float, float]],
        *,
        mode: str | None = None,
    ) -> CommandResult:
        """Paint a gradient across the light's colour points.

        Args:
            colors: The stops, in order, each a CIE ``(x, y)`` pair. The light
                interpolates between them; it accepts at most
                ``gradient.points_capable`` of them.
            mode: How the stops are laid out over the pixels, for example
                ``"interpolated_palette"``. Left to the light when omitted.

        Returns:
            A CommandResult containing the bridge references affected.

        Raises:
            DetachedResourceError: If this resource is not bound to a client.
            HueResponseError: If the light renders no gradient, or was given
                more points than it supports.

        """
        gradient: dict[str, Any] = {
            "points": [{"color": {"xy": {"x": x, "y": y}}} for x, y in colors],
        }
        if mode is not None:
            gradient["mode"] = mode
        return await self._put(self._command_path(), {"gradient": gradient})

    async def set_powerup(  # noqa: PLR0913 - one PUT carries the whole config
        self,
        preset: str = "custom",
        *,
        on: bool | None = None,
        on_mode: str | None = None,
        brightness: float | None = None,
        xy: tuple[float, float] | None = None,
        rgb: tuple[int, int, int] | None = None,
        hex_color: str | None = None,
        mirek: int | None = None,
        kelvin: int | None = None,
    ) -> CommandResult:
        """Choose what the light does when mains power returns.

        Passing any of the on/brightness/colour fields configures a custom
        powerup and forces ``preset`` to ``"custom"``. With none, the bare
        presets apply as-is.

        Args:
            preset: The behaviour when no custom field is given, one of
                ``"safety"``, ``"powerfail"``, ``"last_on_state"`` or
                ``"custom"``.
            on: The power state to restore. ``on_mode`` selects how.
            on_mode: How to restore power: ``"on"``, ``"toggle"`` or
                ``"previous"``. Defaults to ``"on"`` when ``on`` is given.
            brightness: The brightness percentage to restore, clamped to 0-100.
            xy: A colour to restore, as a CIE ``(x, y)`` pair.
            rgb: A colour to restore, as 8-bit ``(red, green, blue)`` channels.
            hex_color: A colour to restore, as ``"#rrggbb"`` or ``"#rgb"``.
            mirek: A colour temperature to restore, in mirek.
            kelvin: A colour temperature to restore, in kelvin.

        Returns:
            A CommandResult containing the bridge references affected.

        Raises:
            DetachedResourceError: If this resource is not bound to a client.
            ValueError: If a colour is combined with a colour temperature, or a
                colour value is malformed.
            HueResponseError: If the light does not support the configuration.

        """
        payload = build_powerup_payload(
            preset,
            on=on,
            on_mode=on_mode,
            brightness=brightness,
            xy=xy,
            rgb=rgb,
            hex_color=hex_color,
            mirek=mirek,
            kelvin=kelvin,
            gamut=self._default_gamut(),
        )
        return await self._put(self._command_path(), {"powerup": payload})

    async def signal(
        self,
        signal: Signal | str,
        *,
        duration: float | None = None,
        colors: list[tuple[float, float]] | None = None,
    ) -> CommandResult:
        """Display an attention signal for a set time.

        Unlike :meth:`alert`, a signal runs for ``duration`` and can carry
        colours: ``ON_OFF_COLOR`` blinks in one, ``ALTERNATING`` cycles between
        two.

        Args:
            signal: The signal to show, a :class:`Signal` member or its raw
                name. ``Signal.NO_SIGNAL`` cancels one that is running.
            duration: How long the signal lasts, in seconds. Required for a real
                signal.
            colors: One or two colours, each a CIE ``(x, y)`` pair, clamped to
                the light's gamut. Only ``ON_OFF_COLOR`` and ``ALTERNATING``
                take them.

        Returns:
            A CommandResult containing the bridge references affected.

        Raises:
            DetachedResourceError: If this resource is not bound to a client.
            ValueError: If more than two colours are given, or colours are given
                for a signal that takes none.
            HueResponseError: If the light does not support signalling.

        """
        signaling: dict[str, Any] = {"signal": str(signal)}
        if duration is not None:
            signaling["duration"] = int(duration * MILLISECONDS_PER_SECOND)
        if colors:
            if len(colors) > _MAX_SIGNAL_COLORS:
                msg = f"a signal takes at most two colours, got {len(colors)}"
                raise ValueError(msg)
            if str(signal) not in _COLOUR_SIGNALS:
                msg = f"the {signal} signal takes no colours"
                raise ValueError(msg)
            signaling["colors"] = [
                {"xy": {"x": point[0], "y": point[1]}}
                for point in self._clamp_points(colors)
            ]
        return await self._put(self._command_path(), {"signaling": signaling})

    def _clamp_points(
        self,
        points: list[tuple[float, float]],
    ) -> list[tuple[float, float]]:
        """Clamp colour points to this resource's gamut, when it reports one."""
        gamut = self._default_gamut()
        if gamut is None:
            return points
        return [clamp_to_gamut(point, gamut) for point in points]

    async def identify(self) -> CommandResult:
        """Ask the light to identify itself with a short breathe cycle.

        Returns:
            A CommandResult containing the bridge references affected.

        Raises:
            DetachedResourceError: If this resource is not bound to a client.
            HueResponseError: If the light does not support identify.

        """
        payload: dict[str, Any] = {"identify": {"action": "identify"}}
        return await self._put(self._command_path(), payload)

    async def adjust_brightness(self, delta: float) -> CommandResult:
        """Nudge brightness up or down without knowing its current value.

        Args:
            delta: The percentage-point change; positive brightens, negative
                dims, zero halts an in-progress dimming. Magnitude is clamped to
                100.

        Returns:
            A CommandResult containing the bridge references affected.

        Raises:
            DetachedResourceError: If this resource is not bound to a client.
            HueResponseError: If the light does not support relative dimming.

        """
        action = "stop" if delta == 0 else ("up" if delta > 0 else "down")
        dimming: dict[str, Any] = {"action": action}
        if delta != 0:
            dimming["brightness_delta"] = min(abs(delta), _MAX_BRIGHTNESS_DELTA)
        return await self._put(
            self._command_path(),
            {"dimming_delta": dimming},
        )

    async def adjust_color_temperature(self, mirek_delta: int) -> CommandResult:
        """Nudge colour temperature warmer or cooler by a relative amount.

        Args:
            mirek_delta: The mirek change; positive warms, negative cools, zero
                halts an in-progress change. Magnitude is clamped to 950.

        Returns:
            A CommandResult containing the bridge references affected.

        Raises:
            DetachedResourceError: If this resource is not bound to a client.
            HueResponseError: If the light has no adjustable colour temperature.

        """
        action = "stop" if mirek_delta == 0 else ("up" if mirek_delta > 0 else "down")
        delta: dict[str, Any] = {"action": action}
        if mirek_delta != 0:
            delta["mirek_delta"] = min(abs(mirek_delta), _MAX_MIREK_DELTA)
        return await self._put(
            self._command_path(),
            {"color_temperature_delta": delta},
        )

    async def alert(self) -> CommandResult:
        """Flash the light once to identify it.

        The light returns to its previous state on its own, so nothing needs
        restoring afterwards.

        Returns:
            A CommandResult containing the bridge references affected.

        Raises:
            DetachedResourceError: If this resource is not bound to a client.
            HueResponseError: If the bridge rejects the alert.

        """
        payload: dict[str, Any] = {"alert": {"action": BREATHE}}
        return await self._put(self._command_path(), payload)


class GroupedColor(HueModel):
    """A group's aggregate colour, which the bridge may report as empty."""

    xy: ColorXY | None = None


class GroupedLight(LightCommands):
    """The aggregate light service of a room or zone.

    Carries no `metadata` of its own -- the name lives on the owning room.
    """

    on: On | None = None
    dimming: Dimming | None = None
    color: GroupedColor | None = None
    color_temperature: ColorTemperature | None = None

    @property
    def is_on(self) -> bool:
        """Whether any light in the group is currently powered on."""
        return self.on is not None and self.on.on


class LightLevelReport(HueModel):
    """The most recent ambient-light reading and bridge timestamp."""

    changed: AwareDatetime | None = None
    light_level: int | None = None


class LightLevelReading(HueModel):
    """A light-level measurement in 10000*log10(lux) + 1 units."""

    light_level: int | None = None
    light_level_valid: bool | None = None
    light_level_report: LightLevelReport | None = None


class LightLevel(HueResource):
    """An ambient light-level sensor."""

    enabled: bool = True
    light: LightLevelReading | None = None

    @property
    def level(self) -> int | None:
        """The current raw light level, or None if unavailable."""
        return self.light.light_level if self.light is not None else None

    @computed_field
    @property
    def lux(self) -> float | None:
        """Current illuminance in lux, or None for an invalid reading."""
        if (
            self.light is None
            or not self.light.light_level_valid
            or self.light.light_level is None
        ):
            return None
        return 10 ** ((self.light.light_level - 1) / 10_000)


class GroupedLightLevel(HueResource):
    """An aggregate of several light-level sensors."""

    enabled: bool = True
    light: LightLevelReading | None = None
