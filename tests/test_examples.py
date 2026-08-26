"""Checks the paired examples actually deliver what they claim.

`two_ways_*.py` each promise that their long and short halves do the same
thing. That promise is the whole point of the files, and it is exactly the kind
of claim that rots -- so it is asserted here rather than left to the reader.
"""

import asyncio
import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

from huepy import Hue
from huepy.color import GAMUT_B, clamp_to_gamut, hex_to_rgb, rgb_to_xy
from huepy.models.event import HueEvent

from .conftest import Call, FakeHttp

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
FIXTURES = Path(__file__).parent / "fixtures"
LIGHT = "/clip/v2/resource/light"
GROUPED = "/clip/v2/resource/grouped_light"

# Saturated enough to sit well outside gamut B, so a dropped clamp shows up.
WIDE_GREEN = "#00ff00"


def load(name: str) -> Any:
    """Import one example script by path, without packaging `examples/`."""
    spec = importlib.util.spec_from_file_location(name, EXAMPLES / f"{name}.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def body(call: Call) -> dict[str, Any]:
    """Return one recorded write's payload, which a PUT always carries."""
    payload = call[2]
    assert payload is not None
    return payload


@pytest.fixture
def no_sleep(monkeypatch):
    """Skip the examples' hold and fade waits."""

    async def instant(_seconds: float) -> None:
        return

    monkeypatch.setattr(asyncio, "sleep", instant)


@pytest.fixture(scope="module")
def events_example():
    return load("two_ways_events")


@pytest.fixture(scope="module")
def color_example():
    return load("two_ways_color")


@pytest.fixture(scope="module")
def room_example():
    return load("two_ways_room")


class TestTwoWaysEvents:
    """The file prints both descriptions and flags a mismatch; none may occur."""

    def _light_resources(self) -> list[dict[str, Any]]:
        frames = json.loads((FIXTURES / "event_frames.json").read_text("utf-8"))
        return [
            resource
            for frame in frames
            for event in frame["events"]
            for resource in event.get("data", [])
            if resource.get("type") in {"light", "grouped_light"}
        ]

    def test_both_halves_agree_on_real_bridge_frames(self, events_example):
        resources = self._light_resources()
        assert resources, "the fixture carries no light events to compare"
        for resource in resources:
            assert events_example.the_long_way(
                resource
            ) == events_example.the_short_way(resource)

    @pytest.mark.parametrize(
        "resource",
        [
            {"id": "l", "type": "light", "on": {"on": True}},
            {"id": "l", "type": "light", "on": {"on": False}},
            {"id": "l", "type": "light", "dimming": {"brightness": 62.4}},
            {"id": "l", "type": "light", "color_temperature": {"mirek": 370}},
            {"id": "l", "type": "light", "color": {"xy": {"x": 0.5, "y": 0.4}}},
            {
                "id": "l",
                "type": "light",
                "on": {"on": True},
                "dimming": {"brightness": 40.0},
                "color_temperature": {"mirek": 300},
            },
            {"id": "l", "type": "light"},
        ],
    )
    def test_both_halves_agree_section_by_section(self, events_example, resource):
        assert events_example.the_long_way(resource) == events_example.the_short_way(
            resource
        )

    def test_the_short_way_is_the_model_property(self, events_example):
        """If they ever diverge, the long way is the one that went stale."""
        resource = {"id": "l", "type": "light", "dimming": {"brightness": 20.16}}
        expected = HueEvent.model_validate({"data": [resource]}).data[0].summary
        assert events_example.the_short_way(resource) == expected


class TestTwoWaysColor:
    """Both halves must leave the light exactly as they found it."""

    @staticmethod
    def _light(*, on: bool, brightness: float | None = 50.0) -> dict[str, Any]:
        body: dict[str, Any] = {
            "id": "light-1",
            "type": "light",
            "metadata": {"name": "Desk"},
            "on": {"on": on},
            "color": {"xy": {"x": 0.4, "y": 0.4}, "gamut_type": "C"},
        }
        if brightness is not None:
            body["dimming"] = {"brightness": brightness}
        return body

    async def test_the_long_way_puts_back_the_power_state(
        self, hue: Hue, http: FakeHttp, color_example, no_sleep
    ) -> None:
        """It switches the light on; forgetting `on` would leave it lit."""
        http.queue_collection("light", [self._light(on=False)])
        light = await hue.lights.get("Desk")
        http.calls.clear()

        await color_example.the_long_way(hue, light, "#3366ff")

        restore = http.writes[-1]
        assert restore[0] == "PUT"
        assert restore[1] == f"{LIGHT}/light-1"
        assert body(restore)["on"] == {"on": False}
        assert body(restore)["dimming"] == {"brightness": 50.0}
        assert body(restore)["color"] == {"xy": {"x": 0.4, "y": 0.4}}

    async def test_both_halves_restore_the_same_state(
        self, hue: Hue, http: FakeHttp, color_example, no_sleep
    ) -> None:
        http.queue_collection("light", [self._light(on=True, brightness=40.0)])
        light = await hue.lights.get("Desk")

        http.calls.clear()
        await color_example.the_long_way(hue, light, "#3366ff")
        long_restore = body(http.writes[-1])

        http.calls.clear()
        await color_example.the_short_way(light, "#3366ff")
        short_restore = body(http.writes[-1])

        for section in ("on", "dimming", "color"):
            assert long_restore[section] == short_restore[section], section

    async def test_both_halves_send_the_same_gamut_clamped_colour(
        self, hue: Hue, http: FakeHttp, color_example, no_sleep
    ) -> None:
        """The hand-rolled clamp must land where `set(hex_color=...)` does.

        Gamut B and a saturated green, because the clamp has to actually move
        the point: a colour already inside the triangle would let a missing
        clamp pass unnoticed on both sides.
        """
        narrow = self._light(on=True)
        narrow["color"]["gamut_type"] = "B"
        http.queue_collection("light", [narrow])
        light = await hue.lights.get("Desk")

        raw = rgb_to_xy(hex_to_rgb(WIDE_GREEN))
        clamped = clamp_to_gamut(raw, GAMUT_B)
        assert clamped != raw, "pick a colour the clamp genuinely moves"

        http.calls.clear()
        await color_example.the_long_way(hue, light, WIDE_GREEN)
        long_target = body(http.writes[0])["color"]

        http.calls.clear()
        await color_example.the_short_way(light, WIDE_GREEN)
        short_target = body(http.writes[0])["color"]

        assert long_target == short_target
        assert long_target == {"xy": {"x": clamped[0], "y": clamped[1]}}

    async def test_a_white_only_bulb_is_refused_by_both(
        self, hue: Hue, http: FakeHttp, color_example, no_sleep
    ) -> None:
        http.queue_collection(
            "light",
            [{"id": "light-1", "type": "light", "metadata": {"name": "Desk"}}],
        )
        light = await hue.lights.get("Desk")
        http.calls.clear()

        await color_example.the_long_way(hue, light, "#3366ff")
        await color_example.the_short_way(light, "#3366ff")

        assert http.writes == []


class TestTwoWaysRoom:
    """The long way must restore per light, colour temperature included."""

    @pytest.fixture
    def room_http(self, http: FakeHttp) -> FakeHttp:
        http.queue_collection(
            "room",
            [
                {
                    "id": "room-1",
                    "type": "room",
                    "metadata": {"name": "Kitchen"},
                    "children": [{"rid": "device-1", "rtype": "device"}],
                    "services": [{"rid": "gl-1", "rtype": "grouped_light"}],
                }
            ],
        )
        http.queue_collection(
            "light",
            [
                {
                    "id": "light-1",
                    "type": "light",
                    "owner": {"rid": "device-1", "rtype": "device"},
                    "metadata": {"name": "Hob"},
                    "on": {"on": True},
                    "dimming": {"brightness": 80.0},
                    "color_temperature": {"mirek": 450, "mirek_valid": True},
                }
            ],
        )
        return http

    async def test_the_long_way_dims_through_the_grouped_light(
        self, hue: Hue, room_http: FakeHttp, room_example, no_sleep
    ) -> None:
        await room_example.the_long_way(hue, "kitchen")

        dim = room_http.writes[0]
        assert dim[1] == f"{GROUPED}/gl-1"
        assert body(dim)["dimming"] == {"brightness": room_example.DIM_BRIGHTNESS}
        assert body(dim)["color_temperature"]["mirek"] == 455  # 2200 K

    async def test_the_long_way_restores_colour_temperature_per_light(
        self, hue: Hue, room_http: FakeHttp, room_example, no_sleep
    ) -> None:
        """Restoring through the group would silently drop the mirek."""
        await room_example.the_long_way(hue, "kitchen")

        restore = room_http.writes[-1]
        assert restore[1] == f"{LIGHT}/light-1"
        assert body(restore)["color_temperature"] == {"mirek": 450}
        assert body(restore)["dimming"] == {"brightness": 80.0}

    async def test_both_halves_send_the_same_dim_and_the_same_restore(
        self, hue: Hue, room_http: FakeHttp, room_example, no_sleep
    ) -> None:
        await room_example.the_long_way(hue, "kitchen")
        long_writes = [(call[1], body(call)) for call in room_http.writes]

        room_http.calls.clear()
        await room_example.the_short_way(hue, "Kitchen")
        short_writes = [(call[1], body(call)) for call in room_http.writes]

        assert long_writes == short_writes

    async def test_an_unknown_room_writes_nothing_either_way(
        self, hue: Hue, room_http: FakeHttp, room_example, no_sleep
    ) -> None:
        await room_example.the_long_way(hue, "nowhere")
        await room_example.the_short_way(hue, "nowhere")

        assert room_http.writes == []
