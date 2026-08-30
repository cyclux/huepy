"""Find a light and nudge it: identify, signal, alert, and relative deltas.

    python examples/attention.py "Desk lamp"

These are the "get a light's attention" and "adjust from wherever it is now"
verbs. A relative delta (adjust_brightness / adjust_color_temperature) changes
the light without huepy first reading its current value -- the bridge does the
arithmetic, so two of them never race against a stale read.
"""

import asyncio
import sys

from huepy import Hue, HueResponseError, ResourceNotFoundError
from huepy.models import Signal

SIGNAL_SECONDS = 4.0
HOLD_SECONDS = 3.0
BRIGHTER_BY = 20.0
WARMER_BY_MIREK = 50


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

        # identify() asks the bulb to blink so you can tell which one it is.
        # Not every bulb supports it, so treat a refusal as information.
        print("Identifying (which bulb is this?)...")
        try:
            await light.identify()
        except HueResponseError as exc:
            print(f"  the bulb declined identify: {exc}")
        await asyncio.sleep(HOLD_SECONDS)

        # A signal runs for a set time; alert() is a single breathe. Both differ
        # from identify in that you choose how long, or a colour, to draw the eye.
        print(f"Blinking on/off for {SIGNAL_SECONDS:.0f}s...")
        await light.signal(Signal.ON_OFF, duration=SIGNAL_SECONDS)
        await asyncio.sleep(SIGNAL_SECONDS)

        print("A single alert breathe...")
        await light.alert()
        await asyncio.sleep(HOLD_SECONDS)

        # Relative nudges: no read first, the bridge adds the delta to whatever
        # the light is at. Make sure it is on so a brightness change is visible.
        await light.turn_on()
        print(f"Nudging brightness up {BRIGHTER_BY:.0f} points...")
        await light.adjust_brightness(BRIGHTER_BY)
        print(f"Nudging {WARMER_BY_MIREK} mirek warmer...")
        await light.adjust_color_temperature(WARMER_BY_MIREK)


if __name__ == "__main__":
    asyncio.run(main())
