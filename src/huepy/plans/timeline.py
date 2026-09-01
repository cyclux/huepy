"""When a scenario changes, and what it should look like at any instant.

This is the whole scheduling brain, and it is deliberately pure: no clock, no
bridge, no I/O. ``now`` is always a parameter. That is what lets a simulated
day run in milliseconds in the test suite, and it is what makes crash recovery
possible -- the runner keeps no durable state, and instead asks this module
"where should this scope be right now?" every time it starts or reconnects.

Two things are worth stating plainly, because they are the format's semantics:

* **A ramp starts at its anchor.** ``at = "sunset"`` with ``ramp = "2h"``
  begins dimming as the sun goes down and settles two hours later. That is how
  someone writing the line reads it.
* **Interpolation is only for catching up.** In normal running the runner sends
  one PUT with the target and a duration, and the *bridge* does the fade. The
  arithmetic here answers a different question: restarted at 19:40 into a fade
  that began at 19:00, where should the light be?

Typical usage example:

    target = target_at(plan, scenario, now, zone_of(plan.location))
"""

import datetime
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from huepy.plans.fields import ClockAnchor, TimeAnchor
from huepy.plans.schema import Action, Location, Plan, Rule, Scenario
from huepy.plans.sun import solar_event

# A day either side of the day in question. Yesterday's last step is still in
# force at 03:00 today, and tomorrow's first is the next thing to wake for, so
# neither can be left out of the search.
_SPAN = (-1, 0, 1)

type Zone = datetime.tzinfo | None
"""A plan's timezone. None means the host's own, resolved per instant."""


@dataclass(frozen=True, slots=True)
class Waypoint:
    """One scenario step, pinned to an absolute instant.

    Attributes:
        at: When the fade starts.
        ends_at: When it finishes.
        ramp: Its length in seconds.
        action: Where it ends up.
        scenario: The scenario that declared it.

    """

    at: datetime.datetime
    ends_at: datetime.datetime
    ramp: float
    action: Action
    scenario: str


def zone_of(location: Location | None) -> Zone:
    """Determine the timezone a plan's clock times are written in.

    Args:
        location: The plan's location, if it declared one.

    Returns:
        The named zone, or None for the host's own. The host is normally in
        the same zone as the bridge and the lights, which is why that is the
        default rather than UTC -- ``at = "23:30"`` means half past eleven at
        night where the lights are.

    """
    if location is not None and location.timezone is not None:
        return ZoneInfo(location.timezone)
    # Deliberately None, not `datetime.now().astimezone().tzinfo`. That
    # expression yields a *fixed offset* frozen at whatever DST is in force at
    # the moment it runs, and the runner holds its zone for the life of the
    # process -- so a daemon started in summer would fire every clock step an
    # hour early all winter. None means "ask the host per instant" instead,
    # which is the only spelling that stays correct across a DST change.
    return None


def combine(day: datetime.date, at: datetime.time, zone: Zone) -> datetime.datetime:
    """Pin a wall-clock time on a local day to an absolute instant.

    Args:
        day: The local calendar date.
        at: The local time of day.
        zone: The plan's zone, or None for the host's own.

    Returns:
        The corresponding aware datetime.

    """
    if zone is None:
        return datetime.datetime.combine(day, at).astimezone()
    return datetime.datetime.combine(day, at, tzinfo=zone)


def in_zone(when: datetime.datetime, zone: Zone) -> datetime.datetime:
    """Express an instant in a plan's zone.

    Args:
        when: The instant.
        zone: The plan's zone, or None for the host's own.

    Returns:
        The same instant, in that zone.

    """
    return when.astimezone() if zone is None else when.astimezone(zone)


def resolve_anchor(
    anchor: TimeAnchor,
    day: datetime.date,
    zone: Zone,
    location: Location | None,
) -> datetime.datetime | None:
    """Pin an anchor to an absolute instant on a given local day.

    Args:
        anchor: The clock or solar anchor to resolve.
        day: The local calendar date.
        zone: The zone the plan's clock times are written in.
        location: Needed for solar anchors.

    Returns:
        The instant, or None when a solar anchor does not occur that day --
        polar day or polar night. The step is skipped rather than failing the
        plan, which is what the bridge's own ``day_type`` reports.

    Raises:
        ValueError: If a solar anchor is resolved with no location. The schema
            rejects that at load time, so reaching this means the plan was
            built in code rather than parsed.

    """
    if isinstance(anchor, ClockAnchor):
        # Twice a year a wall-clock time is not a unique instant. Both cases
        # are left to Python's own fold rules rather than raising, because a
        # plan must still run on those two days. In the spring-forward gap the
        # missing time resolves to the same instant as the hour after it, so a
        # step there is immediately superseded by the next one; in autumn the
        # repeated hour resolves to its first occurrence.
        return combine(day, anchor.at, zone)

    if location is None:
        msg = f"cannot resolve {anchor} without a location"
        raise ValueError(msg)
    event = solar_event(anchor.event, day, location.latitude, location.longitude)
    if event is None:
        return None
    return in_zone(event + datetime.timedelta(seconds=anchor.offset), zone)


def waypoints_for_day(
    plan: Plan,
    scenario: Scenario,
    day: datetime.date,
    zone: Zone,
) -> list[Waypoint]:
    """Every step of one scenario on one local day, in time order.

    Args:
        plan: The plan the scenario belongs to, for defaults and location.
        scenario: The scenario to lay out.
        day: The local calendar date.
        zone: The plan's timezone.

    Returns:
        The day's waypoints, sorted. Empty when the scenario does not run that
        day. Steps whose solar anchor does not occur are dropped.

    """
    if scenario not in plan.scenarios_for_day(day):
        return []

    found: list[Waypoint] = []
    for step in scenario.step:
        at = resolve_anchor(step.at, day, zone, plan.location)
        if at is None:
            continue
        ramp = step.ramp if step.ramp is not None else plan.defaults.ramp
        found.append(
            Waypoint(
                at=at,
                ends_at=at + datetime.timedelta(seconds=ramp),
                ramp=ramp,
                action=step.set.resolved(),
                scenario=scenario.name,
            )
        )
    found.sort(key=lambda waypoint: waypoint.at)
    return found


def waypoints_around(
    plan: Plan,
    scenario: Scenario,
    now: datetime.datetime,
    zone: Zone,
) -> list[Waypoint]:
    """Lay out a scenario's waypoints for the day around an instant.

    Args:
        plan: The plan the scenario belongs to.
        scenario: The scenario to lay out.
        now: The instant of interest.
        zone: The plan's timezone.

    Returns:
        Waypoints from yesterday, today and tomorrow, sorted. The neighbours
        matter: yesterday's last step is still what the scope should look like
        at 03:00, and tomorrow's first is what to wake up for.

    """
    today = in_zone(now, zone).date()
    # A scenario restricted by `days` makes no claim at all on a day it does
    # not run, not even through yesterday's last step. Without this guard a
    # weekend-only scenario keeps asserting every weekday -- and if it outranks
    # the base curve, masks it until its own steps fall out of the window.
    if scenario not in plan.scenarios_for_day(today):
        return []
    found: list[Waypoint] = []
    for offset in _SPAN:
        day = today + datetime.timedelta(days=offset)
        found.extend(waypoints_for_day(plan, scenario, day, zone))
    found.sort(key=lambda waypoint: waypoint.at)
    return found


def _lerp(start: float, end: float, fraction: float) -> float:
    """Linear interpolation between two numbers.

    Args:
        start: Value at fraction 0.
        end: Value at fraction 1.
        fraction: How far along, 0 to 1.

    Returns:
        The interpolated value.

    """
    return start + (end - start) * fraction


def interpolate(start: Action | None, end: Action, fraction: float) -> Action:
    """Compute the state part-way through a fade between two targets.

    Only attributes both ends agree on can be interpolated. One the fade is
    heading *towards* but did not start from -- a step that introduces a colour
    temperature the previous step never mentioned -- is taken at its final
    value, because there is no start to move away from. ``on`` is never
    interpolated: a light is on or it is not.

    Args:
        start: The settled state the fade began from, if it is known.
        end: The target the fade is heading for.
        fraction: How far through the fade, 0 to 1.

    Returns:
        The intermediate state, in canonical colour spelling.

    """
    if start is None or fraction >= 1.0:
        return end
    fraction = max(0.0, fraction)

    brightness = end.brightness
    if brightness is not None and start.brightness is not None:
        brightness = _lerp(start.brightness, brightness, fraction)

    mirek = end.mirek
    if mirek is not None and start.mirek is not None:
        mirek = round(_lerp(start.mirek, mirek, fraction))

    xy = end.xy
    if xy is not None and start.xy is not None:
        xy = (
            _lerp(start.xy[0], xy[0], fraction),
            _lerp(start.xy[1], xy[1], fraction),
        )

    return Action(on=end.on, brightness=brightness, xy=xy, mirek=mirek)


def current_step(
    plan: Plan,
    scenario: Scenario,
    now: datetime.datetime,
    zone: Zone,
) -> Waypoint | None:
    """Find the waypoint currently in force for a scenario.

    This is the *scheduling* question, and it is deliberately not
    :func:`target_at`. In normal running the runner sends the step's final
    target with a duration and lets the bridge run the fade, so what it needs
    is the step itself, not where that step has got to. Asking for the
    interpolated value here would mean writing the fade's starting point at the
    moment the fade is supposed to begin -- a write that changes nothing.

    Args:
        plan: The plan the scenario belongs to.
        scenario: The scenario to evaluate.
        now: The instant of interest.
        zone: The plan's timezone.

    Returns:
        The last waypoint at or before ``now``, or None when the scenario has
        not reached one.

    """
    current: Waypoint | None = None
    for waypoint in waypoints_around(plan, scenario, now, zone):
        if waypoint.at > now:
            break
        current = waypoint
    return current


def target_at(
    plan: Plan,
    scenario: Scenario,
    now: datetime.datetime,
    zone: Zone,
) -> Action | None:
    """Determine what a scenario says its scope should look like now.

    This is the catch-up question. After a restart or a reconnect the runner
    has no memory of what it did, so it asks this and fades there over
    ``defaults.catchup_ramp``.

    Args:
        plan: The plan the scenario belongs to.
        scenario: The scenario to evaluate.
        now: The instant of interest.
        zone: The plan's timezone.

    Returns:
        The state the scope should be in, interpolated when a fade is still
        running. None when the scenario has no step at or before ``now`` and
        therefore makes no claim on the scope yet.

    """
    found = waypoints_around(plan, scenario, now, zone)
    current: Waypoint | None = None
    previous: Waypoint | None = None
    for waypoint in found:
        if waypoint.at > now:
            break
        previous, current = current, waypoint

    if current is None:
        return None
    if current.ramp <= 0 or now >= current.ends_at:
        return current.action
    elapsed = (now - current.at).total_seconds()
    start = previous.action if previous is not None else None
    return interpolate(start, current.action, elapsed / current.ramp)


def next_transition(
    plan: Plan,
    scenario: Scenario,
    now: datetime.datetime,
    zone: Zone,
) -> datetime.datetime | None:
    """When a scenario next needs a write.

    Only the *start* of a fade counts. Once a fade is issued the bridge runs it
    to completion on its own, so there is nothing to wake up for in between.

    Args:
        plan: The plan the scenario belongs to.
        scenario: The scenario to evaluate.
        now: The instant to look forward from.
        zone: The plan's timezone.

    Returns:
        The next waypoint's start, or None if the scenario has none ahead.

    """
    for waypoint in waypoints_around(plan, scenario, now, zone):
        if waypoint.at > now:
            return waypoint.at
    return None


def in_window(
    rule: Rule,
    plan: Plan,
    now: datetime.datetime,
    zone: Zone,
) -> bool:
    """Whether a rule's ``between`` window is open.

    A window whose start is later than its end wraps midnight, so
    ``["sunset", "sunrise"]`` reads as "at night" rather than as an empty
    range.

    Args:
        rule: The rule to test.
        plan: The plan it belongs to, for the location.
        now: The instant to test.
        zone: The plan's timezone.

    Returns:
        True when the rule may fire. A rule with no window is always open, and
        so is one whose bounds fall in polar day or night -- a window that
        cannot be computed must not silently disable the rule forever.

    """
    if rule.between is None:
        return True
    day = in_zone(now, zone).date()
    start = resolve_anchor(rule.between[0], day, zone, plan.location)
    end = resolve_anchor(rule.between[1], day, zone, plan.location)
    if start is None or end is None:
        return True
    if start <= end:
        return start <= now <= end
    return now >= start or now <= end
