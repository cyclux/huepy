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

from huepy.color import Gamut, clamp_to_gamut, hex_to_rgb, kelvin_to_mirek, rgb_to_xy

BRIGHTNESS_MIN = 0.0
"""Lowest brightness percentage the bridge accepts."""

BRIGHTNESS_MAX = 100.0
"""Highest brightness percentage the bridge accepts."""

MILLISECONDS_PER_SECOND = 1000
"""Transitions are given in seconds but sent to the bridge in milliseconds."""


def _clamp_brightness(brightness: float) -> float:
    """Clamp a brightness percentage into the range the bridge accepts."""
    return max(BRIGHTNESS_MIN, min(BRIGHTNESS_MAX, brightness))


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


def build_light_payload(  # noqa: PLR0913 - one PUT carries the whole state
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

    Returns:
        The payload, in the bridge's shape. Empty when nothing was supplied.

    Raises:
        ValueError: If more than one colour or more than one colour
            temperature is given, if a colour is combined with a colour
            temperature, if a colour value is malformed, or if ``transition``
            is negative.

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

    if transition is not None and transition < 0:
        msg = f"transition must not be negative, got {transition}"
        raise ValueError(msg)

    payload: dict[str, Any] = {}
    if on is not None:
        payload["on"] = {"on": on}
    if brightness is not None:
        payload["dimming"] = {"brightness": _clamp_brightness(brightness)}
    if xy is not None:
        payload["color"] = {"xy": {"x": xy[0], "y": xy[1]}}
    if mirek is not None:
        payload["color_temperature"] = {"mirek": mirek}
    if transition is not None:
        duration = int(transition * MILLISECONDS_PER_SECOND)
        payload["dynamics"] = {"duration": duration}
    return payload
