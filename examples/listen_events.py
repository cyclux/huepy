"""Print events as the bridge pushes them, parsed into models.

    python examples/listen_events.py

Runs until interrupted. The stream reconnects on its own with exponential
backoff, and drops an event it cannot parse rather than ending. For the raw
decoded payloads instead, use hue.api.raw.subscribe_events() -- and see
two_ways_events.py for what that costs you.
"""

import asyncio
import logging

from huepy import Hue

NAME_WIDTH = 24


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    async with Hue() as hue:
        print("Listening for events. Press Ctrl-C to stop.\n")
        async for event in hue.get_event_stream():
            if not event.is_update:
                # An add, a delete or an error: the ids are all there is.
                print(f"{event.type:16} {', '.join(event.resource_ids)}")
                continue
            for resource in event.data:
                # `summary` renders whichever state sections this event
                # carries, including ones huepy has no model for yet.
                name = hue.get_name(resource.id)
                print(f"{resource.type:16} {name:{NAME_WIDTH}} {resource.summary}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped.")
