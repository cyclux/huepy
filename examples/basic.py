"""List every light on the bridge with its current state.

    python examples/basic.py

Settings come from the config file written by examples/authenticate.py, or
from HUE_BRIDGE_IP / HUE_APP_KEY.
"""

import asyncio

from huepy import Hue


async def main() -> None:
    async with Hue() as hue:
        # Top-level collections are the normal, human-facing API. They return
        # the same validated models as hue.api, but consistently use names for
        # lookup and commands instead of exposing bridge ids.
        lights = await hue.lights.list()
        print(f"Found {len(lights)} lights\n")

        for light in lights:
            state = "on " if light.is_on else "off"
            brightness = (
                "    -" if light.brightness is None else f"{light.brightness:5.1f}%"
            )
            mirek = "   -" if light.mirek is None else f"{light.mirek:4d}"
            print(f"{state}  {brightness}  {mirek} mirek  {light.name}")


if __name__ == "__main__":
    asyncio.run(main())
