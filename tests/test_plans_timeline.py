"""Scheduling math for a plan's day curve.

Everything here is pure and clock-injected, so a whole simulated day runs in
microseconds and nothing touches a bridge. The cases that matter are the
boundaries: midnight wrap, the moment a fade starts, the moment it settles,
and the polar days where a solar anchor simply does not happen.
"""

import datetime
import zoneinfo

import pytest

from huepy.plans.fields import SunAnchor, SunEvent
from huepy.plans.schema import Action, Location, Plan
from huepy.plans.timeline import (
    combine,
    in_window,
    interpolate,
    next_transition,
    resolve_anchor,
    target_at,
    waypoints_for_day,
    zone_of,
)

BERLIN = zoneinfo.ZoneInfo("Europe/Berlin")

DAY_CURVE = {
    "version": 1,
    "location": {
        "latitude": 48.137,
        "longitude": 11.575,
        "timezone": "Europe/Berlin",
    },
    "scenario": [
        {
            "name": "lr",
            "scope": ["room:Living Room"],
            "step": [
                {
                    "at": "09:00",
                    "ramp": "1h",
                    "set": {"brightness": 100, "kelvin": 5000},
                },
                {
                    "at": "sunset",
                    "ramp": "2h",
                    "set": {"brightness": 20, "kelvin": 2200},
                },
            ],
        }
    ],
}


@pytest.fixture
def plan():
    return Plan.model_validate(DAY_CURVE)


@pytest.fixture
def scenario(plan):
    return plan.scenario[0]


def at(hour, minute=0, day=1):
    return datetime.datetime(2026, 9, day, hour, minute, tzinfo=BERLIN)


class TestTargetAt:
    def test_before_the_first_step_uses_yesterdays_last(self, plan, scenario):
        # 08:30 is still the tail of last night's sunset step. Without the
        # day either side of today this would read as "unmanaged".
        target = target_at(plan, scenario, at(8, 30), BERLIN)
        assert target is not None
        assert target.brightness == 20

    def test_at_the_anchor_the_fade_has_not_moved_yet(self, plan, scenario):
        target = target_at(plan, scenario, at(9, 0), BERLIN)
        assert target is not None
        assert target.brightness == 20

    def test_halfway_through_a_ramp_is_halfway_between_targets(self, plan, scenario):
        target = target_at(plan, scenario, at(9, 30), BERLIN)
        assert target is not None
        assert target.brightness == pytest.approx(60.0)
        # 5000K is 200 mirek, 2200K is 455; halfway is ~328.
        assert target.mirek == pytest.approx(328, abs=1)

    def test_after_the_ramp_it_is_settled(self, plan, scenario):
        target = target_at(plan, scenario, at(10, 30), BERLIN)
        assert target is not None
        assert target.brightness == 100

    def test_a_scenario_with_no_step_yet_claims_nothing(self):
        plan = Plan.model_validate(
            {
                "version": 1,
                "scenario": [
                    {
                        "name": "evening-only",
                        "scope": ["room:X"],
                        "days": ["friday"],
                        "step": [{"at": "20:00", "set": {"brightness": 10}}],
                    }
                ],
            }
        )
        # A Tuesday: the Friday-only scenario makes no claim at all, so the
        # scope is left to whatever is underneath it.
        assert target_at(plan, plan.scenario[0], at(21, 0), BERLIN) is None

    def test_a_zero_ramp_jumps(self):
        plan = Plan.model_validate(
            {
                "version": 1,
                "scenario": [
                    {
                        "name": "snap",
                        "scope": ["room:X"],
                        "step": [
                            {"at": "08:00", "set": {"brightness": 10}},
                            {"at": "09:00", "set": {"brightness": 90}},
                        ],
                    }
                ],
            }
        )
        target = target_at(plan, plan.scenario[0], at(9, 0), BERLIN)
        assert target is not None
        assert target.brightness == 90


UNTIL_PLAN = {
    "version": 1,
    "location": {"latitude": 48.137, "longitude": 11.575, "timezone": "Europe/Berlin"},
    "scenario": [
        {
            "name": "evening",
            "scope": ["room:Living Room"],
            "step": [
                {"at": "sunset+1h30m", "ramp": "30m", "set": {"brightness": 56}},
                {"at": "22:30", "until": "01:00", "set": {"brightness": 1}},
            ],
        }
    ],
}


class TestUntil:
    def waypoints(self, plan_dict, day=datetime.date(2026, 9, 1)):
        plan = Plan.model_validate(plan_dict)
        return waypoints_for_day(plan, plan.scenario[0], day, BERLIN)

    def test_until_past_midnight_lands_the_next_morning(self):
        found = self.waypoints(UNTIL_PLAN)
        dim = found[-1]
        assert dim.at == at(22, 30)
        assert dim.ends_at == at(1, 0, day=2)
        assert dim.ramp == 2.5 * 3600

    def test_until_later_the_same_day_is_the_plain_difference(self):
        plan = {
            "version": 1,
            "scenario": [
                {
                    "name": "s",
                    "scope": ["room:Living Room"],
                    "step": [
                        {"at": "09:00", "until": "09:45", "set": {"brightness": 1}}
                    ],
                }
            ],
        }
        found = self.waypoints(plan)
        assert found[0].ramp == 45 * 60

    def test_a_solar_until_is_pinned_like_a_solar_at(self):
        plan = {
            "version": 1,
            "location": UNTIL_PLAN["location"],
            "scenario": [
                {
                    "name": "s",
                    "scope": ["room:Living Room"],
                    "step": [
                        {"at": "22:00", "until": "sunrise", "set": {"brightness": 1}}
                    ],
                }
            ],
        }
        found = self.waypoints(plan)
        assert found[0].ends_at.date() == datetime.date(2026, 9, 2)
        assert datetime.time(6, 0) < found[0].ends_at.time() < datetime.time(7, 0)


class TestNextTransition:
    def test_points_at_the_next_anchor(self, plan, scenario):
        assert next_transition(plan, scenario, at(8, 0), BERLIN) == at(9, 0)

    def test_skips_past_a_running_fade(self, plan, scenario):
        # Mid-ramp there is nothing to wake for: the bridge is running the
        # fade. The next wake is the *next* step's start.
        upcoming = next_transition(plan, scenario, at(9, 30), BERLIN)
        assert upcoming is not None
        assert upcoming.hour == 19

    def test_rolls_over_to_tomorrow(self, plan, scenario):
        upcoming = next_transition(plan, scenario, at(23, 0), BERLIN)
        assert upcoming == at(9, 0, day=2)


class TestInterpolate:
    def test_without_a_start_it_jumps_to_the_target(self):
        end = Action(brightness=100)
        assert interpolate(None, end, 0.5) == end

    def test_attributes_the_start_lacks_are_taken_whole(self):
        # The fade introduces a colour temperature the previous step never
        # mentioned; there is no value to move away from.
        start = Action(brightness=0)
        end = Action(brightness=100, mirek=250)
        result = interpolate(start, end, 0.5)
        assert result.brightness == pytest.approx(50.0)
        assert result.mirek == 250

    def test_on_is_never_interpolated(self):
        result = interpolate(
            Action(on=False, brightness=0), Action(on=True, brightness=100), 0.1
        )
        assert result.on is True

    def test_xy_moves_on_both_axes(self):
        result = interpolate(Action(xy=(0.0, 0.0)), Action(xy=(0.5, 0.4)), 0.5)
        assert result.xy == pytest.approx((0.25, 0.2))

    @pytest.mark.parametrize("fraction", [-1.0, 0.0])
    def test_fractions_at_or_below_zero_stay_at_the_start(self, fraction):
        result = interpolate(Action(brightness=10), Action(brightness=90), fraction)
        assert result.brightness == pytest.approx(10.0)

    def test_fraction_beyond_one_clamps_to_the_target(self):
        result = interpolate(Action(brightness=10), Action(brightness=90), 2.0)
        assert result.brightness == 90


class TestSolarAnchors:
    def test_sunset_anchor_lands_in_local_time(self, plan, scenario):
        found = waypoints_for_day(plan, scenario, datetime.date(2026, 9, 1), BERLIN)
        sunset = found[-1]
        assert sunset.at.tzinfo is BERLIN
        assert sunset.at.hour == 19

    def test_offsets_shift_the_anchor(self):
        plan = Plan.model_validate(
            DAY_CURVE
            | {
                "scenario": [
                    {
                        "name": "lr",
                        "scope": ["room:X"],
                        "step": [
                            {"at": "sunset", "set": {"brightness": 50}},
                            {"at": "sunset+30m", "set": {"brightness": 20}},
                        ],
                    }
                ]
            }
        )
        found = waypoints_for_day(
            plan, plan.scenario[0], datetime.date(2026, 9, 1), BERLIN
        )
        assert (found[1].at - found[0].at) == datetime.timedelta(minutes=30)

    def test_a_polar_day_drops_the_step_instead_of_failing(self):
        # This is the bridge's own day_type of "polar_day". A plan that cannot
        # compute sunset that day should skip the step, not crash the runner.
        plan = Plan.model_validate(
            {
                "version": 1,
                "location": {"latitude": 69.6496, "longitude": 18.956},
                "scenario": [
                    {
                        "name": "arctic",
                        "scope": ["room:X"],
                        "step": [
                            {"at": "sunset", "set": {"brightness": 20}},
                            {"at": "12:00", "set": {"brightness": 100}},
                        ],
                    }
                ],
            }
        )
        found = waypoints_for_day(
            plan, plan.scenario[0], datetime.date(2024, 6, 21), BERLIN
        )
        assert len(found) == 1
        assert found[0].action.brightness == 100

    def test_resolving_a_sun_anchor_without_a_location_is_an_error(self):
        with pytest.raises(ValueError, match="without a location"):
            resolve_anchor(
                SunAnchor(event=SunEvent.SUNSET),
                datetime.date(2026, 9, 1),
                BERLIN,
                None,
            )


class TestWindows:
    def make_rule(self, between):
        plan = Plan.model_validate(
            {
                "version": 1,
                "location": {
                    "latitude": 48.137,
                    "longitude": 11.575,
                    "timezone": "Europe/Berlin",
                },
                "scenario": [
                    {
                        "name": "hall",
                        "scope": ["room:Hallway"],
                        "rule": [
                            {
                                "when": "motion:Hall sensor",
                                "between": between,
                                "set": {"brightness": 15},
                            }
                        ],
                    }
                ],
            }
        )
        return plan, plan.scenario[0].rule[0]

    def test_a_rule_without_a_window_is_always_open(self):
        plan = Plan.model_validate(
            {
                "version": 1,
                "scenario": [
                    {
                        "name": "hall",
                        "scope": ["room:Hallway"],
                        "rule": [
                            {"when": "motion:Hall sensor", "set": {"brightness": 15}}
                        ],
                    }
                ],
            }
        )
        assert in_window(plan.scenario[0].rule[0], plan, at(4, 0), BERLIN)

    def test_a_daytime_window(self):
        plan, rule = self.make_rule(["09:00", "17:00"])
        assert in_window(rule, plan, at(12, 0), BERLIN)
        assert not in_window(rule, plan, at(8, 0), BERLIN)
        assert not in_window(rule, plan, at(18, 0), BERLIN)

    def test_a_window_wrapping_midnight_means_at_night(self):
        # ["sunset", "sunrise"] must not read as an empty range.
        plan, rule = self.make_rule(["sunset", "sunrise"])
        assert in_window(rule, plan, at(23, 0), BERLIN)
        assert in_window(rule, plan, at(3, 0), BERLIN)
        assert not in_window(rule, plan, at(12, 0), BERLIN)

    def test_an_uncomputable_window_stays_open(self):
        # A polar day must not silently disable a motion rule forever.
        plan = Plan.model_validate(
            {
                "version": 1,
                "location": {"latitude": 69.6496, "longitude": 18.956},
                "scenario": [
                    {
                        "name": "hall",
                        "scope": ["room:Hallway"],
                        "rule": [
                            {
                                "when": "motion:Hall sensor",
                                "between": ["sunset", "sunrise"],
                                "set": {"brightness": 15},
                            }
                        ],
                    }
                ],
            }
        )
        midsummer = datetime.datetime(2024, 6, 21, 23, 0, tzinfo=BERLIN)
        assert in_window(plan.scenario[0].rule[0], plan, midsummer, BERLIN)


class TestZone:
    def test_named_zone_is_used(self):
        zone = zone_of(Location(latitude=0, longitude=0, timezone="Europe/Berlin"))
        assert zone == BERLIN

    def test_without_a_location_it_defers_to_the_host(self):
        # None, deliberately: `datetime.now().astimezone().tzinfo` is a fixed
        # offset frozen at today's DST, so a daemon started in summer would
        # fire every clock step an hour early all winter.
        assert zone_of(None) is None

    def test_a_host_local_clock_time_survives_a_dst_change(self):
        summer = combine(datetime.date(2026, 9, 1), datetime.time(7, 0), None)
        winter = combine(datetime.date(2026, 11, 1), datetime.time(7, 0), None)
        assert summer.hour == 7
        assert winter.hour == 7
        assert summer.utcoffset() != winter.utcoffset()


DST_PLAN = {
    "version": 1,
    "scenario": [
        {
            "name": "s",
            "scope": ["room:X"],
            "step": [
                {"at": "01:30", "set": {"brightness": 10}},
                {"at": "02:30", "set": {"brightness": 50}},
                {"at": "03:30", "set": {"brightness": 90}},
            ],
        }
    ],
}


class TestDaylightSaving:
    """Twice a year a wall-clock time is not a unique instant.

    A plan still has to run on those two days, so the requirement is that
    nothing raises and the day stays in order -- not that the impossible hour
    is somehow made possible.
    """

    @pytest.mark.parametrize(
        ("label", "day"),
        [
            ("spring forward", datetime.date(2026, 3, 29)),
            ("fall back", datetime.date(2026, 10, 25)),
        ],
    )
    def test_every_step_still_resolves(self, label, day):
        plan = Plan.model_validate(DST_PLAN)
        found = waypoints_for_day(plan, plan.scenario[0], day, BERLIN)
        assert len(found) == 3, label

    @pytest.mark.parametrize(
        ("label", "day"),
        [
            ("spring forward", datetime.date(2026, 3, 29)),
            ("fall back", datetime.date(2026, 10, 25)),
        ],
    )
    def test_the_day_stays_in_order(self, label, day):
        plan = Plan.model_validate(DST_PLAN)
        found = waypoints_for_day(plan, plan.scenario[0], day, BERLIN)
        times = [waypoint.at for waypoint in found]
        assert times == sorted(times), label

    def test_the_missing_hour_collapses_onto_the_next_step(self):
        # 02:30 does not exist on 29 March, so it lands on the same instant as
        # 03:30 and is immediately superseded. The room ends up at 90, which is
        # what the author asked for by that time of day.
        plan = Plan.model_validate(DST_PLAN)
        found = waypoints_for_day(
            plan, plan.scenario[0], datetime.date(2026, 3, 29), BERLIN
        )
        assert found[1].at.astimezone(datetime.UTC) == found[2].at.astimezone(
            datetime.UTC
        )

    def test_the_repeated_hour_uses_its_first_occurrence(self):
        plan = Plan.model_validate(DST_PLAN)
        found = waypoints_for_day(
            plan, plan.scenario[0], datetime.date(2026, 10, 25), BERLIN
        )
        assert found[1].at.utcoffset() == datetime.timedelta(hours=2)


RECURRENCE_PLAN = {
    "version": 1,
    "scenario": [
        {
            "name": "weekend",
            "scope": ["room:X"],
            "priority": 5,
            "days": ["saturday", "sunday"],
            "step": [{"at": "10:30", "set": {"brightness": 100}}],
        },
        {
            "name": "base",
            "scope": ["room:X"],
            "priority": 0,
            "step": [{"at": "23:30", "set": {"on": False}}],
        },
    ],
}


class TestRecurrenceExpiry:
    """A `days`-restricted scenario must fall silent on days it does not run.

    The waypoint search spans yesterday, today and tomorrow so that last
    night's step still governs this morning. Without an explicit guard that
    span also let a weekend-only scenario keep asserting every weekday -- and
    at a higher priority it masked the base curve for two days running.
    """

    def scenario_named(self, plan, name):
        return next(s for s in plan.scenario if s.name == name)

    def test_it_claims_on_a_day_it_runs(self):
        plan = Plan.model_validate(RECURRENCE_PLAN)
        saturday = datetime.datetime(2026, 9, 5, 12, 0, tzinfo=BERLIN)
        assert (
            target_at(plan, self.scenario_named(plan, "weekend"), saturday, BERLIN)
            is not None
        )

    @pytest.mark.parametrize("day", [7, 8, 9])
    def test_it_is_silent_on_days_it_does_not_run(self, day):
        # Monday, Tuesday, Wednesday: the previous Sunday must not leak.
        plan = Plan.model_validate(RECURRENCE_PLAN)
        when = datetime.datetime(2026, 9, day, 12, 0, tzinfo=BERLIN)
        assert (
            target_at(plan, self.scenario_named(plan, "weekend"), when, BERLIN) is None
        )

    def test_the_lower_priority_curve_is_not_masked_on_a_weekday(self):
        plan = Plan.model_validate(RECURRENCE_PLAN)
        monday = datetime.datetime(2026, 9, 7, 23, 45, tzinfo=BERLIN)
        base = target_at(plan, self.scenario_named(plan, "base"), monday, BERLIN)
        assert base is not None
        assert base.on is False
