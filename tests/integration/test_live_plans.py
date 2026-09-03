"""The plan runner against a real bridge, in one vetted room.

Everything here drives `PLAN_ROOM` and nothing else. The unit suite proves the
runner's arithmetic against fakes and a simulated clock; these tests prove the
handful of things only hardware can: that names bind to the lights the operator
meant, that one room write really is one `grouped_light` request that reaches
every member, what the state layer's attribution window actually delivers for a
hand change made mid-fade, and that the bridge accepts a ceiling-length
transition on a group.

Every scenario is flat -- a `set` with no `step` -- so nothing depends on the
wall clock, the date or the sun. Plans are built inline so the room name comes
from `PLAN_ROOM` rather than a file. Written as TOML, the plan the yield tests
run is:

    [defaults]
    catchup_ramp = "1s"

    [[scenario]]
    name = "flat"
    scope = ["room:Arbeitszimmer"]
    set = { on = true, brightness = 30 }

    [[scenario]]
    name = "lift"
    scope = ["room:Arbeitszimmer"]
    priority = 10

    [[scenario.rule]]
    when = "signal:lift"
    ramp = "40s"
    set = { brightness = 90 }

A `button:` rule cannot be exercised here: nothing in a test can press the
dimmer. `test_resolve_binds_the_room_and_the_dimmer` covers the binding half,
and the unit suite's `TestRules` the semantics.
"""

import asyncio
from collections.abc import AsyncIterator, Callable

import pytest

from huepy import BridgeConnectionError, Hue, PlanError, PlanRunner, models
from huepy.plans.resolve import resolve
from huepy.plans.schema import Plan
from huepy.state.records import Change

from .conftest import PLAN_ROOM, PLAN_ROOM_IGNORED, Sent

pytestmark = pytest.mark.integration

PLAN_DIMMER = "Dimmer Arbeitszimmer"
PLAN_LEVEL_SENSOR = "Bewegungssensor Bad"
"""The one motion sensor on the reference bridge whose light level is enabled."""
RESOURCE_ROOT = "/clip/v2/resource"
SETTLE = 2.0
"""Seconds between a write and reading the light back, as in test_live_write."""
EVENT_TIMEOUT = 15.0
CATCHUP_RAMP = "1s"
LONG_RAMP = "40s"
"""Long enough that a hand change lands mid-fade, short enough to bound a test."""
LONG_RAMP_MILLISECONDS = 40_000
PROGRESS_WAIT = 8.0
"""Seconds of a fade to let progress reports arrive before the hand acts."""
FLAT_BRIGHTNESS = 30.0
LIFT_BRIGHTNESS = 90.0
HAND_BRIGHTNESS = 10.0
"""Far enough below any point of a 30 -> 90 fade to be a jump whenever it lands."""
CEILING_MILLISECONDS = 6_000_000


def flat_plan(
    *,
    brightness: float = FLAT_BRIGHTNESS,
    ramp: str | None = None,
    lift_ramp: str | None = None,
) -> Plan:
    """Build the module's plan: a flat resting state, and optionally a lift rule."""
    flat: dict[str, object] = {
        "name": "flat",
        "scope": [f"room:{PLAN_ROOM}"],
        "set": {"on": True, "brightness": brightness},
    }
    if ramp is not None:
        flat["ramp"] = ramp
    scenarios: list[dict[str, object]] = [flat]
    if lift_ramp is not None:
        scenarios.append(
            {
                "name": "lift",
                "scope": [f"room:{PLAN_ROOM}"],
                "priority": 10,
                "rule": [
                    {
                        "when": "signal:lift",
                        "ramp": lift_ramp,
                        "set": {"brightness": LIFT_BRIGHTNESS},
                    }
                ],
            }
        )
    return Plan.model_validate(
        {
            "version": 1,
            "defaults": {"catchup_ramp": CATCHUP_RAMP, "on_manual_change": "yield"},
            "scenario": scenarios,
        }
    )


def group_path(room: models.Room) -> str:
    """Build the write path the runner uses for a room, through public accessors."""
    service = room.service_id(models.ResourceType.GROUPED_LIGHT)
    assert service is not None
    return f"{RESOURCE_ROOT}/grouped_light/{service}"


def brightness_of(change: Change) -> float | None:
    """Pull the brightness out of a change's delta, if it carried one."""
    dimming = change.delta.get("dimming")
    if not isinstance(dimming, dict):
        return None
    value = dimming.get("brightness")
    return float(value) if isinstance(value, (int, float)) else None


def reported_on(change: Change) -> bool | None:
    """Pull the power state out of a change's delta, if it carried one."""
    on = change.delta.get("on")
    if not isinstance(on, dict):
        return None
    value = on.get("on")
    return value if isinstance(value, bool) else None


async def wait_until(condition: Callable[[], bool], *, what: str) -> None:
    """Poll a condition the runner reaches on its own dispatch task.

    Handler order between a test's queue and the runner's `_observe` is not
    guaranteed, so runner state is never asserted straight after a dequeue.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + EVENT_TIMEOUT
    while not condition():
        if loop.time() > deadline:
            msg = f"timed out waiting for {what}"
            raise AssertionError(msg)
        await asyncio.sleep(0.05)


async def next_change(
    queue: "asyncio.Queue[Change]", matches: Callable[[Change], bool]
) -> Change:
    """Drain a queue until a change matches."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + EVENT_TIMEOUT
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            msg = "timed out waiting for a matching change"
            raise AssertionError(msg)
        change = await asyncio.wait_for(queue.get(), remaining)
        if matches(change):
            return change


@pytest.fixture
async def hand(opt_in: None) -> AsyncIterator[Hue]:
    """Open a second, stateless client: a human at a switch, to the first client."""
    del opt_in
    client = Hue()
    try:
        await client.start()
    except (BridgeConnectionError, ValueError) as exc:  # pragma: no cover - hardware
        pytest.skip(f"no reachable bridge: {exc}")
    try:
        yield client
    finally:
        await client.close()


async def dimmable_members(room: models.Room) -> list[models.Light]:
    """List the room's real, dimmable lights: what a test reads back and touches."""
    members = [
        light
        for light in await room.lights()
        if light.dimming is not None and light.name not in PLAN_ROOM_IGNORED
    ]
    if not members:
        pytest.skip(f"{PLAN_ROOM!r} has no dimmable lights")
    return members


async def test_resolve_binds_the_room_and_the_dimmer(
    hue: Hue,
    arbeitszimmer: models.Room,
    request_counter: Callable[[Hue], list[Sent]],
):
    """Names bind to what the operator meant, in one read and no writes."""
    members = await arbeitszimmer.lights()
    devices = await hue.api.devices.list()
    dimmer = next((d for d in devices if d.name == PLAN_DIMMER), None)
    if dimmer is None:
        pytest.skip(f"no device named {PLAN_DIMMER!r}")
    sensor = next((d for d in devices if d.name == PLAN_LEVEL_SENSOR), None)
    if sensor is None:
        pytest.skip(f"no device named {PLAN_LEVEL_SENSOR!r}")
    buttons = {s.rid for s in dimmer.services if s.rtype == "button"}
    levels = {s.rid for s in sensor.services if s.rtype == "light_level"}

    plan = Plan.model_validate(
        {
            "version": 1,
            "scenario": [
                {
                    "name": "room",
                    "scope": [f"room:{PLAN_ROOM}"],
                    "set": {"on": True},
                    "rule": [
                        {"when": f"button:{PLAN_DIMMER}", "set": {"on": False}},
                        {
                            "when": f"light_level:{PLAN_LEVEL_SENSOR}",
                            "below": 30,
                            "set": {"on": True, "brightness": 40},
                        },
                    ],
                }
            ],
        }
    )
    calls = request_counter(hue)
    resolved = await resolve(hue, plan)

    binding = resolved.scopes["room"][0]
    assert binding.path == group_path(arbeitszimmer)
    assert set(binding.light_ids) == {light.id for light in members}
    assert len(binding.light_ids) == len(members)
    assert set(resolved.triggers[f"button:{PLAN_DIMMER}"].resource_ids) == buttons
    assert buttons
    level = resolved.triggers[f"light_level:{PLAN_LEVEL_SENSOR}"]
    assert set(level.resource_ids) == levels
    assert levels
    assert resolved.warnings == (), "the reference sensor's level is enabled"
    assert calls == [Sent("GET", RESOURCE_ROOT, None)]


async def test_an_unknown_room_names_the_real_ones(
    hue: Hue, arbeitszimmer: models.Room
):
    del arbeitszimmer
    plan = Plan.model_validate(
        {
            "version": 1,
            "scenario": [{"name": "s", "scope": ["room:Nope"], "set": {"on": True}}],
        }
    )
    with pytest.raises(PlanError, match=PLAN_ROOM):
        await resolve(hue, plan)


async def test_catch_up_is_one_grouped_light_put_that_reaches_every_member(
    hue: Hue,
    arbeitszimmer_restored: models.Room,
    request_counter: Callable[[Hue], list[Sent]],
):
    """One room write is one broadcast, and every member arrives."""
    room = arbeitszimmer_restored
    members = await dimmable_members(room)
    runner = PlanRunner(hue, flat_plan(brightness=60.0))
    await runner.start()
    calls = request_counter(hue)
    try:
        assert await runner.catch_up() == 1
        puts = [c for c in calls if c.method == "PUT"]
        assert puts == [
            Sent(
                "PUT",
                group_path(room),
                {
                    "on": {"on": True},
                    "dimming": {"brightness": 60.0},
                    "dynamics": {"duration": 1000},
                },
            )
        ]

        await asyncio.sleep(SETTLE)
        for light in members:
            fresh = await light.refresh()
            assert fresh.is_on, light.name
            assert fresh.brightness == pytest.approx(60.0, abs=3.0), light.name

        # Waking up again finds the same target in force and sends nothing.
        assert await runner.tick() == 0
        assert len([c for c in calls if c.method == "PUT"]) == 1
    finally:
        await runner.close()


async def test_a_hand_switch_off_mid_fade_is_seen_through_the_window(
    hue: Hue,
    hand: Hue,
    arbeitszimmer_restored: models.Room,
    request_counter: Callable[[Hue], list[Sent]],
):
    """The state layer's window calls a hand switch-off ours; the runner does not.

    A member light switched off through another client, while this client's
    forty-second fade is running, arrives as `origin="self"` -- the window
    covers the whole fade -- with `observation="reported"`. That is exactly
    what PLANS.md says the runner must judge for itself, and here it does.
    """
    room = arbeitszimmer_restored
    light = (await dimmable_members(room))[0]
    path = group_path(room)
    calls = request_counter(hue)
    async with hue.state as state:
        runner = PlanRunner(hue, flat_plan(ramp=LONG_RAMP), changes=state)
        await runner.start()
        seen: asyncio.Queue[Change] = asyncio.Queue()
        _ = state.on_change(seen.put_nowait, resource_id=light.id)
        try:
            assert await runner.tick() == 1
            put = next(c for c in calls if c.method == "PUT")
            assert put.data is not None
            assert put.data["dynamics"] == {"duration": LONG_RAMP_MILLISECONDS}
            assert put.data["on"] == {"on": True}

            echo = await next_change(seen, lambda c: c.observation == "command_echo")
            assert echo.origin == "self"
            await asyncio.sleep(0.2)
            assert not runner.arbiter.is_yielded(path), "the echo must not yield"

            _ = await (await hand.api.lights.get(light.id)).turn_off()
            change = await next_change(seen, lambda c: reported_on(c) is False)
            assert change.origin == "self", "the window still covers the fade"
            assert change.observation == "reported"

            await wait_until(lambda: runner.arbiter.is_yielded(path), what="the yield")
            assert runner.arbiter.state_of(path).fade is None
            assert await runner.tick() == 0
            assert len([c for c in calls if c.method == "PUT"]) == 1
        finally:
            await runner.close()


async def test_a_hand_brightness_jump_mid_fade_yields_and_a_progress_report_does_not(
    hue: Hue,
    hand: Hue,
    arbeitszimmer_restored: models.Room,
    request_counter: Callable[[Hue], list[Sent]],
):
    """Real progress reports fit the fade's arithmetic; a jump does not.

    The pre-hand assertion is a measurement of `BRIGHTNESS_TOLERANCE` against
    real bulbs: if a progress report lands outside it, the failure message
    lists what was judged, and that is a finding for PLANS.md, not a reason to
    loosen the test. It found one: the room's `grouped_light` reports the
    average of its members' last readings, far off the ramp, and the runner
    now judges light reports only (`probe_plans.py` measured it).
    """
    room = arbeitszimmer_restored
    light = (await dimmable_members(room))[0]
    path = group_path(room)
    calls = request_counter(hue)
    async with hue.state as state:
        runner = PlanRunner(hue, flat_plan(lift_ramp=LONG_RAMP), changes=state)
        await runner.start()
        seen: asyncio.Queue[Change] = asyncio.Queue()
        _ = state.on_change(seen.put_nowait, resource_id=light.id)
        try:
            assert await runner.catch_up() == 1
            _ = await next_change(seen, lambda c: c.observation == "command_echo")
            await asyncio.sleep(SETTLE)

            runner.fire("lift")
            assert await runner.tick() == 1
            lift = [c for c in calls if c.method == "PUT"][1]
            assert lift.data == {
                "dimming": {"brightness": LIFT_BRIGHTNESS},
                "dynamics": {"duration": LONG_RAMP_MILLISECONDS},
            }, "the scope is known to be on, so `on` is not re-sent"
            fade = runner.arbiter.state_of(path).fade
            assert fade is not None
            assert fade.start is not None
            assert fade.start.brightness == FLAT_BRIGHTNESS

            _ = await next_change(seen, lambda c: c.observation == "command_echo")
            await asyncio.sleep(PROGRESS_WAIT)
            judged: list[tuple[str, float | None]] = []
            while not seen.empty():
                change = seen.get_nowait()
                judged.append(
                    (change.received_at.strftime("%H:%M:%S"), brightness_of(change))
                )
            assert not runner.arbiter.is_yielded(path), (
                f"a progress report fell outside the fade's own tolerance: {judged}"
            )

            _ = await (await hand.api.lights.get(light.id)).set(
                brightness=HAND_BRIGHTNESS
            )
            change = await next_change(
                seen,
                lambda c: (
                    (b := brightness_of(c)) is not None
                    and abs(b - HAND_BRIGHTNESS) <= 1.0
                ),
            )
            assert change.origin == "self"
            assert change.observation == "reported"

            await wait_until(lambda: runner.arbiter.is_yielded(path), what="the yield")
            scope = runner.arbiter.state_of(path)
            assert scope.fade is None
            assert scope.hold is None, "a hand change drops the overridden hold"
            assert await runner.tick() == 0
        finally:
            await runner.close()


async def test_a_long_ramp_is_chained_and_the_group_accepts_a_ceiling_segment(
    hue: Hue,
    arbeitszimmer_restored: models.Room,
    request_counter: Callable[[Hue], list[Sent]],
):
    """A 200-minute lift is two segments; the bridge takes 6,000,000 ms on a group.

    The durability probe measured the ceiling on `/light`. Whether
    `grouped_light` honours the same bound, and whether the runner's chain
    survives contact with a bridge, were unmeasured until this.
    """
    room = arbeitszimmer_restored
    path = group_path(room)
    sleeps: list[float] = []
    parked = asyncio.Event()

    async def recording_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        await parked.wait()

    runner = PlanRunner(hue, flat_plan(lift_ramp="200m"), sleep=recording_sleep)
    await runner.start()
    calls = request_counter(hue)
    try:
        assert await runner.catch_up() == 1
        await asyncio.sleep(SETTLE)
        runner.fire("lift")
        assert await runner.tick() == 1
        puts = [c for c in calls if c.method == "PUT"]
        assert puts[1].path == path
        assert puts[1].data == {
            "dimming": {"brightness": 60.0},
            "dynamics": {"duration": CEILING_MILLISECONDS},
        }, "the first segment heads for the midpoint of 30 -> 90"
        for _ in range(5):
            await asyncio.sleep(0)
        assert sleeps == [CEILING_MILLISECONDS / 1000]
    finally:
        await runner.close()
    assert len([c for c in calls if c.method == "PUT"]) == 2, "the tail was cancelled"
