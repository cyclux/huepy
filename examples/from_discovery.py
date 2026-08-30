"""Discover a bridge and connect to it in one call.

    python examples/from_discovery.py

Hue.from_discovery() runs discovery, picks the single bridge it finds, and
returns an unstarted client with the address and bridge id already filled in --
so verified TLS can pin the bridge id from the first request. Enter it like any
other client. If several bridges are found, it asks you to pass ``index=``.
"""

import asyncio

from huepy import AuthenticationError, BridgeConnectionError, Hue


async def main() -> None:
    try:
        hue = await Hue.from_discovery()
    except BridgeConnectionError as exc:
        print(f"Discovery failed: {exc}")
        print("If you have more than one bridge, pass index=0, 1, ... to choose.")
        raise SystemExit(1) from None

    async with hue:
        print(f"Connected to bridge {hue.config.bridge_id} at {hue.config.bridge_ip}")
        try:
            lights = await hue.lights.list()
        except AuthenticationError:
            print("No application key yet -- run examples/authenticate.py to pair.")
            return
        print(f"It has {len(lights)} lights.")


if __name__ == "__main__":
    asyncio.run(main())
