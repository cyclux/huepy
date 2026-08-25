"""Print the bridge's maintained state and every subsequent change."""

import asyncio

from huepy import Hue
from huepy.state import Change, Resync


async def main() -> None:
    async with Hue() as hue, hue.state() as state:
        for room in state.rooms.list():
            lights = ", ".join(light.name for light in state.lights_in(room))
            print(f"{room.name}: {lights or '(no resolvable lights)'}")

        async for item in state.changes():
            if isinstance(item, Resync):
                print(
                    f"possible gap: {item.reason} {item.gap_started}..{item.gap_ended}"
                )
                continue
            if isinstance(item, Change):
                print(
                    item.event_id,
                    item.kind,
                    state.name_of(item.resource_id),
                    item.delta,
                )


if __name__ == "__main__":
    asyncio.run(main())
