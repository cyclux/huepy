"""One event stream, summarised twice: by hand, and by the library.

    python examples/two_ways_events.py

Both columns are computed from the same raw payload, so they must agree. The
long way is what every consumer of `hue.api.raw.subscribe_events()` ends up
writing; the short way is `EventResource.summary`.

To keep the comparison honest this only follows light events, because the long
way below is complete only for the four sections a light sends. Extending it to
motion, temperature, ambient light, buttons, contacts, battery and rotary input
is another thirty-odd lines of the same shape -- which is the whole argument for
the section on the right.
"""

import asyncio
from typing import Any, cast

from huepy import Hue
from huepy.color import mirek_to_kelvin, rgb_to_hex, xy_to_rgb
from huepy.models.event import HueEvent

LIGHT_TYPES = {"light", "grouped_light"}
COLUMN = 34


def section(payload: dict[str, Any], key: str) -> dict[str, Any] | None:
    """Read one optional nested section out of a raw payload."""
    value = payload.get(key)
    return cast("dict[str, Any]", value) if isinstance(value, dict) else None


def the_long_way(resource: dict[str, Any]) -> str:
    """Walk one raw event resource, section by section, checking as you go.

    Every section is optional and each nests differently, so every one needs
    its own presence check before its value can be read -- and the two colour
    sections need a unit conversion on top.
    """
    parts: list[str] = []
    on = section(resource, "on")
    if on is not None and isinstance(on.get("on"), bool):
        parts.append("on" if on["on"] else "off")
    dimming = section(resource, "dimming")
    if dimming is not None and dimming.get("brightness") is not None:
        parts.append(f"{float(dimming['brightness']):.0f}%")
    temperature = section(resource, "color_temperature")
    if temperature is not None and temperature.get("mirek") is not None:
        parts.append(f"{mirek_to_kelvin(int(temperature['mirek']))} K")
    color = section(resource, "color")
    xy = section(color, "xy") if color is not None else None
    if xy is not None:
        parts.append(rgb_to_hex(xy_to_rgb((float(xy["x"]), float(xy["y"])))))
    return ", ".join(parts)


def the_short_way(resource: dict[str, Any]) -> str:
    """Parse the same payload into a model and ask it what changed."""
    return HueEvent.model_validate({"data": [resource]}).data[0].summary


async def main() -> None:
    async with Hue() as hue:
        await hue.refresh_names()
        print(f"{'name':{COLUMN}} {'by hand':{COLUMN}} summary")
        print("-" * (COLUMN * 2 + 8))
        # The raw transport: decoded JSON, no models, no name resolution.
        async for event in hue.api.raw.subscribe_events():
            data = cast("list[dict[str, Any]]", event.get("data", []))
            for resource in data:
                if resource.get("type") not in LIGHT_TYPES:
                    continue
                mine, theirs = the_long_way(resource), the_short_way(resource)
                agree = " " if mine == theirs else "  <- MISMATCH"
                name = hue.get_name(str(resource.get("id", "")))
                print(f"{name:{COLUMN}} {mine:{COLUMN}} {theirs}{agree}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped.")
