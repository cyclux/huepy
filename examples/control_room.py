"""Dim a room to a warm glow, hold it, then put it back -- addressed by name.

    python examples/control_room.py "Living Room"

No resource id appears anywhere below. The room is resolved from the name you
gave it on the bridge, and the dim travels as a single request.
"""

import asyncio
import sys

from huepy import Hue, ResourceNotFoundError, models

DIM_BRIGHTNESS = 30.0
WARM_KELVIN = 2200
FADE_SECONDS = 2.0
HOLD_SECONDS = 5.0
EXPECTED_ARGS = 2


async def members(hue: Hue, room: models.Room) -> list[models.Light]:
    """Return the room's own lights.

    A room's `children` are devices; the lights are the services those devices
    expose, so the two are matched up through each light's `device_id`.
    """
    devices = {child.rid for child in room.children}
    return [light for light in await hue.lights.list() if light.device_id in devices]


async def main() -> None:
    if len(sys.argv) < EXPECTED_ARGS:
        print(__doc__)
        raise SystemExit(1)

    async with Hue() as hue:
        try:
            room = await hue.rooms.get(sys.argv[1])
        except ResourceNotFoundError as exc:
            # The library did the matching, and knows what it could have
            # matched against -- so the typo corrects itself.
            print(f"No room named {exc.name!r}.")
            print(f"Known rooms: {', '.join(exc.known) or 'none'}")
            raise SystemExit(1) from None

        lights = await members(hue, room)
        if not lights:
            print(f"{room.name} has no lights to control.")
            raise SystemExit(1)
        # Per light, not per room: a room's `grouped_light` reports no aggregate
        # colour temperature, so restoring through the group silently drops it
        # and leaves the room the wrong colour. `capture()` also picks the one
        # of colour/temperature the light is actually in, which `set()` requires.
        before = {light.id: light.capture() for light in lights}

        print(f"Dimming {room.name} to {DIM_BRIGHTNESS:.0f}% at {WARM_KELVIN} K...")
        # One PUT carries the complete change. The high-level API resolves the
        # room's name, routes through its grouped_light service, translates
        # Kelvin and seconds to bridge units, and builds the CLIP payload. See
        # low_level.py for those same concerns expressed explicitly.
        await room.set(
            on=True,
            brightness=DIM_BRIGHTNESS,
            kelvin=WARM_KELVIN,
            transition=FADE_SECONDS,
        )

        await asyncio.sleep(HOLD_SECONDS)

        print(f"Restoring {room.name}...")
        for light in lights:
            await light.restore(before[light.id], transition=FADE_SECONDS)


if __name__ == "__main__":
    asyncio.run(main())
