"""Find Hue bridges on the local network.

    python examples/discover_bridge.py

Tries mDNS first, then the Hue cloud endpoint. Prints each bridge's id and
address; store the address (and id, for TLS pinning) to skip discovery next
time, e.g. via HueConfig.save().
"""

import asyncio

from huepy import discover


async def main() -> None:
    bridges = await discover()
    if not bridges:
        print("No Hue bridge found. Check the network, or enter the IP manually.")
        return

    print(f"Found {len(bridges)} bridge(s):\n")
    for bridge in bridges:
        model = bridge.model_id or "?"
        version = bridge.sw_version or "?"
        print(f"  {bridge.bridge_id}  {bridge.ip}  (model {model}, fw {version})")


if __name__ == "__main__":
    asyncio.run(main())
