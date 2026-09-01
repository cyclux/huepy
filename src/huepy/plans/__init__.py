"""Declarative light plans: a day's worth of behaviour in a TOML file.

A plan describes what a scope -- a light, a room, a zone, a whole flat --
should look like over the day, and how it reacts to what happens. The files are
the unit of modularity: one per room, one for the flat, composed by loading a
directory.

The layer leans on the bridge where the bridge is good. A single PUT accepts a
transition of up to 6000 seconds, so a two-hour sunset fade is a couple of
requests and then silence, not a tick loop -- which matters, because the bridge
budgets roughly ten writes a second to lights and one a second to groups.

Nothing here keeps durable state. After a restart or a reconnect the runner
asks the timeline where the lights *should* be at this instant and fades there,
so a crash mid-sunset recovers into the right place.

Triggers -- motion, buttons, door contacts, and signals the application fires
-- all go through one path. A rule that fires holds its scope for a while and
then hands it back to whatever curve was underneath, without a snap.

Typical usage example:

    from huepy import Hue
    from huepy.plans import PlanRunner, load_plans

    plan = load_plans("./plans")
    async with Hue(state=True) as hue, PlanRunner(hue, plan) as runner:
        await runner.run()
"""

from huepy.plans.arbiter import Arbiter, Claim, Fade, Hold, ScopeState
from huepy.plans.executor import (
    MAX_TRANSITION_SECONDS,
    Segment,
    plan_segments,
    run_fade,
)
from huepy.plans.fields import (
    ClockAnchor,
    ScopeKind,
    Selector,
    SunAnchor,
    SunEvent,
    TriggerKind,
    format_duration,
    parse_anchor,
    parse_duration,
    parse_selector,
    parse_trigger,
)
from huepy.plans.loader import load_plan, load_plans
from huepy.plans.protocol import PlanClient
from huepy.plans.resolve import Binding, ResolvedPlan, TriggerBinding, resolve
from huepy.plans.runner import PlanRunner
from huepy.plans.schema import (
    Action,
    Defaults,
    Location,
    Plan,
    Rule,
    Scenario,
    Step,
)
from huepy.plans.sun import solar_event, solar_noon
from huepy.plans.timeline import (
    Waypoint,
    current_step,
    in_window,
    interpolate,
    next_transition,
    resolve_anchor,
    target_at,
    waypoints_around,
    waypoints_for_day,
    zone_of,
)

__all__ = [
    "MAX_TRANSITION_SECONDS",
    "Action",
    "Arbiter",
    "Binding",
    "Claim",
    "ClockAnchor",
    "Defaults",
    "Fade",
    "Hold",
    "Location",
    "Plan",
    "PlanClient",
    "PlanRunner",
    "ResolvedPlan",
    "Rule",
    "Scenario",
    "ScopeKind",
    "ScopeState",
    "Segment",
    "Selector",
    "Step",
    "SunAnchor",
    "SunEvent",
    "TriggerBinding",
    "TriggerKind",
    "Waypoint",
    "current_step",
    "format_duration",
    "in_window",
    "interpolate",
    "load_plan",
    "load_plans",
    "next_transition",
    "parse_anchor",
    "parse_duration",
    "parse_selector",
    "parse_trigger",
    "plan_segments",
    "resolve",
    "resolve_anchor",
    "run_fade",
    "solar_event",
    "solar_noon",
    "target_at",
    "waypoints_around",
    "waypoints_for_day",
    "zone_of",
]
