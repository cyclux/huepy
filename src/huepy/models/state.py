"""Composition of a light-state change into one bridge payload.

The v2 API accepts every attribute of a light in a single PUT, so "switch on,
dim to 40%, go warm, take two seconds" is one round trip -- not four. This
module is where that composition lives, and it is the only place brightness is
clamped.

A colour may be spelled as CIE ``xy``, as 8-bit ``rgb`` or as a ``hex_color``
string, and a colour temperature as ``mirek`` or ``kelvin``. Every spelling is
resolved to the two forms the bridge understands before anything else happens,
so the rest of the library -- and the conflict checks below -- only ever see
``xy`` and ``mirek``.

Typical usage example:

    payload = build_light_payload(on=True, brightness=40, mirek=400)
    payload = build_light_payload(hex_color="#ff8800", gamut=GAMUT_C)
"""

from typing import Any

from huepy.color import (
    MIREK_MAX,
    MIREK_MIN,
    Gamut,
    clamp_to_gamut,
    hex_to_rgb,
    kelvin_to_mirek,
    rgb_to_xy,
)

BRIGHTNESS_MIN = 0.0
"""Lowest brightness percentage the bridge accepts."""

BRIGHTNESS_MAX = 100.0
"""Highest brightness percentage the bridge accepts."""

SPEED_MIN = 0.0
SPEED_MAX = 1.0
"""Dynamics and effect speed run from 0.0 (slowest) to 1.0 (fastest)."""

MILLISECONDS_PER_SECOND = 1000
MAX_TRANSITION_MILLISECONDS = 6_000_000
"""Transitions are given in seconds but sent to the bridge in milliseconds."""

MAX_TIMED_EFFECT_MS = 21_600_000
"""A timed effect (sunrise/sunset) runs for at most six hours."""


def _clamp_brightness(brightness: float) -> float:
    """Clamp a brightness percentage into the range the bridge accepts."""
    return max(BRIGHTNESS_MIN, min(BRIGHTNESS_MAX, brightness))


def _clamp_mirek(mirek: int) -> int:
    """Clamp a colour temperature into the 153-500 mirek range the bridge takes."""
    return min(max(mirek, MIREK_MIN), MIREK_MAX)


def _check_speed(speed: float) -> float:
    """Return ``speed`` if it is a valid 0.0-1.0 fraction, else raise.

    Args:
        speed: The speed fraction to validate.

    Returns:
        The speed unchanged.

    Raises:
        ValueError: If it falls outside 0.0-1.0.

    """
    if not SPEED_MIN <= speed <= SPEED_MAX:
        msg = f"speed must be between {SPEED_MIN} and {SPEED_MAX}, got {speed}"
        raise ValueError(msg)
    return speed


def _supplied(candidates: tuple[tuple[str, object], ...]) -> list[str]:
    """Name the arguments that were actually given.

    Args:
        candidates: ``(name, value)`` pairs, in the order they should be
            reported in an error message.

    Returns:
        The names whose value is not None.

    """
    return [name for name, value in candidates if value is not None]


def _reject_duplicates(given: list[str], kind: str) -> None:
    """Reject a call that spelled the same thing more than once.

    Args:
        given: The names of the arguments that were supplied for one kind of
            state, as returned by :func:`_supplied`.
        kind: What those arguments all describe, for the error message.

    Raises:
        ValueError: If more than one of them was given. Silently preferring
            one spelling over another would apply a colour the caller never
            asked for.

    """
    if len(given) > 1:
        names = " and ".join(given)
        msg = (
            f"A light takes one {kind}, but {names} were given. "
            f"They are different spellings of the same thing; pass one."
        )
        raise ValueError(msg)


def _resolve_xy(
    xy: tuple[float, float] | None,
    rgb: tuple[int, int, int] | None,
    hex_color: str | None,
    gamut: Gamut | None,
) -> tuple[float, float] | None:
    """Reduce the three colour spellings to a single chromaticity.

    Args:
        xy: A colour already given as a CIE ``(x, y)`` pair.
        rgb: A colour given as 8-bit red, green and blue channels.
        hex_color: A colour given as ``"#rrggbb"`` or ``"#rgb"``.
        gamut: The triangle the result is clamped into, if any.

    Returns:
        The chromaticity to send, or None when no colour was given at all.
        Clamping applies to a directly supplied ``xy`` too: a point the light
        cannot reproduce is substituted by the bridge either way, and doing it
        here is the only way the caller learns which colour was actually sent.

    Raises:
        ValueError: If more than one colour spelling was given, or if a
            ``hex_color`` or ``rgb`` value is malformed.

    """
    if hex_color is not None:
        xy = rgb_to_xy(hex_to_rgb(hex_color))
    elif rgb is not None:
        xy = rgb_to_xy(rgb)
    if xy is not None and gamut is not None:
        return clamp_to_gamut(xy, gamut)
    return xy


def build_light_payload(  # noqa: C901, PLR0913 - one PUT carries the whole state
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
) -> dict[str, Any]:
    """Combine every supplied light attribute into one PUT body.

    Anything left as None is omitted, so the bridge changes only what was
    asked for. A call that supplies nothing returns an empty dict, which
    callers treat as "no request needed".

    ``rgb``, ``hex_color`` and ``xy`` are three spellings of one colour, and
    ``kelvin`` and ``mirek`` two spellings of one colour temperature: passing
    two of either is a conflict rather than a refinement.

    Args:
        on: Target power state.
        brightness: Target brightness percentage, clamped to 0-100.
        xy: Target colour as a CIE ``(x, y)`` pair.
        mirek: Target colour temperature; lower is cooler.
        rgb: Target colour as 8-bit ``(red, green, blue)`` channels.
        hex_color: Target colour as ``"#rrggbb"`` or its ``"#rgb"`` shorthand.
        kelvin: Target colour temperature in kelvin; higher is cooler.
        gamut: The triangle the resulting colour is clamped into. Without one,
            an unreachable colour is silently substituted by the bridge.
        transition: How long the change should take, in seconds.
        speed: Speed of the active dynamic palette, from 0.0 to 1.0. Only takes
            effect while the light is running a dynamic scene.

    Returns:
        The payload, in the bridge's shape. Empty when nothing was supplied.

    Raises:
        ValueError: If more than one colour or more than one colour
            temperature is given, if a colour is combined with a colour
            temperature, if a colour value is malformed, if ``transition`` is
            negative or longer than 6,000 seconds, or if ``speed`` is outside
            0.0-1.0.

    """
    # Colour conversions belong here, ahead of the checks below: an rgb, hex
    # or kelvin argument resolves to `xy` or `mirek` and is then indistinguish-
    # able from one the caller supplied directly.
    colors = _supplied((("xy", xy), ("rgb", rgb), ("hex_color", hex_color)))
    temperatures = _supplied((("mirek", mirek), ("kelvin", kelvin)))
    _reject_duplicates(colors, "colour")
    _reject_duplicates(temperatures, "colour temperature")
    if colors and temperatures:
        msg = (
            f"A light takes either a colour ({colors[0]}) or a colour "
            f"temperature ({temperatures[0]}), not both. Pass one of them."
        )
        raise ValueError(msg)
    xy = _resolve_xy(xy, rgb, hex_color, gamut)
    if kelvin is not None:
        mirek = kelvin_to_mirek(kelvin)

    duration: int | None = None
    if transition is not None:
        if transition < 0:
            msg = f"transition must not be negative, got {transition}"
            raise ValueError(msg)
        duration = int(transition * MILLISECONDS_PER_SECOND)
        if duration > MAX_TRANSITION_MILLISECONDS:
            msg = "transition must not exceed 6000 seconds"
            raise ValueError(msg)

    checked_speed = _check_speed(speed) if speed is not None else None

    payload: dict[str, Any] = {}
    if on is not None:
        payload["on"] = {"on": on}
    if brightness is not None:
        payload["dimming"] = {"brightness": _clamp_brightness(brightness)}
    if xy is not None:
        payload["color"] = {"xy": {"x": xy[0], "y": xy[1]}}
    if mirek is not None:
        payload["color_temperature"] = {"mirek": _clamp_mirek(mirek)}
    dynamics: dict[str, Any] = {}
    if duration is not None:
        dynamics["duration"] = duration
    if checked_speed is not None:
        dynamics["speed"] = checked_speed
    if dynamics:
        payload["dynamics"] = dynamics
    return payload


def build_scene_recall(
    action: str = "active",
    *,
    duration: float | None = None,
    brightness: float | None = None,
) -> dict[str, Any]:
    """Compose the ``recall`` PUT body that activates a scene.

    Args:
        action: How to recall it: ``"active"``, ``"dynamic_palette"`` or
            ``"static"``.
        duration: Transition time into the scene, in seconds.
        brightness: A brightness percentage to override the scene's own with,
            clamped to 0-100.

    Returns:
        The payload, in the bridge's shape.

    """
    recall: dict[str, Any] = {"action": action}
    if duration is not None:
        recall["duration"] = int(duration * MILLISECONDS_PER_SECOND)
    if brightness is not None:
        recall["dimming"] = {"brightness": _clamp_brightness(brightness)}
    return {"recall": recall}


def _resolve_colour_and_temperature(  # noqa: PLR0913, PLR0917 - one colour spec, every spelling
    xy: tuple[float, float] | None,
    rgb: tuple[int, int, int] | None,
    hex_color: str | None,
    mirek: int | None,
    kelvin: int | None,
    gamut: Gamut | None,
    subject: str,
) -> tuple[tuple[float, float] | None, int | None]:
    """Resolve the colour spellings to one ``xy`` and one clamped ``mirek``.

    Shared by the effect and powerup builders, which take the same colour or
    colour-temperature the light itself does.

    Args:
        xy: A colour as a CIE ``(x, y)`` pair.
        rgb: A colour as 8-bit red, green and blue channels.
        hex_color: A colour as ``"#rrggbb"`` or ``"#rgb"``.
        mirek: A colour temperature in mirek.
        kelvin: A colour temperature in kelvin.
        gamut: The triangle a colour is clamped into, if any.
        subject: What the parameters describe, for error messages.

    Returns:
        The resolved chromaticity and clamped mirek, either of which may be
        None when nothing of that kind was given.

    Raises:
        ValueError: If more than one colour or temperature is given, or a
            colour is combined with a colour temperature.

    """
    colors = _supplied((("xy", xy), ("rgb", rgb), ("hex_color", hex_color)))
    temperatures = _supplied((("mirek", mirek), ("kelvin", kelvin)))
    _reject_duplicates(colors, "colour")
    _reject_duplicates(temperatures, "colour temperature")
    if colors and temperatures:
        msg = (
            f"{subject} takes either a colour ({colors[0]}) or a colour "
            f"temperature ({temperatures[0]}), not both. Pass one of them."
        )
        raise ValueError(msg)
    resolved_xy = _resolve_xy(xy, rgb, hex_color, gamut)
    resolved_mirek = kelvin_to_mirek(kelvin) if kelvin is not None else mirek
    if resolved_mirek is not None:
        resolved_mirek = _clamp_mirek(resolved_mirek)
    return resolved_xy, resolved_mirek


def build_effect_payload(  # noqa: PLR0913 - one PUT carries the effect and its tint
    effect: str,
    *,
    xy: tuple[float, float] | None = None,
    rgb: tuple[int, int, int] | None = None,
    hex_color: str | None = None,
    mirek: int | None = None,
    kelvin: int | None = None,
    gamut: Gamut | None = None,
    speed: float | None = None,
) -> dict[str, Any]:
    """Compose the ``effects_v2`` PUT body for a light effect.

    The colour, colour temperature and speed are optional parameters the effect
    is tinted and paced by; ``effects_v2`` is the current form and replaces the
    deprecated ``effects`` key.

    Args:
        effect: The effect name, e.g. ``"candle"``. ``"no_effect"`` stops it.
        xy: A tint as a CIE ``(x, y)`` pair.
        rgb: A tint as 8-bit ``(red, green, blue)`` channels.
        hex_color: A tint as ``"#rrggbb"`` or ``"#rgb"``.
        mirek: A tint colour temperature in mirek.
        kelvin: A tint colour temperature in kelvin.
        gamut: The triangle the tint is clamped into, if any.
        speed: How fast the effect runs, from 0.0 to 1.0.

    Returns:
        The payload, in the bridge's shape.

    Raises:
        ValueError: If a colour is combined with a colour temperature, a value
            is malformed, ``speed`` is outside 0.0-1.0, or parameters are given
            for ``"no_effect"``, which takes none.

    """
    resolved_xy, resolved_mirek = _resolve_colour_and_temperature(
        xy, rgb, hex_color, mirek, kelvin, gamut, "An effect"
    )
    parameters: dict[str, Any] = {}
    if resolved_xy is not None:
        parameters["color"] = {"xy": {"x": resolved_xy[0], "y": resolved_xy[1]}}
    if resolved_mirek is not None:
        parameters["color_temperature"] = {"mirek": resolved_mirek}
    if speed is not None:
        parameters["speed"] = _check_speed(speed)
    if effect == "no_effect" and parameters:
        msg = "no_effect stops the running effect and takes no parameters."
        raise ValueError(msg)
    action: dict[str, Any] = {"effect": effect}
    if parameters:
        action["parameters"] = parameters
    return {"effects_v2": {"action": action}}


def build_powerup_payload(  # noqa: PLR0913 - one PUT carries the whole powerup config
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
    gamut: Gamut | None = None,
) -> dict[str, Any]:
    """Compose the ``powerup`` PUT body for what a light does on power return.

    Supplying any of the on/brightness/colour fields configures a custom
    powerup, so ``preset`` is forced to ``"custom"`` whenever one is given; the
    bare presets (``"safety"``, ``"powerfail"``, ``"last_on_state"``) take no
    further configuration.

    Args:
        preset: The powerup behaviour when no custom field is given.
        on: The power state to restore. ``on_mode`` selects how.
        on_mode: How to restore power: ``"on"``, ``"toggle"`` or ``"previous"``.
        brightness: The brightness percentage to restore, clamped to 0-100.
        xy: A colour to restore, as a CIE ``(x, y)`` pair.
        rgb: A colour to restore, as 8-bit ``(red, green, blue)`` channels.
        hex_color: A colour to restore, as ``"#rrggbb"`` or ``"#rgb"``.
        mirek: A colour temperature to restore, in mirek.
        kelvin: A colour temperature to restore, in kelvin.
        gamut: The triangle the colour is clamped into, if any.

    Returns:
        The payload, in the bridge's shape.

    Raises:
        ValueError: If a colour is combined with a colour temperature, or a
            colour value is malformed.

    """
    resolved_xy, resolved_mirek = _resolve_colour_and_temperature(
        xy, rgb, hex_color, mirek, kelvin, gamut, "A powerup"
    )
    has_custom = (
        on is not None
        or on_mode is not None
        or brightness is not None
        or resolved_xy is not None
        or resolved_mirek is not None
    )
    payload: dict[str, Any] = {"preset": "custom" if has_custom else str(preset)}
    if on is not None or on_mode is not None:
        # The nested on-state only applies to mode "on"; "toggle" and "previous"
        # carry no state, so requesting one of them without `on` still works.
        mode = str(on_mode) if on_mode is not None else "on"
        on_config: dict[str, Any] = {"mode": mode}
        if on is not None:
            on_config["on"] = {"on": on}
        payload["on"] = on_config
    if brightness is not None:
        payload["dimming"] = {
            "mode": "dimming",
            "dimming": {"brightness": _clamp_brightness(brightness)},
        }
    if resolved_xy is not None:
        payload["color"] = {
            "mode": "color",
            "color": {"xy": {"x": resolved_xy[0], "y": resolved_xy[1]}},
        }
    elif resolved_mirek is not None:
        payload["color"] = {
            "mode": "color_temperature",
            "color_temperature": {"mirek": resolved_mirek},
        }
    return payload
