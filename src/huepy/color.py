"""Colour conversion between hex, sRGB, CIE xy and mirek for the Hue v2 API.

Every function here is pure: no I/O, no async, and no dependency on the rest of
huepy, so a colour can be prepared long before a bridge is reached. The bridge
silently mangles an ``xy`` pair that falls outside a light's reachable gamut,
so :func:`clamp_to_gamut` should be applied whenever the light reports a
``gamut_type``.

Typical usage example:

    xy = rgb_to_xy(hex_to_rgb("#ff8800"))
    xy = clamp_to_gamut(xy, GAMUT_C)
    warm = kelvin_to_mirek(2700)
"""

import math
import re
from typing import Final, NamedTuple

__all__ = [
    "GAMUT_A",
    "GAMUT_B",
    "GAMUT_C",
    "MIREK_MAX",
    "MIREK_MIN",
    "Gamut",
    "clamp_to_gamut",
    "gamut_for",
    "hex_to_rgb",
    "kelvin_to_mirek",
    "mirek_to_kelvin",
    "rgb_to_hex",
    "rgb_to_xy",
    "xy_to_rgb",
]

type _Point = tuple[float, float]
type _Vector = tuple[float, float, float]
type _Matrix = tuple[_Vector, _Vector, _Vector]


class Gamut(NamedTuple):
    """The triangle of chromaticities a light can actually reproduce.

    Each corner is a CIE 1931 ``(x, y)`` chromaticity. Any point inside the
    triangle is reachable; anything outside has to be clamped, because the
    bridge accepts out-of-gamut values and then renders something else.

    Attributes:
        red: The red primary, as ``(x, y)``.
        green: The green primary, as ``(x, y)``.
        blue: The blue primary, as ``(x, y)``.

    """

    red: _Point
    green: _Point
    blue: _Point


#: Legacy LivingColors fixtures, and the first-generation Hue Iris/Bloom.
GAMUT_A: Final = Gamut(
    red=(0.704, 0.296),
    green=(0.2151, 0.7106),
    blue=(0.138, 0.08),
)

#: Older colour bulbs (LCT001 and relatives); noticeably weak in the greens.
GAMUT_B: Final = Gamut(
    red=(0.675, 0.322),
    green=(0.409, 0.518),
    blue=(0.167, 0.04),
)

#: Current colour bulbs and light strips; the widest of the three.
GAMUT_C: Final = Gamut(
    red=(0.692, 0.308),
    green=(0.17, 0.7),
    blue=(0.153, 0.048),
)

_GAMUTS_BY_TYPE: Final[dict[str, Gamut]] = {
    "A": GAMUT_A,
    "B": GAMUT_B,
    "C": GAMUT_C,
}

#: Coolest colour temperature the bridge accepts, in mirek (about 6536 K).
MIREK_MIN: Final = 153

#: Warmest colour temperature the bridge accepts, in mirek (2000 K).
MIREK_MAX: Final = 500

# Mirek is reciprocal megakelvin: mirek = 1e6 / kelvin.
_MIREK_NUMERATOR: Final = 1_000_000

# The sRGB transfer function, as specified by IEC 61966-2-1.
_GAMMA_THRESHOLD: Final = 0.04045
_LINEAR_THRESHOLD: Final = 0.0031308
_GAMMA_EXPONENT: Final = 2.4
_GAMMA_OFFSET: Final = 0.055
_GAMMA_SCALE: Final = 1.055
_LINEAR_SLOPE: Final = 12.92

_CHANNEL_MIN: Final = 0
_CHANNEL_MAX: Final = 255

_BRIGHTNESS_MIN: Final = 0.0
_BRIGHTNESS_MAX: Final = 100.0

# Slack on the in/out test, so a point the projection lands exactly on an edge
# is not pushed back out again by floating-point noise. At this scale it is
# some six orders of magnitude below the precision xy values carry.
_GAMUT_TOLERANCE: Final = 1e-9

_SHORT_HEX_LENGTH: Final = 3
_FULL_HEX_LENGTH: Final = 6
_HEX_DIGITS: Final = re.compile(r"[0-9a-fA-F]+")

# The Wide Gamut RGB (D65) matrix Philips documents for Hue. Its rows sum to
# (0.95049, 1.0, 1.08884), which is exactly the D65 white point, so pure white
# lands on (0.3127, 0.3290) as it should.
_RGB_TO_XYZ: Final[_Matrix] = (
    (0.649926, 0.103455, 0.197109),
    (0.234327, 0.743075, 0.022598),
    (0.000000, 0.053077, 1.035763),
)

# The exact numerical inverse of _RGB_TO_XYZ. Philips publishes a version of
# this rounded to three decimals, which is not a true inverse and costs about
# 4e-4 per channel on a round trip; the values below round trip to within
# floating-point noise.
_XYZ_TO_RGB: Final[_Matrix] = (
    (1.611756819, -0.202804893, -0.302297717),
    (-0.509057145, 1.411913583, 0.066070444),
    (0.026086302, -0.072352592, 0.962086094),
)


def hex_to_rgb(value: str) -> tuple[int, int, int]:
    """Parse a hex colour string into 8-bit RGB channels.

    Both the six-digit form and the three-digit shorthand are accepted, with
    or without a leading ``#``: ``"#ff8800"``, ``"ff8800"`` and ``"#f80"`` all
    describe the same colour.

    Args:
        value: The hex colour string. Surrounding whitespace is ignored and
            the digits are case-insensitive.

    Returns:
        The red, green and blue channels, each in the range 0-255.

    Raises:
        ValueError: If the string is not 3 or 6 hex digits after an optional
            leading ``#``, or contains a non-hexadecimal character.

    """
    digits = value.strip().removeprefix("#")
    if len(digits) not in {_SHORT_HEX_LENGTH, _FULL_HEX_LENGTH}:
        msg = f"Invalid hex colour {value!r}: expected 3 or 6 hex digits"
        raise ValueError(msg)
    if _HEX_DIGITS.fullmatch(digits) is None:
        msg = f"Invalid hex colour {value!r}: contains non-hexadecimal characters"
        raise ValueError(msg)
    if len(digits) == _SHORT_HEX_LENGTH:
        digits = "".join(digit * 2 for digit in digits)
    return (
        int(digits[0:2], 16),
        int(digits[2:4], 16),
        int(digits[4:6], 16),
    )


def rgb_to_hex(rgb: tuple[int, int, int]) -> str:
    """Format 8-bit RGB channels as a lowercase ``#rrggbb`` string.

    Args:
        rgb: The red, green and blue channels, each in the range 0-255.

    Returns:
        The colour as ``#rrggbb``, always six digits and always lowercase.

    Raises:
        ValueError: If any channel falls outside 0-255.

    """
    _check_channels(rgb)
    red, green, blue = rgb
    return f"#{red:02x}{green:02x}{blue:02x}"


def rgb_to_xy(rgb: tuple[int, int, int]) -> _Point:
    """Convert 8-bit sRGB to a CIE 1931 ``(x, y)`` chromaticity.

    The channels are gamma-expanded with the sRGB transfer function, converted
    to CIE XYZ with the Wide Gamut RGB (D65) matrix Philips documents for Hue,
    and then normalised. Only chromaticity survives: the luminance of the
    input is discarded, which is why :func:`xy_to_rgb` takes a brightness.

    Args:
        rgb: The red, green and blue channels, each in the range 0-255.

    Returns:
        The chromaticity as ``(x, y)``. Pure black has no chromaticity at all,
        and yields ``(0.0, 0.0)`` rather than dividing by zero.

    Raises:
        ValueError: If any channel falls outside 0-255.

    """
    _check_channels(rgb)
    red, green, blue = rgb
    linear: _Vector = (
        _expand_gamma(red / _CHANNEL_MAX),
        _expand_gamma(green / _CHANNEL_MAX),
        _expand_gamma(blue / _CHANNEL_MAX),
    )
    x_value, y_value, z_value = _transform(_RGB_TO_XYZ, linear)
    total = x_value + y_value + z_value
    if total <= 0.0:
        return (0.0, 0.0)
    return (x_value / total, y_value / total)


def xy_to_rgb(xy: _Point, brightness: float = 100.0) -> tuple[int, int, int]:
    """Convert a CIE 1931 chromaticity and a brightness back to 8-bit sRGB.

    The inverse of :func:`rgb_to_xy`, with the luminance that conversion threw
    away supplied by ``brightness``. When the result overflows the displayable
    range, all three channels are scaled down by the same factor: clipping
    them one by one would change the ratios between them, and so shift the hue.

    Args:
        xy: The chromaticity as ``(x, y)``.
        brightness: Luminance as a percentage, 0-100. Defaults to 100.0,
            which yields the brightest sRGB colour of that chromaticity.

    Returns:
        The red, green and blue channels, each in the range 0-255.

    Raises:
        ValueError: If ``brightness`` falls outside 0-100.

    """
    if not _BRIGHTNESS_MIN <= brightness <= _BRIGHTNESS_MAX:
        msg = f"Brightness must be within 0-100, got {brightness!r}"
        raise ValueError(msg)
    x_value, y_value = xy
    if y_value <= 0.0:
        return (_CHANNEL_MIN, _CHANNEL_MIN, _CHANNEL_MIN)
    luminance = brightness / _BRIGHTNESS_MAX
    scale = luminance / y_value
    xyz: _Vector = (
        x_value * scale,
        luminance,
        (1.0 - x_value - y_value) * scale,
    )
    red, green, blue = _transform(_XYZ_TO_RGB, xyz)
    # A negative channel is simply not reproducible, so it becomes zero. Only
    # the overflow above 1.0 can be corrected without distorting the hue.
    red, green, blue = max(red, 0.0), max(green, 0.0), max(blue, 0.0)
    peak = max(red, green, blue)
    if peak > 1.0:
        red, green, blue = red / peak, green / peak, blue / peak
    return (_to_channel(red), _to_channel(green), _to_channel(blue))


def kelvin_to_mirek(kelvin: int) -> int:
    """Convert a colour temperature in kelvin to mirek.

    Args:
        kelvin: The colour temperature in kelvin. Must be positive.

    Returns:
        The equivalent mirek value, clamped into the 153-500 range the bridge
        accepts. Temperatures above about 6536 K or below 2000 K therefore
        come back as the nearest endpoint rather than an unusable number.

    Raises:
        ValueError: If ``kelvin`` is zero or negative.

    """
    if kelvin <= 0:
        msg = f"Colour temperature must be positive, got {kelvin!r} K"
        raise ValueError(msg)
    return _clamp_mirek(round(_MIREK_NUMERATOR / kelvin))


def mirek_to_kelvin(mirek: int) -> int:
    """Convert a colour temperature in mirek to kelvin.

    Args:
        mirek: The colour temperature in mirek. Values outside the 153-500
            range the bridge accepts are clamped before conversion, so this
            stays the exact inverse of :func:`kelvin_to_mirek` and can never
            divide by zero.

    Returns:
        The equivalent colour temperature in kelvin, between 2000 and 6536.

    """
    return round(_MIREK_NUMERATOR / _clamp_mirek(mirek))


def clamp_to_gamut(xy: _Point, gamut: Gamut) -> _Point:
    """Move a chromaticity onto the nearest point a light can reproduce.

    A point inside the gamut triangle is returned untouched. A point outside
    is projected onto each of the three edges and the nearest projection wins,
    which keeps the result as close to the requested colour as the hardware
    allows. Sending an unclamped value instead lets the bridge substitute a
    colour of its own choosing, without reporting that it did so.

    Args:
        xy: The requested chromaticity as ``(x, y)``.
        gamut: The triangle the light can reproduce.

    Returns:
        ``xy`` unchanged when it is already in gamut, otherwise the closest
        point on the triangle's perimeter.

    """
    if _is_inside(xy, gamut):
        return xy
    candidates = (
        _closest_on_segment(gamut.red, gamut.green, xy),
        _closest_on_segment(gamut.green, gamut.blue, xy),
        _closest_on_segment(gamut.blue, gamut.red, xy),
    )
    return min(candidates, key=lambda point: _distance(point, xy))


def gamut_for(gamut_type: str | None) -> Gamut | None:
    """Look up the gamut a light's reported ``gamut_type`` refers to.

    Args:
        gamut_type: The ``gamut_type`` field of a light's colour section,
            typically ``"A"``, ``"B"``, ``"C"`` or ``"other"``. Case and
            surrounding whitespace are ignored.

    Returns:
        The matching gamut, or ``None`` when the type is missing, reported as
        ``"other"``, or otherwise unrecognised. ``None`` means "do not clamp":
        the caller has no reliable triangle to clamp against.

    """
    if gamut_type is None:
        return None
    return _GAMUTS_BY_TYPE.get(gamut_type.strip().upper())


def _check_channels(rgb: tuple[int, int, int]) -> None:
    """Reject RGB channels that fall outside the 8-bit range.

    Args:
        rgb: The red, green and blue channels to validate.

    Raises:
        ValueError: If any channel falls outside 0-255.

    """
    for channel in rgb:
        if not _CHANNEL_MIN <= channel <= _CHANNEL_MAX:
            msg = f"RGB channels must be within 0-255, got {rgb!r}"
            raise ValueError(msg)


def _expand_gamma(channel: float) -> float:
    """Undo the sRGB transfer function for one channel.

    Args:
        channel: A gamma-encoded channel, normalised to 0.0-1.0.

    Returns:
        The linear-light value of that channel.

    """
    if channel <= _GAMMA_THRESHOLD:
        return channel / _LINEAR_SLOPE
    return ((channel + _GAMMA_OFFSET) / _GAMMA_SCALE) ** _GAMMA_EXPONENT


def _compress_gamma(channel: float) -> float:
    """Apply the sRGB transfer function to one channel.

    Args:
        channel: A linear-light channel, normalised to 0.0-1.0.

    Returns:
        The gamma-encoded value of that channel.

    """
    if channel <= _LINEAR_THRESHOLD:
        return channel * _LINEAR_SLOPE
    return _GAMMA_SCALE * channel ** (1.0 / _GAMMA_EXPONENT) - _GAMMA_OFFSET


def _to_channel(value: float) -> int:
    """Gamma-encode a linear channel and round it to an 8-bit value.

    Args:
        value: A linear-light channel, normalised to 0.0-1.0.

    Returns:
        The channel as an integer in the range 0-255.

    """
    encoded = round(_compress_gamma(value) * _CHANNEL_MAX)
    return min(max(encoded, _CHANNEL_MIN), _CHANNEL_MAX)


def _transform(matrix: _Matrix, vector: _Vector) -> _Vector:
    """Multiply a 3x3 matrix by a 3-element column vector.

    Args:
        matrix: The matrix, as three rows of three coefficients.
        vector: The column vector to transform.

    Returns:
        The transformed vector.

    """
    first, second, third = vector
    row_one, row_two, row_three = matrix
    return (
        row_one[0] * first + row_one[1] * second + row_one[2] * third,
        row_two[0] * first + row_two[1] * second + row_two[2] * third,
        row_three[0] * first + row_three[1] * second + row_three[2] * third,
    )


def _clamp_mirek(mirek: int) -> int:
    """Clamp a mirek value into the range the bridge accepts.

    Args:
        mirek: The colour temperature in mirek.

    Returns:
        The value, limited to 153-500.

    """
    return min(max(mirek, MIREK_MIN), MIREK_MAX)


def _distance(first: _Point, second: _Point) -> float:
    """Measure the Euclidean distance between two chromaticities.

    Args:
        first: The first point as ``(x, y)``.
        second: The second point as ``(x, y)``.

    Returns:
        The distance between them.

    """
    return math.hypot(first[0] - second[0], first[1] - second[1])


def _cross(origin: _Point, first: _Point, second: _Point) -> float:
    """Compute the 2D cross product of two vectors sharing an origin.

    Args:
        origin: The shared origin.
        first: The endpoint of the first vector.
        second: The endpoint of the second vector.

    Returns:
        The signed magnitude, positive when ``second`` lies counter-clockwise
        of ``first``.

    """
    first_x = first[0] - origin[0]
    first_y = first[1] - origin[1]
    second_x = second[0] - origin[0]
    second_y = second[1] - origin[1]
    return first_x * second_y - first_y * second_x


def _is_inside(point: _Point, gamut: Gamut) -> bool:
    """Test whether a chromaticity lies within a gamut triangle.

    Args:
        point: The chromaticity as ``(x, y)``.
        gamut: The triangle to test against.

    Returns:
        True when the point is inside the triangle or on its perimeter,
        False otherwise.

    """
    edges = (
        _cross(gamut.red, gamut.green, point),
        _cross(gamut.green, gamut.blue, point),
        _cross(gamut.blue, gamut.red, point),
    )
    outside_left = min(edges) < -_GAMUT_TOLERANCE
    outside_right = max(edges) > _GAMUT_TOLERANCE
    return not (outside_left and outside_right)


def _closest_on_segment(start: _Point, end: _Point, point: _Point) -> _Point:
    """Project a point onto a line segment.

    Args:
        start: The first endpoint of the segment.
        end: The second endpoint of the segment.
        point: The point to project.

    Returns:
        The point on the segment closest to ``point``, which is an endpoint
        when the projection falls beyond the segment.

    """
    run = end[0] - start[0]
    rise = end[1] - start[1]
    length_squared = run * run + rise * rise
    if length_squared == 0.0:
        return start
    along = (point[0] - start[0]) * run + (point[1] - start[1]) * rise
    offset = along / length_squared
    offset = min(max(offset, 0.0), 1.0)
    return (start[0] + offset * run, start[1] + offset * rise)
