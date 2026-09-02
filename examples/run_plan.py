"""Run a declarative light plan.

    python examples/run_plan.py                       # the bundled example
    python examples/run_plan.py path/to/plans

The plan file says what each room should look like through the day. This script
resolves it against the bridge, prints the schedule it worked out, and then runs
it until you press Ctrl-C.

Nothing is written until the plan resolves, so a misspelled room name fails
here rather than half-way through the evening.
"""

import asyncio
import datetime
import sys
from pathlib import Path

from huepy import Hue, PlanError, PlanRunner, load_plans
from huepy.plans import Plan, waypoints_for_day, zone_of

DEFAULT_PLAN = Path(__file__).parent / "plans" / "flat.toml"


def show_schedule(plan: Plan) -> None:
    """Print the waypoints each scenario will hit today."""
    zone = zone_of(plan.location)
    today = datetime.datetime.now(zone).date()
    print(f"Schedule for {today}:")
    for scenario in plan.scenario:
        for waypoint in waypoints_for_day(plan, scenario, today, zone):
            when = waypoint.at.strftime("%H:%M")
            state = ", ".join(
                f"{key}={value}"
                for key, value in waypoint.action.model_dump(exclude_none=True).items()
            )
            print(f"  {when}  {scenario.name}: {state}")


async def main() -> None:
    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_PLAN

    try:
        plan = load_plans(path)
    except PlanError as error:
        print(f"That plan will not load:\n{error}")
        return

    show_schedule(plan)

    async with Hue(state=True) as hue:
        try:
            # `changes=hue.state` is what lets the plan stand back when
            # someone adjusts a light by hand.
            async with PlanRunner(hue, plan, changes=hue.state) as runner:
                print("\nRunning. Press Ctrl-C to stop.")
                await runner.run()
        except PlanError as error:
            # Every unresolved name at once, rather than one per run.
            print(f"\nThe plan does not match this bridge:\n{error}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped.")
