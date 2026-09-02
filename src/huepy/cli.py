"""Command line entry point for huepy.

Five verbs, and only ``run`` writes to a bridge. That is deliberate: the thing
most likely to be wrong about a plan is the plan, and finding out should not
cost anyone a room full of lights changing colour.

Typical usage example:

    huepy plan explain ./plans --at 2026-09-01T18:00
"""

import argparse
import asyncio
import contextlib
import datetime
import json
import logging
import signal
import sys
from collections.abc import Callable, Generator, Sequence
from pathlib import Path

from huepy.client.base import Hue
from huepy.exceptions import HueError, PlanError
from huepy.models import AnyResource, NamedResource
from huepy.plans.executor import plan_segments
from huepy.plans.fields import (
    LIGHT_LEVEL_DEADBAND,
    Selector,
    TriggerKind,
    format_duration,
    lux_of_light_level,
    raw_light_level,
)
from huepy.plans.loader import load_plans
from huepy.plans.resolve import Binding, ResolvedPlan, TriggerBinding, bind
from huepy.plans.runner import PlanRunner
from huepy.plans.schema import Plan, Rule, Scenario
from huepy.plans.timeline import Zone, combine, waypoints_for_day, zone_of

EXIT_OK = 0
EXIT_FAILED = 1

DESCRIPTION = """\
Declarative light plans for a Hue bridge.

  plan check     parse the plan files without touching a bridge
  plan explain   print the day the plan describes; read-only
  plan validate  also resolve every name against the bridge
  plan run       execute the plan until interrupted
  plan schema    print the plan format as JSON Schema

Only `run` writes to a bridge, and only `validate` and `run` need one.

`huepy -v plan run PATH` logs every write the runner makes; `-vv` adds the
sleeps, the skips and the wire payloads; `-q` keeps only errors. Ctrl-C or
SIGTERM stops a running plan after the write it is on; a second Ctrl-C
forces it.
"""


def _placeholder(name: str) -> Binding:
    """Build a stand-in binding for offline output.

    Args:
        name: The scope as the plan wrote it.

    Returns:
        A binding pointing at an obviously unresolved path.

    """
    return Binding(
        selector=Selector(kind="room", name=name),
        path=f"<{name}>",
        light_ids=(),
    )


def _explain(plan: Plan, when: datetime.datetime) -> None:
    """Print the day a plan describes.

    Args:
        plan: The plan to describe.
        when: The day and instant to describe it for.

    """
    zone = zone_of(plan.location)
    local = when.astimezone(zone)
    print(f"Plan for {local.date()} ({zone if zone is not None else 'host zone'})")
    if plan.location is not None:
        print(
            f"  location: {plan.location.latitude:.4f}, {plan.location.longitude:.4f}"
        )

    for scenario in plan.scenario:
        marks: list[str] = []
        if not scenario.enabled:
            marks.append("disabled")
        if scenario.is_mode:
            marks.append(f"mode, activated by {scenario.activate_on}")
        suffix = f"  [{'; '.join(marks)}]" if marks else ""
        scopes = ", ".join(str(selector) for selector in scenario.scope)
        print(f"\n{scenario.name}  (priority {scenario.priority}){suffix}")
        print(f"  scope: {scopes}")
        _explain_flat(scenario, plan)
        _explain_steps(scenario, plan, local.date(), zone)
        for rule in scenario.rule:
            window = (
                f" between {rule.between[0]} and {rule.between[1]}"
                if rule.between is not None
                else ""
            )
            level = ""
            if rule.threshold is not None:
                side, lux = rule.threshold
                level = f" {side} {_lux(lux)}"
            hold = _hold_of(rule, scenario, plan)
            print(f"  on {rule.when}{level}{window}: {rule.set.describe()}, {hold}")


def _explain_flat(scenario: Scenario, plan: Plan) -> None:
    """Print a scenario's flat ``set``, and when it applies.

    Args:
        scenario: The scenario.
        plan: The plan, for the default ramp.

    """
    if scenario.set is None:
        return
    ramp = scenario.ramp if scenario.ramp is not None else plan.defaults.ramp
    if scenario.is_mode:
        label = "while active"
    elif scenario.step:
        label = "when no step is in force"
    else:
        label = "always"
    print(f"  {label}: {scenario.set.describe()}  (ramp {format_duration(ramp)})")


def _explain_steps(
    scenario: Scenario, plan: Plan, day: datetime.date, zone: Zone
) -> None:
    """Print a scenario's day curve for one day, with the requests it costs.

    Args:
        scenario: The scenario.
        plan: The plan it belongs to.
        day: The local day to describe.
        zone: The plan's timezone.

    """
    found = waypoints_for_day(plan, scenario, day, zone)
    if not found and scenario.step:
        print("  no steps today")
    # The day's first step fades from yesterday's last, exactly as the runner
    # would chain it; without that start a long first step would be costed as
    # one request when two would really go out.
    earlier = waypoints_for_day(plan, scenario, day - datetime.timedelta(days=1), zone)
    previous = earlier[-1].action if earlier else None
    for waypoint in found:
        # Chained exactly the way the runner would chain it: each step fades
        # from the one before, so the request count printed here is the count
        # that would really go out.
        segments = plan_segments(
            _placeholder(scenario.name),
            waypoint.action,
            ramp=waypoint.ramp,
            start=previous,
        )
        previous = waypoint.action
        requests = "1 request" if len(segments) == 1 else f"{len(segments)} requests"
        span = (
            f"{waypoint.at.strftime('%H:%M:%S')} -> "
            f"{waypoint.ends_at.strftime('%H:%M:%S')}"
        )
        print(f"  {span}  {waypoint.action.describe()}  ({requests})")


def _lux(value: float) -> str:
    """Render an illuminance the way a plan author would write it.

    Args:
        value: Lux.

    Returns:
        Whole lux from ten up, one decimal below.

    """
    return f"{value:.0f} lux" if value >= 10 else f"{value:.1f} lux"  # noqa: PLR2004 - the point where a decimal stops meaning anything


def _hold_of(rule: Rule, scenario: Scenario, plan: Plan) -> str:
    """Say how long a rule keeps its scope, as the runner would decide it.

    Args:
        rule: The rule.
        scenario: The scenario it belongs to.
        plan: The plan, to see what else covers the scenario's scopes.

    Returns:
        A short phrase.

    """
    if rule.hold is not None:
        held = format_duration(rule.hold)
        if rule.when.kind == TriggerKind.MOTION:
            return f"hold {held} after motion stops"
        if rule.threshold is not None:
            side, lux = rule.threshold
            raw = raw_light_level(lux)
            if side == "below":
                release = lux_of_light_level(raw + LIGHT_LEVEL_DEADBAND)
                return f"hold {held} after it brightens past {_lux(release)}"
            release = lux_of_light_level(raw - LIGHT_LEVEL_DEADBAND)
            return f"hold {held} after it darkens past {_lux(release)}"
        return f"hold {held}"
    covered = {str(selector) for selector in scenario.scope}
    scheduled = any(
        other.step and covered & {str(selector) for selector in other.scope}
        for other in plan.scenario
    )
    return "until the next step" if scheduled else "until released"


RESOURCE_PREFIX = "/clip/v2/resource/"


def _trigger_target(trigger: TriggerBinding) -> str:
    """Say what a trigger was bound to.

    Args:
        trigger: The bound trigger.

    Returns:
        A short phrase.

    """
    if trigger.is_signal:
        return "application signal"
    ids = trigger.resource_ids
    if len(ids) == 1:
        return f"{trigger.selector.kind} {ids[0]}"
    return f"{len(ids)} {trigger.selector.kind} services: {', '.join(ids)}"


def _binding_report(
    plan: Plan, resolved: ResolvedPlan, resources: list[AnyResource]
) -> list[str]:
    """Lay out what every name in a plan was bound to.

    This is what an operator reads before the first run: that ``room:Study``
    really is the four lights they meant, that the dimmer has four button
    services, and that the sensor they named is not switched off in the app.

    Args:
        plan: The plan, for scenario order.
        resolved: The plan with every name bound.
        resources: The snapshot it was bound against, for display names.

    Returns:
        The lines to print, summary first.

    """
    names = {r.id: r.name for r in resources if isinstance(r, NamedResource)}
    scopes = sum(len(bindings) for bindings in resolved.scopes.values())
    summary = (
        f"OK: {len(plan.scenario)} scenarios, {scopes} scopes, "
        f"{len(resolved.triggers)} triggers all resolve"
    )
    lines = [summary, "scopes"]
    width = max((len(scenario.name) for scenario in plan.scenario), default=0)
    for scenario in plan.scenario:
        for binding in resolved.scope_of(scenario):
            count = len(binding.light_ids)
            noun = "light" if count == 1 else "lights"
            lights = ", ".join(names.get(light, light) for light in binding.light_ids)
            target = binding.path.removeprefix(RESOURCE_PREFIX)
            line = (
                f"  {scenario.name:<{width}}  {binding.selector} -> {target}  "
                f"({count} {noun}: {lights})"
            )
            lines.append(line)
    if resolved.triggers:
        lines.append("triggers")
        width = max(len(key) for key in resolved.triggers)
        lines.extend(
            f"  {key:<{width}} -> {_trigger_target(trigger)}"
            for key, trigger in resolved.triggers.items()
        )
    if resolved.warnings:
        lines.append("warnings")
        lines.extend(f"  {warning}" for warning in resolved.warnings)
    return lines


async def _validate(path: str) -> int:
    """Resolve a plan's names against a real bridge, and say what they bound to.

    Args:
        path: The plan file or directory.

    Returns:
        A process exit code.

    """
    plan = load_plans(path)
    async with Hue() as hue:
        resources = await hue.snapshot()
    resolved = bind(resources, plan)
    for line in _binding_report(plan, resolved, resources):
        print(line)
    return EXIT_OK


@contextlib.contextmanager
def _stopping_on_signals(stop: Callable[[], None]) -> Generator[None]:
    """Turn SIGINT and SIGTERM into a call to ``stop`` while the body runs.

    Without this, SIGTERM -- what systemd and ``kill`` send -- ends the
    process on the spot: no ``close()``, the bridge session and the event
    stream dropped mid-frame, the tail of a chained fade lost. Ctrl-C only
    looks graceful because asyncio turns it into a cancellation that happens
    to unwind through the context managers.

    The handlers are removed on the way out, so a second Ctrl-C during the
    shutdown itself is the ordinary ``KeyboardInterrupt`` again: the force
    path, for when a bridge stops answering.

    Args:
        stop: What to call when either signal arrives. Runs on the loop, so
            it may touch loop state directly.

    Yields:
        Nothing; the body runs with the handlers installed.

    """
    loop = asyncio.get_running_loop()
    installed: list[signal.Signals] = []
    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop)
            except NotImplementedError:
                # Windows' Proactor loop has no signal handlers. Ctrl-C then
                # still arrives as KeyboardInterrupt, which main() handles.
                break
            installed.append(sig)
        yield
    finally:
        for sig in installed:
            _ = loop.remove_signal_handler(sig)


async def _run(path: str) -> int:
    """Run a plan until stopped.

    Args:
        path: The plan file or directory.

    Returns:
        A process exit code.

    """
    plan = load_plans(path)
    async with Hue(state=True) as hue:
        # The same report `validate` prints, so the log that follows can be
        # read against what each name meant. The runner resolves once more
        # on start; a second snapshot is cheap next to a first run.
        resources = await hue.snapshot()
        for line in _binding_report(plan, bind(resources, plan), resources):
            print(line)
        runner = PlanRunner(hue, plan, changes=hue.state)
        async with runner:
            print(f"Running {len(plan.scenario)} scenarios. Ctrl-C to stop.")
            with _stopping_on_signals(runner.stop):
                await runner.run()
            print("Stopping.")
    return EXIT_OK


def _log_level(verbose: int, *, quiet: bool) -> int:
    """Map the verbosity flags to a logging level.

    Args:
        verbose: How many times ``-v`` was given.
        quiet: Whether ``-q`` was given.

    Returns:
        The level for the root logger.

    """
    if quiet:
        return logging.ERROR
    if verbose >= 2:  # noqa: PLR2004 - `-vv` is the spelling, not a magic number
        return logging.DEBUG
    return logging.INFO if verbose else logging.WARNING


def _print_schema(_args: argparse.Namespace) -> int:
    """Print the plan format as JSON Schema.

    Args:
        _args: Parsed arguments, unused.

    Returns:
        A process exit code.

    """
    print(json.dumps(Plan.model_json_schema(), indent=2))
    return EXIT_OK


def _check(args: argparse.Namespace) -> int:
    """Parse the plan files without touching a bridge.

    Args:
        args: Parsed arguments.

    Returns:
        A process exit code.

    """
    plan = load_plans(args.path)
    print(f"OK: {Path(args.path).name} parses, {len(plan.scenario)} scenarios")
    return EXIT_OK


def _explain_command(args: argparse.Namespace) -> int:
    """Print the day the plan describes.

    Args:
        args: Parsed arguments.

    Returns:
        A process exit code.

    """
    plan = load_plans(args.path)
    try:
        when = _parse_at(args.at, zone_of(plan.location))
    except ValueError as error:
        msg = f"--at {args.at!r} is not an ISO timestamp: {error}"
        raise PlanError(msg) from error
    _explain(plan, when)
    return EXIT_OK


def _parse_at(text: str | None, zone: Zone) -> datetime.datetime:
    """Read the instant ``--at`` names, in the plan's own clock.

    Args:
        text: The argument as given, or None for now.
        zone: The plan's zone, or None for the host's own.

    Returns:
        An aware instant.

    Raises:
        ValueError: If the text is not an ISO timestamp.

    """
    if text is None:
        return datetime.datetime.now(datetime.UTC).astimezone()
    parsed = datetime.datetime.fromisoformat(text)
    if parsed.tzinfo is not None:
        return parsed
    # A bare clock time means the plan's clock, not the host's. Read in the
    # host's zone, `--at 2026-09-01` for a plan in Los Angeles described
    # 31 August from anywhere east of it.
    return combine(parsed.date(), parsed.time(), zone)


def _build_parser() -> argparse.ArgumentParser:
    """Build the argument parser.

    Returns:
        The configured parser.

    """
    parser = argparse.ArgumentParser(
        prog="huepy",
        description=DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _ = parser.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="log every write the runner makes; -vv adds sleeps, skips and payloads",
    )
    _ = parser.add_argument(
        "-q", "--quiet", action="store_true", help="log nothing but errors"
    )
    verbs = parser.add_subparsers(dest="group", required=True)
    plan = verbs.add_parser("plan", help="work with declarative light plans")
    actions = plan.add_subparsers(dest="action", required=True)

    for name, help_text in (
        ("check", "parse the plan files without touching a bridge"),
        ("validate", "also resolve every name against the bridge"),
        ("run", "execute the plan until interrupted"),
    ):
        sub = actions.add_parser(name, help=help_text)
        _ = sub.add_argument("path", help="a .toml plan file, or a directory of them")

    explain = actions.add_parser("explain", help="print the day the plan describes")
    _ = explain.add_argument("path", help="a .toml plan file, or a directory of them")
    _ = explain.add_argument(
        "--at",
        help="the instant to describe, in ISO format. Defaults to now",
    )
    _ = actions.add_parser("schema", help="print the plan format as JSON Schema")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command line interface.

    Args:
        argv: Arguments to parse. Defaults to ``sys.argv``.

    Returns:
        A process exit code.

    """
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=_log_level(args.verbose, quiet=args.quiet),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    offline = {
        "schema": _print_schema,
        "check": _check,
        "explain": _explain_command,
    }
    online = {"validate": _validate, "run": _run}

    try:
        handler = offline.get(args.action)
        if handler is not None:
            return handler(args)
        return asyncio.run(online[args.action](args.path))
    except HueError as error:
        print(f"error: {error}", file=sys.stderr)
        return EXIT_FAILED
    except KeyboardInterrupt:
        print("\nStopped.")
        return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
