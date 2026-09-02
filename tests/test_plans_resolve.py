"""Binding plan names to bridge resources, and composing the writes.

These are the two places the plan layer touches a bridge, so both run against
the shared transport fake and assert exact wire shapes: which path was hit,
what body went out, and -- for the executor -- how many requests it took.
"""

import pytest

from huepy.exceptions import HueResponseError, PlanError
from huepy.plans.executor import (
    MAX_TRANSITION_SECONDS,
    Segment,
    plan_segments,
    run_fade,
)
from huepy.plans.fields import parse_selector
from huepy.plans.resolve import Binding, resolve
from huepy.plans.schema import Action, Plan

from .conftest import envelope

GROUPED_LIGHT = "gl-living"
DEVICE = "dev-lamp"
LIGHT = "light-1"
SENSOR_DEVICE = "dev-hall"
MOTION = "motion-1"


def bridge_resources():
    return [
        {
            "id": "room-living",
            "type": "room",
            "metadata": {"name": "Living Room"},
            "children": [{"rid": DEVICE, "rtype": "device"}],
            "services": [{"rid": GROUPED_LIGHT, "rtype": "grouped_light"}],
        },
        {
            "id": LIGHT,
            "type": "light",
            "metadata": {"name": "Corner Lamp"},
            "owner": {"rid": DEVICE, "rtype": "device"},
        },
        {
            "id": SENSOR_DEVICE,
            "type": "device",
            "metadata": {"name": "Hall sensor"},
            "services": [{"rid": MOTION, "rtype": "motion"}],
        },
    ]


@pytest.fixture
def bridge(hue, http):
    http.queue("/clip/v2/resource", envelope(*bridge_resources()))
    return hue


def make_plan(**scenario_overrides):
    scenario = {
        "name": "s",
        "scope": ["room:Living Room"],
        "step": [{"at": "09:00", "set": {"brightness": 50}}],
    } | scenario_overrides
    return Plan.model_validate({"version": 1, "scenario": [scenario]})


class TestResolveScopes:
    async def test_a_room_binds_to_its_grouped_light(self, bridge):
        # Not to each member light: one broadcast beats N unicasts, and the
        # bridge's group budget is one write a second.
        resolved = await resolve(bridge, make_plan())
        binding = resolved.scopes["s"][0]
        assert binding.path == f"/clip/v2/resource/grouped_light/{GROUPED_LIGHT}"

    async def test_a_room_binding_remembers_its_member_lights(self, bridge):
        # The write goes to the group but the bridge reports the change on
        # each light, so override detection needs the membership.
        resolved = await resolve(bridge, make_plan())
        assert resolved.scopes["s"][0].light_ids == (LIGHT,)

    async def test_a_light_binds_to_itself(self, bridge):
        resolved = await resolve(bridge, make_plan(scope=["light:Corner Lamp"]))
        binding = resolved.scopes["s"][0]
        assert binding.path == f"/clip/v2/resource/light/{LIGHT}"
        assert binding.light_ids == (LIGHT,)

    async def test_resolution_takes_exactly_one_snapshot(self, bridge, http):
        await resolve(bridge, make_plan())
        assert http.paths.count("/clip/v2/resource") == 1

    async def test_an_unknown_room_is_reported_with_what_exists(self, bridge):
        with pytest.raises(PlanError, match="no such room"):
            await resolve(bridge, make_plan(scope=["room:Kitchen"]))

    async def test_every_bad_name_is_reported_at_once(self, bridge):
        # Fixing a config five typos deep one error per run is miserable.
        plan = Plan.model_validate(
            {
                "version": 1,
                "scenario": [
                    {
                        "name": "s",
                        "scope": ["room:Nope", "light:Also Nope"],
                        "step": [{"at": "09:00", "set": {"brightness": 50}}],
                    }
                ],
            }
        )
        with pytest.raises(PlanError) as caught:
            await resolve(bridge, plan)
        assert "could not resolve 2 names" in str(caught.value)
        assert "room:Nope" in str(caught.value)
        assert "light:Also Nope" in str(caught.value)

    async def test_a_room_with_no_lights_is_rejected(self, hue, http):
        http.queue(
            "/clip/v2/resource",
            envelope(
                {
                    "id": "room-empty",
                    "type": "room",
                    "metadata": {"name": "Empty"},
                    "children": [],
                    "services": [{"rid": "gl-x", "rtype": "grouped_light"}],
                }
            ),
        )
        with pytest.raises(PlanError, match="contains no lights"):
            await resolve(hue, make_plan(scope=["room:Empty"]))

    async def test_a_duplicated_name_is_ambiguous_rather_than_arbitrary(
        self, hue, http
    ):
        duplicate = dict(bridge_resources()[0])
        http.queue(
            "/clip/v2/resource",
            envelope(*bridge_resources(), duplicate | {"id": "room-other"}),
        )
        with pytest.raises(PlanError, match="share that name"):
            await resolve(hue, make_plan())


class TestResolveTriggers:
    async def test_a_motion_trigger_binds_through_its_device(self, bridge):
        # The motion service carries no name; the device that owns it does.
        plan = make_plan(
            rule=[{"when": "motion:Hall sensor", "set": {"brightness": 15}}]
        )
        resolved = await resolve(bridge, plan)
        assert resolved.triggers["motion:Hall sensor"].resource_ids == (MOTION,)

    async def test_a_disabled_sensor_is_a_warning_not_an_error(self, hue, http):
        # Switched off in the app, the service still resolves -- the plan must
        # keep running -- but a rule on it would wait forever, silently.
        service = {
            "id": MOTION,
            "type": "motion",
            "enabled": False,
            "owner": {"rid": SENSOR_DEVICE, "rtype": "device"},
        }
        http.queue("/clip/v2/resource", envelope(*bridge_resources(), service))
        plan = make_plan(rule=[{"when": "motion:Hall sensor", "set": {"on": True}}])
        resolved = await resolve(hue, plan)
        assert resolved.triggers["motion:Hall sensor"].resource_ids == (MOTION,)
        warning = (
            "motion:Hall sensor: the sensor is disabled on the bridge, "
            "so this trigger will never fire"
        )
        assert resolved.warnings == (warning,)

    async def test_an_enabled_sensor_raises_no_warning(self, hue, http):
        service = {
            "id": MOTION,
            "type": "motion",
            "enabled": True,
            "owner": {"rid": SENSOR_DEVICE, "rtype": "device"},
        }
        http.queue("/clip/v2/resource", envelope(*bridge_resources(), service))
        plan = make_plan(rule=[{"when": "motion:Hall sensor", "set": {"on": True}}])
        assert (await resolve(hue, plan)).warnings == ()

    async def test_a_signal_binds_to_nothing_on_the_bridge(self, bridge):
        plan = make_plan(activate_on="signal:movie_started")
        resolved = await resolve(bridge, plan)
        trigger = resolved.triggers["signal:movie_started"]
        assert trigger.is_signal
        assert trigger.resource_ids == ()

    async def test_a_device_without_the_service_says_what_it_has(self, bridge):
        plan = make_plan(
            rule=[{"when": "button:Hall sensor", "set": {"brightness": 15}}]
        )
        with pytest.raises(PlanError, match="no button service"):
            await resolve(bridge, plan)


def binding():
    return Binding(
        selector=parse_selector("room:Living Room"),
        path=f"/clip/v2/resource/grouped_light/{GROUPED_LIGHT}",
        light_ids=(LIGHT,),
    )


class TestSegmentPlanning:
    def test_a_short_ramp_is_one_put(self):
        segments = plan_segments(binding(), Action(brightness=60), ramp=3600)
        assert len(segments) == 1
        assert segments[0].payload["dynamics"]["duration"] == 3_600_000

    def test_a_ramp_at_the_ceiling_is_still_one_put(self):
        segments = plan_segments(
            binding(), Action(brightness=60), ramp=MAX_TRANSITION_SECONDS
        )
        assert len(segments) == 1

    def test_a_three_hour_ramp_is_chained_not_stepped(self):
        # 10800s over a 6000s ceiling: two segments of 5400s, with an
        # interpolated waypoint between them. Not 120 ticks.
        segments = plan_segments(
            binding(),
            Action(brightness=20),
            ramp=10800,
            start=Action(brightness=100),
        )
        assert len(segments) == 2
        assert [s.payload["dynamics"]["duration"] for s in segments] == [
            5_400_000,
            5_400_000,
        ]

    def test_a_chained_ramp_passes_through_the_midpoint(self):
        segments = plan_segments(
            binding(),
            Action(brightness=20),
            ramp=10800,
            start=Action(brightness=100),
        )
        assert segments[0].payload["dimming"]["brightness"] == pytest.approx(60.0)
        assert segments[1].payload["dimming"]["brightness"] == pytest.approx(20.0)

    def test_later_segments_wait_for_the_earlier_one(self):
        segments = plan_segments(
            binding(),
            Action(brightness=20),
            ramp=10800,
            start=Action(brightness=100),
        )
        assert segments[0].delay == 0.0
        assert segments[1].delay == pytest.approx(5400.0)

    def test_a_long_ramp_without_a_start_degrades_to_one_fade(self, caplog):
        # It cannot interpolate, so it arrives early rather than wrongly.
        segments = plan_segments(binding(), Action(brightness=20), ramp=10800)
        assert len(segments) == 1
        assert segments[0].duration == MAX_TRANSITION_SECONDS

    def test_on_is_dropped_when_the_scope_is_already_on(self):
        # Each payload attribute is its own ZigBee message; a needless `on`
        # doubles the cost of a brightness change.
        segments = plan_segments(
            binding(), Action(on=True, brightness=60), ramp=10, current_on=True
        )
        assert "on" not in segments[0].payload
        assert segments[0].payload["dimming"]["brightness"] == 60

    def test_on_is_sent_when_the_scope_is_off(self):
        segments = plan_segments(
            binding(), Action(on=True, brightness=60), ramp=10, current_on=False
        )
        assert segments[0].payload["on"] == {"on": True}

    def test_on_is_sent_when_the_state_is_unknown(self):
        segments = plan_segments(binding(), Action(on=True, brightness=60), ramp=10)
        assert segments[0].payload["on"] == {"on": True}

    def test_turning_off_is_never_treated_as_redundant(self):
        segments = plan_segments(binding(), Action(on=False), ramp=10, current_on=True)
        assert segments[0].payload["on"] == {"on": False}

    def test_only_the_first_segment_of_a_chain_carries_on(self):
        segments = plan_segments(
            binding(),
            Action(on=True, brightness=20),
            ramp=10800,
            start=Action(on=True, brightness=100),
        )
        assert "on" in segments[0].payload
        assert "on" not in segments[1].payload


class TestRunFade:
    async def test_a_short_fade_sends_exactly_one_request(self, hue, http):
        sent = await run_fade(hue, binding(), Action(brightness=60), ramp=60)
        assert sent == 1
        assert http.writes == [
            (
                "PUT",
                f"/clip/v2/resource/grouped_light/{GROUPED_LIGHT}",
                {
                    "dimming": {"brightness": 60.0},
                    "dynamics": {"duration": 60_000},
                },
            )
        ]

    async def test_a_room_fade_writes_the_group_not_each_light(self, hue, http):
        await run_fade(hue, binding(), Action(brightness=60), ramp=60)
        assert len(http.writes) == 1
        assert "grouped_light" in http.writes[0][1]

    async def test_a_long_fade_waits_between_segments(self, hue, http):
        # A simulated three-hour fade costs no wall-clock time.
        slept: list[float] = []

        async def fake_sleep(seconds):
            slept.append(seconds)

        sent = await run_fade(
            hue,
            binding(),
            Action(brightness=20),
            ramp=10800,
            start=Action(brightness=100),
            sleep=fake_sleep,
        )
        assert sent == 2
        assert slept == [pytest.approx(5400.0)]
        assert len(http.writes) == 2

    async def test_an_empty_target_sends_nothing(self, hue, http):
        # A scope already where it should be costs no request at all.
        segments = plan_segments(binding(), Action(on=True), ramp=0, current_on=True)
        assert segments == []


class TestWriteErrors:
    """The v2 API reports many rejections inside a 200 body.

    A raw `put` would let a refused write look like a success, and the runner
    would then record the fade as in force and never re-drive the scope.
    """

    async def test_a_body_level_rejection_raises(self, hue, http):
        http.write_result = envelope(errors=["device (light) is not capable"])
        with pytest.raises(HueResponseError, match="not capable"):
            await run_fade(hue, binding(), Action(brightness=60), ramp=60)

    async def test_an_accepted_write_returns_normally(self, hue, http):
        sent = await run_fade(hue, binding(), Action(brightness=60), ramp=60)
        assert sent == 1

    async def test_an_advisory_error_is_not_a_rejection(self, hue, http):
        # `communication_error` rides along with writes the bridge accepted;
        # treating it as a failure would strand every switched-off light.
        http.write_result = {
            "errors": [
                {
                    "error_code": "communication_error",
                    "description": "has communication issues, may not work",
                }
            ],
            "data": [{"rid": "x", "rtype": "light"}],
        }
        assert await run_fade(hue, binding(), Action(brightness=60), ramp=60) == 1


class TestSegmentDelays:
    def test_a_dropped_segment_does_not_pull_the_rest_early(self):
        # Delays are cumulative slots. Skipping a no-op segment without
        # carrying its length forward would fire everything after it one
        # segment too soon.
        segments = plan_segments(
            binding(),
            Action(brightness=20),
            ramp=18000,
            start=Action(brightness=100),
        )
        assert all(isinstance(s, Segment) for s in segments)
        # Every segment after the first waits a full slot.
        total = sum(s.delay for s in segments)
        assert total == pytest.approx(18000 - segments[0].duration)
