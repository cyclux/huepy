"""Run the bridge's own animations on a light: a candle, a sunrise, a gradient.

    python examples/effects.py "Desk lamp"

Effects play on the bulb itself, not by huepy sending frames. Which ones a bulb
supports is reported in its ``effects.effect_values``; the bridge rejects one it
cannot run, so this asks first. A gradient needs a gradient-capable strip.
"""

import asyncio
import sys

from huepy import Hue, HueResponseError, ResourceNotFoundError
from huepy.models import Effect, GradientMode, TimedEffect

HOLD_SECONDS = 6.0
SUNRISE_SECONDS = 8.0
# A red-to-green sweep, as two CIE (x, y) stops the strip interpolates between.
GRADIENT_STOPS = [(0.675, 0.322), (0.409, 0.518)]


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

        # Restore whatever it was doing once the demo ends.
        before = light.capture()
        supported = light.effects.effect_values if light.effects else []

        if Effect.CANDLE in supported:
            print("Running the candle effect...")
            await light.set_effect(Effect.CANDLE)
            await asyncio.sleep(HOLD_SECONDS)
            await light.set_effect(Effect.NO_EFFECT)  # stop it
        else:
            print(f"{light.name} does not offer a candle effect; skipping.")

        # A timed effect fades like daylight over a duration, then stops itself.
        # Duration is in seconds here; huepy sends it to the bridge in ms.
        print(f"Running a {SUNRISE_SECONDS:.0f}s sunrise...")
        try:
            await light.set_timed_effect(TimedEffect.SUNRISE, duration=SUNRISE_SECONDS)
            await asyncio.sleep(SUNRISE_SECONDS)
        except HueResponseError as exc:
            print(f"  the bulb declined the timed effect: {exc}")

        # Gradients only mean anything on a strip with several colour points.
        if light.is_gradient:
            print("Painting a red-to-green gradient...")
            await light.set_gradient(
                GRADIENT_STOPS, mode=GradientMode.INTERPOLATED_PALETTE
            )
            await asyncio.sleep(HOLD_SECONDS)

        print("Restoring...")
        await light.restore(before)


if __name__ == "__main__":
    asyncio.run(main())
