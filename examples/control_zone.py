"""Dim a zone to a warm glow, hold it, then put it back -- addressed by name.

    python examples/control_zone.py "Downstairs"

A zone groups lights across rooms (all your reading lamps, say), where a room
groups the lights in one physical space. They are controlled identically: this
is control_room.py pointed at hue.zones instead of hue.rooms.
"""

import asyncio
import sys

from huepy import Hue, ResourceNotFoundError

DIM_BRIGHTNESS = 30.0
WARM_KELVIN = 2200
FADE_SECONDS = 2.0
HOLD_SECONDS = 5.0


async def main() -> None:
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        raise SystemExit(1)
    zone_name = args[0]

    async with Hue() as hue:
        try:
            zone = await hue.zones.get(zone_name)
        except ResourceNotFoundError as exc:
            print(f"No zone named {exc.name!r}.")
            print(f"Known zones: {', '.join(exc.known) or 'none'}")
            raise SystemExit(1) from None

        # Capture per light: a group reports no aggregate colour temperature, so
        # restoring through it would drop the warmth. capture()/restore() handle
        # that the same way for a zone as for a room.
        before = await zone.capture()
        if not before.lights:
            print(f"{zone.name} has no lights to control.")
            raise SystemExit(1)
        print(f"Captured {len(before.lights)} lights in {zone.name}")

        print(f"Dimming {zone.name} to {DIM_BRIGHTNESS:.0f}% at {WARM_KELVIN} K...")
        await zone.set(
            on=True,
            brightness=DIM_BRIGHTNESS,
            kelvin=WARM_KELVIN,
            transition=FADE_SECONDS,
        )

        await asyncio.sleep(HOLD_SECONDS)

        print(f"Restoring {zone.name}...")
        await zone.restore(before, transition=FADE_SECONDS)


if __name__ == "__main__":
    asyncio.run(main())
