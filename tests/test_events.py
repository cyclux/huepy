"""Tests for the event-stream models."""

from datetime import UTC, datetime

import pytest

from huepy import models
from huepy.models.event import EventResource, EventType, HueEvent, parse_events

BRIDGE_PAYLOAD = [
    {
        "creationtime": "2026-08-22T10:00:00Z",
        "data": [
            {
                "id": "abc-123",
                "id_v1": "/lights/4",
                "owner": {"rid": "dev-1", "rtype": "device"},
                "on": {"on": True},
                "dimming": {"brightness": 42.0},
                "type": "light",
            },
        ],
        "id": "evt-1",
        "type": "update",
    },
]


class TestParseEvents:
    def test_parses_a_realistic_bridge_array(self):
        events = parse_events(BRIDGE_PAYLOAD)
        assert len(events) == 1
        event = events[0]
        assert isinstance(event, HueEvent)
        assert event.id == "evt-1"
        assert event.type == "update"
        assert event.creationtime == datetime(2026, 8, 22, 10, tzinfo=UTC)

        resource = event.data[0]
        assert isinstance(resource, EventResource)
        assert resource.id == "abc-123"
        assert resource.type == "light"
        assert resource.id_v1 == "/lights/4"
        assert resource.owner is not None
        assert resource.owner.rid == "dev-1"
        assert resource.on is not None
        assert resource.on.on is True
        assert resource.dimming is not None
        assert resource.dimming.brightness == 42.0

    def test_a_bare_object_parses_to_one_event(self):
        events = parse_events(BRIDGE_PAYLOAD[0])
        assert len(events) == 1
        assert events[0].id == "evt-1"

    def test_empty_array_yields_no_events(self):
        assert parse_events([]) == []

    @pytest.mark.parametrize("payload", ["update", 42, None, True])
    def test_non_object_non_array_payload_yields_nothing(self, payload):
        assert parse_events(payload) == []

    def test_garbage_entries_are_skipped_and_siblings_still_parse(self):
        """One malformed entry must not cost the caller the events around it."""
        events = parse_events(
            [
                "not an event",
                {"id": "evt-1", "type": "update"},
                17,
                None,
                {"id": "evt-2", "type": "delete"},
            ]
        )
        assert [event.id for event in events] == ["evt-1", "evt-2"]

    def test_absent_sections_default_rather_than_raise(self):
        event = parse_events([{}])[0]
        assert event.id == ""
        assert event.type == ""
        assert event.creationtime is None
        assert event.data == []


class TestTolerance:
    """The stream has to survive shapes this library has never seen."""

    def test_unknown_event_fields_are_kept_not_rejected(self):
        event = parse_events([{"id": "evt-1", "type": "update", "future": {"a": 1}}])[0]
        assert event.model_extra is not None
        assert event.model_extra["future"] == {"a": 1}

    def test_unknown_resource_fields_are_kept_not_rejected(self):
        event = parse_events(
            [
                {
                    "id": "evt-1",
                    "type": "update",
                    "data": [{"id": "r-1", "sparkle": 9}],
                },
            ]
        )[0]
        resource = event.data[0]
        assert resource.model_extra is not None
        assert resource.model_extra["sparkle"] == 9

    def test_unrecognised_type_parses_and_has_no_enum_member(self):
        event = parse_events([{"id": "evt-1", "type": "teleport"}])[0]
        assert event.type == "teleport"
        assert event.event_type is None
        assert event.is_update is False
        assert event.is_delete is False


class TestEventType:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("update", EventType.UPDATE),
            ("add", EventType.ADD),
            ("delete", EventType.DELETE),
            ("error", EventType.ERROR),
        ],
    )
    def test_each_recognised_type_maps_to_its_member(self, raw, expected):
        event = HueEvent.model_validate({"id": "evt-1", "type": raw})
        assert event.event_type is expected


class TestConveniences:
    def test_resource_ids_lists_every_carried_resource(self):
        event = HueEvent.model_validate(
            {
                "id": "evt-1",
                "type": "update",
                "data": [{"id": "r-1"}, {"id": "r-2"}],
            }
        )
        assert event.resource_ids == ["r-1", "r-2"]

    def test_resource_ids_of_an_empty_event_is_empty(self):
        assert HueEvent.model_validate({"id": "evt-1"}).resource_ids == []

    def test_is_update_and_is_delete_are_mutually_exclusive(self):
        update = HueEvent.model_validate({"type": "update"})
        delete = HueEvent.model_validate({"type": "delete"})
        add = HueEvent.model_validate({"type": "add"})

        assert (update.is_update, update.is_delete) == (True, False)
        assert (delete.is_update, delete.is_delete) == (False, True)
        assert (add.is_update, add.is_delete) == (False, False)


class TestTypedDeltas:
    def test_grouped_light_empty_color_section_is_valid(self):
        resource = EventResource.model_validate(
            {"id": "group-1", "type": "grouped_light", "color": {}}
        )

        assert isinstance(resource.color, models.GroupedColor)
        assert resource.color.xy is None

    def test_sensor_and_input_sections_parse_to_existing_reading_models(self):
        resource = EventResource.model_validate(
            {
                "id": "sensor-1",
                "motion": {
                    "motion": True,
                    "motion_report": {
                        "changed": "2026-08-24T14:16:45.498Z",
                        "motion": True,
                    },
                },
                "temperature": {
                    "temperature_report": {
                        "changed": "2026-08-24T14:16:45.499Z",
                        "temperature": 21.5,
                    },
                },
                "light": {
                    "light_level_report": {
                        "changed": "2026-08-24T14:16:45.500Z",
                        "light_level": 3578,
                    },
                },
                "button": {
                    "button_report": {
                        "updated": "2026-08-24T14:16:45.501Z",
                        "event": "initial_press",
                    },
                },
                "contact_report": {
                    "changed": "2026-08-24T14:16:45.502Z",
                    "state": "contact",
                },
                "power_state": {"battery_level": 87},
                "relative_rotary": {
                    "rotary_report": {
                        "updated": "2026-08-24T14:16:45.503Z",
                        "action": "start",
                        "rotation": {
                            "direction": "clock_wise",
                            "steps": 25,
                            "duration": 40,
                        },
                    },
                },
            }
        )

        assert isinstance(resource.motion, models.MotionReading)
        assert isinstance(resource.temperature, models.TemperatureReading)
        assert isinstance(resource.light, models.LightLevelReading)
        assert isinstance(resource.button, models.ButtonReading)
        assert isinstance(resource.contact_report, models.ContactReport)
        assert isinstance(resource.power_state, models.PowerState)
        assert isinstance(resource.relative_rotary, models.RelativeRotaryReading)
        assert resource.relative_rotary.value is not None
        assert resource.relative_rotary.value.rotation.steps == 25
