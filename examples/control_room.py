"""Dim a room to a warm glow, hold it, then put it back -- addressed by name.

    python examples/control_room.py "Living Room"

No resource id appears anywhere below. The room is resolved from the name you
gave it on the bridge, and the dim travels as a single request. See
two_ways_room.py for the same task written against the id-addressed API.
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

    async with Hue() as hue:
        try:
            room = await hue.rooms.get(args[0])
        except ResourceNotFoundError as exc:
            # The library did the matching, and knows what it could have
            # matched against -- so the typo corrects itself.
            print(f"No room named {exc.name!r}.")
            print(f"Known rooms: {', '.join(exc.known) or 'none'}")
            raise SystemExit(1) from None

        # Per light, not per room: a room's `grouped_light` reports no aggregate
        # colour temperature, so restoring through the group silently drops it
        # and leaves the room the wrong colour. `capture()` handles that, and
        # picks the one of colour/temperature each light is actually in.
        before = await room.capture()
        if not before.lights:
            print(f"{room.name} has no lights to control.")
            raise SystemExit(1)
        print(f"Captured {len(before.lights)} lights in {room.name}")

        print(f"Dimming {room.name} to {DIM_BRIGHTNESS:.0f}% at {WARM_KELVIN} K...")
        # One PUT carries the complete change. The high-level API resolves the
        # room's name, routes through its grouped_light service, translates
        # Kelvin and seconds to bridge units, and builds the CLIP payload.
        await room.set(
            on=True,
            brightness=DIM_BRIGHTNESS,
            kelvin=WARM_KELVIN,
            transition=FADE_SECONDS,
        )

        await asyncio.sleep(HOLD_SECONDS)

        print(f"Restoring {room.name}...")
        # Concurrent, one request per light, skipping any that has since left.
        await room.restore(before, transition=FADE_SECONDS)


if __name__ == "__main__":
    asyncio.run(main())
