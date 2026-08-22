"""Tests for the pure colour-conversion helpers."""

import math

import pytest

from huepy.color import (
    GAMUT_A,
    GAMUT_B,
    GAMUT_C,
    MIREK_MAX,
    MIREK_MIN,
    Gamut,
    clamp_to_gamut,
    gamut_for,
    hex_to_rgb,
    kelvin_to_mirek,
    mirek_to_kelvin,
    rgb_to_hex,
    rgb_to_xy,
    xy_to_rgb,
)

# The CIE D65 white point, which pure white must land on.
D65 = (0.3127, 0.3290)

# The primaries of the Wide Gamut RGB (D65) space the conversion matrix
# describes. These sit outside every Hue gamut, which is the whole reason
# clamp_to_gamut exists.
WIDE_GAMUT_RED = (0.7350, 0.2650)
WIDE_GAMUT_GREEN = (0.1150, 0.8260)
WIDE_GAMUT_BLUE = (0.1570, 0.0180)

CHROMATICITY_TOLERANCE = 1e-3


def distance(first: tuple[float, float], second: tuple[float, float]) -> float:
    return math.hypot(first[0] - second[0], first[1] - second[1])


def distance_to_segment(
    point: tuple[float, float],
    start: tuple[float, float],
    end: tuple[float, float],
) -> float:
    """Distance from a point to a segment, computed independently of the module."""
    run, rise = end[0] - start[0], end[1] - start[1]
    length_squared = run * run + rise * rise
    along = (point[0] - start[0]) * run + (point[1] - start[1]) * rise
    offset = min(max(along / length_squared, 0.0), 1.0)
    return distance(point, (start[0] + offset * run, start[1] + offset * rise))


def distance_to_perimeter(point: tuple[float, float], gamut: Gamut) -> float:
    return min(
        distance_to_segment(point, gamut.red, gamut.green),
        distance_to_segment(point, gamut.green, gamut.blue),
        distance_to_segment(point, gamut.blue, gamut.red),
    )


# --- hex ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    ["#ff8800", "ff8800", "#FF8800", "  #ff8800  ", "#f80", "f80", "#F80"],
)
def test_hex_to_rgb_accepts_every_documented_form(value):
    assert hex_to_rgb(value) == (255, 136, 0)


def test_short_form_doubles_each_digit_rather_than_padding():
    """`#f80` is `#ff8800`, not `#f08000`."""
    assert hex_to_rgb("#abc") == (0xAA, 0xBB, 0xCC)


@pytest.mark.parametrize("value", ["", "#", "#f", "#ff", "#ffff", "#fffff", "#fffffff"])
def test_hex_to_rgb_rejects_wrong_lengths(value):
    with pytest.raises(ValueError, match="expected 3 or 6 hex digits"):
        hex_to_rgb(value)


@pytest.mark.parametrize("value", ["#gg8800", "#xyz", "ff88zz", "#+f8", "#ff 800"])
def test_hex_to_rgb_rejects_non_hex_characters(value):
    with pytest.raises(ValueError, match="non-hexadecimal"):
        hex_to_rgb(value)


def test_rgb_to_hex_is_lowercase_and_zero_padded():
    assert rgb_to_hex((255, 136, 0)) == "#ff8800"
    assert rgb_to_hex((0, 0, 0)) == "#000000"
    assert rgb_to_hex((1, 2, 3)) == "#010203"


@pytest.mark.parametrize("rgb", [(256, 0, 0), (-1, 0, 0), (0, 0, 300)])
def test_rgb_to_hex_rejects_out_of_range_channels(rgb):
    with pytest.raises(ValueError, match="within 0-255"):
        rgb_to_hex(rgb)


@pytest.mark.parametrize("value", ["#ff8800", "#000000", "#ffffff", "#123456"])
def test_hex_round_trips(value):
    assert rgb_to_hex(hex_to_rgb(value)) == value


# --- rgb <-> xy --------------------------------------------------------------


def test_white_lands_on_the_d65_white_point():
    x, y = rgb_to_xy((255, 255, 255))
    assert (x, y) == pytest.approx(D65, abs=CHROMATICITY_TOLERANCE)


@pytest.mark.parametrize(
    ("rgb", "primary"),
    [
        ((255, 0, 0), WIDE_GAMUT_RED),
        ((0, 255, 0), WIDE_GAMUT_GREEN),
        ((0, 0, 255), WIDE_GAMUT_BLUE),
    ],
)
def test_saturated_channels_land_on_their_primary(rgb, primary):
    assert rgb_to_xy(rgb) == pytest.approx(primary, abs=CHROMATICITY_TOLERANCE)


def test_pure_black_has_no_chromaticity_instead_of_dividing_by_zero():
    assert rgb_to_xy((0, 0, 0)) == (0.0, 0.0)


@pytest.mark.parametrize("rgb", [(256, 0, 0), (0, -1, 0)])
def test_rgb_to_xy_rejects_out_of_range_channels(rgb):
    with pytest.raises(ValueError, match="within 0-255"):
        rgb_to_xy(rgb)


# xy carries chromaticity only: the luminance of the input is discarded, so a
# round trip at brightness=100 reproduces the *brightest* colour of that hue.
# These test colours all have a channel at full scale, so they already are that
# colour and survive the trip exactly. A mid-tone like (128, 128, 128) does not,
# and is covered separately below by supplying its luminance explicitly.
@pytest.mark.parametrize(
    "rgb",
    [
        (255, 0, 0),
        (0, 255, 0),
        (0, 0, 255),
        (255, 255, 255),
        (255, 128, 0),
        (0, 255, 128),
        (128, 0, 255),
        (255, 200, 100),
        (255, 64, 32),
        (200, 255, 50),
    ],
)
def test_rgb_to_xy_round_trips_for_full_scale_colours(rgb):
    assert xy_to_rgb(rgb_to_xy(rgb)) == pytest.approx(rgb, abs=1)


@pytest.mark.parametrize(
    ("rgb", "luminance_percent"),
    [
        ((128, 128, 128), 21.586),
        ((100, 50, 25), 5.378),
        ((60, 120, 180), 16.047),
    ],
)
def test_mid_tones_round_trip_when_their_luminance_is_supplied(rgb, luminance_percent):
    """Brightness is exactly the Y that rgb_to_xy threw away, as a percentage."""
    assert xy_to_rgb(rgb_to_xy(rgb), luminance_percent) == pytest.approx(rgb, abs=1)


@pytest.mark.parametrize("brightness", [100.0, 75.0, 50.0, 25.0, 1.0])
def test_brightness_scales_the_result_without_changing_the_hue(brightness):
    red, green, blue = xy_to_rgb((0.4, 0.4), brightness)
    assert (red, green, blue) == pytest.approx(
        xy_to_rgb((0.4, 0.4), 100.0), rel=1.0, abs=255
    )
    assert rgb_to_xy((red, green, blue)) == pytest.approx((0.4, 0.4), abs=0.01)


def test_brightness_is_monotonic():
    levels = [max(xy_to_rgb(D65, percent)) for percent in (0.0, 10.0, 50.0, 100.0)]
    assert levels == sorted(levels)
    assert levels[0] == 0
    assert levels[-1] == 255


def test_zero_brightness_is_black():
    assert xy_to_rgb((0.4, 0.4), 0.0) == (0, 0, 0)


def test_degenerate_y_is_black_instead_of_dividing_by_zero():
    assert xy_to_rgb((0.0, 0.0)) == (0, 0, 0)
    assert xy_to_rgb((0.4, 0.0)) == (0, 0, 0)


@pytest.mark.parametrize("brightness", [-0.1, 100.1, 255.0])
def test_xy_to_rgb_rejects_out_of_range_brightness(brightness):
    with pytest.raises(ValueError, match="within 0-100"):
        xy_to_rgb((0.4, 0.4), brightness)


def test_overflow_is_scaled_proportionally_not_clipped_per_channel():
    """Clipping channels one by one would shift the hue; scaling preserves it.

    At full brightness the red primary overflows badly (the linear channels
    come out around (4.3, 0, 0)). Independent clipping would leave the ratios
    between channels wrong; the chromaticity of the result proves it does not.
    """
    xy = (0.692, 0.308)
    rgb = xy_to_rgb(xy, 100.0)
    assert max(rgb) == 255
    assert rgb_to_xy(rgb) == pytest.approx(xy, abs=0.01)


@pytest.mark.parametrize("gamut", [GAMUT_A, GAMUT_B, GAMUT_C])
def test_every_gamut_corner_survives_a_trip_through_rgb(gamut):
    """Round-tripping a primary through 8-bit RGB stays within quantisation noise."""
    for corner in gamut:
        assert rgb_to_xy(xy_to_rgb(corner)) == pytest.approx(corner, abs=0.02)


# --- colour temperature ------------------------------------------------------


@pytest.mark.parametrize(
    ("kelvin", "mirek"),
    [(2000, 500), (2700, 370), (4000, 250), (5000, 200), (6536, 153)],
)
def test_kelvin_to_mirek_known_anchors(kelvin, mirek):
    assert kelvin_to_mirek(kelvin) == mirek


@pytest.mark.parametrize("kelvin", [2000, 2700, 3000, 4000, 5000, 6500, 6536])
def test_kelvin_round_trips_within_the_resolution_of_one_mirek(kelvin):
    assert mirek_to_kelvin(kelvin_to_mirek(kelvin)) == pytest.approx(kelvin, rel=0.002)


@pytest.mark.parametrize("kelvin", [1, 500, 1000, 1999])
def test_temperatures_below_the_range_clamp_to_the_warm_end(kelvin):
    assert kelvin_to_mirek(kelvin) == MIREK_MAX


@pytest.mark.parametrize("kelvin", [6537, 10_000, 1_000_000])
def test_temperatures_above_the_range_clamp_to_the_cool_end(kelvin):
    assert kelvin_to_mirek(kelvin) == MIREK_MIN


@pytest.mark.parametrize("kelvin", [0, -1, -6500])
def test_kelvin_to_mirek_rejects_non_positive_temperatures(kelvin):
    with pytest.raises(ValueError, match="must be positive"):
        kelvin_to_mirek(kelvin)


def test_mirek_to_kelvin_clamps_out_of_range_input():
    assert mirek_to_kelvin(0) == mirek_to_kelvin(MIREK_MIN)
    assert mirek_to_kelvin(-50) == mirek_to_kelvin(MIREK_MIN)
    assert mirek_to_kelvin(9999) == mirek_to_kelvin(MIREK_MAX)


def test_mirek_bounds_are_the_documented_hue_range():
    assert (MIREK_MIN, MIREK_MAX) == (153, 500)
    assert mirek_to_kelvin(MIREK_MAX) == 2000
    assert mirek_to_kelvin(MIREK_MIN) == 6536


# --- gamut clamping ----------------------------------------------------------


@pytest.mark.parametrize("gamut", [GAMUT_A, GAMUT_B, GAMUT_C])
def test_a_point_inside_the_gamut_is_returned_unchanged(gamut):
    centre = (
        sum(corner[0] for corner in gamut) / 3,
        sum(corner[1] for corner in gamut) / 3,
    )
    assert clamp_to_gamut(centre, gamut) == centre


@pytest.mark.parametrize("gamut", [GAMUT_A, GAMUT_B, GAMUT_C])
def test_the_corners_themselves_are_in_gamut(gamut):
    for corner in gamut:
        assert clamp_to_gamut(corner, gamut) == corner


OUT_OF_GAMUT = [
    (0.0, 0.0),
    (1.0, 0.0),
    (0.0, 1.0),
    (0.9, 0.1),
    (0.5, 0.5),
    (-0.2, 0.9),
    (0.735, 0.265),
    (0.115, 0.826),
]


@pytest.mark.parametrize("gamut", [GAMUT_A, GAMUT_B, GAMUT_C])
@pytest.mark.parametrize("point", OUT_OF_GAMUT)
def test_an_out_of_gamut_point_lands_on_the_perimeter(point, gamut):
    clamped = clamp_to_gamut(point, gamut)
    assert distance_to_perimeter(clamped, gamut) == pytest.approx(0.0, abs=1e-9)


@pytest.mark.parametrize("gamut", [GAMUT_A, GAMUT_B, GAMUT_C])
@pytest.mark.parametrize("point", OUT_OF_GAMUT)
def test_the_clamped_point_beats_every_corner(point, gamut):
    """Snapping to the nearest corner would be the lazy answer; this is closer."""
    clamped = clamp_to_gamut(point, gamut)
    for corner in gamut:
        assert distance(clamped, point) <= distance(corner, point) + 1e-12


@pytest.mark.parametrize("gamut", [GAMUT_A, GAMUT_B, GAMUT_C])
@pytest.mark.parametrize("point", OUT_OF_GAMUT)
def test_clamping_is_idempotent(point, gamut):
    once = clamp_to_gamut(point, gamut)
    assert clamp_to_gamut(once, gamut) == pytest.approx(once, abs=1e-12)


def test_a_point_beyond_a_corner_snaps_to_that_corner():
    """Far outside along the red corner, no edge projection can beat the corner."""
    assert clamp_to_gamut((5.0, -5.0), GAMUT_C) == pytest.approx(GAMUT_C.red, abs=1e-12)


def test_a_gamut_with_a_zero_length_edge_does_not_divide_by_zero():
    """A malformed gamut collapses to a segment; clamping still returns a point."""
    collapsed = Gamut(red=(0.4, 0.4), green=(0.4, 0.4), blue=(0.2, 0.1))
    assert clamp_to_gamut((0.9, 0.1), collapsed) == (0.4, 0.4)
    on_segment = clamp_to_gamut((0.1, 0.5), collapsed)
    assert distance_to_segment(on_segment, (0.2, 0.1), (0.4, 0.4)) == pytest.approx(
        0.0, abs=1e-12
    )


def test_clamping_narrows_more_for_the_narrower_gamut():
    """Deep green is unreachable on gamut B, which barely moves it on gamut C."""
    deep_green = (0.15, 0.75)
    assert distance(clamp_to_gamut(deep_green, GAMUT_B), deep_green) > distance(
        clamp_to_gamut(deep_green, GAMUT_C), deep_green
    )


# --- gamut lookup ------------------------------------------------------------


@pytest.mark.parametrize(
    ("gamut_type", "expected"),
    [
        ("A", GAMUT_A),
        ("a", GAMUT_A),
        ("B", GAMUT_B),
        ("b", GAMUT_B),
        ("C", GAMUT_C),
        ("c", GAMUT_C),
        (" c ", GAMUT_C),
    ],
)
def test_gamut_for_resolves_the_known_types(gamut_type, expected):
    assert gamut_for(gamut_type) is expected


@pytest.mark.parametrize("gamut_type", [None, "other", "", "D", "gamut_c", "ABC"])
def test_gamut_for_returns_none_for_anything_it_cannot_map(gamut_type):
    assert gamut_for(gamut_type) is None


def test_the_three_gamuts_are_distinct_non_degenerate_triangles():
    for gamut in (GAMUT_A, GAMUT_B, GAMUT_C):
        area = abs(
            (gamut.green[0] - gamut.red[0]) * (gamut.blue[1] - gamut.red[1])
            - (gamut.green[1] - gamut.red[1]) * (gamut.blue[0] - gamut.red[0])
        )
        assert area > 0.01
    assert len({GAMUT_A, GAMUT_B, GAMUT_C}) == 3


def test_gamut_c_is_the_widest():
    def area(gamut: Gamut) -> float:
        return abs(
            (gamut.green[0] - gamut.red[0]) * (gamut.blue[1] - gamut.red[1])
            - (gamut.green[1] - gamut.red[1]) * (gamut.blue[0] - gamut.red[0])
        )

    assert area(GAMUT_C) > area(GAMUT_A) > area(GAMUT_B)
