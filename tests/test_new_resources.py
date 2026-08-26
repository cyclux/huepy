"""Request-shaping tests for the device-management and integration handlers."""

import pytest

DISCOVERY = "/clip/v2/resource/zigbee_device_discovery"
SOFTWARE_UPDATE = "/clip/v2/resource/device_software_update"
BEHAVIOR_INSTANCE = "/clip/v2/resource/behavior_instance"
GEOLOCATION = "/clip/v2/resource/geolocation"
GEOFENCE_CLIENT = "/clip/v2/resource/geofence_client"
HOMEKIT = "/clip/v2/resource/homekit"
MATTER = "/clip/v2/resource/matter"
CAMERA_MOTION = "/clip/v2/resource/camera_motion"
ENTERTAINMENT_CONFIG = "/clip/v2/resource/entertainment_configuration"
DEVICE = "/clip/v2/resource/device"
BRIDGE = "/clip/v2/resource/bridge"


class TestZigbeeDeviceDiscovery:
    async def test_search_carries_channels(self, hue, http):
        await hue.api.zigbee_device_discoveries.search("z1", channels=[15, 20])
        assert http.last == (
            "PUT",
            f"{DISCOVERY}/z1",
            {"action": {"action_type": "search", "search_channels": [15, 20]}},
        )

    async def test_search_omits_both_optional_keys(self, hue, http):
        await hue.api.zigbee_device_discoveries.search("z1")
        assert http.last == (
            "PUT",
            f"{DISCOVERY}/z1",
            {"action": {"action_type": "search"}},
        )

    async def test_search_carries_install_codes(self, hue, http):
        await hue.api.zigbee_device_discoveries.search("z1", install_codes=["ABC"])
        assert http.last[2] == {
            "action": {"action_type": "search", "search_codes": ["ABC"]}
        }

    async def test_search_with_default_link_key_changes_the_action_type(
        self, hue, http
    ):
        await hue.api.zigbee_device_discoveries.search_with_default_link_key("z1")
        assert http.last == (
            "PUT",
            f"{DISCOVERY}/z1",
            {"action": {"action_type": "search_allow_default_link_key"}},
        )


class TestDeviceSoftwareUpdate:
    async def test_install_readies_the_update(self, hue, http):
        await hue.api.device_software_updates.install("d1")
        assert http.last == (
            "PUT",
            f"{SOFTWARE_UPDATE}/d1",
            {"state": "ready_to_install"},
        )

    async def test_set_auto_install_carries_the_update_time(self, hue, http):
        await hue.api.device_software_updates.set_auto_install(
            "d1", on=True, update_time="03:00:00"
        )
        assert http.last == (
            "PUT",
            f"{SOFTWARE_UPDATE}/d1",
            {"auto_install": {"on": True, "update_time": "03:00:00"}},
        )

    async def test_set_auto_install_omits_the_update_time_when_absent(self, hue, http):
        await hue.api.device_software_updates.set_auto_install("d1", on=True)
        assert http.last[2] == {"auto_install": {"on": True}}


class TestBehaviorInstance:
    async def test_create_posts_the_full_body_with_metadata(self, hue, http):
        await hue.api.behavior_instances.create(
            "script-1", {"where": []}, name="Wake up"
        )
        assert http.last == (
            "POST",
            BEHAVIOR_INSTANCE,
            {
                "type": "behavior_instance",
                "script_id": "script-1",
                "enabled": True,
                "configuration": {"where": []},
                "metadata": {"name": "Wake up"},
            },
        )

    async def test_create_without_a_name_omits_metadata(self, hue, http):
        await hue.api.behavior_instances.create("script-1", {"where": []})
        assert http.last == (
            "POST",
            BEHAVIOR_INSTANCE,
            {
                "type": "behavior_instance",
                "script_id": "script-1",
                "enabled": True,
                "configuration": {"where": []},
            },
        )

    async def test_enable_sets_enabled_true(self, hue, http):
        await hue.api.behavior_instances.enable("b1")
        assert http.last == ("PUT", f"{BEHAVIOR_INSTANCE}/b1", {"enabled": True})

    async def test_disable_sets_enabled_false(self, hue, http):
        await hue.api.behavior_instances.disable("b1")
        assert http.last == ("PUT", f"{BEHAVIOR_INSTANCE}/b1", {"enabled": False})

    async def test_configure_replaces_the_configuration(self, hue, http):
        await hue.api.behavior_instances.configure("b1", {"x": 1})
        assert http.last == (
            "PUT",
            f"{BEHAVIOR_INSTANCE}/b1",
            {"configuration": {"x": 1}},
        )


class TestGeolocation:
    async def test_set_location_sends_the_coordinates(self, hue, http):
        await hue.api.geolocations.set_location("g1", 52.0, 4.9)
        assert http.last == (
            "PUT",
            f"{GEOLOCATION}/g1",
            {"latitude": 52.0, "longitude": 4.9},
        )

    @pytest.mark.parametrize(
        ("latitude", "longitude"),
        [(200.0, 4.9), (-200.0, 4.9), (52.0, 200.0), (52.0, -200.0)],
    )
    async def test_out_of_range_coordinates_raise_and_write_nothing(
        self, hue, http, latitude, longitude
    ):
        with pytest.raises(ValueError, match="must be between"):
            await hue.api.geolocations.set_location("g1", latitude, longitude)
        assert http.writes == []


class TestGeofenceClient:
    async def test_create_posts_name_and_home_state(self, hue, http):
        await hue.api.geofence_clients.create("Alex phone", is_at_home=True)
        assert http.last == (
            "POST",
            GEOFENCE_CLIENT,
            {"name": "Alex phone", "is_at_home": True},
        )


class TestHomekit:
    async def test_reset_sends_the_reset_action(self, hue, http):
        await hue.api.homekits.reset("h1")
        assert http.last == ("PUT", f"{HOMEKIT}/h1", {"action": "homekit_reset"})


class TestMatter:
    async def test_reset_sends_the_reset_action(self, hue, http):
        await hue.api.matters.reset("m1")
        assert http.last == ("PUT", f"{MATTER}/m1", {"action": "matter_reset"})


class TestCameraMotion:
    async def test_turn_on_sets_enabled_true(self, hue, http):
        await hue.api.camera_motions.turn_on("c1")
        assert http.last == ("PUT", f"{CAMERA_MOTION}/c1", {"enabled": True})

    async def test_turn_off_sets_enabled_false(self, hue, http):
        await hue.api.camera_motions.turn_off("c1")
        assert http.last == ("PUT", f"{CAMERA_MOTION}/c1", {"enabled": False})


class TestEntertainmentConfiguration:
    async def test_start_begins_streaming(self, hue, http):
        await hue.api.entertainment_configurations.start("e1")
        assert http.last == (
            "PUT",
            f"{ENTERTAINMENT_CONFIG}/e1",
            {"action": "start"},
        )

    async def test_stop_ends_streaming(self, hue, http):
        await hue.api.entertainment_configurations.stop("e1")
        assert http.last == ("PUT", f"{ENTERTAINMENT_CONFIG}/e1", {"action": "stop"})


class TestBoundDevice:
    @staticmethod
    async def _device(hue, http, device_id="d1"):
        http.queue_resource("device", device_id, {"id": device_id, "type": "device"})
        return await hue.api.devices.get(device_id)

    async def test_identify_blinks_the_device(self, hue, http):
        device = await self._device(hue, http)
        http.calls.clear()

        await device.identify()

        assert http.calls == [
            ("PUT", f"{DEVICE}/d1", {"identify": {"action": "identify"}}),
        ]

    async def test_identify_carries_a_duration_in_milliseconds(self, hue, http):
        device = await self._device(hue, http)
        http.calls.clear()

        await device.identify(duration=1.5)

        assert http.calls == [
            (
                "PUT",
                f"{DEVICE}/d1",
                {"identify": {"action": "identify", "duration": 1500}},
            ),
        ]

    async def test_usertest_toggles_user_test_mode(self, hue, http):
        device = await self._device(hue, http)
        http.calls.clear()

        await device.usertest(enabled=True)

        assert http.calls == [
            ("PUT", f"{DEVICE}/d1", {"usertest": {"usertest": True}}),
        ]


class TestBoundBridge:
    async def test_set_timezone_sends_the_nested_time_zone(self, hue, http):
        http.queue_resource("bridge", "br-1", {"id": "br-1", "type": "bridge"})
        bridge = await hue.api.bridges.get("br-1")
        http.calls.clear()

        await bridge.set_timezone("Europe/Berlin")

        assert http.calls == [
            (
                "PUT",
                f"{BRIDGE}/br-1",
                {"time_zone": {"time_zone": "Europe/Berlin"}},
            ),
        ]
