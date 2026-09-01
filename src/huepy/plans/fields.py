"""The scalar grammar of a plan file.

A plan is written by hand, so its scalars are strings that read like English:
``"45m"``, ``"sunset+30m"``, ``"room:Living Room"``. Parsing them here rather
than inside the schema keeps :mod:`huepy.plans.schema` a plain description of
the format, and it keeps every "what does this string mean" question answerable
by one small, pure, heavily tested module.

Each parser is exposed twice: as a function that raises :class:`ValueError`,
and as an annotated type that pydantic applies during validation. The schema
uses the annotated types, so a malformed file fails with a pydantic error
naming the exact key.

Typical usage example:

    seconds = parse_duration("1h15m")
    anchor = parse_anchor("sunset+30m")
    scope = parse_selector("room:Living Room")
"""

import datetime
import re
from enum import StrEnum
from typing import Annotated, Any, ClassVar, Literal, override

from pydantic import BaseModel, BeforeValidator, ConfigDict

from huepy.models.state import MILLISECONDS_PER_SECOND

SECONDS_PER_HOUR = 3600
SECONDS_PER_MINUTE = 60

_UNIT_SECONDS: dict[str, float] = {
    "h": float(SECONDS_PER_HOUR),
    "m": float(SECONDS_PER_MINUTE),
    "s": 1.0,
    "ms": 1.0 / MILLISECONDS_PER_SECOND,
}

# Longest unit first: "ms" has to win against "m" or "500ms" reads as 500
# minutes followed by a stray "s".
_DURATION_TOKEN = re.compile(r"(\d+(?:\.\d+)?)(ms|h|m|s)")

# Descending magnitude. A duration must spell its units largest-first and use
# each at most once, so "1h15m" parses and "15m1h" is rejected rather than
# quietly meaning the same thing.
_UNIT_ORDER: tuple[str, ...] = ("h", "m", "s", "ms")

_CLOCK = re.compile(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$")

_SUN_ANCHOR = re.compile(r"^([a-z_]+)(?:\s*([+-])\s*(.+))?$")

# Whitespace around the colon is tolerated: "room : Kitchen" is a typo, not a
# different selector. The name is lazy but anchored, so a name containing its
# own colon ("light:Desk: the good one") still survives intact.
_SELECTOR = re.compile(r"^\s*([a-z_]+)\s*:\s*(.+?)\s*$")

HOURS_PER_DAY = 24
MINUTES_PER_HOUR = 60


class SunEvent(StrEnum):
    """A daily solar event a plan step can be anchored to.

    The bridge reports only ``sunset_time`` and cannot express an offset, so
    every one of these is computed in-process by :mod:`huepy.plans.sun`.
    """

    SUNRISE = "sunrise"
    SUNSET = "sunset"
    DAWN = "dawn"
    DUSK = "dusk"


class ScopeKind(StrEnum):
    """The kind of resource a scope selector addresses.

    A room and a zone are both written through the ``grouped_light`` service
    they own, which costs one broadcast instead of one write per member light.
    There is deliberately no generic ``group``: it would be ambiguous with
    both of these and buy nothing.
    """

    LIGHT = "light"
    ROOM = "room"
    ZONE = "zone"


class TriggerKind(StrEnum):
    """The kind of source a trigger selector listens to.

    Each kind fires on one event: ``MOTION`` when motion starts, ``BUTTON``
    when any button on the device goes down, ``CONTACT`` when the door or
    window opens. ``SIGNAL`` is the one that is not a bridge resource: it
    names a string the hosting application fires through
    ``PlanRunner.fire()``.

    There is deliberately no ``light_level``. A level only means something
    against a threshold, and the format has no key for one yet; accepting the
    selector without a way to say "below 30 lux" would be a trigger that
    parses and never fires.
    """

    MOTION = "motion"
    BUTTON = "button"
    CONTACT = "contact"
    SIGNAL = "signal"


def _sum_tokens(text: str, original: object) -> float:
    """Add up the unit tokens of an already-normalised duration string.

    Args:
        text: The duration with whitespace stripped.
        original: The value as the caller wrote it, for error messages.

    Returns:
        The duration in seconds.

    Raises:
        ValueError: If a unit is unknown, repeated, out of order, or the
            string carries anything the grammar does not cover.

    """
    seen: list[str] = []
    total = 0.0
    position = 0
    for match in _DURATION_TOKEN.finditer(text):
        if match.start() != position:
            msg = (
                f"could not parse duration {original!r}: unexpected "
                f"{text[position : match.start()]!r}"
            )
            raise ValueError(msg)
        amount, unit = match.group(1), match.group(2)
        _check_unit_order(unit, seen, original)
        seen.append(unit)
        total += float(amount) * _UNIT_SECONDS[unit]
        position = match.end()

    if not seen or position != len(text):
        msg = (
            f"could not parse duration {original!r}. Expected something like "
            f"'90s', '45m', '2h' or '1h15m'"
        )
        raise ValueError(msg)
    return total


def _check_unit_order(unit: str, seen: list[str], original: object) -> None:
    """Reject a unit that repeats an earlier one or runs out of order.

    Args:
        unit: The unit just parsed.
        seen: The units already accepted, in order.
        original: The value as the caller wrote it, for error messages.

    Raises:
        ValueError: If the unit repeats or is larger than its predecessor.

    """
    if unit in seen:
        msg = f"could not parse duration {original!r}: {unit!r} given twice"
        raise ValueError(msg)
    if seen and _UNIT_ORDER.index(unit) < _UNIT_ORDER.index(seen[-1]):
        msg = (
            f"could not parse duration {original!r}: units must run "
            f"largest-first, so {seen[-1]!r} cannot precede {unit!r}"
        )
        raise ValueError(msg)


def parse_duration(value: object) -> float:
    """Parse a duration such as ``"1h15m"`` into seconds.

    Units are ``h``, ``m``, ``s`` and ``ms``. They must appear largest-first
    and at most once each, so a typo like ``"15m1h"`` is an error rather than
    a second spelling of the same span. A bare number is taken as seconds,
    which is what a TOML author writing ``ramp = 30`` means.

    Args:
        value: The duration, as a string, int or float.

    Returns:
        The duration in seconds.

    Raises:
        ValueError: If the string is empty, carries an unknown or repeated
            unit, spells its units out of order, has trailing junk, or is
            negative.

    """
    if isinstance(value, bool):
        msg = f"duration must be a string or number, got {value!r}"
        raise TypeError(msg)
    if isinstance(value, (int, float)):
        seconds = float(value)
        if seconds < 0:
            msg = f"duration must not be negative, got {value!r}"
            raise ValueError(msg)
        return seconds
    if not isinstance(value, str):
        msg = f"duration must be a string or number, got {value!r}"
        raise TypeError(msg)

    text = value.strip().replace(" ", "")
    if not text:
        msg = "duration must not be empty"
        raise ValueError(msg)

    return _sum_tokens(text, value)


def format_duration(seconds: float) -> str:
    """Render a duration in seconds back into the plan file's spelling.

    Used by ``huepy plan explain`` so its output can be pasted into a plan.

    Args:
        seconds: The duration in seconds.

    Returns:
        The shortest spelling that round-trips through :func:`parse_duration`.

    """
    if seconds <= 0:
        return "0s"
    parts: list[str] = []
    remaining = seconds
    for unit in ("h", "m", "s"):
        size = _UNIT_SECONDS[unit]
        count, remaining = divmod(remaining, size)
        if count:
            parts.append(f"{int(count)}{unit}")
    milliseconds = round(remaining * MILLISECONDS_PER_SECOND)
    if milliseconds:
        parts.append(f"{milliseconds}ms")
    return "".join(parts)


class ClockAnchor(BaseModel):
    """A step anchored to a wall-clock time in the bridge's local day."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    kind: Literal["clock"] = "clock"
    at: datetime.time

    @override
    def __str__(self) -> str:
        """Render the anchor in the plan file's spelling."""
        return self.at.strftime("%H:%M" if not self.at.second else "%H:%M:%S")


class SunAnchor(BaseModel):
    """A step anchored to a solar event, optionally offset.

    Attributes:
        event: The solar event the step hangs off.
        offset: Seconds after the event; negative means before it.

    """

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    kind: Literal["sun"] = "sun"
    event: SunEvent
    offset: float = 0.0

    @override
    def __str__(self) -> str:
        """Render the anchor in the plan file's spelling."""
        if not self.offset:
            return str(self.event)
        sign = "-" if self.offset < 0 else "+"
        return f"{self.event}{sign}{format_duration(abs(self.offset))}"


type TimeAnchor = ClockAnchor | SunAnchor


def parse_anchor(value: object) -> TimeAnchor:
    """Parse a step's ``at`` value into a clock or solar anchor.

    Accepts ``"07:30"``, ``"07:30:15"``, ``"sunrise"``, ``"sunset+30m"`` and
    ``"sunrise-1h15m"``. TOML's native local-time scalar is accepted too, so
    ``at = 07:30:00`` works without quotes.

    Args:
        value: The anchor, as a string or a :class:`datetime.time`.

    Returns:
        A :class:`ClockAnchor` or :class:`SunAnchor`.

    Raises:
        ValueError: If the string names no known solar event, spells an
            out-of-range clock time, or cannot be parsed at all.

    """
    if isinstance(value, (ClockAnchor, SunAnchor)):
        return value
    if isinstance(value, datetime.time):
        return ClockAnchor(at=value)
    if not isinstance(value, str):
        msg = f"'at' must be a time or a solar anchor, got {value!r}"
        raise TypeError(msg)

    text = value.strip().lower()
    if not text:
        msg = "'at' must not be empty"
        raise ValueError(msg)

    clock = _CLOCK.match(text)
    if clock is not None:
        hour, minute = int(clock.group(1)), int(clock.group(2))
        second = int(clock.group(3) or 0)
        if hour >= HOURS_PER_DAY or minute >= MINUTES_PER_HOUR:
            msg = f"{value!r} is not a valid time of day"
            raise ValueError(msg)
        return ClockAnchor(at=datetime.time(hour, minute, second))

    sun = _SUN_ANCHOR.match(text)
    if sun is None:
        msg = (
            f"could not parse 'at' value {value!r}. Expected a time like "
            f"'07:30' or a solar anchor like 'sunset+30m'"
        )
        raise ValueError(msg)
    name, sign, offset_text = sun.group(1), sun.group(2), sun.group(3)
    if name not in set(SunEvent):
        known = ", ".join(sorted(SunEvent))
        msg = f"unknown solar event {name!r} in {value!r}. Known events: {known}"
        raise ValueError(msg)
    offset = parse_duration(offset_text) if offset_text else 0.0
    return SunAnchor(event=SunEvent(name), offset=-offset if sign == "-" else offset)


class Selector(BaseModel):
    """A ``kind:name`` reference to something on the bridge.

    One grammar addresses both halves of a plan: ``room:Living Room`` names a
    scope to write to, ``motion:Hall sensor`` names a trigger to listen to.
    The name is the same one :meth:`huepy.Hue.lights.get` accepts, and it is
    resolved to a resource id later, by :mod:`huepy.plans.resolve`.

    Attributes:
        kind: The resource kind, as written before the colon.
        name: The resource name, as written after it.

    """

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    kind: str
    name: str

    @override
    def __str__(self) -> str:
        """Render the selector in the plan file's spelling."""
        return f"{self.kind}:{self.name}"


def _parse_selector(value: object, allowed: frozenset[str], role: str) -> Selector:
    """Parse a ``kind:name`` string, checking the kind against a whitelist.

    Args:
        value: The selector string.
        allowed: The kinds acceptable in this position.
        role: What the selector addresses, for the error message.

    Returns:
        The parsed selector.

    Raises:
        ValueError: If the string has no colon or names a kind not in
            ``allowed``.

    """
    if isinstance(value, Selector):
        parsed = value
    else:
        if not isinstance(value, str):
            msg = f"a {role} must be a 'kind:name' string, got {value!r}"
            raise TypeError(msg)
        match = _SELECTOR.match(value.strip())
        if match is None:
            kinds = ", ".join(sorted(allowed))
            msg = (
                f"could not parse {role} {value!r}. Expected 'kind:name', "
                f"where kind is one of: {kinds}"
            )
            raise ValueError(msg)
        parsed = Selector(kind=match.group(1), name=match.group(2).strip())
    if parsed.kind not in allowed:
        kinds = ", ".join(sorted(allowed))
        msg = f"unknown {role} kind {parsed.kind!r}. Known kinds: {kinds}"
        raise ValueError(msg)
    return parsed


_SCOPE_KINDS = frozenset(str(kind) for kind in ScopeKind)
_TRIGGER_KINDS = frozenset(str(kind) for kind in TriggerKind)


def parse_selector(value: object) -> Selector:
    """Parse a scope selector such as ``"room:Living Room"``.

    Args:
        value: The selector string.

    Returns:
        The parsed selector.

    Raises:
        ValueError: If the string is malformed or names an unknown kind.

    """
    return _parse_selector(value, _SCOPE_KINDS, "scope")


def parse_trigger(value: object) -> Selector:
    """Parse a trigger selector such as ``"motion:Hall sensor"``.

    Args:
        value: The selector string.

    Returns:
        The parsed selector.

    Raises:
        ValueError: If the string is malformed or names an unknown kind.

    """
    return _parse_selector(value, _TRIGGER_KINDS, "trigger")


def _validate_duration(value: Any) -> Any:  # noqa: ANN401 - pydantic input is arbitrary
    """Adapt :func:`parse_duration` to pydantic's before-validator signature."""
    return parse_duration(value)


def _validate_anchor(value: Any) -> Any:  # noqa: ANN401 - pydantic input is arbitrary
    """Adapt :func:`parse_anchor` to pydantic's before-validator signature."""
    return parse_anchor(value)


def _validate_scope(value: Any) -> Any:  # noqa: ANN401 - pydantic input is arbitrary
    """Adapt :func:`parse_selector` to pydantic's before-validator signature."""
    return parse_selector(value)


def _validate_trigger(value: Any) -> Any:  # noqa: ANN401 - pydantic input is arbitrary
    """Adapt :func:`parse_trigger` to pydantic's before-validator signature."""
    return parse_trigger(value)


type Duration = Annotated[float, BeforeValidator(_validate_duration)]
type Anchor = Annotated[TimeAnchor, BeforeValidator(_validate_anchor)]
type ScopeSelector = Annotated[Selector, BeforeValidator(_validate_scope)]
type TriggerSelector = Annotated[Selector, BeforeValidator(_validate_trigger)]
