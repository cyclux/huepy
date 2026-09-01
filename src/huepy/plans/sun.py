"""Sunrise, sunset and twilight, computed in-process.

The bridge is no help here. ``GET /resource/geolocation`` reports today's
``sunset_time`` and nothing else -- there is no ``sunrise_time`` -- and the
latitude and longitude it takes on PUT are write-only, so they cannot even be
read back to compute one. ``smart_scene`` timeslots make the same omission:
their ``kind`` is ``time`` or ``sunset``, never ``sunrise``, and neither
accepts an offset.

So a plan that says ``at = "sunrise-15m"`` has to be answered here. This is the
NOAA solar position algorithm, which is accurate to well under a minute for
the latitudes people live at -- far tighter than the bridge's own scheduling
resolution, and it costs no dependency.

Everything in this module is a pure function of (date, latitude, longitude) and
returns timezone-aware UTC datetimes, so the caller owns all local-time
questions.

Typical usage example:

    when = solar_event(SunEvent.SUNSET, date(2026, 9, 1), 48.137, 11.575)
"""

import datetime
import math

from huepy.plans.fields import SunEvent

MINUTES_PER_DAY = 1440
MINUTES_PER_HOUR = 60
DEGREES_PER_HOUR = 15.0

# Degrees below the zenith at which each event is declared. The 90.833 for
# sunrise and sunset is not 90: it adds the ~0.567 degrees of atmospheric
# refraction at the horizon plus the sun's own semi-diameter, which is what
# makes the visible disc touch the horizon rather than its centre.
_SUNRISE_ZENITH = 90.833
_CIVIL_ZENITH = 96.0

_ZENITH: dict[SunEvent, float] = {
    SunEvent.SUNRISE: _SUNRISE_ZENITH,
    SunEvent.SUNSET: _SUNRISE_ZENITH,
    SunEvent.DAWN: _CIVIL_ZENITH,
    SunEvent.DUSK: _CIVIL_ZENITH,
}

# Whether the event happens before or after solar noon.
_MORNING: dict[SunEvent, bool] = {
    SunEvent.SUNRISE: True,
    SunEvent.DAWN: True,
    SunEvent.SUNSET: False,
    SunEvent.DUSK: False,
}

_J2000 = 2451545.0
_DAYS_PER_CENTURY = 36525.0

# One refinement pass. The first estimate uses the sun's declination at solar
# noon; recomputing it at the estimated event time removes the error that
# introduces, which matters at high latitudes where declination moves fastest
# relative to the day length.
_REFINEMENTS = 2

MAX_LATITUDE = 90.0
MAX_LONGITUDE = 180.0


def _julian_day(date: datetime.date) -> float:
    """Julian day number for midnight UTC on a calendar date.

    Args:
        date: The calendar date.

    Returns:
        The Julian day number at 00:00 UTC.

    """
    year, month = date.year, date.month
    if month <= 2:  # noqa: PLR2004 - January and February count as months 13 and 14
        year -= 1
        month += 12
    a = year // 100
    b = 2 - a + a // 4
    return (
        math.floor(365.25 * (year + 4716))
        + math.floor(30.6001 * (month + 1))
        + date.day
        + b
        - 1524.5
    )


def _solar_geometry(julian_century: float) -> tuple[float, float]:
    """Sun declination and the equation of time at a moment.

    Args:
        julian_century: Julian centuries since J2000.0.

    Returns:
        A ``(declination_degrees, equation_of_time_minutes)`` pair. The
        equation of time is apparent solar time minus mean solar time.

    """
    t = julian_century
    mean_longitude = (280.46646 + t * (36000.76983 + t * 0.0003032)) % 360.0
    mean_anomaly = 357.52911 + t * (35999.05029 - 0.0001537 * t)
    eccentricity = 0.016708634 - t * (0.000042037 + 0.0000001267 * t)

    anomaly = math.radians(mean_anomaly)
    center = (
        math.sin(anomaly) * (1.914602 - t * (0.004817 + 0.000014 * t))
        + math.sin(2 * anomaly) * (0.019993 - 0.000101 * t)
        + math.sin(3 * anomaly) * 0.000289
    )
    true_longitude = mean_longitude + center

    # The moon's ascending node wobbles the ecliptic; both corrections below
    # ride on it.
    omega = math.radians(125.04 - 1934.136 * t)
    apparent_longitude = true_longitude - 0.00569 - 0.00478 * math.sin(omega)

    mean_obliquity = (
        23.0
        + (26.0 + (21.448 - t * (46.815 + t * (0.00059 - t * 0.001813))) / 60.0) / 60.0
    )
    obliquity = math.radians(mean_obliquity + 0.00256 * math.cos(omega))

    declination = math.degrees(
        math.asin(math.sin(obliquity) * math.sin(math.radians(apparent_longitude)))
    )

    y = math.tan(obliquity / 2.0) ** 2
    mean_longitude_rad = math.radians(mean_longitude)
    equation_of_time = 4.0 * math.degrees(
        y * math.sin(2 * mean_longitude_rad)
        - 2 * eccentricity * math.sin(anomaly)
        + 4 * eccentricity * y * math.sin(anomaly) * math.cos(2 * mean_longitude_rad)
        - 0.5 * y * y * math.sin(4 * mean_longitude_rad)
        - 1.25 * eccentricity * eccentricity * math.sin(2 * anomaly)
    )
    return declination, equation_of_time


def _hour_angle(latitude: float, declination: float, zenith: float) -> float | None:
    """Half the length of the day, in degrees of the sun's rotation.

    Args:
        latitude: Observer latitude in degrees, positive north.
        declination: Sun declination in degrees.
        zenith: Zenith angle at which the event is declared.

    Returns:
        The hour angle in degrees, or None when the sun never reaches that
        zenith on this date -- polar day or polar night.

    """
    lat = math.radians(latitude)
    dec = math.radians(declination)
    cosine = math.cos(math.radians(zenith)) / (
        math.cos(lat) * math.cos(dec)
    ) - math.tan(lat) * math.tan(dec)
    # Outside [-1, 1] the sun stays above or below that zenith all day. This is
    # not an error: it is a polar day or night, and the caller skips the step.
    if cosine < -1.0 or cosine > 1.0:
        return None
    return math.degrees(math.acos(cosine))


def _validate_location(latitude: float, longitude: float) -> None:
    """Reject a location the solar maths cannot answer for.

    Args:
        latitude: Latitude in degrees, positive north.
        longitude: Longitude in degrees, positive east.

    Raises:
        ValueError: If either coordinate is out of range.

    """
    if not -MAX_LATITUDE <= latitude <= MAX_LATITUDE:
        msg = f"latitude must be between -90 and 90, got {latitude}"
        raise ValueError(msg)
    if not -MAX_LONGITUDE <= longitude <= MAX_LONGITUDE:
        msg = f"longitude must be between -180 and 180, got {longitude}"
        raise ValueError(msg)


def _minutes_to_utc(date: datetime.date, minutes: float) -> datetime.datetime:
    """Turn minutes-after-midnight-UTC into an aware datetime.

    The value may fall outside the day -- a late sunset in a far-eastern
    longitude lands after midnight UTC -- so the offset is applied to the date
    rather than clamped into it.

    Args:
        date: The date the minute count is measured from.
        minutes: Minutes after 00:00 UTC on that date.

    Returns:
        The corresponding aware UTC datetime.

    """
    midnight = datetime.datetime.combine(date, datetime.time(0, 0), tzinfo=datetime.UTC)
    return midnight + datetime.timedelta(minutes=minutes)


def solar_noon(
    date: datetime.date, latitude: float, longitude: float
) -> datetime.datetime:
    """Compute the moment the sun crosses the observer's meridian.

    Args:
        date: The calendar date, in the observer's local reckoning.
        latitude: Latitude in degrees, positive north.
        longitude: Longitude in degrees, positive east.

    Returns:
        Solar noon as an aware UTC datetime.

    Raises:
        ValueError: If the coordinates are out of range.

    """
    _validate_location(latitude, longitude)
    minutes = _solar_noon_minutes(date, longitude)
    return _minutes_to_utc(date, minutes)


def _solar_noon_minutes(date: datetime.date, longitude: float) -> float:
    """Solar noon as minutes after midnight UTC.

    Args:
        date: The calendar date.
        longitude: Longitude in degrees, positive east.

    Returns:
        Minutes after 00:00 UTC.

    """
    # Seed the equation of time from mean noon at this longitude, then let the
    # result correct itself once.
    minutes = MINUTES_PER_DAY / 2 - 4.0 * longitude
    for _ in range(_REFINEMENTS):
        century = (_julian_day(date) + minutes / MINUTES_PER_DAY - _J2000) / (
            _DAYS_PER_CENTURY
        )
        _, equation_of_time = _solar_geometry(century)
        minutes = MINUTES_PER_DAY / 2 - 4.0 * longitude - equation_of_time
    return minutes


def solar_event(
    event: SunEvent,
    date: datetime.date,
    latitude: float,
    longitude: float,
) -> datetime.datetime | None:
    """When a solar event happens at a location on a date.

    Args:
        event: Which event to compute.
        date: The calendar date, in the observer's local reckoning.
        latitude: Latitude in degrees, positive north.
        longitude: Longitude in degrees, positive east.

    Returns:
        The event as an aware UTC datetime, or None when it does not happen
        that day -- the sun stays up, or never rises. Callers skip the step
        rather than treating this as a failure, which is what the bridge's own
        ``geolocation.day_type`` of ``polar_day`` or ``polar_night`` means.

    Raises:
        ValueError: If the coordinates are out of range.

    """
    _validate_location(latitude, longitude)
    zenith = _ZENITH[event]
    morning = _MORNING[event]

    noon_minutes = _solar_noon_minutes(date, longitude)
    minutes = noon_minutes
    for _ in range(_REFINEMENTS):
        century = (_julian_day(date) + minutes / MINUTES_PER_DAY - _J2000) / (
            _DAYS_PER_CENTURY
        )
        declination, equation_of_time = _solar_geometry(century)
        hour_angle = _hour_angle(latitude, declination, zenith)
        if hour_angle is None:
            return None
        offset = -4.0 * hour_angle if morning else 4.0 * hour_angle
        minutes = MINUTES_PER_DAY / 2 - 4.0 * longitude - equation_of_time + offset
    return _minutes_to_utc(date, minutes)
