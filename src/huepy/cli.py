"""Command line entry point for huepy.

Five verbs, and only ``run`` writes to a bridge. That is deliberate: the thing
most likely to be wrong about a plan is the plan, and finding out should not
cost anyone a room full of lights changing colour.

Typical usage example:

    huepy plan explain ./plans --at 2026-09-01T18:00
"""

import argparse
import asyncio
import datetime
import json
import logging
import sys
from collections.abc import Sequence
from pathlib import Path

from huepy.client.base import Hue
from huepy.exceptions import HueError, PlanError
from huepy.plans.executor import plan_segments
from huepy.plans.fields import Selector, TriggerKind, format_duration
from huepy.plans.loader import load_plans
from huepy.plans.resolve import Binding, resolve
from huepy.plans.runner import PlanRunner
from huepy.plans.schema import Action, Plan, Rule, Scenario
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
            hold = _hold_of(rule, scenario, plan)
            print(f"  on {rule.when}{window}: {_describe(rule.set)}, {hold}")


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
    print(f"  {label}: {_describe(scenario.set)}  (ramp {format_duration(ramp)})")


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
        print(f"  {span}  {_describe(waypoint.action)}  ({requests})")


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
        return f"hold {held}"
    covered = {str(selector) for selector in scenario.scope}
    scheduled = any(
        other.step and covered & {str(selector) for selector in other.scope}
        for other in plan.scenario
    )
    return "until the next step" if scheduled else "until released"


def _describe(action: Action) -> str:
    """Render an action as the few fields it actually sets.

    Args:
        action: The action to render.

    Returns:
        A compact description.

    """
    parts = [
        f"{field}={value:.0f}" if field == "brightness" else f"{field}={value}"
        for field, value in action.model_dump(exclude_none=True).items()
    ]
    return " ".join(parts) or "nothing"


async def _validate(path: str) -> int:
    """Resolve a plan's names against a real bridge.

    Args:
        path: The plan file or directory.

    Returns:
        A process exit code.

    """
    plan = load_plans(path)
    async with Hue() as hue:
        resolved = await resolve(hue, plan)
    scopes = sum(len(bindings) for bindings in resolved.scopes.values())
    summary = (
        f"OK: {len(plan.scenario)} scenarios, {scopes} scopes, "
        f"{len(resolved.triggers)} triggers all resolve"
    )
    print(summary)
    return EXIT_OK


async def _run(path: str) -> int:
    """Run a plan until interrupted.

    Args:
        path: The plan file or directory.

    Returns:
        A process exit code.

    """
    plan = load_plans(path)
    async with Hue(state=True) as hue:
        runner = PlanRunner(hue, plan, changes=hue.state)
        async with runner:
            print(f"Running {len(plan.scenario)} scenarios. Ctrl-C to stop.")
            await runner.run()
    return EXIT_OK


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
        "-v", "--verbose", action="store_true", help="log what the runner is doing"
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
        level=logging.INFO if args.verbose else logging.WARNING,
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
