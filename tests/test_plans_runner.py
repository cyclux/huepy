"""Running a plan, with the clock under the test's control.

The runner's whole job is deciding *when* to write and *whether* a write is
still needed, so every test here drives a fake clock and asserts on the exact
requests that came out. A simulated day costs microseconds.
"""

import asyncio
import copy
import datetime
import logging
import zoneinfo
from typing import Any, Literal

import pytest

from huepy.client.http import SSEFrame
from huepy.exceptions import HueAPIError, PlanError
from huepy.models import parse_resource
from huepy.plans.arbiter import BRIGHTNESS_TOLERANCE, Fade
from huepy.plans.fields import raw_light_level
from huepy.plans.runner import PlanRunner, Threshold, _level_edge
from huepy.plans.schema import Action, Plan
from huepy.state.records import Change, ChangeKind, Resync, ResyncReason

from .conftest import StateHttp, envelope

BERLIN = zoneinfo.ZoneInfo("Europe/Berlin")
GROUPED_LIGHT = "gl-living"
GROUP_PATH = f"/clip/v2/resource/grouped_light/{GROUPED_LIGHT}"
LIGHT = "light-1"
DEVICE = "dev-lamp"


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
    ]


class FakeClock:
    """A clock the test moves by hand."""

    def __init__(self, start):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, **kwargs):
        self.now += datetime.timedelta(**kwargs)


async def noop_sleep(_seconds):
    return None


async def blocking_sleep(_seconds):
    """Never returns: for holding a chained fade's tail between segments."""
    await asyncio.Event().wait()


ON_PLAN = {
    "version": 1,
    "defaults": {"catchup_ramp": "5s"},
    "scenario": [
        {
            "name": "day",
            "scope": ["room:Living Room"],
            "step": [
                {"at": "08:00", "set": {"on": True, "brightness": 80}},
                {"at": "12:00", "set": {"on": True, "brightness": 100}},
            ],
        }
    ],
}

CHAINED_PLAN = {
    "version": 1,
    "scenario": [
        {
            "name": "long",
            "scope": ["room:Living Room"],
            "step": [
                {"at": "07:00", "set": {"brightness": 100}},
                {"at": "22:00", "ramp": "3h", "set": {"brightness": 20}},
            ],
        }
    ],
}


DAY_PLAN = {
    "version": 1,
    "location": {
        "latitude": 48.137,
        "longitude": 11.575,
        "timezone": "Europe/Berlin",
    },
    "defaults": {"catchup_ramp": "5s"},
    "scenario": [
        {
            "name": "day",
            "scope": ["room:Living Room"],
            "step": [
                {
                    "at": "09:00",
                    "ramp": "1h",
                    "set": {"brightness": 100, "kelvin": 5000},
                },
                {
                    "at": "22:00",
                    "ramp": "30m",
                    "set": {"brightness": 20, "kelvin": 2200},
                },
            ],
        }
    ],
}


@pytest.fixture
def clock():
    return FakeClock(datetime.datetime(2026, 9, 1, 8, 0, tzinfo=BERLIN))


@pytest.fixture
def bridge(hue, http):
    http.queue("/clip/v2/resource", envelope(*bridge_resources()))
    return hue


async def watched_runner(bridge, clock, changes, plan=None):
    runner = PlanRunner(
        bridge,
        Plan.model_validate(plan or DAY_PLAN),
        changes=changes,
        clock=clock,
        sleep=noop_sleep,
    )
    await runner.start()
    return runner


async def make_runner(bridge, clock, plan=None):
    runner = PlanRunner(
        bridge,
        Plan.model_validate(plan or DAY_PLAN),
        clock=clock,
        sleep=noop_sleep,
    )
    await runner.start()
    return runner


class TestCatchUp:
    async def test_a_cold_start_lands_on_the_current_target(self, bridge, http, clock):
        # 08:00 is after last night's 22:00 step, so the room belongs there.
        runner = await make_runner(bridge, clock)
        assert await runner.catch_up() == 1
        assert http.writes[0][1] == GROUP_PATH
        assert http.writes[0][2]["dimming"]["brightness"] == 20

    async def test_a_restart_mid_fade_lands_part_way(self, bridge, http, clock):
        # Half an hour into the 09:00 one-hour ramp from 20 to 100.
        clock.now = datetime.datetime(2026, 9, 1, 9, 30, tzinfo=BERLIN)
        runner = await make_runner(bridge, clock)
        await runner.catch_up()
        assert http.writes[0][2]["dimming"]["brightness"] == pytest.approx(60.0)

    async def test_catch_up_uses_the_short_catchup_ramp(self, bridge, http, clock):
        runner = await make_runner(bridge, clock)
        await runner.catch_up()
        assert http.writes[0][2]["dynamics"]["duration"] == 5000


class TestTick:
    async def test_a_step_boundary_produces_one_write(self, bridge, http, clock):
        runner = await make_runner(bridge, clock)
        await runner.catch_up()
        http.calls.clear()

        clock.now = datetime.datetime(2026, 9, 1, 9, 0, tzinfo=BERLIN)
        assert await runner.tick() == 1
        assert http.writes[0][2]["dynamics"]["duration"] == 3_600_000

    async def test_a_tick_with_nothing_due_writes_nothing(self, bridge, http, clock):
        # The loop stirs every fifteen minutes; a stir must be free.
        runner = await make_runner(bridge, clock)
        await runner.catch_up()
        http.calls.clear()

        clock.advance(minutes=10)
        assert await runner.tick() == 0
        assert http.writes == []

    async def test_a_whole_simulated_day_writes_once_per_step(
        self, bridge, http, clock
    ):
        runner = await make_runner(bridge, clock)
        await runner.catch_up()
        http.calls.clear()

        # Step through the day in ten-minute stirs.
        for _ in range(6 * 16):
            clock.advance(minutes=10)
            await runner.tick()
        # Two steps in the plan, so exactly two writes -- not 96.
        assert len(http.writes) == 2


class TestOverride:
    async def test_a_foreign_change_yields_the_scope(self, bridge, http, clock):
        runner = await make_runner(bridge, clock)
        await runner.catch_up()
        clock.advance(seconds=10)

        # Someone hits the wall switch: a brightness nowhere near the fade.
        handed_over = runner.arbiter.note_foreign_change(
            GROUP_PATH, brightness=95.0, at=clock.now
        )
        assert handed_over
        assert runner.arbiter.is_yielded(GROUP_PATH)

    async def test_a_yielded_scope_is_left_alone(self, bridge, http, clock):
        runner = await make_runner(bridge, clock)
        await runner.catch_up()
        clock.advance(seconds=10)
        runner.arbiter.note_foreign_change(GROUP_PATH, 95.0, clock.now)
        assert runner.arbiter.is_yielded(GROUP_PATH)
        http.calls.clear()

        clock.advance(minutes=10)
        assert await runner.tick() == 0
        assert http.writes == []

    async def test_it_rejoins_at_the_next_scheduled_step(self, bridge, http, clock):
        # The human wins now; the plan wins later.
        runner = await make_runner(bridge, clock)
        await runner.catch_up()
        clock.advance(seconds=10)
        runner.arbiter.note_foreign_change(GROUP_PATH, 95.0, clock.now)
        assert runner.arbiter.is_yielded(GROUP_PATH)
        http.calls.clear()

        clock.now = datetime.datetime(2026, 9, 1, 9, 0, tzinfo=BERLIN)
        assert await runner.tick() == 1
        assert not runner.arbiter.is_yielded(GROUP_PATH)


class TestFadeAttribution:
    def make_fade(self, at):
        return Fade(
            scope=GROUP_PATH,
            start=Action(brightness=100),
            target=Action(brightness=20),
            started_at=at,
            ramp=6000.0,
        )

    def test_a_long_fade_still_recognises_a_human(self, clock):
        # The state layer's own window would mask this for the whole 100
        # minutes, so the fade is checked against its own arithmetic instead.
        fade = self.make_fade(clock.now)
        halfway = clock.now + datetime.timedelta(seconds=3000)
        assert not fade.explains(brightness=95.0, at=halfway)

    def test_progress_consistent_with_the_ramp_is_ours(self, clock):
        fade = self.make_fade(clock.now)
        halfway = clock.now + datetime.timedelta(seconds=3000)
        assert fade.explains(brightness=60.0, at=halfway)

    def test_the_tolerance_is_honoured(self, clock):
        fade = self.make_fade(clock.now)
        halfway = clock.now + datetime.timedelta(seconds=3000)
        assert fade.explains(60.0 + BRIGHTNESS_TOLERANCE - 0.1, at=halfway)
        assert not fade.explains(60.0 + BRIGHTNESS_TOLERANCE + 1.0, at=halfway)

    def test_a_report_without_brightness_says_nothing_either_way(self, clock):
        fade = self.make_fade(clock.now)
        assert fade.explains(brightness=None, at=clock.now)

    def test_at_and_after_the_end_the_target_is_expected(self, clock):
        fade = self.make_fade(clock.now)
        end = clock.now + datetime.timedelta(seconds=6000)
        assert fade.expected_at(end).brightness == 20
        assert fade.explains(20.0, at=end + datetime.timedelta(hours=3))
        assert not fade.explains(60.0, at=end)

    def test_a_switch_off_is_never_ours(self, clock):
        # A fade to a brightness is a fade on a light that is on.
        fade = self.make_fade(clock.now)
        assert not fade.explains(None, at=clock.now, on=False)
        assert fade.explains(None, at=clock.now, on=True)

    def test_an_off_target_expects_off(self, clock):
        fade = Fade(
            scope=GROUP_PATH,
            start=None,
            target=Action(on=False),
            started_at=clock.now,
            ramp=0.0,
        )
        assert fade.explains(None, at=clock.now, on=False)
        assert not fade.explains(None, at=clock.now, on=True)


PRIORITY_PLAN = {
    "version": 1,
    "scenario": [
        {
            "name": "base",
            "scope": ["room:Living Room"],
            "priority": 0,
            "step": [{"at": "08:00", "set": {"brightness": 80}}],
        },
        {
            "name": "movie",
            "scope": ["room:Living Room"],
            "priority": 20,
            "activate_on": "signal:movie_started",
            "release_on": "signal:movie_ended",
            "set": {"brightness": 8},
        },
    ],
}


class TestPriority:
    async def test_a_dormant_mode_does_not_claim_its_scope(self, bridge, http, clock):
        runner = await make_runner(bridge, clock, PRIORITY_PLAN)
        clock.advance(hours=1)
        await runner.catch_up()
        assert http.writes[0][2]["dimming"]["brightness"] == 80

    async def test_a_signal_activates_the_higher_priority_mode(
        self, bridge, http, clock
    ):
        runner = await make_runner(bridge, clock, PRIORITY_PLAN)
        clock.advance(hours=1)
        await runner.catch_up()
        http.calls.clear()

        runner.fire("movie_started")
        assert await runner.tick() == 1
        assert http.writes[0][2]["dimming"]["brightness"] == 8

    async def test_releasing_hands_the_scope_back(self, bridge, http, clock):
        runner = await make_runner(bridge, clock, PRIORITY_PLAN)
        clock.advance(hours=1)
        await runner.catch_up()
        runner.fire("movie_started")
        await runner.tick()
        http.calls.clear()

        runner.fire("movie_ended")
        assert await runner.tick() == 1
        assert http.writes[0][2]["dimming"]["brightness"] == 80


class TestSignals:
    async def test_signals_lists_what_the_plan_listens_for(self, bridge, clock):
        runner = await make_runner(bridge, clock, PRIORITY_PLAN)
        assert runner.signals == {"movie_started", "movie_ended"}

    async def test_signals_skips_disabled_scenarios(self, bridge, clock):
        # Firing a disabled scenario's signal does nothing, so advertising it
        # would send `huepy plan signal` a name that then warns.
        plan: dict[str, Any] = copy.deepcopy(PRIORITY_PLAN)
        plan["scenario"][1]["enabled"] = False
        runner = await make_runner(bridge, clock, plan)
        assert runner.signals == frozenset()

    async def test_fire_reports_what_it_did(self, bridge, clock):
        runner = await make_runner(bridge, clock, PRIORITY_PLAN)
        assert runner.fire("movie_started") == ("activated 'movie'",)
        assert runner.fire("movie_ended") == ("released 'movie'",)

    async def test_fire_reports_nothing_for_a_signal_nobody_listens_for(
        self, bridge, clock, caplog
    ):
        runner = await make_runner(bridge, clock, PRIORITY_PLAN)
        with caplog.at_level(logging.WARNING, logger="huepy.plans.runner"):
            assert runner.fire("doorbell") == ()
        assert "signal:doorbell: nothing in the plan listens for it" in caplog.text


class TestLifecycle:
    async def test_reading_the_arbiter_before_starting_is_an_error(self, bridge):
        runner = PlanRunner(bridge, Plan.model_validate(DAY_PLAN))
        with pytest.raises(RuntimeError, match="not been started"):
            _ = runner.arbiter

    async def test_a_bad_name_fails_before_anything_is_written(self, hue, http):
        http.queue("/clip/v2/resource", envelope())
        runner = PlanRunner(hue, Plan.model_validate(DAY_PLAN))
        with pytest.raises(PlanError):
            await runner.start()
        assert http.writes == []

    async def test_close_cancels_a_chained_fade(self, bridge, http, clock):
        # A three-hour ramp chains; the tail is asleep between segments when
        # the runner closes, and must never be sent.
        runner = PlanRunner(
            bridge,
            Plan.model_validate(CHAINED_PLAN),
            clock=clock,
            sleep=blocking_sleep,
        )
        await runner.start()
        await runner.catch_up()
        clock.now = datetime.datetime(2026, 9, 1, 22, 0, tzinfo=BERLIN)
        await runner.tick()
        await asyncio.sleep(0)
        sent_before_close = len(http.writes)
        assert runner._fades

        await runner.close()
        assert runner._fades == {}
        assert len(http.writes) == sent_before_close


class Registration:
    """One cancellable subscription."""

    def __init__(self):
        self.cancelled = False

    def cancel(self):
        self.cancelled = True


class FakeChanges:
    """The narrowest thing that satisfies ChangeSource."""

    def __init__(self):
        self.handler = None
        self.resync_handler = None
        self.changes = Registration()
        self.resyncs = Registration()

    def on_change(self, handler, /):
        self.handler = handler
        return self.changes

    def on_resync(self, handler, /):
        self.resync_handler = handler
        return self.resyncs

    def report(self, *args, **kwargs):
        """Deliver a light change, failing loudly if nothing subscribed."""
        self.deliver(change(*args, **kwargs))

    def deliver(self, delivered):
        """Deliver an already-built change, failing loudly if nothing subscribed."""
        assert self.handler is not None, "nothing subscribed to changes"
        self.handler(delivered)


def change(  # noqa: PLR0913 - one keyword per Change field a test varies
    resource_id,
    brightness,
    at,
    *,
    origin: Literal["self", "unattributed"] = "unattributed",
    observation: Literal["reported", "command_echo"] = "reported",
    delta=None,
):
    return Change(
        kind=ChangeKind.UPDATE,
        received_at=at,
        observed_at=at,
        resource_id=resource_id,
        resource_type="light",
        before=None,
        after=None,
        delta={"dimming": {"brightness": brightness}} if delta is None else delta,
        origin=origin,
        observation=observation,
    )


class TestObservation:
    async def test_a_hand_change_yields_the_scope(self, bridge, clock):
        changes = FakeChanges()
        runner = await watched_runner(bridge, clock, changes, DAY_PLAN)
        await runner.catch_up()
        clock.advance(seconds=10)

        # Reported on the member light, though the write went to the group.
        changes.report(LIGHT, 95.0, clock.now)
        assert runner.arbiter.is_yielded(GROUP_PATH)

    async def test_a_jump_the_state_layer_calls_ours_is_still_a_human(
        self, bridge, clock
    ):
        # `origin == "self"` is the state layer's time window talking: during
        # a fade it attributes every report on the light to us. Trusting it
        # would mask a wall switch for the whole of a hundred-minute ramp,
        # which is exactly what the fade's own arithmetic is here to catch.
        changes = FakeChanges()
        runner = await watched_runner(bridge, clock, changes, DAY_PLAN)
        await runner.catch_up()
        clock.advance(seconds=10)

        changes.report(LIGHT, 95.0, clock.now, origin="self")
        assert runner.arbiter.is_yielded(GROUP_PATH)

    async def test_the_bridges_echo_of_our_target_is_ignored(self, bridge, clock):
        # The one report that is ours by construction: the bridge repeating
        # the transition's *target* back the moment it accepts the write.
        # Measured against the fade's expectation at that instant it is a
        # jump, so it must be skipped rather than judged.
        changes = FakeChanges()
        clock.now = datetime.datetime(2026, 9, 1, 9, 0, tzinfo=BERLIN)
        runner = await watched_runner(bridge, clock, changes, DAY_PLAN)
        await runner.catch_up()
        await runner.tick()

        changes.report(
            LIGHT, 100.0, clock.now, origin="self", observation="command_echo"
        )
        assert not runner.arbiter.is_yielded(GROUP_PATH)

    async def test_a_switch_off_by_hand_yields_the_scope(self, bridge, clock):
        # The most common manual action carries no brightness at all.
        changes = FakeChanges()
        runner = await watched_runner(bridge, clock, changes, DAY_PLAN)
        await runner.catch_up()

        changes.report(LIGHT, None, clock.now, delta={"on": {"on": False}})
        assert runner.arbiter.is_yielded(GROUP_PATH)

    async def test_a_switch_off_is_not_forgotten_by_the_next_step(
        self, bridge, http, clock
    ):
        # The executor drops `on` when the previous fade already turned the
        # light on. A hand switch-off has to reset that belief, or the noon
        # step goes out without `on` and the room stays dark.
        changes = FakeChanges()
        clock.now = datetime.datetime(2026, 9, 1, 9, 0, tzinfo=BERLIN)
        runner = await watched_runner(bridge, clock, changes, ON_PLAN)
        await runner.catch_up()
        assert http.writes[0][2]["on"] == {"on": True}

        changes.report(LIGHT, None, clock.now, delta={"on": {"on": False}})
        http.calls.clear()
        clock.now = datetime.datetime(2026, 9, 1, 12, 0, tzinfo=BERLIN)
        assert await runner.tick() == 1
        assert http.writes[0][2]["on"] == {"on": True}

    async def test_a_report_matching_our_fade_is_ignored(self, bridge, clock):
        changes = FakeChanges()
        runner = await watched_runner(bridge, clock, changes, DAY_PLAN)
        await runner.catch_up()

        # The catch-up fade targets brightness 20; a report there is ours even
        # though the bridge attributes it to nobody.
        changes.report(LIGHT, 20.0, clock.now)
        assert not runner.arbiter.is_yielded(GROUP_PATH)

    async def test_a_change_to_an_unrelated_light_is_ignored(self, bridge, clock):
        changes = FakeChanges()
        runner = await watched_runner(bridge, clock, changes, DAY_PLAN)
        await runner.catch_up()

        changes.report("some-other-light", 95.0, clock.now)
        assert not runner.arbiter.is_yielded(GROUP_PATH)

    async def test_reassert_re_drives_after_a_hand_change(self, bridge, http, clock):
        # Not yielding is not the same as not looking. A reassert plan still
        # has to notice the light moved, forget what it believed, and put the
        # light back -- with `on`, since a switch-off is the likely cause.
        changes = FakeChanges()
        plan = ON_PLAN | {
            "defaults": {"catchup_ramp": "5s", "on_manual_change": "reassert"}
        }
        clock.now = datetime.datetime(2026, 9, 1, 9, 0, tzinfo=BERLIN)
        runner = await watched_runner(bridge, clock, changes, plan)
        await runner.catch_up()
        http.calls.clear()

        changes.report(LIGHT, None, clock.now, delta={"on": {"on": False}})
        assert not runner.arbiter.is_yielded(GROUP_PATH)
        assert await runner.tick() == 1
        assert http.writes[0][2]["on"] == {"on": True}

    async def test_close_unsubscribes_from_both_streams(self, bridge, clock):
        changes = FakeChanges()
        runner = await watched_runner(bridge, clock, changes, DAY_PLAN)
        await runner.close()
        assert changes.changes.cancelled
        assert changes.resyncs.cancelled

    async def test_reassert_still_watches_for_resyncs(self, bridge, clock):
        # Not yielding to a human is a separate question from knowing the
        # stream dropped: a reassert plan still has to recompute after a gap.
        changes = FakeChanges()
        plan = DAY_PLAN | {
            "defaults": {"catchup_ramp": "5s", "on_manual_change": "reassert"}
        }
        _ = await watched_runner(bridge, clock, changes, plan)
        assert changes.resync_handler is not None

    async def test_a_resync_forces_a_fresh_catch_up(self, bridge, http, clock):
        changes = FakeChanges()
        runner = await watched_runner(bridge, clock, changes, DAY_PLAN)
        await runner.catch_up()
        http.calls.clear()

        # Nothing changed on the clock, so a plain tick would write nothing.
        assert await runner.tick() == 0
        assert changes.resync_handler is not None
        changes.resync_handler(
            Resync(
                reason=ResyncReason.RECONNECT,
                gap_started=clock.now,
                gap_ended=clock.now,
            )
        )
        # After a gap the runner's beliefs are worthless; it re-derives them.
        assert runner._needs_catchup
        assert await runner.catch_up() == 1


class FailingHttp:
    """Wraps the fake transport and fails chosen writes."""

    def __init__(self, http, fails):
        self._http = http
        self._fails = fails
        self.attempts = 0

    def __getattr__(self, name):
        return getattr(self._http, name)

    async def put(self, path, data):
        self.attempts += 1
        if self.attempts in self._fails:
            raise HueAPIError(503, "bridge is busy")
        return await self._http.put(path, data)


class BrokenClient:
    """A client whose writes fail, sharing the fake's snapshot."""

    def __init__(self, hue, http, fails):
        self._hue = hue
        self._http = FailingHttp(http, fails)

    @property
    def http(self) -> Any:
        return self._http

    async def snapshot(self):
        return await self._hue.snapshot()


class TestFailureIsolation:
    async def test_a_rejected_write_does_not_stop_the_runner(self, bridge, http, clock):
        # A plan runs for weeks; one unreachable bulb must not end it.
        client = BrokenClient(bridge, http, fails={1})
        runner = PlanRunner(
            client,
            Plan.model_validate(DAY_PLAN),
            clock=clock,
            sleep=noop_sleep,
        )
        await runner.start()
        assert await runner.catch_up() == 0

    async def test_a_failed_scope_is_re_driven_next_tick(self, bridge, http, clock):
        # Forgetting the fade is what makes the retry happen; believing the
        # scope arrived would strand it until its next scheduled step.
        client = BrokenClient(bridge, http, fails={1})
        runner = PlanRunner(
            client,
            Plan.model_validate(DAY_PLAN),
            clock=clock,
            sleep=noop_sleep,
        )
        await runner.start()
        await runner.catch_up()
        assert runner.arbiter.state_of(GROUP_PATH).fade is None
        assert await runner.tick() == 1

    async def test_a_failing_chain_tail_clears_the_fade(self, bridge, http, clock):
        # The tail runs in a background task nothing awaits, so without its own
        # handler the exception would vanish and the scope would look arrived.
        client = BrokenClient(bridge, http, fails={2})
        # A three-hour ramp, so the fade chains and it is the tail that fails.
        runner = PlanRunner(
            client,
            Plan.model_validate(CHAINED_PLAN),
            clock=clock,
            sleep=noop_sleep,
        )
        await runner.start()
        await runner.catch_up()
        clock.now = datetime.datetime(2026, 9, 1, 22, 0, tzinfo=BERLIN)
        await runner.tick()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert runner._fades == {}


class TestSharedLights:
    async def test_a_light_in_two_scopes_yields_both(self, hue, http, clock):
        http.queue("/clip/v2/resource", envelope(*bridge_resources()))
        plan = {
            "version": 1,
            "scenario": [
                {
                    "name": "room",
                    "scope": ["room:Living Room", "light:Corner Lamp"],
                    "step": [{"at": "08:00", "set": {"brightness": 50}}],
                }
            ],
        }
        changes = FakeChanges()
        runner = await watched_runner(hue, clock, changes, plan)
        await runner.catch_up()
        clock.advance(seconds=10)

        changes.report(LIGHT, 95.0, clock.now)
        light_path = f"/clip/v2/resource/light/{LIGHT}"
        assert runner.arbiter.is_yielded(GROUP_PATH)
        assert runner.arbiter.is_yielded(light_path)


class TestClose:
    async def test_close_stops_a_running_loop(self, bridge, clock):
        runner = await make_runner(bridge, clock)
        task = asyncio.create_task(runner.run())
        await asyncio.sleep(0)
        await runner.close()
        await asyncio.wait_for(task, timeout=1.0)
        assert task.done()

    async def test_stop_returns_run_without_cancelling_it(self, bridge, clock):
        # A signal handler calls stop(); run() must come back normally so the
        # context managers around it close the session rather than unwind.
        runner = await make_runner(bridge, clock)
        task = asyncio.create_task(runner.run())
        await asyncio.sleep(0)
        runner.stop()
        await asyncio.wait_for(task, timeout=1.0)
        assert not task.cancelled()
        assert task.exception() is None

    async def test_stop_during_the_catch_up_wait_skips_the_tick(
        self, bridge, http, clock
    ):
        clock.now = datetime.datetime(2026, 9, 1, 9, 30, tzinfo=BERLIN)
        started: list[PlanRunner] = []

        async def stopping_sleep(_seconds: float) -> None:
            started[0].stop()

        runner = PlanRunner(
            bridge, Plan.model_validate(DAY_PLAN), clock=clock, sleep=stopping_sleep
        )
        started.append(runner)
        await runner.start()
        await asyncio.wait_for(runner.run(), timeout=1.0)
        assert len(http.writes) == 1


class TestLogging:
    async def test_a_write_logs_scope_target_ramp_and_requests(
        self, bridge, clock, caplog
    ):
        runner = await make_runner(bridge, clock)
        clock.now = datetime.datetime(2026, 9, 1, 9, 0, tzinfo=BERLIN)
        with caplog.at_level(logging.INFO, logger="huepy.plans.runner"):
            assert await runner.tick() == 1
        line = next(r.getMessage() for r in caplog.records if " -> " in r.getMessage())
        assert line.startswith("room:Living Room: day -> brightness=100 ")
        assert line.endswith("over 1h, 1 request, ends 10:00:00")

    async def test_an_idempotent_tick_logs_the_skip_at_debug_only(
        self, bridge, clock, caplog
    ):
        runner = await make_runner(bridge, clock)
        clock.now = datetime.datetime(2026, 9, 1, 9, 0, tzinfo=BERLIN)
        await runner.tick()
        caplog.clear()
        with caplog.at_level(logging.DEBUG, logger="huepy.plans.runner"):
            assert await runner.tick() == 0
        assert [r.levelno for r in caplog.records] == [logging.DEBUG]
        assert "still in force, nothing to send" in caplog.text


SENSOR_DEVICE = "dev-hall"
MOTION = "motion-1"
BUTTON = "button-1"
CONTACT = "contact-1"
LIGHT_LEVEL = "level-1"


def sensor_resources():
    return [
        *bridge_resources(),
        {
            "id": SENSOR_DEVICE,
            "type": "device",
            "metadata": {"name": "Hall sensor"},
            "services": [
                {"rid": MOTION, "rtype": "motion"},
                {"rid": BUTTON, "rtype": "button"},
                {"rid": CONTACT, "rtype": "contact"},
                {"rid": LIGHT_LEVEL, "rtype": "light_level"},
            ],
        },
    ]


def sensor_change(resource_id, resource_type, at, **section):
    """Build a change on a sensor service, folded the way the state layer folds it."""
    delta = {"id": resource_id, "type": resource_type, **section}
    return Change(
        kind=ChangeKind.UPDATE,
        received_at=at,
        resource_id=resource_id,
        resource_type=resource_type,
        before=None,
        after=parse_resource(delta),
        delta=delta,
    )


def motion(at, *, detected=True):
    return sensor_change(
        MOTION,
        "motion",
        at,
        motion={"motion": detected, "motion_report": {"motion": detected}},
    )


def button(at, event):
    return sensor_change(
        BUTTON, "button", at, button={"button_report": {"event": event}}
    )


def contact(at, state):
    return sensor_change(CONTACT, "contact", at, contact_report={"state": state})


def light_level(at, lux, *, valid=True):
    """Build a level report as the portal documents it: the report, not the field."""
    return sensor_change(
        LIGHT_LEVEL,
        "light_level",
        at,
        light={
            "light_level_report": {"light_level": round(raw_light_level(lux))},
            "light_level_valid": valid,
        },
    )


RULE_PLAN: dict[str, Any] = {
    "version": 1,
    "defaults": {"catchup_ramp": "5s"},
    "scenario": [
        {
            "name": "base",
            "scope": ["room:Living Room"],
            "priority": 0,
            "step": [
                {"at": "08:00", "set": {"brightness": 80}},
                {"at": "12:00", "ramp": "1h", "set": {"brightness": 100}},
            ],
        },
        {
            "name": "hall-motion",
            "scope": ["room:Living Room"],
            "priority": 10,
            "rule": [
                {
                    "when": "motion:Hall sensor",
                    "ramp": "2s",
                    "hold": "90s",
                    "set": {"brightness": 15},
                }
            ],
        },
    ],
}


def rule_plan(**rule: Any) -> dict[str, Any]:
    """Copy the rule plan with the motion rule's keys overridden."""
    plan = copy.deepcopy(RULE_PLAN)
    plan["scenario"][1]["rule"][0].update(rule)
    return plan


def level_plan(**rule: Any) -> dict[str, Any]:
    """Copy the rule plan, with its rule reading the hall sensor's light level."""
    return rule_plan(when="light_level:Hall sensor", below=30, **rule)


@pytest.fixture
def sensor_bridge(hue, http):
    http.queue("/clip/v2/resource", envelope(*sensor_resources()))
    return hue


async def rule_runner(sensor_bridge, clock, changes, plan=None):
    clock.now = datetime.datetime(2026, 9, 1, 9, 0, tzinfo=BERLIN)
    runner = await watched_runner(sensor_bridge, clock, changes, plan or RULE_PLAN)
    await runner.catch_up()
    return runner


class TestRules:
    async def test_motion_drives_the_rule_target_with_its_own_ramp(
        self, sensor_bridge, http, clock
    ):
        changes = FakeChanges()
        runner = await rule_runner(sensor_bridge, clock, changes)
        http.calls.clear()

        changes.deliver(motion(clock.now))
        assert await runner.tick() == 1
        assert http.writes[0][1] == GROUP_PATH
        assert http.writes[0][2]["dimming"]["brightness"] == 15
        assert http.writes[0][2]["dynamics"]["duration"] == 2000

    async def test_motion_ending_does_not_fire(self, sensor_bridge, http, clock):
        changes = FakeChanges()
        runner = await rule_runner(sensor_bridge, clock, changes)
        http.calls.clear()

        changes.deliver(motion(clock.now, detected=False))
        assert await runner.tick() == 0

    async def test_a_closed_window_ignores_the_trigger(
        self, sensor_bridge, http, clock
    ):
        changes = FakeChanges()
        plan = rule_plan(between=["22:00", "06:00"])
        runner = await rule_runner(sensor_bridge, clock, changes, plan)
        http.calls.clear()

        # 09:00 is outside 22:00-06:00.
        changes.deliver(motion(clock.now))
        assert await runner.tick() == 0

    async def test_an_open_window_wraps_midnight(self, sensor_bridge, http, clock):
        changes = FakeChanges()
        plan = rule_plan(between=["22:00", "06:00"])
        runner = await rule_runner(sensor_bridge, clock, changes, plan)
        clock.now = datetime.datetime(2026, 9, 1, 23, 30, tzinfo=BERLIN)
        await runner.tick()
        http.calls.clear()

        changes.deliver(motion(clock.now))
        assert await runner.tick() == 1
        assert http.writes[0][2]["dimming"]["brightness"] == 15

    async def test_the_hold_expires_back_to_the_curve(self, sensor_bridge, http, clock):
        changes = FakeChanges()
        runner = await rule_runner(sensor_bridge, clock, changes)
        changes.deliver(motion(clock.now))
        await runner.tick()
        changes.deliver(motion(clock.now, detected=False))
        http.calls.clear()

        clock.advance(seconds=91)
        assert await runner.tick() == 1
        assert http.writes[0][2]["dimming"]["brightness"] == 80

    async def test_handing_back_never_snaps(self, sensor_bridge, http, clock):
        # The base step's own ramp finished hours ago, so "the remaining ramp"
        # is zero. Dropping from 15 to 80 in one frame when someone leaves the
        # room is exactly the kind of thing that makes people rip out an
        # automation, so the way back is floored at the catch-up ramp.
        changes = FakeChanges()
        runner = await rule_runner(sensor_bridge, clock, changes)
        changes.deliver(motion(clock.now))
        await runner.tick()
        changes.deliver(motion(clock.now, detected=False))
        http.calls.clear()

        clock.advance(seconds=91)
        await runner.tick()
        assert http.writes[0][2]["dynamics"]["duration"] == 5000

    async def test_handing_back_mid_fade_keeps_the_curve_ramp(
        self, sensor_bridge, http, clock
    ):
        changes = FakeChanges()
        runner = await rule_runner(sensor_bridge, clock, changes)
        clock.now = datetime.datetime(2026, 9, 1, 12, 0, tzinfo=BERLIN)
        await runner.tick()
        changes.deliver(motion(clock.now))
        await runner.tick()
        changes.deliver(motion(clock.now, detected=False))
        http.calls.clear()

        # Twenty minutes into the hour-long 12:00 ramp: forty minutes remain,
        # which is longer than the floor, so the fade rejoins over those.
        clock.advance(minutes=20)
        await runner.tick()
        assert http.writes[0][2]["dimming"]["brightness"] == 100
        assert http.writes[0][2]["dynamics"]["duration"] == 40 * 60 * 1000

    async def test_repeated_motion_extends_the_hold_without_a_write(
        self, sensor_bridge, http, clock
    ):
        changes = FakeChanges()
        runner = await rule_runner(sensor_bridge, clock, changes)
        changes.deliver(motion(clock.now))
        await runner.tick()
        changes.deliver(motion(clock.now, detected=False))
        http.calls.clear()

        # Someone comes back a minute later: the countdown is abandoned and
        # the light simply stays, at no cost in requests.
        clock.advance(seconds=60)
        changes.deliver(motion(clock.now))
        assert await runner.tick() == 0
        clock.advance(seconds=60)
        assert await runner.tick() == 0
        changes.deliver(motion(clock.now, detected=False))
        clock.advance(seconds=89)
        assert await runner.tick() == 0
        clock.advance(seconds=2)
        assert await runner.tick() == 1

    async def test_a_rule_without_hold_lasts_until_the_next_step(
        self, sensor_bridge, http, clock
    ):
        changes = FakeChanges()
        runner = await rule_runner(sensor_bridge, clock, changes, rule_plan(hold=None))
        changes.deliver(motion(clock.now))
        await runner.tick()
        http.calls.clear()

        clock.advance(hours=2)
        assert await runner.tick() == 0
        clock.now = datetime.datetime(2026, 9, 1, 12, 0, tzinfo=BERLIN)
        assert await runner.tick() == 1
        assert http.writes[0][2]["dimming"]["brightness"] == 100

    async def test_the_loop_wakes_for_the_hold_expiry(self, sensor_bridge, http, clock):
        changes = FakeChanges()
        runner = await rule_runner(sensor_bridge, clock, changes)
        changes.deliver(motion(clock.now))
        await runner.tick()
        changes.deliver(motion(clock.now, detected=False))
        assert runner._seconds_until_next(clock.now) == pytest.approx(90.0)

    async def test_a_button_fires_on_the_initial_press_only(
        self, sensor_bridge, http, clock
    ):
        changes = FakeChanges()
        plan = rule_plan(when="button:Hall sensor")
        runner = await rule_runner(sensor_bridge, clock, changes, plan)
        http.calls.clear()

        changes.deliver(button(clock.now, "short_release"))
        assert await runner.tick() == 0
        changes.deliver(button(clock.now, "initial_press"))
        assert await runner.tick() == 1

    async def test_a_contact_fires_when_it_opens(self, sensor_bridge, http, clock):
        changes = FakeChanges()
        plan = rule_plan(when="contact:Hall sensor")
        runner = await rule_runner(sensor_bridge, clock, changes, plan)
        http.calls.clear()

        changes.deliver(contact(clock.now, "contact"))
        assert await runner.tick() == 0
        changes.deliver(contact(clock.now, "no_contact"))
        assert await runner.tick() == 1

    async def test_a_signal_can_fire_a_rule(self, sensor_bridge, http, clock):
        changes = FakeChanges()
        plan = rule_plan(when="signal:doorbell")
        runner = await rule_runner(sensor_bridge, clock, changes, plan)
        http.calls.clear()

        runner.fire("doorbell")
        assert await runner.tick() == 1
        assert http.writes[0][2]["dimming"]["brightness"] == 15

    async def test_a_sensor_can_activate_a_mode(self, sensor_bridge, http, clock):
        changes = FakeChanges()
        plan = {
            **RULE_PLAN,
            "scenario": [
                RULE_PLAN["scenario"][0],
                {
                    "name": "welcome",
                    "scope": ["room:Living Room"],
                    "priority": 10,
                    "activate_on": "contact:Hall sensor",
                    "release_on": "signal:settled",
                    "set": {"brightness": 100},
                },
            ],
        }
        runner = await rule_runner(sensor_bridge, clock, changes, plan)
        http.calls.clear()

        changes.deliver(contact(clock.now, "no_contact"))
        assert await runner.tick() == 1
        assert http.writes[0][2]["dimming"]["brightness"] == 100

    async def test_a_trigger_takes_back_a_yielded_scope(
        self, sensor_bridge, http, clock
    ):
        # "Yield until the next trigger": the human wins until the plan has a
        # new reason to act, and a sensor firing is one.
        changes = FakeChanges()
        runner = await rule_runner(sensor_bridge, clock, changes)
        clock.advance(seconds=10)
        changes.report(LIGHT, 95.0, clock.now)
        assert runner.arbiter.is_yielded(GROUP_PATH)
        http.calls.clear()

        changes.deliver(motion(clock.now))
        assert await runner.tick() == 1
        assert not runner.arbiter.is_yielded(GROUP_PATH)

    async def test_a_hand_change_during_a_hold_drops_the_hold(
        self, sensor_bridge, http, clock
    ):
        # Otherwise the plan would rejoin at the next step by re-asserting a
        # stale motion hold rather than the schedule.
        changes = FakeChanges()
        runner = await rule_runner(sensor_bridge, clock, changes, rule_plan(hold="6h"))
        changes.deliver(motion(clock.now))
        await runner.tick()

        changes.report(LIGHT, 95.0, clock.now)
        assert runner.arbiter.state_of(GROUP_PATH).hold is None
        http.calls.clear()

        clock.now = datetime.datetime(2026, 9, 1, 12, 0, tzinfo=BERLIN)
        await runner.tick()
        assert http.writes[0][2]["dimming"]["brightness"] == 100

    async def test_a_reassert_plan_with_a_rule_still_listens(
        self, sensor_bridge, clock
    ):
        changes = FakeChanges()
        plan = {**RULE_PLAN, "defaults": {"on_manual_change": "reassert"}}
        _ = await rule_runner(sensor_bridge, clock, changes, plan)
        assert changes.handler is not None

    async def test_a_reassert_plan_still_ignores_hand_changes(
        self, sensor_bridge, clock
    ):
        changes = FakeChanges()
        plan = {**RULE_PLAN, "defaults": {"on_manual_change": "reassert"}}
        runner = await rule_runner(sensor_bridge, clock, changes, plan)
        changes.report(LIGHT, 95.0, clock.now)
        assert not runner.arbiter.is_yielded(GROUP_PATH)

    async def test_a_dormant_modes_rules_stay_asleep(self, sensor_bridge, http, clock):
        # The button only means "pause" while the movie is on. Off-mode, it is
        # just a button, and the day curve keeps the room.
        changes = FakeChanges()
        plan = copy.deepcopy(RULE_PLAN)
        plan["scenario"][1]["activate_on"] = "signal:movie_started"
        runner = await rule_runner(sensor_bridge, clock, changes, plan)
        http.calls.clear()

        changes.deliver(motion(clock.now))
        assert await runner.tick() == 0
        runner.fire("movie_started")
        changes.deliver(motion(clock.now))
        assert await runner.tick() == 1
        assert http.writes[0][2]["dimming"]["brightness"] == 15

    async def test_a_disabled_scenario_never_fires(self, sensor_bridge, http, clock):
        changes = FakeChanges()
        plan = copy.deepcopy(RULE_PLAN)
        plan["scenario"][1]["enabled"] = False
        runner = await rule_runner(sensor_bridge, clock, changes, plan)
        http.calls.clear()

        changes.deliver(motion(clock.now))
        assert await runner.tick() == 0


class TestModeHandback:
    async def test_releasing_a_mode_rejoins_without_a_snap(self, bridge, http, clock):
        runner = await make_runner(bridge, clock, PRIORITY_PLAN)
        clock.advance(hours=1)
        await runner.catch_up()
        runner.fire("movie_started")
        await runner.tick()
        http.calls.clear()

        runner.fire("movie_ended")
        await runner.tick()
        assert http.writes[0][2]["dynamics"]["duration"] == 5000

    async def test_activating_a_mode_uses_its_own_ramp(self, bridge, http, clock):
        # The floor is for rejoining a curve. A mode's ramp is what its author
        # wrote, including zero.
        runner = await make_runner(bridge, clock, PRIORITY_PLAN)
        clock.advance(hours=1)
        await runner.catch_up()
        http.calls.clear()

        runner.fire("movie_started")
        await runner.tick()
        assert http.writes[0][2]["dynamics"]["duration"] == 0


class TriggeringClient:
    """A client whose first write delivers a sensor change while in flight.

    Stands in for the real race: the state layer's dispatch task delivering a
    motion event while ``tick()`` is awaiting a rate-paced PUT.
    """

    def __init__(self, hue, http, changes, delivered):
        self._hue = hue
        self._http = http
        self._changes = changes
        self._delivered = delivered
        self.sent = 0

    @property
    def http(self) -> Any:
        return self

    def __getattr__(self, name):
        return getattr(self._http, name)

    async def put(self, path, data):
        self.sent += 1
        if self.sent == 1:
            self._changes.deliver(self._delivered)
        return await self._http.put(path, data)

    async def snapshot(self):
        return await self._hue.snapshot()


class TestRuleRaces:
    async def test_a_trigger_during_a_write_is_not_lost(self, hue, http, clock):
        # The loop must not clear its wake-up on the way back in: a motion
        # event that lands while catch-up's first PUT is in flight sets it
        # mid-tick, and discarding it would leave the hold to expire during
        # the next long sleep without ever being driven.
        http.queue("/clip/v2/resource", envelope(*sensor_resources()))
        changes = FakeChanges()
        clock.now = datetime.datetime(2026, 9, 1, 9, 0, tzinfo=BERLIN)
        client = TriggeringClient(hue, http, changes, motion(clock.now))
        runner = PlanRunner(
            client,
            Plan.model_validate(RULE_PLAN),
            changes=changes,
            clock=clock,
            sleep=noop_sleep,
        )
        await runner.start()

        task = asyncio.create_task(runner.run())
        for _ in range(10):
            await asyncio.sleep(0)
        await runner.close()
        await task
        assert [w[2]["dimming"]["brightness"] for w in http.writes] == [80.0, 15.0]

    async def test_motion_holds_while_occupied_then_times_out(
        self, sensor_bridge, http, clock
    ):
        # The Hue sensor reports `true` once and then says nothing while
        # movement continues, so a hold timed from the start would drop the
        # light on someone standing in the hall for three minutes.
        changes = FakeChanges()
        runner = await rule_runner(sensor_bridge, clock, changes)
        changes.deliver(motion(clock.now))
        await runner.tick()
        http.calls.clear()

        clock.advance(minutes=3)
        assert await runner.tick() == 0
        assert runner._seconds_until_next(clock.now) > 90.0

        changes.deliver(motion(clock.now, detected=False))
        assert runner._seconds_until_next(clock.now) == pytest.approx(90.0)
        clock.advance(seconds=89)
        assert await runner.tick() == 0
        clock.advance(seconds=2)
        assert await runner.tick() == 1
        assert http.writes[0][2]["dimming"]["brightness"] == 80

    async def test_motion_ending_without_a_hold_changes_nothing(
        self, sensor_bridge, http, clock
    ):
        changes = FakeChanges()
        runner = await rule_runner(sensor_bridge, clock, changes, rule_plan(hold=None))
        changes.deliver(motion(clock.now))
        await runner.tick()
        http.calls.clear()

        changes.deliver(motion(clock.now, detected=False))
        clock.advance(hours=1)
        assert await runner.tick() == 0

    async def test_motion_ending_with_nothing_held_is_ignored(
        self, sensor_bridge, http, clock
    ):
        changes = FakeChanges()
        runner = await rule_runner(sensor_bridge, clock, changes)
        http.calls.clear()

        changes.deliver(motion(clock.now, detected=False))
        assert await runner.tick() == 0
        assert runner.arbiter.state_of(GROUP_PATH).hold is None

    async def test_a_delta_that_only_touches_validity_does_not_fire(
        self, sensor_bridge, http, clock
    ):
        # Only the delta is read. The folded state may well still say
        # "motion" from an hour ago; a report about the reading's validity
        # is not a person walking in.
        changes = FakeChanges()
        runner = await rule_runner(sensor_bridge, clock, changes)
        http.calls.clear()

        changes.deliver(
            sensor_change(MOTION, "motion", clock.now, motion={"motion_valid": False})
        )
        assert await runner.tick() == 0

    async def test_releasing_a_mode_drops_its_holds(self, sensor_bridge, http, clock):
        # A hold-less rule on a scope nothing schedules has no expiry. Left
        # in place across a release, the mode would honour it the moment it
        # woke again -- days later, with no motion at all.
        changes = FakeChanges()
        plan = copy.deepcopy(RULE_PLAN)
        plan["scenario"][0] = {
            "name": "base",
            "scope": ["room:Living Room"],
            "set": {"brightness": 80},
        }
        plan["scenario"][1]["activate_on"] = "signal:movie_started"
        plan["scenario"][1]["release_on"] = "signal:movie_ended"
        plan["scenario"][1]["rule"][0].pop("hold")
        runner = await rule_runner(sensor_bridge, clock, changes, plan)
        runner.fire("movie_started")
        changes.deliver(motion(clock.now))
        await runner.tick()
        runner.fire("movie_ended")
        await runner.tick()
        http.calls.clear()

        clock.advance(days=3)
        runner.fire("movie_started")
        assert await runner.tick() == 0

    async def test_a_dormant_modes_steps_are_not_a_resume_point(
        self, sensor_bridge, clock
    ):
        plan = copy.deepcopy(RULE_PLAN)
        plan["scenario"][0]["activate_on"] = "signal:never"
        runner = await watched_runner(sensor_bridge, clock, FakeChanges(), plan)
        assert runner.arbiter.next_step_for(GROUP_PATH, clock.now) is None

    async def test_a_failed_hand_back_keeps_the_floor_on_retry(self, hue, http, clock):
        # Ownership moves only once the write is on the wire. Recording it
        # first would make the retry look like the same scenario re-asserting
        # itself, and the no-snap floor would be skipped.
        http.queue("/clip/v2/resource", envelope(*sensor_resources()))
        changes = FakeChanges()
        clock.now = datetime.datetime(2026, 9, 1, 9, 0, tzinfo=BERLIN)
        client = BrokenClient(hue, http, fails={3})
        runner = PlanRunner(
            client,
            Plan.model_validate(RULE_PLAN),
            changes=changes,
            clock=clock,
            sleep=noop_sleep,
        )
        await runner.start()
        await runner.catch_up()
        changes.deliver(motion(clock.now))
        await runner.tick()
        changes.deliver(motion(clock.now, detected=False))
        clock.advance(seconds=91)
        assert await runner.tick() == 0  # the hand-back write is refused
        http.calls.clear()

        assert await runner.tick() == 1
        assert http.writes[0][2]["dynamics"]["duration"] == 5000


WEEKEND_ONLY_PLAN = {
    "version": 1,
    "scenario": [
        {
            "name": "weekend",
            "scope": ["room:Living Room"],
            "days": ["saturday", "sunday"],
            "step": [{"at": "10:30", "set": {"brightness": 100}}],
        }
    ],
}


class TestYieldResume:
    async def test_a_yield_with_no_step_in_sight_still_ends(self, bridge, http, clock):
        # Friday: nothing covering the scope runs today, so there is no "next
        # step" to record. The scope must still come back when Saturday's
        # step arrives, not stay yielded forever.
        changes = FakeChanges()
        clock.now = datetime.datetime(2026, 9, 4, 9, 0, tzinfo=BERLIN)
        runner = await watched_runner(bridge, clock, changes, WEEKEND_ONLY_PLAN)
        await runner.catch_up()
        changes.report(LIGHT, 95.0, clock.now)
        assert runner.arbiter.is_yielded(GROUP_PATH)

        clock.now = datetime.datetime(2026, 9, 5, 10, 31, tzinfo=BERLIN)
        assert await runner.tick() == 1
        assert not runner.arbiter.is_yielded(GROUP_PATH)

    async def test_a_signal_activated_mode_takes_a_yielded_scope(
        self, bridge, http, clock
    ):
        # "Yield until the next trigger" -- and a signal is one.
        changes = FakeChanges()
        runner = await watched_runner(bridge, clock, changes, PRIORITY_PLAN)
        clock.advance(hours=1)
        await runner.catch_up()
        clock.advance(seconds=10)
        changes.report(LIGHT, 95.0, clock.now)
        assert runner.arbiter.is_yielded(GROUP_PATH)
        http.calls.clear()

        clock.advance(minutes=1)
        runner.fire("movie_started")
        assert await runner.tick() == 1
        assert http.writes[0][2]["dimming"]["brightness"] == 8

    async def test_releasing_the_mode_hands_back_to_the_curve(
        self, bridge, http, clock
    ):
        # A hand change during the movie stands for the movie. When the
        # movie ends the curve takes the room back; the human's dimming was
        # about the film, not the afternoon.
        changes = FakeChanges()
        runner = await watched_runner(bridge, clock, changes, PRIORITY_PLAN)
        clock.advance(hours=1)
        await runner.catch_up()
        runner.fire("movie_started")
        await runner.tick()
        clock.advance(minutes=1)
        changes.report(LIGHT, 95.0, clock.now)
        http.calls.clear()

        clock.advance(minutes=1)
        runner.fire("movie_ended")
        assert await runner.tick() == 1
        assert http.writes[0][2]["dimming"]["brightness"] == 80

    async def test_a_losing_rule_does_not_take_back_a_yielded_scope(
        self, sensor_bridge, http, clock
    ):
        # The human dimmed the room during the movie. A motion rule that
        # cannot even outrank the movie firing is no reason to undo that.
        changes = FakeChanges()
        plan = copy.deepcopy(RULE_PLAN)
        plan["scenario"].append(
            {
                "name": "movie",
                "scope": ["room:Living Room"],
                "priority": 20,
                "activate_on": "signal:movie_started",
                "release_on": "signal:movie_ended",
                "set": {"brightness": 8},
            }
        )
        runner = await rule_runner(sensor_bridge, clock, changes, plan)
        runner.fire("movie_started")
        await runner.tick()
        clock.advance(minutes=1)
        changes.report(LIGHT, 95.0, clock.now)
        http.calls.clear()

        clock.advance(minutes=1)
        changes.deliver(motion(clock.now))
        assert await runner.tick() == 0
        assert runner.arbiter.is_yielded(GROUP_PATH)


class TestRestart:
    async def test_a_restart_mid_fade_continues_the_ramp(self, bridge, http, clock):
        # Catch-up lands the light where it should already be; the rest of
        # the step's ramp still has to go to the bridge, and only after the
        # catch-up fade has finished, or the second PUT overrides the first.
        clock.now = datetime.datetime(2026, 9, 1, 9, 30, tzinfo=BERLIN)
        slept: list[float] = []

        async def recording_sleep(seconds):
            slept.append(seconds)

        runner = PlanRunner(
            bridge,
            Plan.model_validate(DAY_PLAN),
            clock=clock,
            sleep=recording_sleep,
        )
        await runner.start()
        task = asyncio.create_task(runner.run())
        for _ in range(10):
            await asyncio.sleep(0)
        await runner.close()
        await task

        assert slept == [5.0]
        assert [w[2]["dimming"]["brightness"] for w in http.writes] == [60.0, 100.0]
        assert http.writes[1][2]["dynamics"]["duration"] == 1_800_000

    async def test_the_loop_wakes_for_midnight_when_days_gate_a_scenario(
        self, bridge, clock
    ):
        # `days` gates by the local calendar date, so a scenario can start
        # or stop claiming its scope at 00:00 with no step to wake for.
        runner = await make_runner(bridge, clock, WEEKEND_ONLY_PLAN)
        clock.now = datetime.datetime(2026, 9, 4, 23, 50, tzinfo=BERLIN)
        assert runner._seconds_until_next(clock.now) == pytest.approx(600.0)


RAMPED_ON_PLAN = {
    "version": 1,
    "defaults": {"catchup_ramp": "5s"},
    "scenario": [
        {
            "name": "day",
            "scope": ["room:Living Room"],
            "step": [
                {"at": "08:00", "set": {"on": True, "brightness": 80}},
                {"at": "12:00", "ramp": "1h", "set": {"on": True, "brightness": 100}},
            ],
        }
    ],
}


class TestProgressAfterHandChange:
    async def test_the_fade_after_a_yield_starts_where_the_human_left_it(
        self, bridge, http, clock
    ):
        # Yield to 10 at 08:30. The 09:00 step resumes the scope and fades to
        # 100 over an hour: at 09:15 the bulb reports 32, which is the fade
        # progressing from 10. Judged from the target instead, that report
        # would re-yield the scope -- and clear the start for the next step,
        # so every step after the first hand change would do the same.
        changes = FakeChanges()
        runner = await watched_runner(bridge, clock, changes, DAY_PLAN)
        await runner.catch_up()
        clock.now = datetime.datetime(2026, 9, 1, 8, 30, tzinfo=BERLIN)
        changes.report(LIGHT, 10.0, clock.now)
        assert runner.arbiter.is_yielded(GROUP_PATH)

        clock.now = datetime.datetime(2026, 9, 1, 9, 0, tzinfo=BERLIN)
        assert await runner.tick() == 1
        clock.now = datetime.datetime(2026, 9, 1, 9, 15, tzinfo=BERLIN)
        changes.report(LIGHT, 32.0, clock.now)
        assert not runner.arbiter.is_yielded(GROUP_PATH)

    async def test_reassert_does_not_fight_its_own_progress(self, bridge, http, clock):
        # One re-drive after the hand change, then silence: the bulb's
        # progress towards the re-driven target is ours, not five more PUTs.
        changes = FakeChanges()
        plan = DAY_PLAN | {
            "defaults": {"catchup_ramp": "5s", "on_manual_change": "reassert"}
        }
        runner = await watched_runner(bridge, clock, changes, plan)
        await runner.catch_up()
        clock.now = datetime.datetime(2026, 9, 1, 9, 0, tzinfo=BERLIN)
        await runner.tick()
        clock.advance(minutes=5)
        changes.report(LIGHT, 10.0, clock.now)
        http.calls.clear()
        assert await runner.tick() == 1

        for brightness in (18.0, 26.0, 34.0, 42.0, 50.0):
            clock.advance(minutes=5)
            changes.report(LIGHT, brightness, clock.now)
            assert await runner.tick() == 0
        assert len(http.writes) == 1

    async def test_a_fade_from_a_switch_off_starts_from_the_interrupted_target(
        self, bridge, http, clock
    ):
        # A switch-off leaves the bridge's brightness at the interrupted
        # fade's target (tests/fixtures/plan_probe.json), so the fade that
        # follows has a start to judge against: progress consistent with
        # 80 -> 100 over an hour is ours, and a second switch-off is seen.
        changes = FakeChanges()
        clock.now = datetime.datetime(2026, 9, 1, 9, 0, tzinfo=BERLIN)
        runner = await watched_runner(bridge, clock, changes, RAMPED_ON_PLAN)
        await runner.catch_up()
        clock.advance(hours=1)
        changes.report(LIGHT, None, clock.now, delta={"on": {"on": False}})
        clock.now = datetime.datetime(2026, 9, 1, 12, 0, tzinfo=BERLIN)
        assert await runner.tick() == 1
        assert http.writes[-1][2]["on"] == {"on": True}

        clock.advance(minutes=15)
        changes.report(LIGHT, 85.0, clock.now)
        assert not runner.arbiter.is_yielded(GROUP_PATH)
        changes.report(LIGHT, None, clock.now, delta={"on": {"on": False}})
        assert runner.arbiter.is_yielded(GROUP_PATH)


class TestReleaseAfterRuleHold:
    async def test_releasing_a_mode_that_held_through_its_rule_ends_the_yield(
        self, sensor_bridge, http, clock
    ):
        # The scope's owner is the rule hold, `movie/motion:...`, not the
        # mode's bare name; the release still has to find it.
        changes = FakeChanges()
        plan = copy.deepcopy(RULE_PLAN)
        plan["scenario"][1]["activate_on"] = "signal:movie_started"
        plan["scenario"][1]["release_on"] = "signal:movie_ended"
        plan["scenario"][1]["rule"][0]["hold"] = "6h"
        runner = await rule_runner(sensor_bridge, clock, changes, plan)
        runner.fire("movie_started")
        changes.deliver(motion(clock.now))
        await runner.tick()
        clock.advance(minutes=1)
        changes.report(LIGHT, 95.0, clock.now)
        assert runner.arbiter.is_yielded(GROUP_PATH)
        http.calls.clear()

        clock.advance(minutes=1)
        runner.fire("movie_ended")
        assert await runner.tick() == 1
        assert http.writes[0][2]["dimming"]["brightness"] == 80
        assert not runner.arbiter.is_yielded(GROUP_PATH)


class TestCloseDuringSettle:
    async def test_close_during_the_catch_up_wait_writes_nothing_more(
        self, bridge, http, clock
    ):
        # The wait after catch-up is a plain sleep in production. A close()
        # that lands inside it must not be followed by a tick that writes
        # after the caller was told the runner had stopped.
        clock.now = datetime.datetime(2026, 9, 1, 9, 30, tzinfo=BERLIN)
        gate = asyncio.Event()

        async def gated_sleep(_seconds):
            await gate.wait()

        runner = PlanRunner(
            bridge, Plan.model_validate(DAY_PLAN), clock=clock, sleep=gated_sleep
        )
        await runner.start()
        task = asyncio.create_task(runner.run())
        for _ in range(5):
            await asyncio.sleep(0)
        assert len(http.writes) == 1

        await runner.close()
        gate.set()
        await asyncio.wait_for(task, timeout=1.0)
        assert len(http.writes) == 1


LONG_FADE_PLAN = {
    "version": 1,
    "defaults": {"catchup_ramp": "5s"},
    "scenario": [
        {
            "name": "day",
            "scope": ["room:Living Room"],
            "step": [
                {"at": "08:00", "set": {"brightness": 100}},
                {"at": "09:00", "ramp": "100m", "set": {"brightness": 20}},
            ],
        }
    ],
}


def lit_resources(brightness):
    """Build the runner's bridge, with the lamp carrying a real state to fold."""
    room, lamp = bridge_resources()
    return [room, lamp | {"on": {"on": True}, "dimming": {"brightness": brightness}}]


def light_frame(brightness, event_id):
    return SSEFrame(
        event_id=event_id,
        received_at=datetime.datetime.now(datetime.UTC),
        events=[
            {
                "id": f"event-{event_id}",
                "type": "update",
                "creationtime": "2026-09-01T09:30:00Z",
                "data": [
                    {
                        "id": LIGHT,
                        "type": "light",
                        "dimming": {"brightness": brightness},
                    }
                ],
            }
        ],
    )


class TestWithRealState:
    """Through a real HueState, not a fake: the attribution it applies is the point.

    The state layer marks every report on a light as ``origin="self"`` while a
    fade this client issued is running, plus a grace period. For the runner's
    hundred-minute fades that window would mask a wall switch for the whole
    ramp, so these pin what actually reaches the runner and what it does with
    it. A change to ``HueState._matching_command`` shows up here.
    """

    async def mid_fade(self, hue, clock):
        http = StateHttp([lit_resources(100)])
        hue._http = http
        clock.now = datetime.datetime(2026, 9, 1, 8, 30, tzinfo=BERLIN)
        state = hue.state
        await state.__aenter__()
        runner = PlanRunner(
            hue,
            Plan.model_validate(LONG_FADE_PLAN),
            changes=state,
            clock=clock,
            sleep=noop_sleep,
        )
        await runner.start()
        await runner.catch_up()
        clock.now = datetime.datetime(2026, 9, 1, 9, 0, tzinfo=BERLIN)
        await runner.tick()
        return http, state, runner

    async def deliver(self, http, state, frame):
        delivered = asyncio.Event()
        with state.on_change(lambda _change: delivered.set()):
            await http.connections[0].put(frame)
            await asyncio.wait_for(delivered.wait(), timeout=1.0)

    async def test_a_wall_switch_mid_fade_is_seen_despite_the_window(self, hue, clock):
        http, state, runner = await self.mid_fade(hue, clock)
        try:
            # Thirty minutes into 100 -> 20 over 100 minutes: expected 76.
            clock.advance(minutes=30)
            await self.deliver(http, state, light_frame(95.0, "1:1"))
            assert runner.arbiter.is_yielded(GROUP_PATH)
        finally:
            await runner.close()
            await state.__aexit__(None, None, None)

    async def test_progress_consistent_with_the_fade_is_ours(self, hue, clock):
        http, state, runner = await self.mid_fade(hue, clock)
        try:
            clock.advance(minutes=30)
            await self.deliver(http, state, light_frame(76.0, "1:1"))
            assert not runner.arbiter.is_yielded(GROUP_PATH)
        finally:
            await runner.close()
            await state.__aexit__(None, None, None)

    async def test_the_echo_of_our_own_target_is_ours(self, hue, clock):
        # The bridge reports a transition's target the moment it accepts the
        # write. Against the fade's expectation at that instant it is a jump.
        http, state, runner = await self.mid_fade(hue, clock)
        try:
            await self.deliver(http, state, light_frame(20.0, "1:1"))
            assert not runner.arbiter.is_yielded(GROUP_PATH)
        finally:
            await runner.close()
            await state.__aexit__(None, None, None)


OFF_AT_NIGHT_PLAN = {
    "version": 1,
    "defaults": {"catchup_ramp": "5s"},
    "scenario": [
        {
            "name": "day",
            "scope": ["room:Living Room"],
            "step": [
                {"at": "08:00", "set": {"on": True, "brightness": 80}},
                {"at": "23:00", "set": {"on": False}},
            ],
        }
    ],
}


class ClosingClient:
    """A client whose first write closes the runner from the inside."""

    def __init__(self, hue, http):
        self._hue = hue
        self._http = http
        self.runner: PlanRunner | None = None
        self.sent = 0

    @property
    def http(self) -> Any:
        return self

    def __getattr__(self, name):
        return getattr(self._http, name)

    async def put(self, path, data):
        self.sent += 1
        if self.sent == 1:
            assert self.runner is not None
            await self.runner.close()
        return await self._http.put(path, data)

    async def snapshot(self):
        return await self._hue.snapshot()


class TestBeliefAfterFailure:
    async def test_a_refused_write_retries_from_where_the_light_was(
        self, hue, http, clock
    ):
        # The plan switched the room off at 23:00 and the bridge's echo was
        # explained, so the last *foreign* report still says "on" from the
        # morning before. When the next morning's write is refused, the retry
        # must start from where the plan's own fade had left the light -- off
        # -- and carry `on`, or the room stays dark for the whole step.
        http.queue("/clip/v2/resource", envelope(*bridge_resources()))
        changes = FakeChanges()
        clock.now = datetime.datetime(2026, 9, 1, 7, 0, tzinfo=BERLIN)
        client = BrokenClient(hue, http, fails={4})
        runner = PlanRunner(
            client,
            Plan.model_validate(OFF_AT_NIGHT_PLAN),
            changes=changes,
            clock=clock,
            sleep=noop_sleep,
        )
        await runner.start()
        await runner.catch_up()
        clock.advance(minutes=10)
        changes.report(LIGHT, None, clock.now, delta={"on": {"on": True}})
        clock.now = datetime.datetime(2026, 9, 1, 8, 0, tzinfo=BERLIN)
        await runner.tick()
        clock.now = datetime.datetime(2026, 9, 1, 23, 0, tzinfo=BERLIN)
        await runner.tick()
        assert http.writes[-1][2]["on"] == {"on": False}
        changes.report(LIGHT, None, clock.now, delta={"on": {"on": False}})
        assert not runner.arbiter.is_yielded(GROUP_PATH)

        clock.now = datetime.datetime(2026, 9, 2, 8, 0, tzinfo=BERLIN)
        assert await runner.tick() == 0
        http.calls.clear()
        assert await runner.tick() == 1
        assert http.writes[0][2]["on"] == {"on": True}

    async def chained_after_a_refusal(self, hue, http, clock, fails):
        http.queue("/clip/v2/resource", envelope(*bridge_resources()))
        client = BrokenClient(hue, http, fails=fails)
        runner = PlanRunner(
            client,
            Plan.model_validate(CHAINED_PLAN),
            clock=clock,
            sleep=noop_sleep,
        )
        await runner.start()
        await runner.catch_up()
        clock.now = datetime.datetime(2026, 9, 1, 22, 0, tzinfo=BERLIN)
        await runner.tick()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return runner

    async def test_a_refused_first_segment_is_retried_as_a_chain(
        self, hue, http, clock
    ):
        # The retry knows where the light was, so it is chained again rather
        # than degraded to one ceiling-length fade. The catch-up fade stays
        # in force meanwhile: it is what the bridge is still running.
        runner = await self.chained_after_a_refusal(hue, http, clock, fails={2})
        fade = runner.arbiter.state_of(GROUP_PATH).fade
        assert fade is not None
        assert fade.target.brightness == 100
        http.calls.clear()

        assert await runner.tick() == 1
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert len(http.writes) == 2
        assert http.writes[1][2]["dimming"]["brightness"] == 20

    async def test_a_failed_chain_tail_is_retried_as_a_chain(self, hue, http, clock):
        # The first segment went out and the tail did not. The fade is
        # forgotten, but where the first segment took the light is kept, so
        # the retry chains from there.
        runner = await self.chained_after_a_refusal(hue, http, clock, fails={3})
        assert runner.arbiter.state_of(GROUP_PATH).fade is None
        http.calls.clear()

        assert await runner.tick() == 1
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert len(http.writes) == 2
        assert http.writes[1][2]["dimming"]["brightness"] == 20

    async def test_a_switch_off_after_a_refused_write_remembers_the_held_target(
        self, hue, http, clock
    ):
        # The refused write left the previous fade running on the bridge, so
        # a switch-off leaves its target behind -- not the level the fade had
        # reached when the write was refused.
        http.queue("/clip/v2/resource", envelope(*bridge_resources()))
        plan: dict[str, Any] = copy.deepcopy(LONG_FADE_PLAN)
        plan["scenario"].append(
            {
                "name": "movie",
                "scope": ["room:Living Room"],
                "priority": 20,
                "activate_on": "signal:movie_started",
                "set": {"brightness": 8},
            }
        )
        changes = FakeChanges()
        runner = PlanRunner(
            BrokenClient(hue, http, fails={3}),
            Plan.model_validate(plan),
            changes=changes,
            clock=clock,
            sleep=noop_sleep,
        )
        await runner.start()
        clock.now = datetime.datetime(2026, 9, 1, 8, 30, tzinfo=BERLIN)
        await runner.catch_up()
        clock.now = datetime.datetime(2026, 9, 1, 9, 0, tzinfo=BERLIN)
        await runner.tick()
        clock.advance(minutes=30)
        runner.fire("movie_started")
        assert await runner.tick() == 0

        clock.advance(minutes=1)
        changes.deliver(change(LIGHT, None, clock.now, delta={"on": {"on": False}}))
        reported = runner.arbiter.state_of(GROUP_PATH).reported
        assert reported == Action(on=False, brightness=20.0)


class TestCloseMidPass:
    async def test_close_during_one_scopes_write_stops_the_pass(self, hue, http, clock):
        http.queue("/clip/v2/resource", envelope(*bridge_resources()))
        plan = {
            "version": 1,
            "scenario": [
                {
                    "name": "room",
                    "scope": ["room:Living Room", "light:Corner Lamp"],
                    "step": [{"at": "07:00", "set": {"brightness": 50}}],
                }
            ],
        }
        client = ClosingClient(hue, http)
        runner = PlanRunner(
            client, Plan.model_validate(plan), clock=clock, sleep=noop_sleep
        )
        client.runner = runner
        await runner.start()
        assert await runner.catch_up() == 1
        assert len(http.writes) == 1


class TestSwitchOffMemory:
    """What a switch-off leaves behind, as measured in tests/fixtures/plan_probe.json.

    The bridge's brightness is a transition's target from the moment it accepts
    the write, and a bare switch-off leaves it there. So the fade after a
    switch-off can start from a known brightness, and a hand change during it
    is no longer invisible.
    """

    async def fading_then_switched_off(self, bridge, clock, changes):
        clock.now = datetime.datetime(2026, 9, 1, 9, 0, tzinfo=BERLIN)
        runner = await watched_runner(bridge, clock, changes, DAY_PLAN)
        await runner.catch_up()
        await runner.tick()
        clock.advance(minutes=30)
        changes.deliver(change(LIGHT, None, clock.now, delta={"on": {"on": False}}))
        assert runner.arbiter.is_yielded(GROUP_PATH)
        return runner

    async def test_a_switch_off_keeps_the_interrupted_fades_target(self, bridge, clock):
        runner = await self.fading_then_switched_off(bridge, clock, FakeChanges())
        reported = runner.arbiter.state_of(GROUP_PATH).reported
        assert reported == Action(on=False, brightness=100.0)

    async def test_a_jump_during_the_fade_after_a_switch_off_is_seen(
        self, bridge, http, clock
    ):
        changes = FakeChanges()
        runner = await self.fading_then_switched_off(bridge, clock, changes)
        clock.now = datetime.datetime(2026, 9, 1, 22, 0, tzinfo=BERLIN)
        assert await runner.tick() == 1
        assert not runner.arbiter.is_yielded(GROUP_PATH)

        # Ten minutes into 100 -> 20 over thirty, the fade expects 73.
        clock.advance(minutes=10)
        changes.report(LIGHT, 40.0, clock.now)
        assert runner.arbiter.is_yielded(GROUP_PATH)

    async def test_progress_during_the_fade_after_a_switch_off_is_still_ours(
        self, bridge, clock
    ):
        changes = FakeChanges()
        runner = await self.fading_then_switched_off(bridge, clock, changes)
        clock.now = datetime.datetime(2026, 9, 1, 22, 0, tzinfo=BERLIN)
        await runner.tick()
        clock.advance(minutes=10)
        changes.report(LIGHT, 73.0, clock.now)
        assert not runner.arbiter.is_yielded(GROUP_PATH)

    async def chained_runner(self, bridge, clock, changes):
        clock.now = datetime.datetime(2026, 9, 1, 21, 0, tzinfo=BERLIN)
        runner = PlanRunner(
            bridge,
            Plan.model_validate(CHAINED_PLAN),
            changes=changes,
            clock=clock,
            sleep=blocking_sleep,
        )
        await runner.start()
        await runner.catch_up()
        clock.now = datetime.datetime(2026, 9, 1, 22, 0, tzinfo=BERLIN)
        assert await runner.tick() == 1
        return runner

    async def test_a_switch_off_during_a_chained_fade_keeps_the_segments_waypoint(
        self, bridge, clock
    ):
        # 100 -> 20 over three hours is two segments; the bridge was only
        # ever given the first one's waypoint, 60, and that is what it holds.
        changes = FakeChanges()
        runner = await self.chained_runner(bridge, clock, changes)
        clock.advance(minutes=50)
        changes.deliver(change(LIGHT, None, clock.now, delta={"on": {"on": False}}))
        reported = runner.arbiter.state_of(GROUP_PATH).reported
        assert reported == Action(on=False, brightness=60.0)
        await runner.close()

    async def test_a_switch_off_during_the_last_segment_keeps_the_target(
        self, bridge, clock
    ):
        changes = FakeChanges()
        runner = await self.chained_runner(bridge, clock, changes)
        clock.advance(minutes=110)
        changes.deliver(change(LIGHT, None, clock.now, delta={"on": {"on": False}}))
        reported = runner.arbiter.state_of(GROUP_PATH).reported
        assert reported == Action(on=False, brightness=20.0)
        await runner.close()

    async def test_a_hand_switch_on_after_the_plans_off_step_keeps_its_level(
        self, bridge, clock
    ):
        # The last hand report is stale once the plan has driven the light
        # since; the level the off step started from is what the bridge holds.
        plan = {
            "version": 1,
            "defaults": {"catchup_ramp": "5s"},
            "scenario": [
                {
                    "name": "day",
                    "scope": ["room:Living Room"],
                    "step": [
                        {"at": "08:00", "set": {"on": True, "brightness": 80}},
                        {"at": "12:00", "set": {"on": True, "brightness": 100}},
                        {"at": "23:00", "set": {"on": False}},
                    ],
                }
            ],
        }
        changes = FakeChanges()
        clock.now = datetime.datetime(2026, 9, 1, 9, 0, tzinfo=BERLIN)
        runner = await watched_runner(bridge, clock, changes, plan)
        await runner.catch_up()
        clock.advance(minutes=10)
        changes.report(LIGHT, 40.0, clock.now)
        assert runner.arbiter.is_yielded(GROUP_PATH)

        clock.now = datetime.datetime(2026, 9, 1, 12, 0, tzinfo=BERLIN)
        assert await runner.tick() == 1
        clock.now = datetime.datetime(2026, 9, 1, 23, 0, tzinfo=BERLIN)
        assert await runner.tick() == 1
        clock.advance(minutes=30)
        changes.deliver(change(LIGHT, None, clock.now, delta={"on": {"on": True}}))
        reported = runner.arbiter.state_of(GROUP_PATH).reported
        assert reported == Action(on=True, brightness=100.0)

    async def test_a_brightness_the_fade_never_asked_for_is_a_human(
        self, bridge, clock
    ):
        # A fade that only switches the light on has no brightness to explain
        # a dimming report with; waving one through left the dial unremembered.
        plan = {
            "version": 1,
            "defaults": {"catchup_ramp": "5s"},
            "scenario": [
                {
                    "name": "day",
                    "scope": ["room:Living Room"],
                    "step": [{"at": "08:00", "set": {"on": True}}],
                }
            ],
        }
        changes = FakeChanges()
        clock.now = datetime.datetime(2026, 9, 1, 9, 0, tzinfo=BERLIN)
        runner = await watched_runner(bridge, clock, changes, plan)
        await runner.catch_up()
        clock.advance(seconds=10)
        changes.report(LIGHT, 70.0, clock.now)
        assert runner.arbiter.is_yielded(GROUP_PATH)
        reported = runner.arbiter.state_of(GROUP_PATH).reported
        assert reported == Action(on=True, brightness=70.0)

    async def test_a_report_naming_only_on_keeps_the_remembered_brightness(
        self, bridge, clock
    ):
        changes = FakeChanges()
        runner = await watched_runner(bridge, clock, changes, DAY_PLAN)
        await runner.catch_up()
        clock.advance(seconds=10)
        changes.report(LIGHT, 50.0, clock.now)
        clock.advance(seconds=10)
        changes.deliver(change(LIGHT, None, clock.now, delta={"on": {"on": True}}))
        reported = runner.arbiter.state_of(GROUP_PATH).reported
        assert reported == Action(on=True, brightness=50.0)


def hold_of(runner: PlanRunner):
    hold = runner.arbiter.state_of(GROUP_PATH).hold
    assert hold is not None
    return hold


class TestLevelRules:
    """A level fires on the crossing, releases past a band, never on a repeat."""

    async def test_a_first_report_below_the_threshold_fires(
        self, sensor_bridge, http, clock
    ):
        changes = FakeChanges()
        runner = await rule_runner(sensor_bridge, clock, changes, level_plan())
        http.calls.clear()

        changes.deliver(light_level(clock.now, lux=8))
        assert await runner.tick() == 1
        assert http.writes[0][2]["dimming"]["brightness"] == 15

    async def test_a_first_report_above_the_threshold_does_not_fire(
        self, sensor_bridge, http, clock
    ):
        changes = FakeChanges()
        runner = await rule_runner(sensor_bridge, clock, changes, level_plan())
        http.calls.clear()

        changes.deliver(light_level(clock.now, lux=300))
        assert await runner.tick() == 0

    async def test_a_repeat_below_the_threshold_does_not_refire(
        self, sensor_bridge, http, clock
    ):
        changes = FakeChanges()
        runner = await rule_runner(sensor_bridge, clock, changes, level_plan())
        changes.deliver(light_level(clock.now, lux=8))
        await runner.tick()
        placed = runner.arbiter.state_of(GROUP_PATH).hold
        assert placed is not None
        http.calls.clear()

        clock.advance(minutes=3)
        changes.deliver(light_level(clock.now, lux=6))
        assert await runner.tick() == 0
        assert runner.arbiter.state_of(GROUP_PATH).hold is placed

    async def test_crossing_back_past_the_band_starts_the_hold_clock(
        self, sensor_bridge, http, clock
    ):
        changes = FakeChanges()
        runner = await rule_runner(sensor_bridge, clock, changes, level_plan())
        changes.deliver(light_level(clock.now, lux=8))
        await runner.tick()
        assert hold_of(runner).until is None

        # 30 lux times the band's factor of five is 150; 316 is well past it.
        clock.advance(minutes=3)
        changes.deliver(light_level(clock.now, lux=316))
        assert hold_of(runner).until is not None
        assert runner._seconds_until_next(clock.now) == pytest.approx(90.0)

    async def test_inside_the_band_neither_fires_nor_ends(
        self, sensor_bridge, http, clock
    ):
        changes = FakeChanges()
        runner = await rule_runner(sensor_bridge, clock, changes, level_plan())
        changes.deliver(light_level(clock.now, lux=8))
        await runner.tick()
        http.calls.clear()

        clock.advance(minutes=3)
        changes.deliver(light_level(clock.now, lux=63))
        assert await runner.tick() == 0
        assert hold_of(runner).until is None

    async def test_an_invalid_reading_is_ignored_and_not_remembered(
        self, sensor_bridge, http, clock
    ):
        changes = FakeChanges()
        runner = await rule_runner(sensor_bridge, clock, changes, level_plan())
        http.calls.clear()

        changes.deliver(light_level(clock.now, lux=8, valid=False))
        assert await runner.tick() == 0
        # Still a first reading: it fires as a crossing from the far side.
        changes.deliver(light_level(clock.now, lux=8))
        assert await runner.tick() == 1

    async def test_a_report_without_a_level_is_ignored(
        self, sensor_bridge, http, clock
    ):
        changes = FakeChanges()
        runner = await rule_runner(sensor_bridge, clock, changes, level_plan())
        http.calls.clear()

        changes.deliver(
            sensor_change(LIGHT_LEVEL, "light_level", clock.now, enabled=True)
        )
        assert await runner.tick() == 0

    async def test_the_deprecated_top_level_field_is_a_fallback(
        self, sensor_bridge, http, clock
    ):
        changes = FakeChanges()
        runner = await rule_runner(sensor_bridge, clock, changes, level_plan())
        http.calls.clear()

        raw = round(raw_light_level(8))
        changes.deliver(
            sensor_change(
                LIGHT_LEVEL,
                "light_level",
                clock.now,
                light={"light_level": raw, "light_level_valid": True},
            )
        )
        assert await runner.tick() == 1

    async def test_an_above_rule_mirrors_below(self, sensor_bridge, http, clock):
        changes = FakeChanges()
        plan = rule_plan(when="light_level:Hall sensor", above=200)
        runner = await rule_runner(sensor_bridge, clock, changes, plan)
        http.calls.clear()

        changes.deliver(light_level(clock.now, lux=500))
        assert await runner.tick() == 1
        hold = runner.arbiter.state_of(GROUP_PATH).hold
        assert hold is not None
        assert hold.until is None

        # 200 lux over the band's factor of five is 40; 100 is still inside.
        clock.advance(minutes=1)
        changes.deliver(light_level(clock.now, lux=100))
        assert hold_of(runner).until is None
        changes.deliver(light_level(clock.now, lux=30))
        assert hold_of(runner).until is not None

    async def test_a_still_dark_report_does_not_take_back_a_yielded_scope(
        self, sensor_bridge, http, clock
    ):
        # The bug the crossing rule prevents: a periodic "still dark" report
        # refreshing the hold and un-yielding a room someone dimmed by hand.
        changes = FakeChanges()
        runner = await rule_runner(sensor_bridge, clock, changes, level_plan())
        changes.deliver(light_level(clock.now, lux=8))
        await runner.tick()
        clock.advance(seconds=10)
        changes.report(LIGHT, 50.0, clock.now)
        assert runner.arbiter.is_yielded(GROUP_PATH)
        http.calls.clear()

        clock.advance(minutes=3)
        changes.deliver(light_level(clock.now, lux=6))
        assert await runner.tick() == 0
        assert runner.arbiter.is_yielded(GROUP_PATH)


class TestLevelEdge:
    BELOW = Threshold(raw=20_000, side="below")
    ABOVE = Threshold(raw=20_000, side="above")

    @pytest.mark.parametrize(
        ("previous", "level", "expected"),
        [
            (None, 15_000, "start"),
            (None, 30_000, "end"),
            (None, 25_000, None),
            (15_000, 16_000, None),
            (25_000, 15_000, "start"),
            (15_000, 30_000, "end"),
            (15_000, 25_000, None),
            (30_000, 31_000, None),
            (30_000, 25_000, None),
        ],
    )
    def test_below(self, previous, level, expected):
        assert _level_edge(previous, level, self.BELOW) == expected

    @pytest.mark.parametrize(
        ("previous", "level", "expected"),
        [
            (None, 25_000, "start"),
            (None, 10_000, "end"),
            (25_000, 26_000, None),
            (25_000, 10_000, "end"),
            (25_000, 15_000, None),
            (10_000, 25_000, "start"),
        ],
    )
    def test_above(self, previous, level, expected):
        assert _level_edge(previous, level, self.ABOVE) == expected
