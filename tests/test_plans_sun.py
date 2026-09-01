"""Solar event computation.

The bridge cannot answer these questions -- it reports no sunrise at all -- so
this module is the only source of truth for a sun-anchored step. The reference
values below are published almanac times, converted to UTC, and are checked to
within a minute: tighter than the bridge's own scheduling resolution.
"""

import datetime

import pytest

from huepy.plans.fields import SunEvent
from huepy.plans.sun import solar_event, solar_noon

TOLERANCE = datetime.timedelta(minutes=1)

MUNICH = (48.137, 11.575)
LONDON = (51.5074, -0.1278)
SYDNEY = (-33.8688, 151.2093)
TROMSO = (69.6496, 18.9560)
EQUATOR = (0.0, 0.0)


def utc(year, month, day, hour, minute):
    return datetime.datetime(year, month, day, hour, minute, tzinfo=datetime.UTC)


class TestAgainstPublishedTimes:
    @pytest.mark.parametrize(
        ("place", "date", "event", "expected"),
        [
            # London, midsummer: 04:43 / 21:21 BST.
            (LONDON, (2024, 6, 21), SunEvent.SUNRISE, (2024, 6, 21, 3, 43)),
            (LONDON, (2024, 6, 21), SunEvent.SUNSET, (2024, 6, 21, 20, 21)),
            # Munich, early autumn: 06:32 / 19:56 CEST.
            (MUNICH, (2026, 9, 1), SunEvent.SUNRISE, (2026, 9, 1, 4, 31)),
            (MUNICH, (2026, 9, 1), SunEvent.SUNSET, (2026, 9, 1, 17, 55)),
            # Southern hemisphere, and the sunrise falls on the previous UTC
            # date -- 05:41 AEDT is 18:41 UTC the day before.
            (SYDNEY, (2024, 12, 21), SunEvent.SUNRISE, (2024, 12, 20, 18, 40)),
            (SYDNEY, (2024, 12, 21), SunEvent.SUNSET, (2024, 12, 21, 9, 5)),
        ],
    )
    def test_matches_almanac_within_a_minute(self, place, date, event, expected):
        result = solar_event(event, datetime.date(*date), *place)
        assert result is not None
        assert abs(result - utc(*expected)) <= TOLERANCE

    def test_solar_noon_sits_between_sunrise_and_sunset(self):
        date = datetime.date(2026, 9, 1)
        rise = solar_event(SunEvent.SUNRISE, date, *MUNICH)
        noon = solar_noon(date, *MUNICH)
        sets = solar_event(SunEvent.SUNSET, date, *MUNICH)
        assert rise is not None
        assert sets is not None
        assert rise < noon < sets


class TestTwilight:
    def test_dawn_precedes_sunrise_and_dusk_follows_sunset(self):
        date = datetime.date(2026, 9, 1)
        dawn = solar_event(SunEvent.DAWN, date, *MUNICH)
        rise = solar_event(SunEvent.SUNRISE, date, *MUNICH)
        sets = solar_event(SunEvent.SUNSET, date, *MUNICH)
        dusk = solar_event(SunEvent.DUSK, date, *MUNICH)
        assert dawn is not None
        assert rise is not None
        assert sets is not None
        assert dusk is not None
        assert dawn < rise < sets < dusk

    def test_civil_twilight_is_roughly_half_an_hour_at_mid_latitudes(self):
        date = datetime.date(2026, 9, 1)
        dawn = solar_event(SunEvent.DAWN, date, *MUNICH)
        rise = solar_event(SunEvent.SUNRISE, date, *MUNICH)
        assert dawn is not None
        assert rise is not None
        assert (
            datetime.timedelta(minutes=25)
            < rise - dawn
            < datetime.timedelta(minutes=45)
        )


class TestPolarCases:
    def test_midnight_sun_has_no_sunrise_or_sunset(self):
        # Not an error: this is what the bridge calls day_type "polar_day".
        # The caller skips the step instead of failing the plan.
        date = datetime.date(2024, 6, 21)
        assert solar_event(SunEvent.SUNRISE, date, *TROMSO) is None
        assert solar_event(SunEvent.SUNSET, date, *TROMSO) is None

    def test_polar_night_has_no_sunrise_or_sunset(self):
        date = datetime.date(2024, 12, 21)
        assert solar_event(SunEvent.SUNRISE, date, *TROMSO) is None
        assert solar_event(SunEvent.SUNSET, date, *TROMSO) is None

    def test_solar_noon_still_answers_inside_the_polar_circle(self):
        # The sun still crosses the meridian even when it never sets, so noon
        # is always defined and never returns None.
        assert solar_noon(datetime.date(2024, 6, 21), *TROMSO) is not None


class TestInvariants:
    @pytest.mark.parametrize("day", range(1, 366, 29))
    def test_sunrise_precedes_sunset_all_year(self, day):
        date = datetime.date(2024, 1, 1) + datetime.timedelta(days=day - 1)
        rise = solar_event(SunEvent.SUNRISE, date, *MUNICH)
        sets = solar_event(SunEvent.SUNSET, date, *MUNICH)
        assert rise is not None
        assert sets is not None
        assert rise < sets

    def test_results_are_timezone_aware_utc(self):
        result = solar_event(SunEvent.SUNSET, datetime.date(2026, 9, 1), *MUNICH)
        assert result is not None
        assert result.tzinfo is datetime.UTC

    def test_equatorial_day_is_close_to_twelve_hours(self):
        date = datetime.date(2024, 3, 20)
        rise = solar_event(SunEvent.SUNRISE, date, *EQUATOR)
        sets = solar_event(SunEvent.SUNSET, date, *EQUATOR)
        assert rise is not None
        assert sets is not None
        length = sets - rise
        assert (
            datetime.timedelta(hours=12)
            < length
            < datetime.timedelta(hours=12, minutes=15)
        )

    @pytest.mark.parametrize(
        ("latitude", "longitude"), [(91.0, 0.0), (-91.0, 0.0), (0.0, 181.0)]
    )
    def test_rejects_impossible_coordinates(self, latitude, longitude):
        with pytest.raises(ValueError, match=r"lat|long"):
            solar_event(
                SunEvent.SUNRISE, datetime.date(2026, 9, 1), latitude, longitude
            )
