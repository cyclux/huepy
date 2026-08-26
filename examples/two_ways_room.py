"""Dim one room, twice: through the id-addressed API, and through names.

    python examples/two_ways_room.py "Living Room"

Both halves do the identical thing -- resolve the room, list its lights, save
their state, dim to a warm glow, then put it all back. The long way spells out
every step the short way performs for you: the name match, the grouped_light
hop, the Kelvin and seconds conversions, the children-to-lights join, and the
per-light restore.

`hue.api` is not deprecated by any of this. It is what you reach for when you
already hold bridge ids, need a resource type with no named collection, or must
send a CLIP operation huepy does not model yet -- see low_level.py.
"""

import asyncio
import sys

from huepy import Hue, ResourceNotFoundError, models
from huepy.color import kelvin_to_mirek

DIM_BRIGHTNESS = 30.0
WARM_KELVIN = 2200
FADE_SECONDS = 2.0
HOLD_SECONDS = 4.0
MILLISECONDS = 1000
EXPECTED_ARGS = 2


async def the_long_way(hue: Hue, wanted: str) -> None:
    """Do it with ids, hand-built payloads and hand-converted units."""
    rooms = await hue.api.rooms.list()
    matches = [room for room in rooms if room.name.strip().casefold() == wanted]
    if not matches:
        print(f"  no room named {wanted!r}; known: {[r.name for r in rooms]}")
        return
    room = matches[0]

    # A room takes no light command itself; its grouped_light service does.
    group_id = room.service_id(models.ResourceType.GROUPED_LIGHT)
    if group_id is None:
        print(f"  {room.name} has no grouped_light service")
        return

    # A room's children are devices; its lights are the services those devices
    # expose, so the two are matched up through each light's owner.
    device_ids = {child.rid for child in room.children}
    lights = [
        light for light in await hue.api.lights.list() if light.device_id in device_ids
    ]
    before = {light.id: light.capture() for light in lights}
    print(f"  {room.name}: {len(lights)} lights, group {group_id}")

    # Kelvin is mirek on the wire and seconds are milliseconds, so both are
    # converted here before the payload can be assembled.
    await hue.api.grouped_lights.update(
        group_id,
        {
            "on": {"on": True},
            "dimming": {"brightness": DIM_BRIGHTNESS},
            "color_temperature": {"mirek": kelvin_to_mirek(WARM_KELVIN)},
            "dynamics": {"duration": int(FADE_SECONDS * MILLISECONDS)},
        },
    )
    await asyncio.sleep(HOLD_SECONDS)

    # Per light, not through the group: a grouped_light reports no aggregate
    # colour temperature, so a group restore drops it and leaves the room the
    # wrong colour.
    for light in lights:
        await light.restore(before[light.id], transition=FADE_SECONDS)


async def the_short_way(hue: Hue, wanted: str) -> None:
    """Do it by name, in the units you would say out loud."""
    try:
        room = await hue.rooms.get(wanted)
    except ResourceNotFoundError as exc:
        print(f"  no room named {exc.name!r}; known: {exc.known}")
        return

    before = await room.capture()
    print(f"  {room.name}: {len(before.lights)} lights")

    await room.set(
        on=True,
        brightness=DIM_BRIGHTNESS,
        kelvin=WARM_KELVIN,
        transition=FADE_SECONDS,
    )
    await asyncio.sleep(HOLD_SECONDS)
    await room.restore(before, transition=FADE_SECONDS)


async def main() -> None:
    if len(sys.argv) < EXPECTED_ARGS:
        print(__doc__)
        raise SystemExit(1)

    async with Hue() as hue:
        print("the long way (hue.api, ids, hand-built payloads):")
        await the_long_way(hue, sys.argv[1].strip().casefold())
        print("\nthe short way (names, human units):")
        await the_short_way(hue, sys.argv[1])
        print("\nSame lights, same two fades.")


if __name__ == "__main__":
    asyncio.run(main())
