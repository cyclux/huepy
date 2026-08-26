"""Human-readable one-line summaries of Hue state payloads.

The bridge describes a change as a nested dict of state sections, and that same
shape reaches callers twice: on the event stream as
:class:`~huepy.models.event.EventResource`, and in the state layer as
:attr:`~huepy.state.Change.delta`. Rendering it was the one piece of work every
consumer of either had to write for itself, so it lives here once and both
reach it through a ``summary`` property.

Deliberately dict-in rather than model-in: a section this library does not model
yet still arrives intact on ``model_extra``, and summarising it must not have to
wait for a model to exist. Unrecognised keys are skipped, so a payload that is
all future firmware summarises to ``""`` instead of raising.

Typical usage example:

    async for event in hue.get_event_stream():
        for resource in event.data:
            print(hue.get_name(resource.id), resource.summary)
"""

from collections.abc import Callable, Mapping
from typing import Any, Final, cast

from huepy.color import mirek_to_kelvin, rgb_to_hex, xy_to_rgb

__all__ = ["summarize"]

_NO_EFFECT: Final = "no_effect"
"""The effect status a light reports when it is running none."""


def _mapping(value: object) -> Mapping[str, Any] | None:
    """Narrow one payload section to a mapping, or None when it is not one.

    A bridge section genuinely is arbitrary JSON, so every renderer below has
    to check before indexing. Doing it here keeps that check to one line each
    and gives the reads a key type instead of an unknown one.
    """
    return cast("Mapping[str, Any]", value) if isinstance(value, Mapping) else None


def _number(value: object) -> float | None:
    """Return ``value`` as a float when it is a real number.

    ``bool`` is an ``int`` subclass, so an unguarded ``isinstance(value, int)``
    would render ``on: true`` as the brightness ``1``.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _reported(section: object, key: str) -> object:
    """Read one reading, preferring a nested ``<key>_report`` over the flat field.

    The bridge sends both for motion, temperature and ambient light. The report
    is the fresher of the two, and the flat field is missing entirely on some
    firmware, so neither one alone is enough.
    """
    reading = _mapping(section)
    if reading is None:
        return None
    report = _mapping(reading.get(f"{key}_report"))
    if report is not None and report.get(key) is not None:
        return report[key]
    return reading.get(key)


def _on(value: object) -> str | None:
    """Render the power section as ``on`` or ``off``."""
    section = _mapping(value)
    if section is None:
        return None
    state = section.get("on")
    return ("on" if state else "off") if isinstance(state, bool) else None


def _dimming(value: object) -> str | None:
    """Render brightness as a whole percentage."""
    section = _mapping(value)
    if section is None:
        return None
    brightness = _number(section.get("brightness"))
    return None if brightness is None else f"{brightness:.0f}%"


def _color_temperature(value: object) -> str | None:
    """Render colour temperature in Kelvin, the unit people set it in.

    ``mirek`` is null while the light is in colour mode, which is exactly when
    its colour temperature means nothing -- so a null renders as nothing.
    """
    section = _mapping(value)
    if section is None:
        return None
    mirek = _number(section.get("mirek"))
    return None if mirek is None else f"{mirek_to_kelvin(int(mirek))} K"


def _color(value: object) -> str | None:
    """Render a CIE xy pair as the hex colour it is closest to.

    Gamut-unaware on purpose: this is a summary of what the bridge reported,
    not a prediction of what a particular bulb can reproduce.
    """
    section = _mapping(value)
    xy = _mapping(section.get("xy")) if section is not None else None
    if xy is None:
        return None
    x, y = _number(xy.get("x")), _number(xy.get("y"))
    return None if x is None or y is None else rgb_to_hex(xy_to_rgb((x, y)))


def _effects(value: object) -> str | None:
    """Render a running effect by name, ignoring "running none"."""
    section = _mapping(value)
    if section is None:
        return None
    status = section.get("status")
    if not isinstance(status, str) or status == _NO_EFFECT:
        return None
    return f"effect {status}"


def _motion(value: object) -> str | None:
    """Render a motion reading as ``motion`` or ``clear``."""
    detected = _reported(value, "motion")
    return ("motion" if detected else "clear") if isinstance(detected, bool) else None


def _temperature(value: object) -> str | None:
    """Render a temperature reading in degrees Celsius."""
    celsius = _number(_reported(value, "temperature"))
    return None if celsius is None else f"{celsius:.1f} \N{DEGREE SIGN}C"


def _light_level(value: object) -> str | None:
    """Render an ambient light reading in the bridge's raw level units."""
    level = _number(_reported(value, "light_level"))
    return None if level is None else f"light level {level:.0f}"


def _button(value: object) -> str | None:
    """Render the most recent button event, e.g. ``initial_press``."""
    section = _mapping(value)
    if section is None:
        return None
    report = _mapping(section.get("button_report"))
    event = report.get("event") if report is not None else None
    if not isinstance(event, str):
        event = section.get("last_event")
    return event if isinstance(event, str) else None


def _contact(value: object) -> str | None:
    """Render a contact-sensor transition, e.g. ``no_contact``."""
    section = _mapping(value)
    if section is None:
        return None
    state = section.get("state")
    return state if isinstance(state, str) else None


def _power_state(value: object) -> str | None:
    """Render a battery level as a percentage."""
    section = _mapping(value)
    if section is None:
        return None
    level = _number(section.get("battery_level"))
    return None if level is None else f"battery {level:.0f}%"


def _relative_rotary(value: object) -> str | None:
    """Render a rotary movement as its direction and step count."""
    section = _mapping(value)
    if section is None:
        return None
    # `rotary_report` first, matching the model's own `value` property: current
    # firmware sends it, and `last_event` is the deprecated shape beside it.
    event = _mapping(section.get("rotary_report")) or _mapping(
        section.get("last_event")
    )
    rotation = _mapping(event.get("rotation")) if event is not None else None
    if rotation is None:
        return None
    direction, steps = rotation.get("direction"), _number(rotation.get("steps"))
    if not isinstance(direction, str) or steps is None:
        return None
    return f"{direction} {steps:.0f} steps"


def _status(value: object) -> str | None:
    """Render a scene's recall status, or a service's connectivity string."""
    if isinstance(value, str):
        return value
    section = _mapping(value)
    if section is None:
        return None
    active = section.get("active")
    return f"scene {active}" if isinstance(active, str) else None


def _metadata(value: object) -> str | None:
    """Render a rename, which is the metadata change worth reporting."""
    section = _mapping(value)
    if section is None:
        return None
    name = section.get("name")
    return f"named {name!r}" if isinstance(name, str) else None


_RENDERERS: Final[tuple[tuple[str, Callable[[object], str | None]], ...]] = (
    ("on", _on),
    ("dimming", _dimming),
    ("color_temperature", _color_temperature),
    ("color", _color),
    ("effects", _effects),
    ("effects_v2", _effects),
    ("motion", _motion),
    ("temperature", _temperature),
    ("light", _light_level),
    ("button", _button),
    ("contact_report", _contact),
    ("power_state", _power_state),
    ("relative_rotary", _relative_rotary),
    ("status", _status),
    ("metadata", _metadata),
)
"""Section renderers in output order, most significant first."""


def summarize(state: Mapping[str, Any]) -> str:
    """Describe whichever state sections a Hue payload actually carries.

    Args:
        state: One resource's state, as the bridge nests it -- an event's
            resource entry, a :attr:`~huepy.state.Change.delta`, or any
            equivalent decoded payload.

    Returns:
        A comma-separated summary such as ``"on, 62%, 2700 K"``, or ``""``
        when the payload carries no section this function recognises.

    """
    parts = [
        rendered
        for key, render in _RENDERERS
        if key in state and (rendered := render(state[key])) is not None
    ]
    return ", ".join(parts)
