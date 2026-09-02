"""The plan file's scalar grammar.

These are the strings a human types, so every rejection here is a message
someone reads at 2am. The tests pin both halves: what parses, and what fails
loudly rather than quietly meaning something else.
"""

import datetime

import pytest

from huepy.models import LightLevel, parse_resource
from huepy.plans.fields import (
    LIGHT_LEVEL_DEADBAND,
    LIGHT_LEVEL_OFFSET,
    ClockAnchor,
    ScopeKind,
    Selector,
    SunAnchor,
    SunEvent,
    TriggerKind,
    format_duration,
    lux_of_light_level,
    parse_anchor,
    parse_duration,
    parse_selector,
    parse_trigger,
    raw_light_level,
)


class TestParseDuration:
    @pytest.mark.parametrize(
        ("text", "seconds"),
        [
            ("90s", 90.0),
            ("45m", 2700.0),
            ("2h", 7200.0),
            ("1h15m", 4500.0),
            ("1h15m30s", 4530.0),
            ("500ms", 0.5),
            ("2h30m15s250ms", 9015.25),
            ("0s", 0.0),
            ("1.5s", 1.5),
        ],
    )
    def test_accepts_the_documented_spellings(self, text, seconds):
        assert parse_duration(text) == pytest.approx(seconds)

    def test_bare_number_is_seconds(self):
        assert parse_duration(30) == 30.0
        assert parse_duration(1.5) == 1.5

    def test_whitespace_is_ignored(self):
        assert parse_duration(" 1h 15m ") == 4500.0

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "   ",
            "45",  # bare unit-less string is not a number
            "45x",
            "m45",
            "1h15",
            "1h15mx",
            "abc",
        ],
    )
    def test_rejects_junk(self, text):
        with pytest.raises(ValueError, match="duration"):
            parse_duration(text)

    def test_rejects_repeated_units(self):
        with pytest.raises(ValueError, match="given twice"):
            parse_duration("1h2h")

    def test_rejects_units_out_of_order(self):
        # "15m1h" would otherwise silently parse as 1h15m, which is not what
        # the author wrote.
        with pytest.raises(ValueError, match="largest-first"):
            parse_duration("15m1h")

    def test_rejects_negative_numbers(self):
        with pytest.raises(ValueError, match="negative"):
            parse_duration(-5)

    def test_rejects_booleans(self):
        # bool is an int subclass; True must not silently become 1 second.
        with pytest.raises(TypeError):
            parse_duration(True)

    @pytest.mark.parametrize("seconds", [0.0, 1.0, 90.0, 2700.0, 4500.0, 9015.25])
    def test_format_round_trips(self, seconds):
        assert parse_duration(format_duration(seconds)) == pytest.approx(seconds)

    def test_format_of_zero(self):
        assert format_duration(0) == "0s"


class TestParseAnchor:
    def test_clock_time(self):
        assert parse_anchor("07:30") == ClockAnchor(at=datetime.time(7, 30))

    def test_clock_time_with_seconds(self):
        assert parse_anchor("07:30:15") == ClockAnchor(at=datetime.time(7, 30, 15))

    def test_native_toml_time_scalar(self):
        # TOML parses `at = 07:30:00` into a datetime.time before we see it.
        assert parse_anchor(datetime.time(7, 30)) == ClockAnchor(
            at=datetime.time(7, 30)
        )

    @pytest.mark.parametrize("event", list(SunEvent))
    def test_bare_solar_events(self, event):
        assert parse_anchor(str(event)) == SunAnchor(event=event, offset=0.0)

    def test_positive_offset(self):
        assert parse_anchor("sunset+30m") == SunAnchor(
            event=SunEvent.SUNSET, offset=1800.0
        )

    def test_negative_offset(self):
        assert parse_anchor("sunrise-1h15m") == SunAnchor(
            event=SunEvent.SUNRISE, offset=-4500.0
        )

    def test_is_case_insensitive(self):
        assert parse_anchor("Sunset+30M") == SunAnchor(
            event=SunEvent.SUNSET, offset=1800.0
        )

    @pytest.mark.parametrize("text", ["25:00", "07:60", "99:99"])
    def test_rejects_impossible_clock_times(self, text):
        with pytest.raises(ValueError, match="not a valid time"):
            parse_anchor(text)

    def test_rejects_unknown_solar_event(self):
        with pytest.raises(ValueError, match="unknown solar event"):
            parse_anchor("moonrise+10m")

    @pytest.mark.parametrize("text", ["", "  ", "7.30", "sunset+"])
    def test_rejects_junk(self, text):
        with pytest.raises(ValueError, match=r"'at'|duration"):
            parse_anchor(text)

    @pytest.mark.parametrize(
        "text", ["07:30", "sunrise", "sunset+30m", "sunrise-1h15m"]
    )
    def test_str_round_trips(self, text):
        assert str(parse_anchor(text)) == text


class TestLightLevelUnits:
    def test_raw_round_trips_through_the_model(self):
        level = parse_resource(
            {
                "id": "level-1",
                "type": "light_level",
                "light": {
                    "light_level": round(raw_light_level(30)),
                    "light_level_valid": True,
                },
            }
        )
        assert isinstance(level, LightLevel)
        assert level.lux == pytest.approx(30, rel=0.001)

    def test_raw_of_one_lux_is_the_offset(self):
        assert raw_light_level(1) == LIGHT_LEVEL_OFFSET

    def test_lux_is_the_inverse(self):
        assert lux_of_light_level(raw_light_level(123.4)) == pytest.approx(123.4)

    @pytest.mark.parametrize("lux", [0, -1])
    def test_rejects_non_positive_lux(self, lux):
        with pytest.raises(ValueError, match="above 0 lux"):
            _ = raw_light_level(lux)

    def test_the_deadband_is_about_five_times_in_lux(self):
        release = lux_of_light_level(raw_light_level(30) + LIGHT_LEVEL_DEADBAND)
        assert release == pytest.approx(150.4, abs=0.5)


class TestParseSelector:
    def test_scope(self):
        assert parse_selector("room:Living Room") == Selector(
            kind="room", name="Living Room"
        )

    @pytest.mark.parametrize("kind", list(ScopeKind))
    def test_every_scope_kind_is_accepted(self, kind):
        assert parse_selector(f"{kind}:Somewhere").kind == str(kind)

    @pytest.mark.parametrize("kind", list(TriggerKind))
    def test_every_trigger_kind_is_accepted(self, kind):
        assert parse_trigger(f"{kind}:Something").kind == str(kind)

    def test_name_may_contain_colons_and_spaces(self):
        assert parse_selector("light:Desk: the good one") == Selector(
            kind="light", name="Desk: the good one"
        )

    def test_surrounding_whitespace_is_trimmed(self):
        assert parse_selector("  room : x ") == Selector(kind="room", name="x")

    def test_trigger_kinds_are_not_scope_kinds(self):
        # A scope is written to, a trigger is listened to. Mixing them would
        # mean trying to dim a motion sensor.
        with pytest.raises(ValueError, match="unknown scope kind"):
            parse_selector("motion:Hall sensor")
        with pytest.raises(ValueError, match="unknown trigger kind"):
            parse_trigger("room:Living Room")

    @pytest.mark.parametrize("text", ["Living Room", "", ":", "room:"])
    def test_rejects_junk(self, text):
        with pytest.raises(ValueError, match="scope"):
            parse_selector(text)

    def test_str_round_trips(self):
        assert str(parse_selector("zone:Downstairs")) == "zone:Downstairs"
