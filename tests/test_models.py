"""Tests for the pydantic model layer and the response envelope."""

import logging
from datetime import UTC, datetime
from typing import Any, ClassVar

import pytest

from huepy import color, models
from huepy.exceptions import DetachedResourceError, HueResponseError
from huepy.models.common import unwrap, unwrap_one
from huepy.models.light import Effect, Signal, TimedEffect
from huepy.models.state import (
    build_effect_payload,
    build_light_payload,
    build_powerup_payload,
    build_scene_recall,
)

# A narrow, old-generation gamut: wide enough to be realistic, small enough
# that a saturated colour lands well outside it.
GAMUT_B_PAYLOAD = {
    "red": {"x": 0.675, "y": 0.322},
    "green": {"x": 0.409, "y": 0.518},
    "blue": {"x": 0.167, "y": 0.04},
}
GAMUT_B_CORNERS = ((0.675, 0.322), (0.409, 0.518), (0.167, 0.04))
GAMUT_C_CORNERS = ((0.692, 0.308), (0.17, 0.7), (0.153, 0.048))

# A Hue gradient strip reports every service this library models, and a few
# keys it does not.
GRADIENT_STRIP = {
    "id": "light-2",
    "type": "light",
    "metadata": {"name": "Strip"},
    "on": {"on": True},
    "effects": {
        "status": "candle",
        "status_values": ["no_effect", "candle", "fire"],
        "effect_values": ["no_effect", "candle", "fire"],
    },
    "timed_effects": {
        "status": "no_effect",
        "status_values": ["no_effect", "sunrise"],
        "effect_values": ["no_effect", "sunrise"],
        "duration": 1800000,
    },
    "gradient": {
        "points": [
            {"color": {"xy": {"x": 0.2, "y": 0.3}}},
            {"color": {"xy": {"x": 0.4, "y": 0.5}}},
        ],
        "mode": "interpolated_palette",
        "points_capable": 5,
        "pixel_count": 25,
        "mode_values": ["interpolated_palette", "random_pixelated"],
    },
    "powerup": {
        "preset": "safety",
        "configured": True,
        "on": {"mode": "on", "on": {"on": True}},
        "dimming": {"mode": "dimming", "dimming": {"brightness": 100.0}},
    },
    "alert": {"action_values": ["breathe"]},
    "signaling": {
        "status": {"signal": "no_signal", "estimated_end": "2026-01-01T00:00:00Z"},
        "signal_values": ["no_signal", "on_off", "alternating"],
    },
}


def is_inside(point, corners) -> bool:
    """Report whether a point lies inside a triangle.

    Written barycentrically and independently of huepy.color, so a clamped
    point is checked against the geometry rather than against the code that
    produced it.
    """
    x, y = point
    (x1, y1), (x2, y2), (x3, y3) = corners
    denominator = (y2 - y3) * (x1 - x3) + (x3 - x2) * (y1 - y3)
    first = ((y2 - y3) * (x - x3) + (x3 - x2) * (y - y3)) / denominator
    second = ((y3 - y1) * (x - x3) + (x1 - x3) * (y - y3)) / denominator
    return all(weight >= -1e-9 for weight in (first, second, 1 - first - second))


def sent_xy(http) -> tuple[float, float]:
    """Return the CIE point of the most recent write."""
    _, _, payload = http.last
    return (payload["color"]["xy"]["x"], payload["color"]["xy"]["y"])


def bound_light(hue, body) -> models.Light:
    """Parse a light payload and bind it to the fake client."""
    return models.Light.model_validate(body).bind(hue, "light")


class TestTolerance:
    """Models must survive fields the bridge adds in later firmware."""

    def test_unknown_fields_are_kept_not_rejected(self):
        light = models.Light.model_validate(
            {"id": "a", "type": "light", "brand_new_field": {"nested": 1}},
        )
        assert light.id == "a"
        assert light.model_extra is not None
        assert light.model_extra["brand_new_field"] == {"nested": 1}

    def test_absent_optional_sections_are_none(self):
        light = models.Light.model_validate({"id": "a"})
        assert light.on is None
        assert light.dimming is None
        assert light.brightness is None
        assert light.mirek is None
        assert light.is_on is False


class TestLight:
    def test_parses_full_payload(self):
        light = models.Light.model_validate(
            {
                "id": "light-1",
                "type": "light",
                "owner": {"rid": "dev-1", "rtype": "device"},
                "metadata": {"name": "Desk", "archetype": "desk_lamp"},
                "on": {"on": True},
                "dimming": {"brightness": 62.5, "min_dim_level": 0.2},
                "color_temperature": {"mirek": 366, "mirek_valid": True},
                "color": {"xy": {"x": 0.42, "y": 0.39}},
            },
        )
        assert light.name == "Desk"
        assert light.is_on is True
        assert light.brightness == 62.5
        assert light.mirek == 366
        assert light.device_id == "dev-1"
        assert light.color is not None
        assert light.color.xy.x == 0.42

    def test_device_id_is_none_without_owner(self):
        assert models.Light.model_validate({"id": "a"}).device_id is None

    def test_human_unit_fields_are_computed_and_serialized(self):
        light = models.Light.model_validate(
            {
                "id": "light-1",
                "dimming": {"brightness": 50},
                "color": {"xy": {"x": 0.4, "y": 0.4}},
                "color_temperature": {"mirek": 300, "mirek_valid": True},
            }
        )
        assert light.kelvin == color.mirek_to_kelvin(300)
        assert light.rgb == color.xy_to_rgb((0.4, 0.4), 50)
        assert light.hex_color == color.rgb_to_hex(color.xy_to_rgb((0.4, 0.4), 50))
        dumped = light.model_dump()
        assert dumped["kelvin"] == light.kelvin
        assert dumped["rgb"] == light.rgb
        assert dumped["hex_color"] == light.hex_color

    def test_hex_color_round_trips_the_hex_color_setter(self):
        """The read side must name and spell colour the way `set()` takes it."""
        wanted = "#3366ff"
        payload = build_light_payload(hex_color=wanted, brightness=100)
        light = models.Light.model_validate(
            {"id": "light-1", **payload, "dimming": {"brightness": 100}}
        )
        assert light.hex_color == wanted

    def test_a_white_only_light_has_no_hex_color(self):
        light = models.Light.model_validate(
            {"id": "light-1", "dimming": {"brightness": 50}}
        )
        assert light.rgb is None
        assert light.hex_color is None

    def test_invalid_colour_temperature_has_no_kelvin(self):
        light = models.Light.model_validate(
            {
                "id": "light-1",
                "color_temperature": {"mirek": 300, "mirek_valid": False},
            }
        )
        assert light.kelvin is None


class TestGroup:
    def test_service_id_finds_grouped_light(self):
        room = models.Room.model_validate(
            {
                "id": "room-1",
                "services": [
                    {"rid": "motion-1", "rtype": "motion"},
                    {"rid": "gl-1", "rtype": "grouped_light"},
                ],
            },
        )
        assert room.service_id(models.ResourceType.GROUPED_LIGHT) == "gl-1"
        assert room.service_id("nonexistent") is None

    def test_contains_device_matches_only_device_children(self):
        room = models.Room.model_validate(
            {
                "id": "room-1",
                "children": [
                    {"rid": "dev-1", "rtype": "device"},
                    {"rid": "svc-1", "rtype": "light"},
                ],
            },
        )
        assert room.contains_device("dev-1") is True
        assert room.contains_device("svc-1") is False

    def test_contains_light_resolves_a_room_through_the_owning_device(self):
        """A room's children are devices, so the light matches by its owner."""
        room = models.Room.model_validate(
            {"id": "room-1", "children": [{"rid": "dev-1", "rtype": "device"}]},
        )
        mine = models.Light.model_validate(
            {"id": "light-1", "owner": {"rid": "dev-1", "rtype": "device"}},
        )
        theirs = models.Light.model_validate(
            {"id": "light-2", "owner": {"rid": "dev-9", "rtype": "device"}},
        )
        assert room.contains_light(mine) is True
        assert room.contains_light(theirs) is False

    def test_contains_light_resolves_a_zone_through_the_service_itself(self):
        """A zone's children are the light services, not their devices."""
        zone = models.Zone.model_validate(
            {"id": "zone-1", "children": [{"rid": "light-1", "rtype": "light"}]},
        )
        mine = models.Light.model_validate(
            {"id": "light-1", "owner": {"rid": "dev-9", "rtype": "device"}},
        )
        theirs = models.Light.model_validate(
            {"id": "light-2", "owner": {"rid": "dev-1", "rtype": "device"}},
        )
        assert zone.contains_light(mine) is True
        assert zone.contains_light(theirs) is False

    def test_contains_light_tolerates_a_light_with_no_owner(self):
        room = models.Room.model_validate(
            {"id": "room-1", "children": [{"rid": "dev-1", "rtype": "device"}]},
        )
        assert room.contains_light(models.Light.model_validate({"id": "l"})) is False


class TestSensors:
    def test_motion_reads_nested_state(self):
        motion = models.Motion.model_validate(
            {
                "id": "m-1",
                "motion": {
                    "motion": True,
                    "motion_report": {"changed": "2026-01-01T00:00:00Z"},
                },
                "sensitivity": {"sensitivity": 2, "sensitivity_max": 4},
            },
        )
        assert motion.motion_detected is True
        assert motion.last_motion == "2026-01-01T00:00:00Z"
        assert motion.sensitivity.sensitivity_max == 4

    def test_motion_defaults_when_absent(self):
        motion = models.Motion.model_validate({"id": "m-1"})
        assert motion.motion_detected is False
        assert motion.last_motion == ""
        assert motion.sensitivity.sensitivity_max == 4

    def test_temperature_celsius(self):
        temp = models.Temperature.model_validate(
            {"id": "t-1", "temperature": {"temperature": 21.5}},
        )
        assert temp.celsius == 21.5

    def test_device_power_battery_level(self):
        power = models.DevicePower.model_validate(
            {"id": "p-1", "power_state": {"battery_level": 87}},
        )
        assert power.battery_level == 87

    def test_button_reads_nested_real_bridge_shape(self):
        button = models.Button.model_validate(
            {
                "id": "button-1",
                "button": {
                    "button_report": {
                        "event": "long_release",
                        "updated": "2026-08-22T20:38:49.591Z",
                    },
                    "event_values": ["initial_press", "long_release"],
                    "last_event": "long_release",
                    "repeat_interval": 800,
                },
            }
        )

        assert button.last_event == "long_release"
        assert button.button is not None
        assert button.button.button_report is not None
        assert button.button.button_report.updated == datetime(
            2026, 8, 22, 20, 38, 49, 591000, tzinfo=UTC
        )

    def test_zigbee_connectivity_parses_fixture_shape(self):
        connectivity = models.parse_resource(
            {
                "id": "zigbee-1",
                "type": "zigbee_connectivity",
                "owner": {"rid": "device-1", "rtype": "device"},
                "status": "connected",
                "mac_address": "00:11:22:33:44:55:66:77",
            }
        )

        assert isinstance(connectivity, models.ZigbeeConnectivity)
        assert connectivity.is_connected is True

    def test_relative_rotary_prefers_timestamped_report(self):
        rotary = models.parse_resource(
            {
                "id": "rotary-1",
                "type": "relative_rotary",
                "relative_rotary": {
                    "rotary_report": {
                        "updated": "2026-08-24T14:16:45.503Z",
                        "action": "repeat",
                        "rotation": {
                            "direction": "counter_clock_wise",
                            "steps": 12,
                            "duration": 20,
                        },
                    },
                },
            }
        )

        assert isinstance(rotary, models.RelativeRotary)
        assert rotary.relative_rotary is not None
        assert rotary.relative_rotary.value is rotary.relative_rotary.rotary_report

    @pytest.mark.parametrize(
        ("valid", "expected"),
        [(True, pytest.approx(10 ** ((3578 - 1) / 10_000))), (False, None)],
    )
    def test_light_level_converts_only_valid_readings(self, valid, expected):
        sensor = models.LightLevel.model_validate(
            {
                "id": "lux-1",
                "light": {
                    "light_level": 3578,
                    "light_level_valid": valid,
                    "light_level_report": {
                        "changed": "2026-08-24T14:16:45.498Z",
                        "light_level": 3578,
                    },
                },
            }
        )
        assert sensor.lux == expected
        assert sensor.light is not None
        assert sensor.light.light_level_report is not None
        assert sensor.light.light_level_report.changed is not None


class TestEnvelope:
    def test_unwrap_parses_each_entry(self):
        payload = {"errors": [], "data": [{"id": "a"}, {"id": "b"}]}
        lights = unwrap(payload, models.Light)
        assert [light.id for light in lights] == ["a", "b"]
        assert all(isinstance(light, models.Light) for light in lights)

    def test_unwrap_of_empty_data_is_empty(self):
        assert unwrap({"errors": [], "data": []}, models.Light) == []

    def test_errors_in_body_raise_even_though_http_was_ok(self):
        """The bridge reports many failures with HTTP 200 and a populated errors[]."""
        payload = {
            "errors": [{"description": "device (light-1) is not responding"}],
            "data": [],
        }
        with pytest.raises(HueResponseError) as excinfo:
            unwrap(payload, models.Light)
        assert excinfo.value.errors == ["device (light-1) is not responding"]
        assert "not responding" in str(excinfo.value)

    def test_errors_raise_even_when_data_is_present(self):
        payload = {
            "errors": [{"description": "partial failure"}],
            "data": [{"id": "a"}],
        }
        with pytest.raises(HueResponseError):
            unwrap(payload, models.Light)

    def test_unwrap_one_returns_the_single_entry(self):
        light = unwrap_one({"errors": [], "data": [{"id": "a"}]}, models.Light)
        assert light.id == "a"

    def test_unwrap_one_raises_when_nothing_returned(self):
        with pytest.raises(HueResponseError, match="no resource"):
            unwrap_one({"errors": [], "data": []}, models.Light)


class TestColorGamut:
    def test_gamut_parses_alongside_xy(self):
        light = models.Light.model_validate(
            {
                "id": "light-1",
                "color": {
                    "xy": {"x": 0.42, "y": 0.39},
                    "gamut": {
                        "red": {"x": 0.68, "y": 0.31},
                        "green": {"x": 0.11, "y": 0.82},
                        "blue": {"x": 0.14, "y": 0.04},
                    },
                    "gamut_type": "C",
                },
            },
        )
        assert light.color is not None
        assert light.color.gamut is not None
        assert light.color.gamut.red.x == 0.68
        assert light.color.gamut.blue.y == 0.04
        assert light.color.gamut_type == "C"

    def test_gamut_is_none_when_the_bridge_omits_it(self):
        light = models.Light.model_validate(
            {"id": "light-1", "color": {"xy": {"x": 0.4, "y": 0.4}}},
        )
        assert light.color is not None
        assert light.color.gamut is None


class TestScene:
    def test_name_comes_from_the_shared_named_resource(self):
        """Scene no longer re-implements `name`; it inherits NamedResource."""
        scene = models.Scene.model_validate(
            {"id": "s-1", "metadata": {"name": "Movie"}},
        )
        assert isinstance(scene, models.NamedResource)
        assert scene.name == "Movie"

    def test_name_defaults_to_empty(self):
        assert models.Scene.model_validate({"id": "s-1"}).name == ""

    def test_actions_status_and_service_group_services_parse(self):
        scene = models.Scene.model_validate(
            {
                "id": "s-1",
                "actions": [
                    {
                        "target": {"rid": "light-1", "rtype": "light"},
                        "action": {"on": {"on": True}},
                    }
                ],
                "status": {"active": "static"},
            }
        )
        group = models.ServiceGroup.model_validate(
            {
                "id": "group-1",
                "services": [{"rid": "light-1", "rtype": "light"}],
            }
        )
        assert scene.actions[0].target is not None
        assert scene.actions[0].target.rid == "light-1"
        assert scene.status is not None
        assert scene.status.active == "static"
        assert group.services[0].rid == "light-1"


class TestSmartScene:
    """A smart scene recalls other scenes on a weekly schedule."""

    BODY: ClassVar[dict[str, Any]] = {
        "id": "ss-1",
        "type": "smart_scene",
        "metadata": {"name": "Daily rhythm"},
        "group": {"rid": "room-1", "rtype": "room"},
        "week_timeslots": [
            {
                "timeslots": [
                    {
                        "start_time": {
                            "kind": "time",
                            "time": {"hour": 7, "minute": 30, "second": 0},
                        },
                        "target": {"rid": "scene-1", "rtype": "scene"},
                    }
                ],
                "recurrence": ["monday"],
            }
        ],
        "transition_duration": 60000,
        "active_timeslot": {"timeslot_id": 0, "weekday": "monday"},
        "state": "inactive",
    }

    def test_parses_the_weekly_schedule_active_slot_and_state(self):
        scene = models.parse_resource(self.BODY)
        assert isinstance(scene, models.SmartScene)
        assert scene.name == "Daily rhythm"
        assert scene.group is not None
        assert scene.group.rid == "room-1"
        assert scene.transition_duration == 60000
        assert scene.state == "inactive"

        week = scene.week_timeslots[0]
        assert week.recurrence == ["monday"]
        timeslot = week.timeslots[0]
        assert timeslot.start_time is not None
        assert timeslot.start_time.kind == "time"
        assert timeslot.start_time.time is not None
        assert timeslot.start_time.time.hour == 7
        assert timeslot.target is not None
        assert timeslot.target.rid == "scene-1"
        assert timeslot.target.rtype == "scene"

        assert scene.active_timeslot is not None
        assert scene.active_timeslot.timeslot_id == 0
        assert scene.active_timeslot.weekday == "monday"


class TestBinding:
    """Models parsed by hand are inert; only a handler can bind them."""

    def test_a_hand_built_model_is_detached(self):
        assert models.Light.model_validate({"id": "x"}).is_bound is False

    @pytest.mark.parametrize(
        "command",
        [
            lambda light: light.set(on=True),
            lambda light: light.turn_on(),
            lambda light: light.update({"on": {"on": True}}),
            lambda light: light.delete(),
            lambda light: light.refresh(),
        ],
        ids=["set", "turn_on", "update", "delete", "refresh"],
    )
    async def test_commands_on_a_detached_model_raise(self, command):
        light = models.Light.model_validate({"id": "x"})
        with pytest.raises(DetachedResourceError, match=r"hue\.<resource>\.get"):
            await command(light)

    async def test_a_detached_room_also_raises(self):
        room = models.Room.model_validate(
            {"id": "r", "services": [{"rid": "gl", "rtype": "grouped_light"}]},
        )
        with pytest.raises(DetachedResourceError):
            await room.turn_on()

    def test_bind_falls_back_to_the_models_own_type(self, hue):
        light = models.Light.model_validate({"id": "x", "type": "light"}).bind(hue, "")
        assert light._path == "/clip/v2/resource/light/x"

    def test_bind_prefers_the_rtype_it_is_given(self, hue):
        light = models.Light.model_validate({"id": "x", "type": "light"})
        assert light.bind(hue, "grouped_light")._path == (
            "/clip/v2/resource/grouped_light/x"
        )

    def test_bind_returns_the_same_instance(self, hue):
        light = models.Light.model_validate({"id": "x"})
        assert light.bind(hue, "light") is light

    def test_the_binding_never_reaches_the_wire(self, hue):
        """PrivateAttr keeps the client out of validation *and* serialisation."""
        light = models.Light.model_validate({"id": "x"}).bind(hue, "light")
        dumped = light.model_dump()
        assert "_hue" not in dumped
        assert "_rtype" not in dumped
        assert dumped["id"] == "x"


class TestBuildLightPayload:
    """One call composes a whole state change into one body."""

    def test_an_empty_call_builds_nothing(self):
        assert build_light_payload() == {}

    @pytest.mark.parametrize(
        ("kwargs", "expected"),
        [
            ({"on": True}, {"on": {"on": True}}),
            ({"on": False}, {"on": {"on": False}}),
            ({"brightness": 42.0}, {"dimming": {"brightness": 42.0}}),
            ({"xy": (0.3, 0.4)}, {"color": {"xy": {"x": 0.3, "y": 0.4}}}),
            ({"mirek": 350}, {"color_temperature": {"mirek": 350}}),
            ({"transition": 1.0}, {"dynamics": {"duration": 1000}}),
        ],
        ids=["on", "off", "brightness", "xy", "mirek", "transition"],
    )
    def test_each_field_alone(self, kwargs, expected):
        assert build_light_payload(**kwargs) == expected

    def test_every_field_combines_into_a_single_dict(self):
        assert build_light_payload(
            on=True,
            brightness=40.0,
            xy=(0.3, 0.4),
            transition=2.5,
        ) == {
            "on": {"on": True},
            "dimming": {"brightness": 40.0},
            "color": {"xy": {"x": 0.3, "y": 0.4}},
            "dynamics": {"duration": 2500},
        }

    @pytest.mark.parametrize(
        ("given", "expected"),
        [(50.0, 50.0), (150.0, 100.0), (-5.0, 0.0), (0.0, 0.0), (100.0, 100.0)],
    )
    def test_brightness_is_clamped_at_both_ends(self, given, expected):
        assert build_light_payload(brightness=given) == {
            "dimming": {"brightness": expected}
        }

    @pytest.mark.parametrize(
        ("seconds", "milliseconds"),
        [(0.0, 0), (0.25, 250), (1.0, 1000), (2.5, 2500), (10.0, 10000)],
    )
    def test_transition_seconds_become_milliseconds(self, seconds, milliseconds):
        assert build_light_payload(transition=seconds) == {
            "dynamics": {"duration": milliseconds}
        }

    def test_a_negative_transition_is_rejected(self):
        with pytest.raises(ValueError, match="must not be negative"):
            build_light_payload(transition=-1.0)

    def test_transition_above_the_measured_bridge_ceiling_is_rejected(self):
        assert build_light_payload(transition=6000) == {
            "dynamics": {"duration": 6_000_000}
        }
        with pytest.raises(ValueError, match="must not exceed 6000"):
            build_light_payload(transition=6000.001)

    def test_colour_and_colour_temperature_together_are_rejected(self):
        """The bridge takes one or the other; silently dropping one is a trap."""
        with pytest.raises(ValueError, match="not both"):
            build_light_payload(xy=(0.3, 0.4), mirek=350)

    def test_nothing_is_sent_when_a_conflicting_call_fails(self):
        """Validation happens before any key lands in the payload."""
        with pytest.raises(ValueError, match="not both"):
            build_light_payload(on=True, xy=(0.3, 0.4), mirek=350)

    @pytest.mark.parametrize(
        ("kwargs", "expected"),
        [
            ({"speed": 0.5}, {"dynamics": {"speed": 0.5}}),
            (
                {"transition": 2.0, "speed": 0.5},
                {"dynamics": {"duration": 2000, "speed": 0.5}},
            ),
        ],
        ids=["speed", "transition+speed"],
    )
    def test_speed_rides_the_dynamics_block_beside_any_transition(
        self, kwargs, expected
    ):
        assert build_light_payload(**kwargs) == expected

    @pytest.mark.parametrize("speed", [-0.1, 1.5])
    def test_a_speed_outside_0_1_is_rejected(self, speed):
        with pytest.raises(ValueError, match="speed must be between"):
            build_light_payload(speed=speed)

    @pytest.mark.parametrize(
        ("given", "expected"),
        [(1000, 500), (10, 153), (153, 153), (500, 500), (350, 350)],
    )
    def test_mirek_is_clamped_to_the_bridge_range(self, given, expected):
        assert build_light_payload(mirek=given) == {
            "color_temperature": {"mirek": expected}
        }


class TestBuildEffectPayload:
    """An effect names itself and may carry a tint and a pace."""

    def test_the_bare_effect_sends_only_its_name(self):
        assert build_effect_payload("candle") == {
            "effects_v2": {"action": {"effect": "candle"}}
        }

    def test_a_hex_tint_becomes_a_colour_parameter(self):
        action = build_effect_payload("candle", hex_color="#ff8800")["effects_v2"][
            "action"
        ]
        assert action["effect"] == "candle"
        assert "xy" in action["parameters"]["color"]

    def test_a_colour_temperature_tint_is_carried_and_clamped(self):
        assert build_effect_payload("candle", mirek=300) == {
            "effects_v2": {
                "action": {
                    "effect": "candle",
                    "parameters": {"color_temperature": {"mirek": 300}},
                }
            }
        }

    def test_speed_paces_the_effect(self):
        action = build_effect_payload("candle", speed=0.5)["effects_v2"]["action"]
        assert action["parameters"]["speed"] == 0.5

    def test_a_colour_and_a_temperature_together_are_rejected(self):
        with pytest.raises(ValueError, match="not both"):
            build_effect_payload("candle", xy=(0.3, 0.3), mirek=300)

    def test_no_effect_takes_no_parameters(self):
        with pytest.raises(ValueError, match="no_effect"):
            build_effect_payload("no_effect", speed=0.5)


class TestBuildPowerupPayload:
    """A bare preset takes no config; any custom field forces `custom`."""

    def test_a_bare_preset_carries_only_itself(self):
        assert build_powerup_payload("safety") == {"preset": "safety"}

    def test_on_alone_configures_a_custom_powerup(self):
        assert build_powerup_payload(on=True) == {
            "preset": "custom",
            "on": {"mode": "on", "on": {"on": True}},
        }

    def test_brightness_is_wrapped_in_its_own_mode_envelope(self):
        assert build_powerup_payload(on=True, brightness=50) == {
            "preset": "custom",
            "on": {"mode": "on", "on": {"on": True}},
            "dimming": {"mode": "dimming", "dimming": {"brightness": 50.0}},
        }

    def test_a_colour_temperature_selects_the_temperature_mode(self):
        assert build_powerup_payload(mirek=300) == {
            "preset": "custom",
            "color": {
                "mode": "color_temperature",
                "color_temperature": {"mirek": 300},
            },
        }

    def test_a_colour_selects_the_colour_mode(self):
        assert build_powerup_payload(xy=(0.3, 0.3)) == {
            "preset": "custom",
            "color": {"mode": "color", "color": {"xy": {"x": 0.3, "y": 0.3}}},
        }

    def test_a_custom_field_forces_custom_even_over_a_named_preset(self):
        """`safety` with a custom field is a contradiction the builder resolves."""
        assert build_powerup_payload("safety", on=True)["preset"] == "custom"

    def test_a_mode_without_on_is_reachable_and_carries_no_state(self):
        """`toggle`/`previous` power-up modes take no nested on-state."""
        payload = build_powerup_payload(on_mode="toggle")
        assert payload == {"preset": "custom", "on": {"mode": "toggle"}}


class TestBuildSceneRecall:
    """The recall body that applies a scene onto its room or zone."""

    def test_the_default_action_recalls_the_active_state(self):
        assert build_scene_recall("active") == {"recall": {"action": "active"}}

    def test_a_dynamic_palette_recall_carries_duration_and_brightness(self):
        assert build_scene_recall("dynamic_palette", duration=0.8, brightness=80) == {
            "recall": {
                "action": "dynamic_palette",
                "duration": 800,
                "dimming": {"brightness": 80.0},
            }
        }


class TestColorConvenience:
    """rgb, hex and xy are three spellings of one colour; kelvin and mirek two."""

    def test_every_colour_spelling_builds_the_same_payload(self):
        xy = color.rgb_to_xy((255, 136, 0))
        by_rgb = build_light_payload(rgb=(255, 136, 0))
        by_hex = build_light_payload(hex_color="#ff8800")
        by_xy = build_light_payload(xy=xy)
        assert by_rgb == by_hex == by_xy
        assert by_rgb == {"color": {"xy": {"x": xy[0], "y": xy[1]}}}

    def test_the_hex_shorthand_is_the_same_colour(self):
        assert build_light_payload(hex_color="#f80") == build_light_payload(
            hex_color="#ff8800"
        )

    def test_kelvin_and_mirek_build_the_same_payload(self):
        by_kelvin = build_light_payload(kelvin=2200)
        assert by_kelvin == build_light_payload(mirek=color.kelvin_to_mirek(2200))
        assert by_kelvin == {"color_temperature": {"mirek": 455}}

    def test_kelvin_outside_the_bridges_range_is_clamped_not_rejected(self):
        assert build_light_payload(kelvin=10000) == {
            "color_temperature": {"mirek": color.MIREK_MIN}
        }

    def test_a_converted_colour_combines_with_every_other_field(self):
        xy = color.rgb_to_xy((255, 136, 0))
        assert build_light_payload(on=True, brightness=40.0, rgb=(255, 136, 0)) == {
            "on": {"on": True},
            "dimming": {"brightness": 40.0},
            "color": {"xy": {"x": xy[0], "y": xy[1]}},
        }

    def test_a_malformed_hex_colour_is_rejected(self):
        with pytest.raises(ValueError, match="Invalid hex colour"):
            build_light_payload(hex_color="#ff88")

    @pytest.mark.parametrize(
        ("kwargs", "names"),
        [
            ({"rgb": (255, 0, 0), "hex_color": "#ff0000"}, ("rgb", "hex_color")),
            ({"xy": (0.3, 0.4), "rgb": (255, 0, 0)}, ("xy", "rgb")),
            ({"xy": (0.3, 0.4), "hex_color": "#ff0000"}, ("xy", "hex_color")),
        ],
        ids=["rgb+hex", "xy+rgb", "xy+hex"],
    )
    def test_two_colours_are_rejected_and_both_are_named(self, kwargs, names):
        with pytest.raises(ValueError, match="A light takes one colour") as excinfo:
            build_light_payload(**kwargs)
        assert all(name in str(excinfo.value) for name in names)

    def test_two_colour_temperatures_are_rejected_and_both_are_named(self):
        with pytest.raises(ValueError, match="one colour temperature") as excinfo:
            build_light_payload(mirek=350, kelvin=2200)
        message = str(excinfo.value)
        assert "mirek" in message
        assert "kelvin" in message

    @pytest.mark.parametrize(
        ("kwargs", "names"),
        [
            ({"rgb": (255, 0, 0), "kelvin": 2200}, ("rgb", "kelvin")),
            ({"hex_color": "#ff0000", "mirek": 350}, ("hex_color", "mirek")),
            ({"xy": (0.3, 0.4), "kelvin": 2200}, ("xy", "kelvin")),
        ],
        ids=["rgb+kelvin", "hex+mirek", "xy+kelvin"],
    )
    def test_a_converted_colour_still_conflicts_with_a_temperature(self, kwargs, names):
        with pytest.raises(ValueError, match="not both") as excinfo:
            build_light_payload(**kwargs)
        assert all(name in str(excinfo.value) for name in names)

    def test_an_explicit_gamut_clamps_the_converted_colour(self):
        payload = build_light_payload(rgb=(0, 255, 0), gamut=color.GAMUT_B)
        point = (payload["color"]["xy"]["x"], payload["color"]["xy"]["y"])
        assert point != color.rgb_to_xy((0, 255, 0))
        assert is_inside(point, GAMUT_B_CORNERS)

    def test_without_a_gamut_the_colour_is_sent_untouched(self):
        payload = build_light_payload(rgb=(0, 255, 0))
        point = (payload["color"]["xy"]["x"], payload["color"]["xy"]["y"])
        assert point == color.rgb_to_xy((0, 255, 0))
        assert not is_inside(point, GAMUT_B_CORNERS)


class TestLightColorCommands:
    """A light clamps to its own gamut; a group has none to clamp to."""

    async def test_set_rgb_sends_the_converted_colour(self, hue, http):
        light = bound_light(hue, {"id": "l1", "type": "light"})
        await light.set_rgb((255, 136, 0), transition=1.0)
        method, path, payload = http.last
        assert (method, path) == ("PUT", "/clip/v2/resource/light/l1")
        assert payload["color"]["xy"]["x"] == color.rgb_to_xy((255, 136, 0))[0]
        assert payload["dynamics"] == {"duration": 1000}

    async def test_set_kelvin_sends_mirek(self, hue, http):
        light = bound_light(hue, {"id": "l1", "type": "light"})
        await light.set_kelvin(2200)
        assert http.last[2] == {"color_temperature": {"mirek": 455}}

    async def test_hex_reaches_the_bridge_through_set(self, hue, http):
        light = bound_light(hue, {"id": "l1", "type": "light"})
        await light.set(on=True, hex_color="#ff8800")
        assert http.last[2]["on"] == {"on": True}
        assert "color" in http.last[2]

    async def test_an_out_of_gamut_colour_is_clamped_to_the_lights_own_gamut(
        self,
        hue,
        http,
    ):
        light = bound_light(
            hue,
            {
                "id": "l1",
                "type": "light",
                "color": {"xy": {"x": 0.4, "y": 0.4}, "gamut": GAMUT_B_PAYLOAD},
            },
        )
        await light.set_rgb((0, 255, 0))
        point = sent_xy(http)
        assert point != color.rgb_to_xy((0, 255, 0))
        assert is_inside(point, GAMUT_B_CORNERS)

    async def test_the_gamut_type_is_used_when_the_corners_are_absent(self, hue, http):
        light = bound_light(
            hue,
            {
                "id": "l1",
                "type": "light",
                "color": {"xy": {"x": 0.4, "y": 0.4}, "gamut_type": "C"},
            },
        )
        await light.set_rgb((0, 255, 0))
        point = sent_xy(http)
        assert point != color.rgb_to_xy((0, 255, 0))
        assert is_inside(point, GAMUT_C_CORNERS)

    async def test_reported_corners_win_over_the_gamut_type(self, hue, http):
        """A light that reports both is trusted about its own hardware."""
        light = bound_light(
            hue,
            {
                "id": "l1",
                "type": "light",
                "color": {
                    "xy": {"x": 0.4, "y": 0.4},
                    "gamut": GAMUT_B_PAYLOAD,
                    "gamut_type": "C",
                },
            },
        )
        await light.set_rgb((0, 255, 0))
        assert is_inside(sent_xy(http), GAMUT_B_CORNERS)

    async def test_an_explicit_gamut_overrides_the_lights_own(self, hue, http):
        light = bound_light(
            hue,
            {
                "id": "l1",
                "type": "light",
                "color": {"xy": {"x": 0.4, "y": 0.4}, "gamut": GAMUT_B_PAYLOAD},
            },
        )
        await light.set(rgb=(0, 255, 0), gamut=color.GAMUT_C)
        point = sent_xy(http)
        assert is_inside(point, GAMUT_C_CORNERS)
        assert not is_inside(point, GAMUT_B_CORNERS)

    async def test_a_light_with_no_colour_support_clamps_nothing(self, hue, http):
        light = bound_light(hue, {"id": "l1", "type": "light"})
        await light.set_rgb((0, 255, 0))
        assert sent_xy(http) == color.rgb_to_xy((0, 255, 0))

    async def test_an_unknown_gamut_type_clamps_nothing(self, hue, http):
        light = bound_light(
            hue,
            {
                "id": "l1",
                "type": "light",
                "color": {"xy": {"x": 0.4, "y": 0.4}, "gamut_type": "other"},
            },
        )
        await light.set_rgb((0, 255, 0))
        assert sent_xy(http) == color.rgb_to_xy((0, 255, 0))

    async def test_a_group_has_no_gamut_of_its_own_to_clamp_to(self, hue, http):
        """Its members may be different bulbs, so there is no one triangle."""
        group = models.GroupedLight.model_validate({"id": "g1"}).bind(
            hue,
            "grouped_light",
        )
        await group.set_rgb((0, 255, 0))
        assert sent_xy(http) == color.rgb_to_xy((0, 255, 0))

    def test_a_group_may_report_an_empty_aggregate_colour(self):
        group = models.GroupedLight.model_validate(
            {"id": "g1", "type": "grouped_light", "color": {}}
        )

        assert isinstance(group.color, models.GroupedColor)
        assert group.color.xy is None


class TestLightServices:
    """The services a light reports beyond on/off, colour and brightness."""

    def test_effects_parse(self):
        light = models.Light.model_validate(GRADIENT_STRIP)
        assert light.effects is not None
        assert light.effects.status == "candle"
        assert light.effect == "candle"
        assert Effect.CANDLE in light.effects.effect_values
        assert light.effects.status_values == ["no_effect", "candle", "fire"]

    def test_timed_effects_parse_with_their_countdown(self):
        light = models.Light.model_validate(GRADIENT_STRIP)
        assert light.timed_effects is not None
        assert light.timed_effects.duration == 1800000
        assert light.timed_effects.status == "no_effect"
        assert light.timed_effects.effect_values == ["no_effect", "sunrise"]

    def test_gradient_parses_every_point(self):
        light = models.Light.model_validate(GRADIENT_STRIP)
        assert light.is_gradient is True
        assert light.gradient is not None
        assert light.gradient.mode == "interpolated_palette"
        assert light.gradient.points_capable == 5
        assert light.gradient.pixel_count == 25
        assert [p.color.xy.x for p in light.gradient.points] == [0.2, 0.4]

    def test_gradient_keeps_fields_this_library_does_not_model(self):
        light = models.Light.model_validate(GRADIENT_STRIP)
        assert light.gradient is not None
        assert light.gradient.model_extra is not None
        assert light.gradient.model_extra["mode_values"] == [
            "interpolated_palette",
            "random_pixelated",
        ]

    def test_powerup_parses_through_the_bridges_mode_envelope(self):
        """Powerup nests its state a second time: {"mode": ..., "on": {...}}."""
        light = models.Light.model_validate(GRADIENT_STRIP)
        assert light.powerup is not None
        assert light.powerup.preset == "safety"
        assert light.powerup.configured is True
        assert light.powerup.on is not None
        assert light.powerup.on.on is True
        assert light.powerup.dimming is not None
        assert light.powerup.dimming.brightness == 100.0

    def test_powerup_also_takes_the_plain_shape(self):
        light = models.Light.model_validate(
            {"id": "l1", "powerup": {"preset": "custom", "on": {"on": False}}},
        )
        assert light.powerup is not None
        assert light.powerup.on is not None
        assert light.powerup.on.on is False

    def test_alert_and_signaling_parse(self):
        light = models.Light.model_validate(GRADIENT_STRIP)
        assert light.alert_actions is not None
        assert light.alert_actions.action_values == ["breathe"]
        assert light.signaling is not None
        assert light.signaling.signal_values == ["no_signal", "on_off", "alternating"]
        assert light.signaling.status is not None
        assert light.signaling.status["signal"] == "no_signal"

    def test_an_unrecognised_effect_parses_without_raising(self):
        """A firmware that ships a new effect must not break every light."""
        light = models.Light.model_validate(
            {
                "id": "l1",
                "effects": {
                    "status": "aurora",
                    "effect_values": ["no_effect", "aurora"],
                    "brand_new_key": 1,
                },
            },
        )
        assert light.effect == "aurora"
        assert light.effects is not None
        assert light.effects.model_extra == {"brand_new_key": 1}

    def test_a_plain_bulb_reports_none_of_them(self):
        light = models.Light.model_validate({"id": "l1"})
        assert light.effects is None
        assert light.timed_effects is None
        assert light.gradient is None
        assert light.powerup is None
        assert light.alert_actions is None
        assert light.signaling is None
        assert light.effect is None
        assert light.is_gradient is False

    def test_a_gradient_capable_light_without_points_is_not_a_gradient(self):
        light = models.Light.model_validate(
            {"id": "l1", "gradient": {"points": [], "points_capable": 5}},
        )
        assert light.is_gradient is False

    def test_the_effect_enum_carries_the_documented_values(self):
        assert Effect.NO_EFFECT == "no_effect"
        assert [effect.value for effect in Effect] == [
            "no_effect",
            "candle",
            "fire",
            "prism",
            "sparkle",
            "opal",
            "glisten",
            "underwater",
            "cosmos",
            "sunbeam",
            "enchant",
        ]


class TestLightServiceCommands:
    """Each service command writes one documented payload shape."""

    async def test_set_effect_takes_an_enum_member(self, hue, http):
        light = bound_light(hue, {"id": "l1", "type": "light"})
        await light.set_effect(Effect.CANDLE)
        assert http.last == (
            "PUT",
            "/clip/v2/resource/light/l1",
            {"effects_v2": {"action": {"effect": "candle"}}},
        )

    async def test_set_effect_takes_a_raw_string_too(self, hue, http):
        """An effect newer than this library must still be reachable."""
        light = bound_light(hue, {"id": "l1", "type": "light"})
        await light.set_effect("aurora")
        assert http.last[2] == {"effects_v2": {"action": {"effect": "aurora"}}}

    async def test_set_effect_carries_tint_and_speed(self, hue, http):
        light = bound_light(hue, {"id": "l1", "type": "light"})
        await light.set_effect(Effect.CANDLE, hex_color="#ff8800", speed=0.5)
        action = http.last[2]["effects_v2"]["action"]
        assert action["effect"] == "candle"
        assert action["parameters"]["speed"] == 0.5
        assert "color" in action["parameters"]

    async def test_set_gradient_sends_one_point_per_colour(self, hue, http):
        light = bound_light(hue, {"id": "l1", "type": "light"})
        await light.set_gradient([(0.2, 0.3), (0.4, 0.5)])
        assert http.last[2] == {
            "gradient": {
                "points": [
                    {"color": {"xy": {"x": 0.2, "y": 0.3}}},
                    {"color": {"xy": {"x": 0.4, "y": 0.5}}},
                ],
            },
        }

    async def test_set_gradient_carries_the_mode_when_given(self, hue, http):
        light = bound_light(hue, {"id": "l1", "type": "light"})
        await light.set_gradient([(0.2, 0.3)], mode="interpolated_palette")
        assert http.last[2]["gradient"]["mode"] == "interpolated_palette"

    async def test_set_powerup_sends_the_preset(self, hue, http):
        light = bound_light(hue, {"id": "l1", "type": "light"})
        await light.set_powerup("safety")
        assert http.last[2] == {"powerup": {"preset": "safety"}}

    async def test_alert_breathes_once(self, hue, http):
        light = bound_light(hue, {"id": "l1", "type": "light"})
        assert await light.alert() != []
        assert http.last[2] == {"alert": {"action": "breathe"}}

    async def test_set_timed_effect_sends_the_effect_and_its_duration(self, hue, http):
        light = bound_light(hue, {"id": "l1", "type": "light"})
        await light.set_timed_effect(TimedEffect.SUNRISE, duration=1800)
        assert http.last == (
            "PUT",
            "/clip/v2/resource/light/l1",
            {"timed_effects": {"effect": "sunrise", "duration": 1800000}},
        )

    async def test_set_timed_effect_without_a_duration_just_names_it(self, hue, http):
        light = bound_light(hue, {"id": "l1", "type": "light"})
        await light.set_timed_effect("no_effect")
        assert http.last[2] == {"timed_effects": {"effect": "no_effect"}}

    async def test_set_timed_effect_rejects_a_negative_duration(self, hue, http):
        light = bound_light(hue, {"id": "l1", "type": "light"})
        with pytest.raises(ValueError, match="must not be negative"):
            await light.set_timed_effect(TimedEffect.SUNRISE, duration=-1)
        assert http.writes == []

    async def test_set_timed_effect_rejects_a_duration_over_six_hours(self, hue, http):
        light = bound_light(hue, {"id": "l1", "type": "light"})
        with pytest.raises(ValueError, match="six hours"):
            await light.set_timed_effect(TimedEffect.SUNRISE, duration=21601)
        assert http.writes == []

    async def test_signal_carries_its_duration_and_colours(self, hue, http):
        """A bare light reports no gamut, so the xy passes through unclamped."""
        light = bound_light(hue, {"id": "l1", "type": "light"})
        await light.signal(Signal.ON_OFF_COLOR, duration=10, colors=[(0.3, 0.3)])
        assert http.last == (
            "PUT",
            "/clip/v2/resource/light/l1",
            {
                "signaling": {
                    "signal": "on_off_color",
                    "duration": 10000,
                    "colors": [{"xy": {"x": 0.3, "y": 0.3}}],
                }
            },
        )

    async def test_a_signal_that_takes_no_colours_rejects_them(self, hue, http):
        light = bound_light(hue, {"id": "l1", "type": "light"})
        with pytest.raises(ValueError, match="no colours"):
            await light.signal(Signal.NO_SIGNAL, colors=[(0.3, 0.3)])
        assert http.writes == []

    async def test_more_than_two_signal_colours_are_rejected(self, hue, http):
        light = bound_light(hue, {"id": "l1", "type": "light"})
        with pytest.raises(ValueError, match="at most two colours"):
            await light.signal(
                Signal.ON_OFF_COLOR, colors=[(0.1, 0.1), (0.2, 0.2), (0.3, 0.3)]
            )
        assert http.writes == []

    async def test_identify_asks_the_light_to_announce_itself(self, hue, http):
        light = bound_light(hue, {"id": "l1", "type": "light"})
        await light.identify()
        assert http.last[2] == {"identify": {"action": "identify"}}

    @pytest.mark.parametrize(
        ("delta", "expected"),
        [
            (10, {"action": "up", "brightness_delta": 10}),
            (-5, {"action": "down", "brightness_delta": 5}),
            (0, {"action": "stop"}),
        ],
        ids=["up", "down", "stop"],
    )
    async def test_adjust_brightness_maps_sign_to_direction(
        self, hue, http, delta, expected
    ):
        light = bound_light(hue, {"id": "l1", "type": "light"})
        await light.adjust_brightness(delta)
        assert http.last[2] == {"dimming_delta": expected}

    @pytest.mark.parametrize(
        ("delta", "expected"),
        [
            (50, {"action": "up", "mirek_delta": 50}),
            (-50, {"action": "down", "mirek_delta": 50}),
            (0, {"action": "stop"}),
        ],
        ids=["up", "down", "stop"],
    )
    async def test_adjust_color_temperature_maps_sign_to_direction(
        self, hue, http, delta, expected
    ):
        light = bound_light(hue, {"id": "l1", "type": "light"})
        await light.adjust_color_temperature(delta)
        assert http.last[2] == {"color_temperature_delta": expected}

    async def test_set_powerup_with_custom_fields_forces_the_custom_preset(
        self, hue, http
    ):
        light = bound_light(hue, {"id": "l1", "type": "light"})
        await light.set_powerup(on=True, brightness=50)
        assert http.last[2] == {
            "powerup": {
                "preset": "custom",
                "on": {"mode": "on", "on": {"on": True}},
                "dimming": {"mode": "dimming", "dimming": {"brightness": 50.0}},
            }
        }

    async def test_set_speed_sends_only_the_dynamics_block(self, hue, http):
        light = bound_light(hue, {"id": "l1", "type": "light"})
        await light.set(speed=0.5)
        assert http.last[2] == {"dynamics": {"speed": 0.5}}

    @pytest.mark.parametrize(
        "command",
        [
            lambda light: light.set_rgb((255, 0, 0)),
            lambda light: light.set_kelvin(2700),
            lambda light: light.set_effect(Effect.FIRE),
            lambda light: light.set_gradient([(0.2, 0.3)]),
            lambda light: light.set_powerup("safety"),
            lambda light: light.alert(),
        ],
        ids=["set_rgb", "set_kelvin", "set_effect", "set_gradient", "powerup", "alert"],
    )
    async def test_every_new_command_needs_a_bound_light(self, command):
        light = models.Light.model_validate({"id": "x"})
        with pytest.raises(DetachedResourceError):
            await command(light)


class TestModelExports:
    """Everything public is reachable from huepy.models."""

    @pytest.mark.parametrize(
        "name",
        [
            "Alert",
            "Effect",
            "Effects",
            "EventResource",
            "EventType",
            "Gradient",
            "GradientPoint",
            "HueEvent",
            "Powerup",
            "Signaling",
            "TimedEffects",
            "parse_events",
        ],
    )
    def test_the_new_names_are_exported(self, name):
        assert name in models.__all__
        assert getattr(models, name, None) is not None


class TestPowerupFromRealHardware:
    """Powerup shapes captured from a live bridge (BSB002, sw 1978074000).

    Regression: the bridge sends `{"mode": "previous"}` with no nested state
    for a field that keeps whatever it had. Parsing that as the inner model
    failed its required fields and took the whole `api.lights.list()` down --
    every light on the bridge, not just the one.
    """

    def test_mode_only_fields_parse_as_unset(self):
        light = models.Light.model_validate(
            {
                "id": "l1",
                "type": "light",
                "powerup": {
                    "configured": True,
                    "on": {"mode": "previous"},
                    "preset": "powerfail",
                },
            }
        )
        assert light.powerup is not None
        assert light.powerup.preset == "powerfail"
        assert light.powerup.configured is True
        assert light.powerup.on is None, "mode-only carries no state"

    def test_mixed_nested_and_mode_only_fields(self):
        light = models.Light.model_validate(
            {
                "id": "l2",
                "type": "light",
                "powerup": {
                    "color": {"mode": "previous"},
                    "configured": True,
                    "dimming": {"mode": "previous"},
                    "on": {"mode": "on", "on": {"on": True}},
                    "preset": "last_on_state",
                },
            }
        )
        assert light.powerup is not None
        assert light.powerup.dimming is None
        assert light.powerup.on is not None
        assert light.powerup.on.on is True

    def test_every_light_on_a_real_bridge_parses(self):
        """Both shapes together, as list() would see them."""
        payload = {
            "errors": [],
            "data": [
                {
                    "id": "l1",
                    "type": "light",
                    "powerup": {"configured": True, "on": {"mode": "previous"}},
                },
                {
                    "id": "l2",
                    "type": "light",
                    "powerup": {"dimming": {"mode": "previous"}, "configured": True},
                },
            ],
        }
        lights = unwrap(payload, models.Light)
        assert [light.id for light in lights] == ["l1", "l2"]


class TestPartialFailureFromRealHardware:
    """207 Multi-Status envelopes captured from a live bridge.

    The bridge overloads errors[] for two different things and only
    `error_code` separates them. Getting this wrong breaks one way or the
    other: raise on everything and a single bulb with a flaky radio breaks
    every call that touches it; raise on nothing and an unsupported attribute
    is silently dropped, which is the silent-success bug this envelope exists
    to prevent.
    """

    COMMUNICATION: ClassVar[dict[str, object]] = {
        "data": [{"rid": "7d235587", "rtype": "light"}],
        "errors": [
            {
                "description": (
                    "device (light) 7d235587 has communication issues, "
                    "command (.on.on) may not have effect"
                ),
                "error_code": "communication_error",
            }
        ],
    }
    SOFT_OFF: ClassVar[dict[str, object]] = {
        "data": [{"rid": "7d235587", "rtype": "light"}],
        "errors": [
            {
                "description": (
                    'device (light) 7d235587 is "soft off", '
                    "command (.dimming.brightness) may not have effect"
                ),
                "error_code": "attribute_may_have_no_effect",
            }
        ],
    }
    UNSUPPORTED: ClassVar[dict[str, object]] = {
        "data": [{"rid": "17abe584", "rtype": "light"}],
        "errors": [
            {
                "description": (
                    "attribute (.color_temperature.mirek) is not supported "
                    "by resource 17abe584"
                ),
                "error_code": "client_error",
            }
        ],
    }

    def test_flaky_device_does_not_raise(self, caplog):
        with caplog.at_level(logging.WARNING):
            result = unwrap(self.COMMUNICATION, models.ResourceIdentifier)
        assert [r.rid for r in result] == ["7d235587"], "the command was accepted"
        assert "communication_error" in caplog.text

    def test_soft_off_light_does_not_raise(self, caplog):
        """Observed on a real bridge: a brightness write to an off light.

        Capture/restore sends brightness for every light it puts back, so
        raising here made restoring fail whenever one of them was switched
        off -- while the bridge had in fact accepted the command.
        """
        with caplog.at_level(logging.WARNING):
            result = unwrap(self.SOFT_OFF, models.ResourceIdentifier)
        assert [r.rid for r in result] == ["7d235587"], "the command was accepted"
        assert "attribute_may_have_no_effect" in caplog.text

    def test_unsupported_attribute_still_raises(self):
        """Even though the bridge lists the resource in data."""
        with pytest.raises(HueResponseError, match="not supported"):
            unwrap(self.UNSUPPORTED, models.ResourceIdentifier)

    def test_a_blocking_error_beside_an_advisory_one_still_raises(self):
        """Widening the advisory set must not let a real rejection through.

        The bridge can report both at once -- a light that is off *and* asked
        for an attribute it does not have. Classification is per error, so the
        blocking one must still win.
        """
        mixed = {
            "data": [{"rid": "7d235587", "rtype": "light"}],
            "errors": [
                {
                    "description": 'device is "soft off"',
                    "error_code": "attribute_may_have_no_effect",
                },
                {
                    "description": "attribute is not supported by resource",
                    "error_code": "client_error",
                },
            ],
        }
        with pytest.raises(HueResponseError, match="not supported"):
            unwrap(mixed, models.ResourceIdentifier)

    def test_advisory_error_that_changed_nothing_still_raises(self):
        payload = {"data": [], "errors": self.COMMUNICATION["errors"]}
        with pytest.raises(HueResponseError):
            unwrap(payload, models.ResourceIdentifier)

    def test_error_code_is_captured(self):
        response = models.HueResponse[models.ResourceIdentifier].model_validate(
            self.UNSUPPORTED
        )
        assert response.errors[0].error_code == "client_error"

    def test_an_error_without_a_code_is_treated_as_blocking(self):
        """Unknown shapes must fail loudly rather than be assumed advisory."""
        payload = {
            "data": [{"rid": "a", "rtype": "light"}],
            "errors": [{"description": "?"}],
        }
        with pytest.raises(HueResponseError):
            unwrap(payload, models.ResourceIdentifier)
