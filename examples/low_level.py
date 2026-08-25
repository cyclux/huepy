"""Inspect the bridge through the lower-level typed and raw APIs.

    python examples/low_level.py
    python examples/low_level.py <room-id>

The top-level collections in the other examples are preferable when people
address resources by name. ``hue.api`` is useful when an application already
stores bridge ids, needs a resource type without a high-level collection, or
must send a CLIP operation huepy does not model yet.

This example is read-only. With no argument it inspects the first room after
listing every room id; pass one of those ids to select another.
"""

import asyncio
import json
import sys

from huepy import Hue, models


async def main() -> None:
    async with Hue() as hue:
        # Typed lower level: plural handlers uniformly list by resource type
        # and get by bridge id. The returned values are still pydantic models.
        rooms = await hue.api.rooms.list()
        if not rooms:
            print("The bridge has no rooms.")
            return

        print("Rooms (name -> id):")
        for room in rooms:
            print(f"  {room.name} -> {room.id}")

        room_id = sys.argv[1] if len(sys.argv) > 1 else rooms[0].id
        room = await hue.api.rooms.get(room_id)
        grouped_light_id = room.service_id(models.ResourceType.GROUPED_LIGHT)

        print(f"\nSelected {room.name!r} by id: {room.id}")
        print(f"grouped_light service: {grouped_light_id or 'none'}")

        if grouped_light_id is not None:
            grouped_light = await hue.api.grouped_lights.get(grouped_light_id)
            brightness = (
                grouped_light.dimming.brightness
                if grouped_light.dimming is not None
                else None
            )
            state = "on" if grouped_light.is_on else "off"
            level = f" at {brightness}%" if brightness is not None else ""
            print(f"group state: {state}{level}")

        # Raw lower level: the same authenticated transport, but no model
        # parsing. Use this escape hatch for a new or deliberately untyped
        # CLIP operation; prefer the typed handler above whenever it exists.
        payload = await hue.api.raw.get(f"/clip/v2/resource/room/{room.id}")
        print("\nRaw decoded response:")
        print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
