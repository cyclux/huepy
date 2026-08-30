"""Pair a new light with the bridge -- the v2 way to add a bulb.

    python examples/pair_device.py

Put a new bulb into pairing mode (power-cycle it per its manual, or factory-reset
it), then run this. It asks the bridge to search, polls until the search stops,
and reports any devices that appeared. Adding a device is only possible through
the zigbee_device_discovery service; there is no other v2 route.
"""

import asyncio

from huepy import Hue

SEARCH_TIMEOUT_SECONDS = 60.0
POLL_SECONDS = 5.0


async def main() -> None:
    async with Hue() as hue:
        services = await hue.api.zigbee_device_discoveries.list()
        if not services:
            print("This bridge exposes no device-discovery service.")
            raise SystemExit(1)
        discovery = services[0]

        before = {device.id for device in await hue.api.devices.list()}

        # search() is a self-acting method on the fetched service: the bound
        # model issues the PUT itself, the same shape the handler would send.
        print("Searching for new devices -- put a bulb into pairing mode now.")
        await discovery.search()

        for _ in range(int(SEARCH_TIMEOUT_SECONDS / POLL_SECONDS)):
            await asyncio.sleep(POLL_SECONDS)
            discovery = await hue.api.zigbee_device_discoveries.get(discovery.id)
            if not discovery.is_searching:
                break

        new = [d for d in await hue.api.devices.list() if d.id not in before]
        if new:
            print(f"Paired {len(new)} new device(s):")
            for device in new:
                print(f"  {device.name}")
        else:
            print("No new devices appeared before the search stopped.")


if __name__ == "__main__":
    asyncio.run(main())
