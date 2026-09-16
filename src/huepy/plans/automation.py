"""Recognizing what a Hue app motion automation does to a light.

A motion automation made in the Hue app runs on the bridge, not here, and the
event stream says nothing about who moved a light: a report is this client's
own or it is unattributed. One thing such an automation does looks exactly
like a hand at the dial. Before it switches a room off it dims the room as a
warning, and a plan that judged that dim a hand change stood back from the
room until its next step (measured: the bathroom at 97 % at 02:13 against a
curve at 20).

The dim has a signature, though, measured on all 147 of them in the
bathroom's recorder history: exactly :data:`WARNING_DIM_POINTS` down, or to
zero, with ``on`` left alone, 259-260 s after the sensor's ``motion=false``
report for a rule whose ``after`` is five minutes, and followed by the
switch-off 29-30 s later. This module reads the rules off the bridge's
``behavior_instance`` resources and tests reports against that signature.
Like the arbiter it is pure: no clock, no client, no I/O. The runner holds
the memory and decides what a recognized dim does.

Typical usage example:

    rule = parse_motion_rule(
        instance,
        motion_ids=motion_ids,
        motions_of_device=motions_of_device,
        lights_of_group=lights_of_group,
    )
    if isinstance(rule, MotionRule) and is_warning_dim(
        delta=change.delta,
        before_on=True,
        before_brightness=96.84,
        no_motion_at=no_motion_at,
        received_at=change.received_at,
        afters=rule.afters,
    ):
        ...
"""

import datetime
import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, cast

from huepy.models.automation import BehaviorInstance

WARNING_DIM_POINTS = 50.2
"""How far the bridge's warning dim lowers a light, in brightness points.

Measured, not documented: 100 to 49.8, 96.84 to 46.64, 64.43 to 14.23. A light
at or below it goes to 0 and stays on.
"""

LEVEL_TOLERANCE = 1.0
"""How far a reported level may sit from the expected one and still match.

The measured dims are exact to the hundredth; this only absorbs the bridge
rounding a level to its 254 steps.
"""

WARNING_EARLIEST = 50.0
"""Seconds before a rule's ``after`` that a warning dim may come, at the earliest."""

WARNING_LATEST = 30.0
"""Seconds before a rule's ``after`` that a warning dim may come, at the latest.

Measured at ``after - 40 s`` every time; the window is ten seconds either side.
"""

FOLLOW_UP_SECONDS = 35.0
"""How long a recognized dim waits for the bridge's switch-off or a restore.

Measured: the switch-off follows the dim by 29-30 s.
"""

MOTION_TYPE = "motion"
DEVICE_TYPE = "device"
GROUP_TYPES = frozenset({"room", "zone"})


@dataclass(frozen=True, slots=True)
class MotionRule:
    """A Hue app motion automation that switches a room off after a while.

    Attributes:
        behavior_id: The ``behavior_instance`` it was read from.
        name: Its name in the app, for logs.
        motion_service_id: The motion service whose reports drive it.
        light_ids: Every light in the rooms and zones it acts on.
        afters: The ``on_no_motion`` delay of each of its timeslots, in
            seconds. Any of them may be the one in force.

    """

    behavior_id: str
    name: str
    motion_service_id: str
    light_ids: frozenset[str]
    afters: frozenset[float]


@dataclass(frozen=True, slots=True)
class NotMotion:
    """An automation that is not a motion rule with a switch-off: nothing to read."""


@dataclass(frozen=True, slots=True)
class Malformed:
    """A motion rule this module cannot read with certainty.

    Attributes:
        reason: Why, for a warning.

    """

    reason: str


type Parsed = MotionRule | NotMotion | Malformed
type FollowUp = Literal["off", "restored", "other"]


@dataclass(frozen=True, slots=True)
class PendingWarning:
    """A recognized warning dim, waiting for what the bridge does next.

    Attributes:
        light_id: The light that dimmed.
        behavior_id: The rule that dimmed it.
        rule_name: That rule's name, for logs.
        pre: The light's level before the dim, which a restore brings back.
        dimmed: The level the dim reported.
        observed_at: When the runner saw the dim, on the runner's clock. A
            claim that began after it has superseded the dim.
        due: When to stop waiting, on the runner's clock.

    """

    light_id: str
    behavior_id: str
    rule_name: str
    pre: float
    dimmed: float
    observed_at: datetime.datetime
    due: datetime.datetime


def _object(value: object) -> dict[str, Any] | None:
    """Narrow bridge JSON to an object.

    Args:
        value: Anything from a bridge payload.

    Returns:
        The object, or None when it is not one.

    """
    # Bridge JSON is the one place `Any` is the honest type.
    return cast("dict[str, Any]", value) if isinstance(value, dict) else None


def _number(value: object) -> float | None:
    """Narrow bridge JSON to a finite number.

    Args:
        value: Anything from a bridge payload.

    Returns:
        The number, or None for a bool, a non-number or a non-finite float.

    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        # JSON integers are unbounded; raising here would escape the runner's
        # synchronous change callback.
        return None
    return number if math.isfinite(number) else None


def _reference(value: object) -> tuple[str, str] | None:
    """Read a ``{rid, rtype}`` resource reference.

    Args:
        value: Anything from a bridge payload.

    Returns:
        The ``(rid, rtype)`` pair, or None when it is not a reference.

    """
    reference = _object(value)
    if reference is None:
        return None
    rid = reference.get("rid")
    rtype = reference.get("rtype")
    if not isinstance(rid, str) or not isinstance(rtype, str):
        return None
    return rid, rtype


def _motion_service(
    configuration: dict[str, Any],
    motion: dict[str, Any] | None,
    *,
    motion_ids: frozenset[str],
    motions_of_device: Mapping[str, tuple[str, ...]],
) -> str | NotMotion | Malformed:
    """Find the one motion service a rule listens to.

    Args:
        configuration: The rule's configuration.
        motion: Its ``motion`` object, in the app's newer shape.
        motion_ids: Every motion service on the bridge.
        motions_of_device: The motion services each device owns.

    Returns:
        The service id, :class:`NotMotion` when nothing names a motion
        service, or :class:`Malformed` when the rule names more than one.

    """
    from_source: tuple[str, ...] = ()
    source = _reference(configuration.get("source"))
    if source is not None:
        rid, rtype = source
        if rtype == MOTION_TYPE and rid in motion_ids:
            from_source = (rid,)
        elif rtype == DEVICE_TYPE:
            from_source = motions_of_device.get(rid, ())
    explicit = _reference(motion.get("motion_service")) if motion is not None else None
    if explicit is not None:
        rid, _ = explicit
        if rid not in motion_ids:
            return Malformed(f"motion_service {rid} is not a motion sensor")
        if source is not None and from_source and rid not in from_source:
            return Malformed("its source and its motion_service are different sensors")
        return rid
    if len(from_source) > 1:
        return Malformed(f"its source has {len(from_source)} motion services")
    return next(iter(from_source), NotMotion())


def _afters(when: object) -> frozenset[float] | NotMotion | Malformed:
    """Read the ``on_no_motion`` delay of every timeslot.

    Args:
        when: The rule's ``when`` object.

    Returns:
        The delays in seconds, :class:`NotMotion` when no timeslot switches
        anything off, or :class:`Malformed` for a delay that is not a positive
        number of minutes and seconds.

    """
    body = _object(when)
    if body is None:
        return NotMotion()
    slots: list[object] = []
    timeslots = body.get("timeslots")
    if isinstance(timeslots, list):
        slots.extend(cast("list[object]", timeslots))
    if "always" in body:
        slots.append(body["always"])
    afters: set[float] = set()
    for raw in slots:
        slot = _object(raw)
        if slot is None or "on_no_motion" not in slot:
            continue
        off = _object(slot["on_no_motion"])
        if off is None:
            return Malformed("an on_no_motion is not an object")
        after = _object(off.get("after"))
        if after is None or not ({"minutes", "seconds"} & after.keys()):
            return Malformed("an on_no_motion has no after")
        minutes = _number(after.get("minutes", 0))
        seconds = _number(after.get("seconds", 0))
        if minutes is None or seconds is None or minutes * 60 + seconds <= 0:
            return Malformed(f"an on_no_motion after is not a delay: {after}")
        afters.add(minutes * 60 + seconds)
    return frozenset(afters) if afters else NotMotion()


def _lights(
    where: object, lights_of_group: Mapping[str, frozenset[str]]
) -> frozenset[str] | Malformed:
    """Collect the lights of every room and zone a rule acts on.

    Args:
        where: The rule's ``where`` list.
        lights_of_group: The lights of each room and zone.

    Returns:
        The light ids, or :class:`Malformed` when an entry is not a known room
        or zone with lights.

    """
    if not isinstance(where, list) or not where:
        return Malformed("its where names no room")
    lights: set[str] = set()
    for raw in cast("list[object]", where):
        entry = _object(raw)
        group = _reference(entry.get("group")) if entry is not None else None
        if group is None or group[1] not in GROUP_TYPES:
            return Malformed(f"its where entry {raw} is not a room or zone")
        members = lights_of_group.get(group[0], frozenset())
        if not members:
            return Malformed(f"its {group[1]} {group[0]} has no lights")
        lights.update(members)
    return frozenset(lights)


def parse_motion_rule(
    instance: BehaviorInstance,
    *,
    motion_ids: frozenset[str],
    motions_of_device: Mapping[str, tuple[str, ...]],
    lights_of_group: Mapping[str, frozenset[str]],
) -> Parsed:
    """Read a motion rule off a ``behavior_instance``, or say why not.

    Two shapes are read, because the app rewrote the bathroom's rule from one
    to the other when its daylight setting changed: the older one keeps
    ``when`` and ``where`` beside a ``source`` device, the newer one nests
    them in ``motion`` beside a ``motion_service``. Nothing is guessed; a rule
    naming two sensors, or a delay that is not one, is :class:`Malformed`.

    Args:
        instance: The automation, as the bridge reported it.
        motion_ids: Every motion service on the bridge.
        motions_of_device: The motion services each device owns.
        lights_of_group: The lights of each room and zone.

    Returns:
        The rule; :class:`NotMotion` for anything that is not an enabled
        motion rule with a switch-off; :class:`Malformed` for a motion rule
        this module cannot read with certainty.

    """
    if not instance.enabled:
        return NotMotion()
    configuration = instance.configuration
    motion = _object(configuration.get("motion"))
    service = _motion_service(
        configuration,
        motion,
        motion_ids=motion_ids,
        motions_of_device=motions_of_device,
    )
    if not isinstance(service, str):
        return service
    body = motion if motion is not None else configuration
    afters = _afters(body.get("when"))
    if not isinstance(afters, frozenset):
        return afters
    lights = _lights(body.get("where"), lights_of_group)
    if isinstance(lights, Malformed):
        return lights
    return MotionRule(
        behavior_id=instance.id,
        name=instance.name,
        motion_service_id=service,
        light_ids=lights,
        afters=afters,
    )


def bare_level(delta: Mapping[str, Any]) -> float | None:
    """Read a delta that changes a light's level and nothing else.

    Args:
        delta: The bridge's delta for one report.

    Returns:
        The level, or None when the delta carries anything besides exactly
        ``{"dimming": {"brightness": <finite number>}}``.

    """
    if set(delta) != {"dimming"}:
        return None
    dimming = _object(delta["dimming"])
    if dimming is None or set(dimming) != {"brightness"}:
        return None
    return _number(dimming["brightness"])


def is_warning_dim(  # noqa: PLR0913 - one keyword per measured condition
    *,
    delta: Mapping[str, Any],
    before_on: bool | None,
    before_brightness: float | None,
    no_motion_at: datetime.datetime | None,
    received_at: datetime.datetime,
    afters: frozenset[float],
) -> bool:
    """Whether a light report is a motion rule's warning dim.

    Every condition must hold, and anything unknown fails closed: a report
    that is not recognized is judged as a hand change, as before.

    Args:
        delta: The report's delta.
        before_on: Whether the light was on before the report.
        before_brightness: Its level before the report.
        no_motion_at: When the rule's sensor last reported ``motion=false``,
            as ``Change.received_at``; None if not since this runner started.
        received_at: The report's ``Change.received_at``. Both instants are on
            the host's clock, so no bridge clock is compared with it.
        afters: The rule's ``on_no_motion`` delays, in seconds.

    Returns:
        True for a bare level exactly :data:`WARNING_DIM_POINTS` below a lit
        light's level, or at 0 from below that, arriving inside the window
        before one of the rule's delays runs out.

    """
    level = bare_level(delta)
    if level is None or before_on is not True or before_brightness is None:
        return False
    expected = max(0.0, before_brightness - WARNING_DIM_POINTS)
    if abs(level - expected) > LEVEL_TOLERANCE or no_motion_at is None:
        return False
    still = (received_at - no_motion_at).total_seconds()
    return any(
        after - WARNING_EARLIEST <= still <= after - WARNING_LATEST for after in afters
    )


def follow_up(pending: PendingWarning, *, delta: Mapping[str, Any]) -> FollowUp:
    """Classify the next report on a light with a pending warning dim.

    Args:
        pending: The recognized dim.
        delta: The next report's delta.

    Returns:
        ``"off"`` for a switch-off, ``"restored"`` for a bare level back at
        the one before the dim -- motion came back in time -- and
        ``"other"`` for anything else.

    """
    on = _object(delta.get("on"))
    if on is not None and on.get("on") is False:
        return "off"
    level = bare_level(delta)
    if level is not None and abs(level - pending.pre) <= LEVEL_TOLERANCE:
        return "restored"
    return "other"


def motion_transition(delta: Mapping[str, Any]) -> bool | None:
    """Read a motion transition out of a motion service's delta.

    Only the delta, never the folded state: an update that re-enables the
    sensor while its last state was "no motion" must not restart the
    countdown to a warning.

    Args:
        delta: The motion service's delta.

    Returns:
        The reported ``motion``, or None when the delta carries none.

    """
    motion = _object(delta.get("motion"))
    value = motion.get("motion") if motion is not None else None
    return value if isinstance(value, bool) else None
