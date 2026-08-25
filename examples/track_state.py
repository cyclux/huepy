"""Print the bridge's maintained state and react to every subsequent change.

No `isinstance` guard and no ids: changes and continuity markers go to their
own handlers, and the resource is named the way you named it on the bridge.
"""

import asyncio

from huepy import Hue
from huepy.state import Change, Resync


async def main() -> None:
    async with Hue(state=True) as hue:
        state = hue.state
        for room in state.rooms.list():
            lights = ", ".join(light.name for light in state.lights_in(room))
            print(f"{room.name}: {lights or '(no resolvable lights)'}")

        def on_change(change: Change) -> None:
            context = state.describe(change)
            room = context.room.name if context.room is not None else "-"
            print(change.event_id, change.kind, context.name, room, change.delta)

        def on_gap(marker: Resync) -> None:
            window = f"{marker.gap_started}..{marker.gap_ended}"
            print(f"possible gap: {marker.reason} {window}")

        state.on_change(on_change)
        state.on_resync(on_gap)

        await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
