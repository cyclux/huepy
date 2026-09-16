"""Recognizing what a Hue app motion automation does to a light.

Pure functions, so every case is a table of bridge JSON and host instants.
The measured values come from the bathroom's recorder history: 147 warning
dims, each exactly 50.2 points down (or to 0), 259-260 s after the sensor's
``motion=false`` report, for a rule whose ``after`` is five minutes.
"""

import datetime

import pytest

from huepy.models.automation import BehaviorInstance
from huepy.plans.automation import (
    Malformed,
    MotionRule,
    NotMotion,
    PendingWarning,
    follow_up,
    is_warning_dim,
    motion_transition,
    parse_motion_rule,
)

MOTION = "motion-bad"
DEVICE = "dev-sensor"
ROOM = "room-bad"
LIGHTS = frozenset({"light-1", "light-2"})
T0 = datetime.datetime(2026, 9, 16, 0, 0, tzinfo=datetime.UTC)

WHEN = {
    "timeslots": [
        {
            "on_motion": {"recall_single": [{"action": "last_on"}]},
            "on_no_motion": {
                "after": {"minutes": 5},
                "recall_single": [{"action": "all_off"}],
            },
            "start_time": {"time": {"hour": 23, "minute": 0}, "type": "time"},
        }
    ]
}
WHERE = [{"group": {"rid": ROOM, "rtype": "room"}}]


def instance(configuration, *, enabled=True):
    return BehaviorInstance.model_validate(
        {
            "id": "behavior-1",
            "type": "behavior_instance",
            "metadata": {"name": "Bewegungssensor Bad"},
            "enabled": enabled,
            "configuration": configuration,
        }
    )


def parse(configuration, *, enabled=True, motions_of_device=None):
    return parse_motion_rule(
        instance(configuration, enabled=enabled),
        motion_ids=frozenset({MOTION, "motion-other"}),
        motions_of_device=(
            {DEVICE: (MOTION,)} if motions_of_device is None else motions_of_device
        ),
        lights_of_group={ROOM: LIGHTS},
    )


NEW_SHAPE = {
    "motion": {
        "motion_service": {"rid": MOTION, "rtype": "motion"},
        "when": WHEN,
        "where": WHERE,
    },
    "source": {"rid": DEVICE, "rtype": "device"},
}
OLD_SHAPE = {
    "settings": {"daylight_sensitivity": {"dark_threshold": 17493, "offset": 7000}},
    "source": {"rid": DEVICE, "rtype": "device"},
    "when": WHEN,
    "where": WHERE,
}
RULE = MotionRule(
    behavior_id="behavior-1",
    name="Bewegungssensor Bad",
    motion_service_id=MOTION,
    light_ids=LIGHTS,
    afters=frozenset({300.0}),
)


class TestParse:
    def test_the_app_s_new_shape(self):
        assert parse(NEW_SHAPE) == RULE

    def test_the_old_shape(self):
        assert parse(OLD_SHAPE) == RULE

    def test_a_source_that_is_the_motion_service_itself(self):
        configuration = {**OLD_SHAPE, "source": {"rid": MOTION, "rtype": "motion"}}
        assert parse(configuration) == RULE

    def test_a_disabled_rule_is_not_a_motion_rule(self):
        assert parse(NEW_SHAPE, enabled=False) == NotMotion()

    def test_an_automation_without_a_motion_sensor_is_silent(self):
        # A dimmer's or a wake-up's automation is none of this module's business.
        configuration = {"source": {"rid": "dev-dimmer", "rtype": "device"}}
        assert parse(configuration, motions_of_device={"dev-dimmer": ()}) == NotMotion()

    def test_a_rule_that_never_switches_off_is_silent(self):
        # No off, no warning dim before it.
        when = {"timeslots": [{"on_motion": {"recall_single": []}}]}
        configuration = {**OLD_SHAPE, "when": when}
        assert parse(configuration) == NotMotion()

    def test_sources_that_disagree_are_malformed(self):
        configuration = {
            **NEW_SHAPE,
            "source": {"rid": "motion-other", "rtype": "motion"},
        }
        assert isinstance(parse(configuration), Malformed)

    def test_an_explicit_service_settles_a_device_with_two(self):
        # The newer shape names the service outright; the device's other
        # motion service is no ambiguity, as long as the named one is its own.
        result = parse(NEW_SHAPE, motions_of_device={DEVICE: (MOTION, "motion-other")})
        assert result == RULE

    def test_a_malformed_on_no_motion_warns(self):
        when = {"timeslots": [{"on_no_motion": "broken"}]}
        assert isinstance(parse({**OLD_SHAPE, "when": when}), Malformed)

    def test_an_enormous_after_is_malformed_not_a_crash(self):
        when = {"timeslots": [{"on_no_motion": {"after": {"minutes": 10**1000}}}]}
        assert isinstance(parse({**OLD_SHAPE, "when": when}), Malformed)

    def test_a_device_with_two_motion_services_is_malformed(self):
        result = parse(OLD_SHAPE, motions_of_device={DEVICE: (MOTION, "motion-other")})
        assert isinstance(result, Malformed)

    @pytest.mark.parametrize(
        "after",
        [{}, {"minutes": 0}, {"minutes": -1}, {"minutes": True}, "5m", None],
    )
    def test_a_bad_after_is_malformed(self, after):
        when = {
            "timeslots": [
                {
                    "on_no_motion": {
                        "after": after,
                        "recall_single": [{"action": "all_off"}],
                    }
                }
            ]
        }
        assert isinstance(parse({**OLD_SHAPE, "when": when}), Malformed)

    def test_seconds_and_minutes_add_up(self):
        when = {
            "timeslots": [{"on_no_motion": {"after": {"minutes": 1, "seconds": 30}}}]
        }
        result = parse({**OLD_SHAPE, "when": when})
        assert isinstance(result, MotionRule)
        assert result.afters == frozenset({90.0})

    def test_every_timeslot_s_after_is_kept(self):
        slots = [
            {"on_no_motion": {"after": {"minutes": 5}}},
            {"on_no_motion": {"after": {"minutes": 1}}},
        ]
        result = parse({**OLD_SHAPE, "when": {"timeslots": slots}})
        assert isinstance(result, MotionRule)
        assert result.afters == frozenset({300.0, 60.0})

    @pytest.mark.parametrize(
        "where",
        [
            [],
            [{"group": {"rid": "room-unknown", "rtype": "room"}}],
            [{"group": {"rid": ROOM, "rtype": "bridge_home"}}],
            "room",
        ],
    )
    def test_a_where_that_names_no_lights_is_malformed(self, where):
        assert isinstance(parse({**OLD_SHAPE, "where": where}), Malformed)


def dim(brightness):
    return {"dimming": {"brightness": brightness}}


def warning(
    delta,
    *,
    before: float | None = 100.0,
    before_on=True,
    since=260.0,
    no_motion=True,
):
    return is_warning_dim(
        delta=delta,
        before_on=before_on,
        before_brightness=before,
        no_motion_at=T0 if no_motion else None,
        received_at=T0 + datetime.timedelta(seconds=since),
        afters=frozenset({300.0}),
    )


class TestWarningDim:
    @pytest.mark.parametrize(
        ("before", "after"),
        [(100.0, 49.8), (96.84, 46.64), (64.43, 14.23), (30.83, 0.0), (20.16, 0.0)],
    )
    def test_the_measured_dims(self, before, after):
        assert warning(dim(after), before=before)

    @pytest.mark.parametrize("since", [250.0, 260.0, 270.0])
    def test_inside_the_window(self, since):
        assert warning(dim(49.8), since=since)

    @pytest.mark.parametrize("since", [0.0, 249.0, 271.0, 300.0])
    def test_outside_the_window_is_a_hand(self, since):
        assert not warning(dim(49.8), since=since)

    def test_the_wrong_amount_is_a_hand(self):
        assert not warning(dim(40.0))

    def test_no_no_motion_report_is_a_hand(self):
        assert not warning(dim(49.8), no_motion=False)

    def test_an_unknown_level_before_is_a_hand(self):
        assert not warning(dim(49.8), before=None)

    def test_a_dark_light_is_not_warned(self):
        assert not warning(dim(49.8), before_on=False)

    @pytest.mark.parametrize(
        "delta",
        [
            {"on": {"on": True}, "dimming": {"brightness": 49.8}},
            {"dimming": {"brightness": 49.8}, "color_temperature": {"mirek": 447}},
            {"dimming": {"brightness": 49.8, "unexpected": 1}},
            {"dimming": {"brightness": True}},
            {"dimming": {"brightness": float("nan")}},
            {"dimming": {"brightness": 10**1000}},
            {"dimming": 49.8},
            {},
        ],
    )
    def test_anything_but_a_bare_level_is_a_hand(self, delta):
        assert not warning(delta)


def pending():
    return PendingWarning(
        light_id="light-1",
        behavior_id="behavior-1",
        rule_name="Bewegungssensor Bad",
        pre=96.84,
        dimmed=46.64,
        observed_at=T0,
        due=T0 + datetime.timedelta(seconds=35),
    )


class TestFollowUp:
    def test_an_off(self):
        assert follow_up(pending(), delta={"on": {"on": False}}) == "off"

    def test_the_level_coming_back(self):
        assert follow_up(pending(), delta=dim(96.5)) == "restored"

    def test_a_different_level(self):
        assert follow_up(pending(), delta=dim(70.0)) == "other"

    def test_a_switch_on_with_the_level_is_not_a_restore(self):
        delta = {"on": {"on": True}, "dimming": {"brightness": 96.84}}
        assert follow_up(pending(), delta=delta) == "other"


class TestMotionTransition:
    def test_reads_only_the_delta(self):
        assert motion_transition({"motion": {"motion": False}}) is False
        assert motion_transition({"motion": {"motion": True}}) is True

    @pytest.mark.parametrize(
        "delta",
        [{"enabled": True}, {"motion": {"motion_valid": True}}, {"motion": False}],
    )
    def test_anything_else_is_no_transition(self, delta):
        assert motion_transition(delta) is None
