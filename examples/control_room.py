"""Dim a room to a warm glow, hold it, then put it back -- addressed by name.

    python examples/control_room.py "Living Room"

No resource id appears anywhere below. The room is resolved from the name you
gave it on the bridge, and the dim travels as a single request.
"""

import asyncio
import sys

from huepy import Hue, ResourceNotFoundError, models

DIM_BRIGHTNESS = 30.0
WARM_KELVIN = 2200
FADE_SECONDS = 2.0
HOLD_SECONDS = 5.0
EXPECTED_ARGS = 2


async def members(hue: Hue, room: models.Room) -> list[models.Light]:
    """Return the room's own lights.

    A room's `children` are devices; the lights are the services those devices
    expose, so the two are matched up through each light's `device_id`.
    """
    devices = {child.rid for child in room.children}
    return [light for light in await hue.lights.all() if light.device_id in devices]


def snapshot(light: models.Light) -> dict[str, object]:
    """Capture what it takes to put one light back.

    Per light, not per room: a room's `grouped_light` reports no aggregate
    colour temperature, so restoring through the group silently drops it and
    leaves the room the wrong colour. A light is also in colour-temperature
    mode or colour mode but never both -- `mirek_valid` says which -- and
    `set()` refuses to be given both.
    """
    temperature = light.color_temperature
    in_ct_mode = temperature is not None and bool(temperature.mirek_valid)
    return {
        "on": light.is_on,
        "brightness": light.brightness,
        "mirek": temperature.mirek if in_ct_mode and temperature else None,
        "xy": (
            (light.color.xy.x, light.color.xy.y)
            if light.color is not None and not in_ct_mode
            else None
        ),
    }


async def main() -> None:
    if len(sys.argv) < EXPECTED_ARGS:
        print(__doc__)
        raise SystemExit(1)

    async with Hue() as hue:
        try:
            room = await hue.rooms[sys.argv[1]]
        except ResourceNotFoundError as exc:
            # The library did the matching, and knows what it could have
            # matched against -- so the typo corrects itself.
            print(f"No room named {exc.name!r}.")
            print(f"Known rooms: {', '.join(exc.known) or 'none'}")
            raise SystemExit(1) from None

        lights = await members(hue, room)
        if not lights:
            print(f"{room.name} has no lights to control.")
            raise SystemExit(1)
        before = {light.id: snapshot(light) for light in lights}

        print(f"Dimming {room.name} to {DIM_BRIGHTNESS:.0f}% at {WARM_KELVIN} K...")
        # One request, not four. A room arrives carrying the reference to its
        # own grouped_light service, so power, brightness, colour temperature
        # and the fade go to the bridge in a single PUT -- where the id-based
        # API needed a fetch, a service lookup and one write per attribute.
        await room.set(
            on=True,
            brightness=DIM_BRIGHTNESS,
            kelvin=WARM_KELVIN,
            transition=FADE_SECONDS,
        )

        await asyncio.sleep(HOLD_SECONDS)

        print(f"Restoring {room.name}...")
        for light in lights:
            await light.set(**before[light.id], transition=FADE_SECONDS)


if __name__ == "__main__":
    asyncio.run(main())
