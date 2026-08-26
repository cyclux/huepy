"""Request-shaping tests for the resource handlers."""

import pytest

from huepy import models
from huepy.exceptions import (
    AmbiguousResourceError,
    DetachedResourceError,
    HueResponseError,
    ResourceNotFoundError,
)
from huepy.resources import NamedResourceHandler

LIGHT = "/clip/v2/resource/light"
GROUPED = "/clip/v2/resource/grouped_light"
ROOM = "/clip/v2/resource/room"
ZONE = "/clip/v2/resource/zone"
MOTION = "/clip/v2/resource/motion"
SCENE = "/clip/v2/resource/scene"
SMART_SCENE = "/clip/v2/resource/smart_scene"

# One day of a smart scene's schedule, in the bridge's shape: a fixed clock
# time that recalls a scene, recurring every Monday.
WEEK_TIMESLOT = {
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


class TestLight:
    async def test_turn_on(self, hue, http):
        await hue.api.lights.turn_on("abc")
        assert http.last == ("PUT", f"{LIGHT}/abc", {"on": {"on": True}})

    async def test_turn_off(self, hue, http):
        await hue.api.lights.turn_off("abc")
        assert http.last == ("PUT", f"{LIGHT}/abc", {"on": {"on": False}})

    @pytest.mark.parametrize(
        ("given", "expected"),
        [(50.0, 50.0), (150.0, 100.0), (-5.0, 0.0), (0.0, 0.0), (100.0, 100.0)],
    )
    async def test_set_brightness_clamps_to_0_100(self, hue, http, given, expected):
        await hue.api.lights.set_brightness("abc", given)
        assert http.last[2] == {"dimming": {"brightness": expected}}

    async def test_set_color(self, hue, http):
        await hue.api.lights.set_color("abc", 0.3, 0.4)
        assert http.last[2] == {"color": {"xy": {"x": 0.3, "y": 0.4}}}

    async def test_set_color_temperature(self, hue, http):
        await hue.api.lights.set_color_temperature("abc", 350)
        assert http.last[2] == {"color_temperature": {"mirek": 350}}

    async def test_update_returns_resource_references(self, hue, http):
        result = await hue.api.lights.turn_on("abc")
        assert [r.rid for r in result] == ["updated-id"]
        assert isinstance(result[0], models.ResourceIdentifier)

    async def test_get_returns_a_model(self, hue, http):
        http.queue_resource(
            "light", "abc", {"id": "abc", "dimming": {"brightness": 42.0}}
        )
        light = await hue.api.lights.get("abc")
        assert isinstance(light, models.Light)
        assert light.brightness == 42.0

    async def test_list_returns_models(self, hue, http):
        http.queue_collection("light", [{"id": "a"}, {"id": "b"}])
        lights = await hue.api.lights.list()
        assert [light.id for light in lights] == ["a", "b"]

    async def test_list_of_empty_collection(self, hue, http):
        http.queue_collection("light", [])
        assert await hue.api.lights.list() == []

    async def test_get_lights_on_filters_to_powered_lights(self, hue, http):
        http.queue_collection(
            "light",
            [
                {"id": "on-1", "on": {"on": True}},
                {"id": "off-1", "on": {"on": False}},
                {"id": "on-2", "on": {"on": True}},
            ],
        )
        assert [light.id for light in await hue.api.lights.get_lights_on()] == [
            "on-1",
            "on-2",
        ]

    async def test_get_service_ids_on(self, hue, http):
        http.queue_collection(
            "light",
            [{"id": "on-1", "on": {"on": True}}, {"id": "off-1", "on": {"on": False}}],
        )
        assert await hue.api.lights.get_service_ids_on() == ["on-1"]

    async def test_get_device_ids_on_skips_lights_without_owner(self, hue, http):
        http.queue_collection(
            "light",
            [
                {
                    "id": "on-1",
                    "on": {"on": True},
                    "owner": {"rid": "dev-1", "rtype": "device"},
                },
                {"id": "on-2", "on": {"on": True}},
            ],
        )
        assert await hue.api.lights.get_device_ids_on() == ["dev-1"]


class TestGroupedLight:
    async def test_targets_the_grouped_light_endpoint(self, hue, http):
        await hue.api.grouped_lights.turn_on("group-1")
        assert http.last == ("PUT", f"{GROUPED}/group-1", {"on": {"on": True}})

    async def test_set_brightness_clamps(self, hue, http):
        await hue.api.grouped_lights.set_brightness("group-1", 999.0)
        assert http.last[2] == {"dimming": {"brightness": 100.0}}


class TestRoom:
    """Low-level rooms expose their grouped-light relationship explicitly."""

    @staticmethod
    def _room_with_grouped_light(http, room_id="room-1", rid="gl-1"):
        http.queue_resource(
            "room",
            room_id,
            {
                "id": room_id,
                "services": [
                    {"rid": "motion-1", "rtype": "motion"},
                    {"rid": rid, "rtype": "grouped_light"},
                ],
            },
        )

    async def test_grouped_light_id_resolves(self, hue, http):
        self._room_with_grouped_light(http)
        assert await hue.api.rooms.grouped_light_id("room-1") == "gl-1"

    def test_does_not_hide_grouped_light_reads_inside_commands(self, hue):
        for command in (
            "turn_on",
            "turn_off",
            "set_brightness",
            "set_color",
            "set_color_temperature",
        ):
            assert not hasattr(hue.api.rooms, command)

    async def test_raises_when_room_has_no_grouped_light(self, hue, http):
        http.queue_resource(
            "room",
            "room-1",
            {"id": "room-1", "services": [{"rid": "m", "rtype": "motion"}]},
        )
        with pytest.raises(ValueError, match="No grouped_light service"):
            await hue.api.rooms.grouped_light_id("room-1")

    async def test_create_posts_children_as_device_refs(self, hue, http):
        await hue.api.rooms.create("Kitchen", ["dev-1", "dev-2"])
        method, path, payload = http.last
        assert (method, path) == ("POST", ROOM)
        assert payload == {
            "metadata": {"name": "Kitchen"},
            "children": [
                {"rid": "dev-1", "rtype": "device"},
                {"rid": "dev-2", "rtype": "device"},
            ],
        }

    async def test_get_from_light_service_id_finds_owning_room(self, hue, http):
        http.queue_resource(
            "light",
            "light-1",
            {"id": "light-1", "owner": {"rid": "dev-1", "rtype": "device"}},
        )
        http.queue_collection(
            "room",
            [
                {"id": "room-a", "children": [{"rid": "other", "rtype": "device"}]},
                {"id": "room-b", "children": [{"rid": "dev-1", "rtype": "device"}]},
            ],
        )
        assert await hue.api.rooms.get_from_light_service_id("light-1") == "room-b"

    async def test_get_from_light_service_id_without_owner(self, hue, http):
        http.queue_resource("light", "light-1", {"id": "light-1"})
        assert await hue.api.rooms.get_from_light_service_id("light-1") is None

    async def test_get_from_light_service_id_when_unassigned(self, hue, http):
        http.queue_resource(
            "light",
            "light-1",
            {"id": "light-1", "owner": {"rid": "dev-1", "rtype": "device"}},
        )
        http.queue_collection("room", [{"id": "room-a", "children": []}])
        assert await hue.api.rooms.get_from_light_service_id("light-1") is None


class TestZone:
    async def test_create_is_async_and_posts_children(self, hue, http):
        services = [{"rid": "room-1", "rtype": "room"}]
        await hue.api.zones.create("Living Areas", services)
        assert http.last == (
            "POST",
            ZONE,
            {"metadata": {"name": "Living Areas"}, "children": services},
        )

    async def test_low_level_control_addresses_grouped_light_explicitly(
        self, hue, http
    ):
        http.queue_resource(
            "zone",
            "zone-1",
            {"id": "zone-1", "services": [{"rid": "gl-9", "rtype": "grouped_light"}]},
        )
        service_id = await hue.api.zones.grouped_light_id("zone-1")
        await hue.api.grouped_lights.turn_on(service_id)
        assert http.last == ("PUT", f"{GROUPED}/gl-9", {"on": {"on": True}})


class TestServiceGroup:
    async def test_is_reachable_from_the_client(self, hue):
        assert hue.api.service_groups is not None

    async def test_create_is_async_and_posts_archetype(self, hue, http):
        services = [{"rid": "sensor-1", "rtype": "motion"}]
        await hue.api.service_groups.create("Motion Sensors", services)
        method, path, payload = http.last
        assert (method, path) == ("POST", "/clip/v2/resource/service_group")
        assert payload["metadata"] == {
            "name": "Motion Sensors",
            "archetype": "sensor_group",
        }


class TestScene:
    async def test_activate(self, hue, http):
        await hue.api.scenes.activate("scene-1")
        assert http.last == (
            "PUT",
            "/clip/v2/resource/scene/scene-1",
            {"recall": {"action": "active"}},
        )

    async def test_activate_with_dynamic_palette_carries_the_recall(self, hue, http):
        await hue.api.scenes.activate(
            "scene-1", action="dynamic_palette", duration=0.8, brightness=80
        )
        assert http.last == (
            "PUT",
            f"{SCENE}/scene-1",
            {
                "recall": {
                    "action": "dynamic_palette",
                    "duration": 800,
                    "dimming": {"brightness": 80.0},
                }
            },
        )

    async def test_create(self, hue, http):
        await hue.api.scenes.create("Movie", "room-1")
        assert http.last == (
            "POST",
            "/clip/v2/resource/scene",
            {
                "metadata": {"name": "Movie"},
                "group": {"rid": "room-1", "rtype": "room"},
            },
        )


class TestMotion:
    async def test_turn_on_sets_enabled(self, hue, http):
        await hue.api.motions.turn_on("m-1")
        assert http.last[2] == {"enabled": True}

    async def test_turn_off_sets_enabled_false(self, hue, http):
        await hue.api.motions.turn_off("m-1")
        assert http.last[2] == {"enabled": False}

    async def test_set_sensitivity_within_max(self, hue, http):
        http.queue_resource(
            "motion", "m-1", {"id": "m-1", "sensitivity": {"sensitivity_max": 4}}
        )
        await hue.api.motions.set_sensitivity("m-1", 3)
        assert http.last[2] == {"sensitivity": {"sensitivity": 3}}

    async def test_set_sensitivity_above_max_raises(self, hue, http):
        http.queue_resource(
            "motion", "m-1", {"id": "m-1", "sensitivity": {"sensitivity_max": 4}}
        )
        with pytest.raises(ValueError, match="exceeds maximum allowed 4"):
            await hue.api.motions.set_sensitivity("m-1", 9)
        assert http.writes == []

    async def test_negative_sensitivity_raises_value_error(self, hue):
        with pytest.raises(ValueError, match="cannot be negative"):
            await hue.api.motions.set_sensitivity("m-1", -1)

    async def test_non_integer_sensitivity_raises_type_error(self, hue):
        """A wrong *type* is a TypeError, not a ValueError."""
        with pytest.raises(TypeError, match="must be an integer"):
            await hue.api.motions.set_sensitivity("m-1", "high")  # type: ignore[arg-type]

    async def test_get_motion_state(self, hue, http):
        http.queue_resource("motion", "m-1", {"id": "m-1", "motion": {"motion": True}})
        assert await hue.api.motions.get_motion_state("m-1") is True

    async def test_get_motion_state_defaults_false(self, hue, http):
        http.queue_resource("motion", "m-1", {"id": "m-1"})
        assert await hue.api.motions.get_motion_state("m-1") is False

    async def test_get_last_motion(self, hue, http):
        http.queue_resource(
            "motion",
            "m-1",
            {
                "id": "m-1",
                "motion": {"motion_report": {"changed": "2026-01-01T00:00:00Z"}},
            },
        )
        assert await hue.api.motions.get_last_motion("m-1") == "2026-01-01T00:00:00Z"


class TestDevice:
    async def test_delete_uses_the_inherited_signature(self, hue, http):
        """Device.delete no longer renames the base class's parameter."""
        await hue.api.devices.delete(resource_id="dev-1")
        assert http.last == ("DELETE", "/clip/v2/resource/device/dev-1", None)


class TestSensorHandlersAreReachable:
    @pytest.mark.parametrize(
        ("attribute", "resource_type"),
        [
            ("temperatures", "temperature"),
            ("buttons", "button"),
            ("contacts", "contact"),
            ("device_powers", "device_power"),
            ("grouped_motions", "grouped_motion"),
            ("light_levels", "light_level"),
            ("grouped_light_levels", "grouped_light_level"),
            ("bridges", "bridge"),
            ("bridge_homes", "bridge_home"),
            ("service_groups", "service_group"),
        ],
    )
    async def test_handler_targets_the_right_endpoint(
        self, hue, attribute, resource_type
    ):
        handler = getattr(hue.api, attribute)
        assert handler.base_url == f"/clip/v2/resource/{resource_type}"


class TestErrorPropagation:
    async def test_body_errors_surface_from_a_write(self, hue, http):
        http.write_result = {
            "errors": [{"description": "device is not responding"}],
            "data": [],
        }
        with pytest.raises(HueResponseError, match="not responding"):
            await hue.api.lights.turn_on("abc")

    async def test_not_started_raises_runtime_error(self, bare_hue):
        with pytest.raises(RuntimeError, match="Client not initialized"):
            await bare_hue.api.lights.list()


class TestParsedResourcesAreBound:
    """Everything a handler parses comes back able to act on itself."""

    async def test_get_returns_a_bound_model(self, hue, http):
        http.queue_resource("light", "abc", {"id": "abc"})
        light = await hue.api.lights.get("abc")
        assert light.is_bound is True

    async def test_list_binds_every_model(self, hue, http):
        http.queue_collection("light", [{"id": "a"}, {"id": "b"}])
        assert [light.is_bound for light in await hue.api.lights.list()] == [True, True]

    async def test_list_returns_bound_models(self, hue, http):
        http.queue_collection("light", [{"id": "a"}, {"id": "b"}])
        lights = await hue.api.lights.list()
        assert [light.id for light in lights] == ["a", "b"]
        assert all(light.is_bound for light in lights)

    async def test_the_handler_supplies_the_rtype_the_payload_omits(self, hue, http):
        """A payload without a `type` still binds to the right endpoint."""
        http.queue_resource("light", "abc", {"id": "abc"})
        light = await hue.api.lights.get("abc")
        assert light._path == f"{LIGHT}/abc"

    async def test_a_bound_light_commands_its_own_endpoint(self, hue, http):
        http.queue_resource("light", "abc", {"id": "abc"})
        light = await hue.api.lights.get("abc")
        await light.turn_on()
        assert http.last == ("PUT", f"{LIGHT}/abc", {"on": {"on": True}})

    async def test_a_bound_grouped_light_commands_its_own_endpoint(self, hue, http):
        http.queue_resource("grouped_light", "gl-1", {"id": "gl-1"})
        group = await hue.api.grouped_lights.get("gl-1")
        await group.set_brightness(999.0)
        assert http.last == (
            "PUT",
            f"{GROUPED}/gl-1",
            {"dimming": {"brightness": 100.0}},
        )

    async def test_update_targets_the_resource_path(self, hue, http):
        http.queue_resource("light", "abc", {"id": "abc"})
        light = await hue.api.lights.get("abc")
        result = await light.update({"metadata": {"name": "Desk"}})
        assert http.last == ("PUT", f"{LIGHT}/abc", {"metadata": {"name": "Desk"}})
        assert [r.rid for r in result.resources] == ["updated-id"]

    async def test_delete_targets_the_resource_path(self, hue, http):
        http.queue_resource("light", "abc", {"id": "abc"})
        light = await hue.api.lights.get("abc")
        await light.delete()
        assert http.last == ("DELETE", f"{LIGHT}/abc", None)

    async def test_body_errors_surface_from_a_bound_command(self, hue, http):
        http.queue_resource("light", "abc", {"id": "abc"})
        light = await hue.api.lights.get("abc")
        http.write_result = {
            "errors": [{"description": "device is not responding"}],
            "data": [],
        }
        with pytest.raises(HueResponseError, match="not responding"):
            await light.turn_on()


class TestBoundLightCommands:
    """`set` composes; the convenience methods are wrappers over it."""

    @staticmethod
    async def _light(hue, http, light_id="abc"):
        http.queue_resource("light", light_id, {"id": light_id})
        return await hue.api.lights.get(light_id)

    async def test_set_composes_one_payload(self, hue, http):
        light = await self._light(hue, http)
        await light.set(on=True, brightness=40.0, mirek=350, transition=1.5)
        assert http.last == (
            "PUT",
            f"{LIGHT}/abc",
            {
                "on": {"on": True},
                "dimming": {"brightness": 40.0},
                "color_temperature": {"mirek": 350},
                "dynamics": {"duration": 1500},
            },
        )

    async def test_set_without_arguments_sends_nothing(self, hue, http):
        light = await self._light(hue, http)
        http.calls.clear()
        assert (await light.set()).sent is False
        assert http.calls == []

    async def test_turn_off_with_a_transition(self, hue, http):
        light = await self._light(hue, http)
        await light.turn_off(transition=2.0)
        assert http.last[2] == {"on": {"on": False}, "dynamics": {"duration": 2000}}

    async def test_set_color(self, hue, http):
        light = await self._light(hue, http)
        await light.set_color(0.3, 0.4)
        assert http.last[2] == {"color": {"xy": {"x": 0.3, "y": 0.4}}}

    async def test_set_color_temperature(self, hue, http):
        light = await self._light(hue, http)
        await light.set_color_temperature(350)
        assert http.last[2] == {"color_temperature": {"mirek": 350}}

    async def test_conflicting_colour_arguments_send_nothing(self, hue, http):
        light = await self._light(hue, http)
        http.calls.clear()
        with pytest.raises(ValueError, match="not both"):
            await light.set(xy=(0.3, 0.4), mirek=350)
        assert http.calls == []


class TestBoundRoomRoundTrips:
    """The room already knows its grouped_light service; it must not re-fetch it."""

    @staticmethod
    def _queue_room(http, room_id="room-1", rid="gl-1"):
        http.queue_resource(
            "room",
            room_id,
            {
                "id": room_id,
                "type": "room",
                "services": [
                    {"rid": "motion-1", "rtype": "motion"},
                    {"rid": rid, "rtype": "grouped_light"},
                ],
            },
        )

    async def test_bound_room_set_issues_exactly_one_request(self, hue, http):
        """Regression guard: the handler used to re-GET the room per command.

        Dimming and warming a room once cost four round trips, because every
        command re-resolved the grouped_light service the room object already
        carried. One `set` on a bound room is one PUT -- nothing else.
        """
        self._queue_room(http)
        room = await hue.api.rooms.get("room-1")
        http.calls.clear()

        await room.set(on=True, brightness=60.0, mirek=350, transition=1.5)

        assert http.calls == [
            (
                "PUT",
                f"{GROUPED}/gl-1",
                {
                    "on": {"on": True},
                    "dimming": {"brightness": 60.0},
                    "color_temperature": {"mirek": 350},
                    "dynamics": {"duration": 1500},
                },
            ),
        ]

    @pytest.mark.parametrize(
        ("command", "expected"),
        [
            (lambda room: room.turn_on(), {"on": {"on": True}}),
            (lambda room: room.turn_off(), {"on": {"on": False}}),
            (
                lambda room: room.set_brightness(60.0),
                {"dimming": {"brightness": 60.0}},
            ),
            (
                lambda room: room.set_color(0.1, 0.2),
                {"color": {"xy": {"x": 0.1, "y": 0.2}}},
            ),
            (
                lambda room: room.set_color_temperature(300),
                {"color_temperature": {"mirek": 300}},
            ),
        ],
        ids=["turn_on", "turn_off", "set_brightness", "set_color", "set_ct"],
    )
    async def test_every_command_is_one_put_to_grouped_light(
        self, hue, http, command, expected
    ):
        self._queue_room(http)
        room = await hue.api.rooms.get("room-1")
        http.calls.clear()

        await command(room)

        assert http.calls == [("PUT", f"{GROUPED}/gl-1", expected)]

    async def test_a_bound_zone_routes_the_same_way(self, hue, http):
        http.queue_resource(
            "zone",
            "zone-1",
            {
                "id": "zone-1",
                "type": "zone",
                "services": [{"rid": "gl-9", "rtype": "grouped_light"}],
            },
        )
        zone = await hue.api.zones.get("zone-1")
        http.calls.clear()

        await zone.turn_on()

        assert http.calls == [("PUT", f"{GROUPED}/gl-9", {"on": {"on": True}})]

    async def test_a_room_without_a_grouped_light_service_raises(self, hue, http):
        http.queue_resource(
            "room",
            "room-1",
            {
                "id": "room-1",
                "type": "room",
                "services": [{"rid": "m", "rtype": "motion"}],
            },
        )
        room = await hue.api.rooms.get("room-1")
        http.calls.clear()

        with pytest.raises(ValueError, match="No grouped_light service found for room"):
            await room.turn_on()
        assert http.calls == []


class TestBoundGroupMembership:
    """A group's children are references; resolving them to lights is one join."""

    @staticmethod
    def _queue(http, *, children, lights):
        http.queue_resource(
            "room",
            "room-1",
            {"id": "room-1", "type": "room", "children": children},
        )
        http.queue_collection("light", lights)

    @staticmethod
    def _light(light_id, owner, *, brightness=50.0, mirek=300):
        return {
            "id": light_id,
            "type": "light",
            "owner": {"rid": owner, "rtype": "device"},
            "on": {"on": True},
            "dimming": {"brightness": brightness},
            "color_temperature": {"mirek": mirek, "mirek_valid": True},
        }

    async def test_lights_lists_once_and_keeps_only_members(self, hue, http):
        self._queue(
            http,
            children=[{"rid": "dev-1", "rtype": "device"}],
            lights=[self._light("light-1", "dev-1"), self._light("light-2", "dev-9")],
        )
        room = await hue.api.rooms.get("room-1")
        http.calls.clear()

        lights = await room.lights()

        assert [light.id for light in lights] == ["light-1"]
        assert http.paths == [LIGHT]

    async def test_lights_come_back_bound(self, hue, http):
        """An unbound light would raise the moment anyone tried to command it."""
        self._queue(
            http,
            children=[{"rid": "dev-1", "rtype": "device"}],
            lights=[self._light("light-1", "dev-1")],
        )
        room = await hue.api.rooms.get("room-1")
        http.calls.clear()

        (light,) = await room.lights()
        await light.turn_on()

        assert http.writes == [("PUT", f"{LIGHT}/light-1", {"on": {"on": True}})]

    async def test_capture_and_restore_round_trip_per_light(self, hue, http):
        """Per light, because a grouped_light carries no colour temperature."""
        self._queue(
            http,
            children=[
                {"rid": "dev-1", "rtype": "device"},
                {"rid": "dev-2", "rtype": "device"},
            ],
            lights=[
                self._light("light-1", "dev-1", brightness=40.0, mirek=300),
                self._light("light-2", "dev-2", brightness=80.0, mirek=450),
            ],
        )
        room = await hue.api.rooms.get("room-1")
        captured = await room.capture()
        assert captured.group_id == "room-1"
        assert [state.light_id for state in captured.lights] == ["light-1", "light-2"]
        http.calls.clear()

        results = await room.restore(captured, transition=1.5)

        assert len(results) == 2
        assert http.writes == [
            (
                "PUT",
                f"{LIGHT}/light-1",
                {
                    "on": {"on": True},
                    "dimming": {"brightness": 40.0},
                    "color_temperature": {"mirek": 300},
                    "dynamics": {"duration": 1500},
                },
            ),
            (
                "PUT",
                f"{LIGHT}/light-2",
                {
                    "on": {"on": True},
                    "dimming": {"brightness": 80.0},
                    "color_temperature": {"mirek": 450},
                    "dynamics": {"duration": 1500},
                },
            ),
        ]

    async def test_restore_skips_a_light_that_has_since_left(self, hue, http):
        """Resurrecting a light that moved rooms would be worse than a gap."""
        self._queue(
            http,
            children=[{"rid": "dev-1", "rtype": "device"}],
            lights=[self._light("light-1", "dev-1")],
        )
        room = await hue.api.rooms.get("room-1")
        captured = await room.capture()
        http.queue_collection("light", [])
        http.calls.clear()

        assert await room.restore(captured) == []
        assert http.writes == []

    async def test_restore_refuses_a_snapshot_from_another_group(self, hue, http):
        self._queue(
            http,
            children=[{"rid": "dev-1", "rtype": "device"}],
            lights=[self._light("light-1", "dev-1")],
        )
        room = await hue.api.rooms.get("room-1")
        foreign = models.GroupState(group_id="room-9", lights=())

        with pytest.raises(ValueError, match="belongs to group room-9"):
            await room.restore(foreign)

    async def test_lights_needs_a_bound_group(self):
        room = models.Room.model_validate({"id": "room-1", "type": "room"})
        with pytest.raises(DetachedResourceError):
            await room.lights()


class TestRefresh:
    async def test_refresh_returns_a_new_bound_instance_with_fresh_state(
        self, hue, http
    ):
        http.queue_resource("light", "abc", {"id": "abc", "on": {"on": False}})
        stale = await hue.api.lights.get("abc")
        assert stale.is_on is False

        http.queue_resource(
            "light",
            "abc",
            {"id": "abc", "on": {"on": True}, "dimming": {"brightness": 80.0}},
        )
        fresh = await stale.refresh()

        assert fresh is not stale
        assert isinstance(fresh, models.Light)
        assert fresh.is_on is True
        assert fresh.brightness == 80.0
        assert fresh.is_bound is True
        assert stale.is_on is False, "the original snapshot must not be mutated"

    async def test_refresh_re_reads_the_resource_path(self, hue, http):
        http.queue_resource("light", "abc", {"id": "abc"})
        light = await hue.api.lights.get("abc")
        http.calls.clear()
        await light.refresh()
        assert http.calls == [("GET", f"{LIGHT}/abc", None)]

    async def test_refresh_of_a_room_keeps_the_group_commands(self, hue, http):
        http.queue_resource(
            "room",
            "room-1",
            {
                "id": "room-1",
                "type": "room",
                "services": [{"rid": "gl-1", "rtype": "grouped_light"}],
            },
        )
        room = await hue.api.rooms.get("room-1")
        fresh = await room.refresh()
        http.calls.clear()

        await fresh.turn_on()

        assert http.calls == [("PUT", f"{GROUPED}/gl-1", {"on": {"on": True}})]


class TestBoundScene:
    async def test_activate_sends_the_recall_payload(self, hue, http):
        http.queue_resource(
            "scene",
            "scene-1",
            {"id": "scene-1", "type": "scene", "metadata": {"name": "Movie"}},
        )
        scene = await hue.api.scenes.get("scene-1")
        http.calls.clear()

        await scene.activate()

        assert http.calls == [
            (
                "PUT",
                "/clip/v2/resource/scene/scene-1",
                {"recall": {"action": "active"}},
            ),
        ]

    async def test_activate_can_override_action_duration_and_brightness(
        self, hue, http
    ):
        http.queue_resource(
            "scene",
            "scene-1",
            {"id": "scene-1", "type": "scene", "metadata": {"name": "Movie"}},
        )
        scene = await hue.api.scenes.get("scene-1")
        http.calls.clear()

        await scene.activate(action="dynamic_palette", duration=0.8, brightness=80)

        assert http.calls == [
            (
                "PUT",
                f"{SCENE}/scene-1",
                {
                    "recall": {
                        "action": "dynamic_palette",
                        "duration": 800,
                        "dimming": {"brightness": 80.0},
                    }
                },
            ),
        ]

    async def test_a_hand_built_scene_cannot_activate(self):
        scene = models.Scene.model_validate({"id": "scene-1"})
        with pytest.raises(DetachedResourceError):
            await scene.activate()


class TestSmartScene:
    """Handler shaping for smart scenes -- schedules recalled as a whole."""

    async def test_create_posts_the_schedule_and_group(self, hue, http):
        await hue.api.smart_scenes.create(
            "Rhythm", "room-1", [WEEK_TIMESLOT], transition_duration=60
        )
        assert http.last == (
            "POST",
            SMART_SCENE,
            {
                "metadata": {"name": "Rhythm"},
                "group": {"rid": "room-1", "rtype": "room"},
                "week_timeslots": [WEEK_TIMESLOT],
                "transition_duration": 60000,
            },
        )

    async def test_activate_recalls_the_schedule(self, hue, http):
        await hue.api.smart_scenes.activate("ss-1")
        assert http.last == (
            "PUT",
            f"{SMART_SCENE}/ss-1",
            {"recall": {"action": "activate"}},
        )

    async def test_deactivate_stops_the_schedule(self, hue, http):
        await hue.api.smart_scenes.deactivate("ss-1")
        assert http.last == (
            "PUT",
            f"{SMART_SCENE}/ss-1",
            {"recall": {"action": "deactivate"}},
        )


class TestBoundSmartScene:
    @staticmethod
    async def _scene(hue, http, scene_id="ss-1"):
        http.queue_resource(
            "smart_scene",
            scene_id,
            {
                "id": scene_id,
                "type": "smart_scene",
                "metadata": {"name": "Rhythm"},
                "week_timeslots": [WEEK_TIMESLOT],
            },
        )
        return await hue.api.smart_scenes.get(scene_id)

    async def test_activate_sends_the_recall_payload(self, hue, http):
        scene = await self._scene(hue, http)
        http.calls.clear()

        await scene.activate()

        assert http.calls == [
            ("PUT", f"{SMART_SCENE}/ss-1", {"recall": {"action": "activate"}}),
        ]

    async def test_deactivate_sends_the_recall_payload(self, hue, http):
        scene = await self._scene(hue, http)
        http.calls.clear()

        await scene.deactivate()

        assert http.calls == [
            ("PUT", f"{SMART_SCENE}/ss-1", {"recall": {"action": "deactivate"}}),
        ]

    async def test_a_hand_built_smart_scene_cannot_activate(self):
        scene = models.SmartScene.model_validate({"id": "ss-1"})
        with pytest.raises(DetachedResourceError):
            await scene.activate()


class TestNameLookup:
    """Handlers for named resources close the gap between a name and an id."""

    @staticmethod
    def _queue_rooms(http, *names):
        """Queue one room per name, in the order given."""
        http.queue_collection(
            "room",
            [
                {"id": f"room-{index}", "type": "room", "metadata": {"name": name}}
                for index, name in enumerate(names)
            ],
        )

    async def test_finds_a_resource_by_its_exact_name(self, hue, http):
        self._queue_rooms(http, "Kitchen", "Bedroom")
        room = await hue.rooms.get("Kitchen")
        assert isinstance(room, models.Room)
        assert room.id == "room-0"
        assert room.name == "Kitchen"

    @pytest.mark.parametrize(
        "wanted",
        ["Kitchen", "kitchen", "KITCHEN", "KiTcHeN", "  Kitchen", "kitchen \n"],
    )
    async def test_matching_ignores_case_and_surrounding_whitespace(
        self, hue, http, wanted
    ):
        self._queue_rooms(http, "Bedroom", "Kitchen")
        assert (await hue.rooms.get(wanted)).id == "room-1"

    async def test_a_name_the_bridge_padded_still_matches(self, hue, http):
        """Whitespace is trimmed on both sides of the comparison, not just ours."""
        self._queue_rooms(http, "  Kitchen  ")
        assert (await hue.rooms.get("kitchen")).id == "room-0"

    async def test_a_miss_names_what_it_looked_for_and_what_exists(self, hue, http):
        self._queue_rooms(http, "Kitchen", "Bedroom", "Attic")

        with pytest.raises(ResourceNotFoundError) as caught:
            await hue.rooms.get("Kithcen")

        error = caught.value
        assert error.name == "Kithcen"
        assert error.known == ["Attic", "Bedroom", "Kitchen"]
        # The message is the whole point: a typo has to be self-correcting.
        assert str(error) == (
            "No resource named 'Kithcen'. Known names: Attic, Bedroom, Kitchen"
        )

    async def test_a_miss_on_an_empty_collection_still_explains_itself(self, hue, http):
        http.queue_collection("room", [])

        with pytest.raises(ResourceNotFoundError) as caught:
            await hue.rooms.get("Kitchen")

        assert caught.value.known == []
        assert str(caught.value) == "No resource named 'Kitchen'. Known names: none"

    async def test_duplicate_names_raise_before_a_resource_is_selected(self, hue, http):
        http.queue_collection(
            "room",
            [
                {"id": "room-first", "metadata": {"name": "Kitchen"}},
                {"id": "room-second", "metadata": {"name": "kitchen"}},
            ],
        )
        with pytest.raises(AmbiguousResourceError) as caught:
            await hue.rooms.get("KITCHEN")
        assert caught.value.resource_ids == ["room-first", "room-second"]

    async def test_names_are_sorted(self, hue, http):
        self._queue_rooms(http, "Kitchen", "Attic", "Bedroom")
        assert await hue.rooms.names() == ["Attic", "Bedroom", "Kitchen"]

    async def test_names_keeps_duplicates_and_skips_the_unnamed(self, hue, http):
        http.queue_collection(
            "room",
            [
                {"id": "a", "metadata": {"name": "Kitchen"}},
                {"id": "b", "metadata": {"name": "Kitchen"}},
                {"id": "c"},
            ],
        )
        assert await hue.rooms.names() == ["Kitchen", "Kitchen"]

    async def test_a_lookup_costs_exactly_one_round_trip(self, hue, http):
        self._queue_rooms(http, "Kitchen")
        http.calls.clear()

        await hue.rooms.get("Kitchen")

        assert http.calls == [("GET", ROOM, None)]

    async def test_names_costs_exactly_one_round_trip(self, hue, http):
        self._queue_rooms(http, "Kitchen")
        http.calls.clear()

        await hue.rooms.names()

        assert http.calls == [("GET", ROOM, None)]

    async def test_lookup_by_name_works_on_every_named_collection(self, hue, http):
        http.queue_collection("light", [{"id": "l-1", "metadata": {"name": "Desk"}}])
        http.queue_collection("zone", [{"id": "z-1", "metadata": {"name": "Desk"}}])
        http.queue_collection("scene", [{"id": "s-1", "metadata": {"name": "Desk"}}])
        http.queue_collection("device", [{"id": "d-1", "metadata": {"name": "Desk"}}])
        http.queue_collection(
            "service_group",
            [{"id": "sg-1", "metadata": {"name": "Desk"}}],
        )

        found = [
            (await hue.lights.get("desk")).id,
            (await hue.zones.get("desk")).id,
            (await hue.scenes.get("desk")).id,
            (await hue.devices.get("desk")).id,
            (await hue.service_groups.get("desk")).id,
        ]

        assert found == ["l-1", "z-1", "s-1", "d-1", "sg-1"]


class TestResourcesFoundByNameAreBound:
    """A collection lookup returns a resource that can act on itself."""

    async def test_a_light_found_by_name_commands_its_own_endpoint(self, hue, http):
        http.queue_collection("light", [{"id": "abc", "metadata": {"name": "Desk"}}])
        light = await hue.lights.get("Desk")
        assert light.is_bound is True
        http.calls.clear()

        await light.turn_on()

        assert http.calls == [("PUT", f"{LIGHT}/abc", {"on": {"on": True}})]

    async def test_a_room_found_by_name_commands_its_grouped_light(self, hue, http):
        http.queue_collection(
            "room",
            [
                {
                    "id": "room-1",
                    "type": "room",
                    "metadata": {"name": "Kitchen"},
                    "services": [{"rid": "gl-1", "rtype": "grouped_light"}],
                },
            ],
        )
        room = await hue.rooms.get("Kitchen")
        http.calls.clear()

        await room.set_brightness(60.0)

        assert http.calls == [
            ("PUT", f"{GROUPED}/gl-1", {"dimming": {"brightness": 60.0}}),
        ]


class TestNameLookupIsOnTheRightHandlers:
    """Only resources that carry a `metadata.name` may be addressed by one."""

    @pytest.mark.parametrize(
        "attribute",
        ["lights", "rooms", "zones", "scenes", "devices", "service_groups"],
    )
    def test_named_handlers_are_markers_without_name_lookup(self, hue, attribute):
        handler = getattr(hue.api, attribute)
        assert isinstance(handler, NamedResourceHandler)
        assert issubclass(handler.model, models.NamedResource)
        assert not hasattr(handler, "by_name")
        assert not hasattr(handler, "names")

    @pytest.mark.parametrize(
        "attribute",
        [
            "grouped_lights",
            "motions",
            "grouped_motions",
            "temperatures",
            "buttons",
            "contacts",
            "device_powers",
            "bridges",
            "bridge_homes",
            "light_levels",
            "grouped_light_levels",
        ],
    )
    def test_unnamed_handlers_do_not(self, hue, attribute):
        handler = getattr(hue.api, attribute)
        assert not isinstance(handler, NamedResourceHandler)
        assert not hasattr(handler, "by_name")
        assert not hasattr(handler, "names")
        assert not issubclass(handler.model, models.NamedResource)

    def test_a_handler_is_named_exactly_when_its_model_is(self, hue):
        """The two must not drift: whichever a future edit changes, both move."""
        handlers = [
            value
            for name, value in vars(hue.api).items()
            if not name.startswith("_") and hasattr(value, "model")
        ]
        assert handlers, "no handlers found on the client"
        for handler in handlers:
            assert isinstance(handler, NamedResourceHandler) is issubclass(
                handler.model, models.NamedResource
            ), f"{type(handler).__name__} disagrees with its model"
