"""Paint one light a hex colour, twice: by hand, and in one call.

    python examples/two_ways_color.py "Desk lamp"
    python examples/two_ways_color.py "Desk lamp" "#3366ff"

Both halves send the same colour to the same bulb. The long way performs every
conversion explicitly -- hex to RGB to CIE xy, clamped into the gamut this
particular bulb reports, seconds to milliseconds -- and reads the current
colour back the same way. The short way passes the hex string straight to
`set()` and reads `light.hex_color`.

`huepy.color` stays public and pure for exactly this reason: when you need to
know what the bulb will actually show *before* you send it, as color_light.py
does, the same functions the library uses internally are right there.
"""

import asyncio
import sys
from typing import Any

from huepy import Hue, ResourceNotFoundError, models
from huepy.color import (
    clamp_to_gamut,
    gamut_for,
    hex_to_rgb,
    rgb_to_hex,
    rgb_to_xy,
    xy_to_rgb,
)

DEFAULT_HEX = "#ff8800"
FADE_SECONDS = 1.0
HOLD_SECONDS = 3.0
MILLISECONDS = 1000


async def the_long_way(hue: Hue, light: models.Light, wanted: str) -> None:
    """Convert, clamp and build the payload yourself, then PUT it by id."""
    if light.color is None:
        print("  white-only bulb, no colour to set")
        return

    # Reading the current colour back means undoing the same conversion by
    # hand, and brightness is part of it: xy alone does not make an RGB value.
    was = light.color.xy
    shown = (
        rgb_to_hex(xy_to_rgb((was.x, was.y), brightness=light.brightness))
        if light.brightness is not None
        else None
    )
    print(f"  showing {shown}")

    # Everything the restore below has to put back, remembered by hand -- the
    # power state included, since the PUT below switches the light on. This is
    # the dict `light.capture()` builds for you, and the piece most easily got
    # wrong: forget `on` here and a light that started off stays lit.
    before: dict[str, Any] = {
        "on": {"on": light.is_on},
        "color": {"xy": {"x": was.x, "y": was.y}},
        "dynamics": {"duration": int(FADE_SECONDS * MILLISECONDS)},
    }
    if light.brightness is not None:
        before["dimming"] = {"brightness": light.brightness}

    # The bulb's gamut is a triangle; a colour outside it is substituted by the
    # bridge unless it is clamped first, and then you never learn what was sent.
    gamut = gamut_for(light.color.gamut_type)
    target = rgb_to_xy(hex_to_rgb(wanted))
    if gamut is not None:
        target = clamp_to_gamut(target, gamut)
    print(f"  sending xy ({target[0]:.4f}, {target[1]:.4f})")

    await hue.api.lights.update(
        light.id,
        {
            "on": {"on": True},
            "color": {"xy": {"x": target[0], "y": target[1]}},
            "dynamics": {"duration": int(FADE_SECONDS * MILLISECONDS)},
        },
    )
    await asyncio.sleep(HOLD_SECONDS)
    await hue.api.lights.update(light.id, before)


async def the_short_way(light: models.Light, wanted: str) -> None:
    """Say the colour, in the notation you wrote it in."""
    if light.color is None:
        print("  white-only bulb, no colour to set")
        return

    print(f"  showing {light.hex_color}")
    # Power, brightness and whichever of colour/temperature the light is
    # actually in -- captured, and put back, without naming any of them.
    before = light.capture()

    # One PUT. `set()` resolves the hex, clamps it into the gamut this light
    # reported, and converts the fade to milliseconds.
    await light.set(on=True, hex_color=wanted, transition=FADE_SECONDS)
    await asyncio.sleep(HOLD_SECONDS)
    await light.restore(before, transition=FADE_SECONDS)


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

        print(f"the long way (huepy.color by hand, hue.api by id) -> {wanted}:")
        await the_long_way(hue, light, wanted)

        # Let the restore fade land before re-reading. Without this the second
        # half captures a light mid-fade and "restores" it to the wrong colour.
        await asyncio.sleep(FADE_SECONDS)

        print(f"\nthe short way (one call) -> {wanted}:")
        await the_short_way(await light.refresh(), wanted)
        print("\nSame colour, both times.")


if __name__ == "__main__":
    asyncio.run(main())
