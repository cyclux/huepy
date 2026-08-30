"""Choose what a light does when its mains power comes back on.

    python examples/powerup.py "Desk lamp"

This is the behaviour after a real power cut or a wall switch -- not a command
that changes the light now. The bridge stores it, and the bulb applies it the
next time it is powered. A bare preset covers the common cases; passing any
custom field switches the light to a fully custom powerup.
"""

import asyncio
import sys

from huepy import Hue, ResourceNotFoundError
from huepy.models import PowerupOnMode, PowerupPreset

WARM_KELVIN = 2700
CUSTOM_BRIGHTNESS = 60.0

# Each set_powerup only stores a preference; nothing changes on the light now,
# so there is nothing to pause for between them.


async def main() -> None:
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        raise SystemExit(1)
    light_name = args[0]

    async with Hue() as hue:
        try:
            light = await hue.lights.get(light_name)
        except ResourceNotFoundError as exc:
            print(f"No light named {exc.name!r}.")
            print(f"Known lights: {', '.join(exc.known) or 'none'}")
            raise SystemExit(1) from None

        # The safest default: come back on, warm and bright, whatever happened.
        print("Powerup -> safety (on, warm, full brightness)...")
        await light.set_powerup(PowerupPreset.SAFETY)

        # Or restore exactly the state it was in when power was cut.
        print("Powerup -> last on-state...")
        await light.set_powerup(PowerupPreset.LAST_ON_STATE)

        # A custom powerup composes an explicit on-state, brightness and colour.
        # Passing any of these forces preset="custom" for you; on_mode picks how
        # the on/off state is decided (here, always come on).
        print(f"Powerup -> custom (on, {CUSTOM_BRIGHTNESS:.0f}%, {WARM_KELVIN} K)...")
        await light.set_powerup(
            on=True,
            on_mode=PowerupOnMode.ON,
            brightness=CUSTOM_BRIGHTNESS,
            kelvin=WARM_KELVIN,
        )

        print(f"Stored. {light.name} will use this the next time it is powered on.")


if __name__ == "__main__":
    asyncio.run(main())
