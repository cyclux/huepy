"""Print the bridge's maintained state and react to every subsequent change.

    python examples/track_state.py
    python examples/track_state.py "Living Room"   # only that room

No `isinstance` guard and no ids: changes and continuity markers go to their
own handlers, the resource is named the way you named it on the bridge, and
`change.summary` renders the delta in the units you set it in.
"""

import asyncio
import sys

from huepy import Hue
from huepy.state import Change, Resync

WAIT_SECONDS = 30.0


async def main() -> None:
    room = sys.argv[1] if len(sys.argv) > 1 else None

    async with Hue(state=True) as hue:
        state = hue.state
        for known in state.rooms.list():
            lights = ", ".join(light.name for light in state.lights_in(known))
            print(f"{known.name}: {lights or '(no resolvable lights)'}")

        def on_change(change: Change) -> None:
            context = state.describe(change)
            where = context.room.name if context.room is not None else "-"
            print(change.event_id, change.kind, context.name, where, change.summary)

        def on_gap(marker: Resync) -> None:
            window = f"{marker.gap_started}..{marker.gap_ended}"
            print(f"possible gap: {marker.reason} {window}")

        # `room=` filters on topology the library already resolves, including
        # for a delete, whose resource has left the graph by the time you see it.
        state.on_change(on_change, room=room)
        state.on_resync(on_gap)

        if room is not None:
            print(f"\nWaiting up to {WAIT_SECONDS:.0f}s for a light in {room}...")
            try:
                first = await state.wait_for(room=room, timeout=WAIT_SECONDS)
            except TimeoutError:
                print("Nothing changed in time.")
            else:
                print(f"First change was {state.describe(first).name}: {first.summary}")

        await asyncio.Event().wait()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped.")
