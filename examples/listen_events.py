"""Print events as the bridge pushes them, parsed into models.

    python examples/listen_events.py

Runs until interrupted. The stream reconnects on its own with exponential
backoff if the connection drops, and drops an event it cannot parse rather
than ending. For the raw decoded payloads instead, use
hue.http.subscribe_events().
"""

import asyncio
import logging

from huepy import Hue, models

NAME_WIDTH = 24


def describe(resource: models.EventResource) -> str:  # noqa: C901, PLR0912
    """Summarise whichever pieces of state this event actually carries."""
    parts: list[str] = []
    if resource.on is not None:
        parts.append("on" if resource.on.on else "off")
    if resource.dimming is not None:
        parts.append(f"{resource.dimming.brightness:.0f}%")
    if resource.color_temperature is not None:
        mirek = resource.color_temperature.mirek
        if mirek is not None:
            parts.append(f"{mirek} mirek")
    if resource.color is not None and resource.color.xy is not None:
        parts.append(f"xy ({resource.color.xy.x:.3f}, {resource.color.xy.y:.3f})")
    if resource.motion is not None:
        report = resource.motion.motion_report
        detected = report.motion if report is not None else resource.motion.motion
        if detected is not None:
            parts.append("motion" if detected else "clear")
    if resource.temperature is not None:
        report = resource.temperature.temperature_report
        value = (
            report.temperature
            if report is not None
            else resource.temperature.temperature
        )
        if value is not None:
            parts.append(f"{value:.1f} °C")
    if resource.light is not None:
        report = resource.light.light_level_report
        level = report.light_level if report is not None else resource.light.light_level
        if level is not None:
            parts.append(f"light level {level}")
    if resource.button is not None and resource.button.button_report is not None:
        event = resource.button.button_report.event
        if event is not None:
            parts.append(event)
    if (
        resource.contact_report is not None
        and resource.contact_report.state is not None
    ):
        parts.append(resource.contact_report.state)
    if (
        resource.power_state is not None
        and resource.power_state.battery_level is not None
    ):
        parts.append(f"battery {resource.power_state.battery_level}%")
    if resource.relative_rotary is not None:
        rotary = resource.relative_rotary.value
        if rotary is not None:
            parts.append(f"{rotary.rotation.direction} {rotary.rotation.steps} steps")
    return ", ".join(parts)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    async with Hue() as hue:
        print("Listening for events. Press Ctrl-C to stop.\n")
        async for event in hue.get_event_stream():
            if not event.is_update:
                # An add, a delete or an error: the ids are all there is.
                print(f"{event.type:16} {', '.join(event.resource_ids)}")
                continue
            for resource in event.data:
                name = hue.get_name(resource.id)
                print(f"{resource.type:16} {name:{NAME_WIDTH}} {describe(resource)}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped.")
