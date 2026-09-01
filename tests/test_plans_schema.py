"""The plan file format.

A plan is hand-written, so the tests that matter most here are the rejections:
what the format refuses to accept quietly. Anything accepted silently is
something a user will debug at 2am.
"""

import datetime

import pytest
from pydantic import ValidationError

from huepy.models.group import WeekDay
from huepy.plans.fields import SunAnchor, SunEvent
from huepy.plans.schema import Action, Plan, Rule, Scenario, Step


def scenario(**overrides):
    base = {
        "name": "base",
        "scope": ["room:Living Room"],
        "step": [{"at": "09:00", "set": {"brightness": 100}}],
    }
    return base | overrides


class TestAction:
    def test_mirrors_build_light_payload_vocabulary(self):
        action = Action.model_validate({"on": True, "brightness": 40, "kelvin": 2700})
        payload = action.to_payload(transition=1.5)
        assert payload["on"] == {"on": True}
        assert payload["dimming"] == {"brightness": 40}
        assert payload["color_temperature"]["mirek"] == 370
        assert payload["dynamics"] == {"duration": 1500}

    def test_rejects_an_empty_set_block(self):
        with pytest.raises(ValidationError, match="must change something"):
            Action.model_validate({})

    def test_inherits_the_bridge_colour_rules(self):
        # Not restated here: build_light_payload owns the rule, and the format
        # inherits it, so the conflict is caught when the file loads.
        with pytest.raises(ValidationError, match="not both"):
            Action.model_validate({"rgb": [255, 0, 0], "kelvin": 2700})

    def test_rejects_two_spellings_of_one_colour(self):
        with pytest.raises(ValidationError):
            Action.model_validate({"rgb": [255, 0, 0], "hex_color": "#ff0000"})

    @pytest.mark.parametrize("brightness", [-1, 101])
    def test_rejects_out_of_range_brightness(self, brightness):
        with pytest.raises(ValidationError):
            Action.model_validate({"brightness": brightness})

    def test_a_two_hour_ramp_cannot_be_one_payload(self):
        # This is why the executor chains segments: the bridge's ceiling is
        # 6000 seconds in a single PUT.
        action = Action.model_validate({"brightness": 60})
        with pytest.raises(ValueError, match="6000 seconds"):
            action.to_payload(transition=7200)


class TestUnknownKeys:
    def test_a_typo_is_rejected_rather_than_ignored(self):
        # extra="forbid", unlike HueModel. An unknown key in a bridge payload
        # is new firmware; an unknown key here is a typo.
        with pytest.raises(ValidationError, match="brightnes"):
            Action.model_validate({"brightnes": 40})

    def test_a_typo_at_scenario_level_is_rejected(self):
        with pytest.raises(ValidationError, match="prioritty"):
            Scenario.model_validate(scenario(prioritty=5))

    def test_a_typo_at_plan_level_is_rejected(self):
        with pytest.raises(ValidationError, match="scenarios"):
            Plan.model_validate({"version": 1, "scenarios": []})


class TestScenario:
    def test_rejects_a_scenario_that_does_nothing(self):
        with pytest.raises(ValidationError, match="does nothing"):
            Scenario.model_validate({"name": "empty", "scope": ["room:X"], "step": []})

    def test_rejects_release_without_activate(self):
        with pytest.raises(
            ValidationError, match=r"never be activated|no 'activate_on'"
        ):
            Scenario.model_validate(
                scenario(name="m", release_on="signal:done", step=[])
                | {"set": {"brightness": 10}}
            )

    def test_rejects_two_steps_at_the_same_anchor(self):
        # One of them could never be reached.
        with pytest.raises(ValidationError, match="two steps at"):
            Scenario.model_validate(
                scenario(
                    step=[
                        {"at": "09:00", "set": {"brightness": 10}},
                        {"at": "09:00", "set": {"brightness": 90}},
                    ]
                )
            )

    def test_distinct_sun_anchors_are_not_duplicates(self):
        parsed = Scenario.model_validate(
            scenario(
                step=[
                    {"at": "sunset", "set": {"brightness": 60}},
                    {"at": "sunset+30m", "set": {"brightness": 40}},
                ]
            )
        )
        assert len(parsed.step) == 2

    def test_requires_at_least_one_scope(self):
        with pytest.raises(ValidationError):
            Scenario.model_validate(scenario(scope=[]))

    def test_is_mode_tracks_activate_on(self):
        assert not Scenario.model_validate(scenario()).is_mode
        assert Scenario.model_validate(
            scenario(activate_on="signal:movie_started")
        ).is_mode

    def test_uses_sun_sees_step_anchors(self):
        assert not Scenario.model_validate(scenario()).uses_sun()
        assert Scenario.model_validate(
            scenario(step=[{"at": "sunrise-15m", "set": {"brightness": 40}}])
        ).uses_sun()

    def test_uses_sun_sees_rule_windows(self):
        # A `between` window is a sun consumer too, and forgetting it would
        # let a plan load and then fail at the first motion event.
        parsed = Scenario.model_validate(
            scenario(
                step=[],
                rule=[
                    {
                        "when": "motion:Hall sensor",
                        "between": ["sunset", "sunrise"],
                        "set": {"brightness": 15},
                    }
                ],
            )
        )
        assert parsed.uses_sun()


class TestPlan:
    def test_rejects_duplicate_scenario_names(self):
        with pytest.raises(ValidationError, match="two scenarios are named"):
            Plan.model_validate({"version": 1, "scenario": [scenario(), scenario()]})

    def test_sun_anchor_without_location_is_rejected(self):
        with pytest.raises(ValidationError, match=r"\[location\]"):
            Plan.model_validate(
                {
                    "version": 1,
                    "scenario": [
                        scenario(step=[{"at": "sunset", "set": {"brightness": 60}}])
                    ],
                }
            )

    def test_clock_only_plan_needs_no_location(self):
        plan = Plan.model_validate({"version": 1, "scenario": [scenario()]})
        assert plan.location is None

    def test_rejects_an_unknown_format_version(self):
        with pytest.raises(ValidationError):
            Plan.model_validate({"version": 2, "scenario": []})

    @pytest.mark.parametrize(("latitude", "longitude"), [(91, 0), (0, 181), (-91, 0)])
    def test_rejects_impossible_coordinates(self, latitude, longitude):
        with pytest.raises(ValidationError):
            Plan.model_validate(
                {
                    "version": 1,
                    "location": {"latitude": latitude, "longitude": longitude},
                }
            )

    def test_defaults_are_applied(self):
        plan = Plan.model_validate({"version": 1})
        assert plan.defaults.on_manual_change == "yield"
        assert plan.defaults.catchup_ramp == 5.0
        assert plan.defaults.ramp == 0.0


class TestRecurrence:
    def test_scenarios_for_day_filters_by_weekday(self):
        plan = Plan.model_validate(
            {
                "version": 1,
                "scenario": [
                    scenario(name="weekdays", days=["monday", "tuesday"]),
                    scenario(name="always"),
                ],
            }
        )
        monday = datetime.date(2026, 8, 31)
        wednesday = datetime.date(2026, 9, 2)
        assert [s.name for s in plan.scenarios_for_day(monday)] == [
            "weekdays",
            "always",
        ]
        assert [s.name for s in plan.scenarios_for_day(wednesday)] == ["always"]

    def test_weekday_mapping_matches_python(self):
        # date.weekday() is Monday=0, and WeekDay is declared in that order.
        for offset, day in enumerate(WeekDay):
            date = datetime.date(2026, 8, 31) + datetime.timedelta(days=offset)
            plan = Plan.model_validate(
                {"version": 1, "scenario": [scenario(days=[str(day)])]}
            )
            assert plan.scenarios_for_day(date), f"{day} should match {date}"

    def test_disabled_scenarios_are_dropped(self):
        plan = Plan.model_validate(
            {"version": 1, "scenario": [scenario(enabled=False)]}
        )
        assert plan.scenarios_for_day(datetime.date(2026, 9, 1)) == []


class TestStepSemantics:
    def test_ramp_may_be_omitted_and_falls_back_later(self):
        step = Step.model_validate({"at": "09:00", "set": {"brightness": 100}})
        assert step.ramp is None

    def test_sun_anchor_offsets_survive_validation(self):
        step = Step.model_validate({"at": "sunrise-15m", "set": {"brightness": 40}})
        assert isinstance(step.at, SunAnchor)
        assert step.at.event is SunEvent.SUNRISE
        assert step.at.offset == -900.0


class TestRuleHold:
    def test_a_zero_hold_is_rejected(self):
        # `until == now` would be cleared on the next tick before the rule
        # was ever claimed: a hold that does nothing is a typo.
        with pytest.raises(ValidationError, match="hold"):
            _ = Rule.model_validate(
                {"when": "motion:Hall sensor", "hold": "0s", "set": {"brightness": 1}}
            )
