"""Write checks against a real bridge, with state restored afterwards.

Every test here depends on `restore_all_lights` (via `a_light` / `a_room`),
which snapshots every light before the test and puts them all back after,
including when the test fails.
"""

import asyncio

import pytest

from huepy import Hue, models

pytestmark = pytest.mark.integration

SETTLE = 2.0
"""Seconds to let the bridge apply a change before reading it back."""

GROUPED_LIGHT_PATH = "/clip/v2/resource/grouped_light/"


class TestOneRequestPerCommand:
    """The headline claim of 0.2.0, verified against real hardware.

    Before bound models, dimming and warming a room cost four round trips:
    each command re-fetched the room to re-resolve its grouped_light. A bound
    room already carries `services[]`, so it does not.
    """

    async def test_a_composite_room_command_is_one_request(
        self, hue: Hue, a_room: models.Room, request_counter
    ):
        calls = request_counter(hue)
        await a_room.set(on=True, brightness=40, mirek=300, transition=1.0)

        assert len(calls) == 1, f"expected one request, got {[c.path for c in calls]}"
        sent = calls[0]
        assert sent.method == "PUT"
        assert sent.path.startswith(GROUPED_LIGHT_PATH), (
            "a room command must address its grouped_light service"
        )
        assert sent.data is not None
        assert set(sent.data) == {"on", "dimming", "color_temperature", "dynamics"}

    async def test_transition_seconds_reach_the_bridge_as_milliseconds(
        self, hue: Hue, a_light: models.Light, request_counter
    ):
        calls = request_counter(hue)
        await a_light.set(brightness=50, transition=2.5)
        assert calls[0].data == {
            "dimming": {"brightness": 50},
            "dynamics": {"duration": 2500},
        }


class TestCommandsActuallyApply:
    async def test_brightness_applies(self, hue: Hue, a_light: models.Light):
        await a_light.set(on=True, brightness=42.0)
        await asyncio.sleep(SETTLE)
        assert (await a_light.refresh()).brightness == pytest.approx(42.0, abs=1.5)

    async def test_on_and_off_round_trip(self, hue: Hue, a_light: models.Light):
        await a_light.turn_off()
        await asyncio.sleep(SETTLE)
        assert (await a_light.refresh()).is_on is False

        await a_light.turn_on()
        await asyncio.sleep(SETTLE)
        assert (await a_light.refresh()).is_on is True

    async def test_colour_temperature_applies(self, hue: Hue, a_light: models.Light):
        if a_light.color_temperature is None:
            pytest.skip(f"{a_light.name} has no colour temperature")
        await a_light.set(on=True, kelvin=2500)
        await asyncio.sleep(SETTLE)
        assert (await a_light.refresh()).mirek == pytest.approx(400, abs=4)

    async def test_rgb_applies_and_stays_in_gamut(
        self, hue: Hue, a_colour_light: models.Light
    ):
        light = a_colour_light
        await light.set(on=True, rgb=(255, 0, 0))
        await asyncio.sleep(SETTLE)
        after = await light.refresh()
        assert after.color is not None

        gamut = light.color.gamut if light.color else None
        if gamut is None:
            pytest.skip(f"{light.name} reports no gamut to check against")
        xs = [gamut.red.x, gamut.green.x, gamut.blue.x]
        ys = [gamut.red.y, gamut.green.y, gamut.blue.y]
        assert min(xs) - 0.01 <= after.color.xy.x <= max(xs) + 0.01
        assert min(ys) - 0.01 <= after.color.xy.y <= max(ys) + 0.01


class TestRoomsRouteThroughGroupedLight:
    async def test_room_command_reaches_its_member_lights(
        self, hue: Hue, a_room: models.Room
    ):
        member_ids = {child.rid for child in a_room.children}
        lights = [
            light
            for light in await hue.api.lights.list()
            if light.device_id in member_ids and light.brightness is not None
        ]
        if not lights:
            pytest.skip(f"{a_room.name} has no dimmable lights")

        await a_room.set(on=True, brightness=35.0)
        await asyncio.sleep(SETTLE)

        refreshed = [await light.refresh() for light in lights]
        assert all(light.is_on for light in refreshed)
        for light in refreshed:
            assert light.brightness == pytest.approx(35.0, abs=3.0), light.name
