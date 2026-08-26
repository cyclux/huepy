"""Tests for the shared state summariser.

The summariser is the one piece both the event stream and the state layer hand
to callers verbatim, so it is pinned against the sections the bridge actually
sends -- including the payloads captured from a real bridge.
"""

import json
from pathlib import Path
from typing import Any, cast

import pytest

from huepy import summarize
from huepy.models.event import HueEvent
from huepy.state.records import Change, ChangeKind

FIXTURES = Path(__file__).parent / "fixtures"


class TestSections:
    @pytest.mark.parametrize(
        ("payload", "expected"),
        [
            ({"on": {"on": True}}, "on"),
            ({"on": {"on": False}}, "off"),
            ({"dimming": {"brightness": 62.4}}, "62%"),
            ({"color_temperature": {"mirek": 370}}, "2703 K"),
            ({"color": {"xy": {"x": 0.5, "y": 0.4}}}, "#ffb45f"),
            ({"effects": {"status": "candle"}}, "effect candle"),
            ({"motion": {"motion": True}}, "motion"),
            ({"motion": {"motion": False}}, "clear"),
            ({"temperature": {"temperature": 22.44}}, "22.4 \N{DEGREE SIGN}C"),
            ({"light": {"light_level": 12034}}, "light level 12034"),
            ({"button": {"last_event": "initial_press"}}, "initial_press"),
            ({"contact_report": {"state": "no_contact"}}, "no_contact"),
            ({"power_state": {"battery_level": 87}}, "battery 87%"),
            ({"status": {"active": "static"}}, "scene static"),
            ({"status": "connected"}, "connected"),
            ({"metadata": {"name": "Desk"}}, "named 'Desk'"),
        ],
    )
    def test_each_section_renders(self, payload, expected):
        assert summarize(payload) == expected

    def test_sections_are_joined_in_a_stable_order(self):
        """Read left to right the way a person describes a light."""
        payload = {
            "color_temperature": {"mirek": 370},
            "dimming": {"brightness": 62.0},
            "on": {"on": True},
        }
        assert summarize(payload) == "on, 62%, 2703 K"

    def test_an_effect_running_none_says_nothing(self):
        assert summarize({"effects": {"status": "no_effect"}}) == ""

    def test_a_rotary_report_wins_over_the_legacy_event(self):
        payload = {
            "relative_rotary": {
                "rotary_report": {"rotation": {"direction": "clock_wise", "steps": 3}},
                "last_event": {
                    "rotation": {"direction": "counter_clock_wise", "steps": 9}
                },
            }
        }
        assert summarize(payload) == "clock_wise 3 steps"

    @pytest.mark.parametrize("section", ["motion", "temperature", "light"])
    def test_a_report_wins_over_the_flat_field_beside_it(self, section):
        """The report is the fresher of the two, and some firmware sends only it."""
        readings: dict[str, tuple[Any, Any, str]] = {
            "motion": (False, True, "motion"),
            "temperature": (1.0, 22.44, "22.4 \N{DEGREE SIGN}C"),
            "light": (1, 12034, "light level 12034"),
        }
        key = "light_level" if section == "light" else section
        flat, reported, expected = readings[section]
        payload = {section: {key: flat, f"{key}_report": {key: reported}}}
        assert summarize(payload) == expected


class TestTolerance:
    @pytest.mark.parametrize(
        "payload",
        [
            {},
            {"unmodelled_future_section": {"whatever": 1}},
            {"on": "not a section"},
            {"dimming": {"brightness": None}},
            {"color": {"xy": None}},
            {"button": {}},
            {"relative_rotary": {"rotary_report": {}}},
        ],
    )
    def test_nothing_recognisable_summarises_to_empty(self, payload):
        """A summary is a convenience; it must never be the thing that raises."""
        assert summarize(payload) == ""

    def test_a_null_mirek_is_not_a_colour_temperature(self):
        """Null mirek is what a light in colour mode reports, not 0 K."""
        payload = {"color_temperature": {"mirek": None, "mirek_valid": False}}
        assert summarize(payload) == ""

    def test_on_is_never_read_as_a_number(self):
        """``bool`` subclasses ``int``: an unguarded numeric read would say '1%'."""
        assert summarize({"dimming": {"brightness": True}}) == ""


class TestEventResource:
    def test_summary_reads_the_typed_sections(self):
        event = HueEvent.model_validate(
            {
                "id": "e-1",
                "type": "update",
                "data": [
                    {
                        "id": "light-1",
                        "type": "light",
                        "on": {"on": True},
                        "dimming": {"brightness": 20.16},
                    }
                ],
            }
        )
        assert event.data[0].summary == "on, 20%"

    def test_summary_reaches_a_section_with_no_model(self):
        """An unmodelled section lands on model_extra, and must still render."""
        event = HueEvent.model_validate(
            {
                "id": "e-1",
                "type": "update",
                "data": [
                    {"id": "s-1", "type": "scene", "status": {"active": "static"}}
                ],
            }
        )
        assert "status" in (event.data[0].model_extra or {})
        assert event.data[0].summary == "scene static"

    def test_real_bridge_frames_all_summarise(self):
        """Every captured frame renders something, and nothing raises."""
        frames = cast(
            "list[dict[str, Any]]",
            json.loads((FIXTURES / "event_frames.json").read_text(encoding="utf-8")),
        )
        summaries = [
            resource.summary
            for frame in frames
            for raw in frame["events"]
            for resource in HueEvent.model_validate(raw).data
        ]
        assert summaries
        assert all(isinstance(summary, str) for summary in summaries)
        assert "20%" in summaries


class TestChange:
    def test_summary_renders_the_delta_not_the_whole_resource(self):
        change = Change.model_validate(
            {
                "kind": ChangeKind.UPDATE,
                "received_at": "2026-01-01T00:00:00Z",
                "resource_id": "light-1",
                "resource_type": "light",
                "before": None,
                "after": None,
                "delta": {"dimming": {"brightness": 70.0}},
            }
        )
        assert change.summary == "70%"

    def test_an_empty_delta_summarises_to_empty(self):
        change = Change.model_validate(
            {
                "kind": ChangeKind.DELETE,
                "received_at": "2026-01-01T00:00:00Z",
                "resource_id": "light-1",
                "resource_type": "light",
                "before": None,
                "after": None,
                "delta": {},
            }
        )
        assert change.summary == ""
