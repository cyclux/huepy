"""Paint one light a colour written the way a human writes it, then restore it.

    python examples/color_light.py "Desk lamp"
    python examples/color_light.py "Desk lamp" "#3366ff"

huepy turns the hex string into the CIE xy pair the bridge wants, and clamps
it into the gamut this particular bulb reports -- so a colour the bulb cannot
reproduce lands on the nearest one it can, instead of on whatever the bridge
decides to substitute. See two_ways_color.py for the same task done by hand.
"""

import asyncio
import sys

from huepy import Hue, ResourceNotFoundError
from huepy.color import clamp_to_gamut, gamut_for, hex_to_rgb, rgb_to_xy

DEFAULT_HEX = "#ff8800"
FADE_SECONDS = 1.0
HOLD_SECONDS = 5.0


async def main() -> None:
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        raise SystemExit(1)
    extra = args[1:]
    wanted = extra[0] if extra else DEFAULT_HEX

    async with Hue() as hue:
        try:
            light = await hue.lights.get(args[0])
        except ResourceNotFoundError as exc:
            print(f"No light named {exc.name!r}.")
            print(f"Known lights: {', '.join(exc.known) or 'none'}")
            raise SystemExit(1) from None

        if light.color is None:
            print(f"{light.name} is a white-only bulb, so it takes no colour.")
            raise SystemExit(1)

        was = light.color.xy
        # `hex_color` is the read side of the `hex_color=` below.
        print(f"{light.name} is showing {light.hex_color}")

        # huepy.color is pure: the same conversion the command below performs,
        # run here so the script can report what the bulb will actually show.
        gamut = gamut_for(light.color.gamut_type)
        target = rgb_to_xy(hex_to_rgb(wanted))
        if gamut is not None:
            target = clamp_to_gamut(target, gamut)
        xy_text = f"({target[0]:.4f}, {target[1]:.4f})"
        print(f"Setting {wanted} -> xy {xy_text} on gamut {light.color.gamut_type}")

        # One PUT: switch on, set the colour, fade over a second.
        await light.set(on=True, hex_color=wanted, transition=FADE_SECONDS)
        await asyncio.sleep(HOLD_SECONDS)

        print("Restoring...")
        await light.set_color(was.x, was.y, transition=FADE_SECONDS)


if __name__ == "__main__":
    asyncio.run(main())
