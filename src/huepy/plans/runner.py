"""Running a plan against a bridge.

The runner is a small loop around a lot of pure code. It asks
:mod:`huepy.plans.arbiter` what each scope should look like now, hands the
differences to :mod:`huepy.plans.executor`, and sleeps until the next thing is
due. Almost nothing is decided here.

It keeps **no durable state**, and that is a design choice rather than an
omission. On start, and again after every reconnect, it asks the timeline where
the lights *should* be at this instant -- interpolating if a fade was part-way
through -- and moves there over ``defaults.catchup_ramp``. A process killed
half an hour into a sunset fade comes back and lands in the right place, with
no journal to replay and nothing to get out of sync.

Typical usage example:

    async with Hue(state=True) as hue:
        async with PlanRunner(hue, load_plans("./plans")) as runner:
            await runner.run()
"""

import asyncio
import contextlib
import datetime
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Literal, Self, cast

from huepy.exceptions import HueError
from huepy.plans.arbiter import Arbiter, Claim, Fade
from huepy.plans.executor import Segment, plan_segments, send, send_chain
from huepy.plans.fields import TriggerKind
from huepy.plans.protocol import Cancellable, ChangeSource, PlanClient
from huepy.plans.resolve import ResolvedPlan, TriggerBinding, resolve
from huepy.plans.schema import Plan
from huepy.plans.timeline import Zone, next_transition, zone_of
from huepy.state.records import Change, Resync

logger = logging.getLogger(__name__)

MAX_SLEEP = 900.0
"""Longest nap between wake-ups, in seconds.

The next scheduled step may be many hours away, but the runner still stirs
every quarter hour. That is what lets a mode activated from outside, or a
clock that jumped, be noticed without waiting for the next sunset.
"""

BUTTON_PRESS = "initial_press"
"""The button event a ``button:`` trigger fires on.

The first of the events a press produces, so a rule reacts the instant the
button goes down rather than when it comes up. ``repeat``, ``short_release``
and the long-press events all follow it and are ignored.
"""

CONTACT_OPENED = "no_contact"
"""The report state a ``contact:`` trigger fires on: the door or window opening."""

type Clock = Callable[[], datetime.datetime]
type Sleeper = Callable[[float], Awaitable[None]]
type Edge = Literal["start", "end"]
"""Which way a trigger moved: it fired, or -- for motion only -- it stopped."""


def _system_clock() -> datetime.datetime:
    """Read the current instant as an aware datetime.

    Returns:
        Now, in the host's local zone.

    """
    return datetime.datetime.now(datetime.UTC).astimezone()


def _reported_brightness(change: Change) -> float | None:
    """Pull a brightness out of a change's delta, if it carried one.

    The delta is bridge JSON, so every step is checked rather than assumed.

    Args:
        change: The observed transition.

    Returns:
        The reported brightness, or None when the change was about something
        else.

    """
    raw = change.delta.get("dimming")
    if not isinstance(raw, dict):
        return None
    # A delta is bridge JSON, which is the one place `Any` is the honest type.
    dimming = cast("dict[str, Any]", raw)
    value = dimming.get("brightness")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _nested(delta: dict[str, Any], *keys: str) -> object:
    """Walk a path into a delta, stopping at the first thing that is not a dict.

    Args:
        delta: Bridge JSON, so nothing about its shape is assumed.
        *keys: The path to follow.

    Returns:
        The value at the end of the path, or None if the path is not there.

    """
    # Bridge JSON is the one place `Any` is the honest type.
    current: Any = delta
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = cast("dict[str, Any]", current).get(key)
    return current


def _edge(kind: str, change: Change) -> Edge | None:
    """Work out whether a change on a sensor service is the event its trigger means.

    Each kind has one meaning, chosen to be the thing a plan author means by
    naming it: motion *starting*, a button going *down*, a door *opening*.
    Only the delta is read -- what the bridge sent for this event -- never the
    folded state, so a sensor being enabled or reporting its reading invalid
    while its last state happened to be "motion" fires nothing.

    Motion is the one kind with an *end*: the sensor reports ``false`` once
    the room has been still for its own timeout, and that is when a hold's
    clock should start. Buttons and contacts fire and are done.

    Args:
        kind: The trigger kind, from the selector.
        change: The observed transition on the service.

    Returns:
        ``"start"`` when a rule listening on this service should fire,
        ``"end"`` when motion stopped, None when the change means nothing here.

    """
    if kind == TriggerKind.MOTION:
        moving = _nested(change.delta, "motion", "motion")
        if moving is True:
            return "start"
        return "end" if moving is False else None
    if kind == TriggerKind.BUTTON:
        event = _nested(change.delta, "button", "button_report", "event")
        return "start" if event == BUTTON_PRESS else None
    if kind == TriggerKind.CONTACT:
        state = _nested(change.delta, "contact_report", "state")
        return "start" if state == CONTACT_OPENED else None
    return None


class PlanRunner:
    """Executes a plan against a bridge until it is closed.

    Attributes:
        plan: The plan being run.

    """

    def __init__(
        self,
        client: PlanClient,
        plan: Plan,
        *,
        changes: ChangeSource | None = None,
        clock: Clock = _system_clock,
        sleep: Sleeper = asyncio.sleep,
    ) -> None:
        """Prepare a runner. Nothing is resolved or written until it starts.

        Args:
            client: The client to resolve names against and write through.
            plan: The plan to run.
            changes: Where to watch for changes this client did not make, so a
                scope someone adjusts by hand can be left alone. Pass
                ``hue.state`` from a client started with ``state=True``.
                Without it the plan simply never yields.
            clock: Where "now" comes from. Injectable so a simulated day runs
                in microseconds.
            sleep: How to wait between wake-ups. Injectable for the same
                reason.

        """
        self.plan: Plan = plan
        self._client: PlanClient = client
        self._clock: Clock = clock
        self._sleep: Sleeper = sleep
        self._zone: Zone = zone_of(plan.location)
        self._resolved: ResolvedPlan | None = None
        self._arbiter: Arbiter | None = None
        self._changes: ChangeSource | None = changes
        self._subscription: Cancellable | None = None
        self._resync: Cancellable | None = None
        self._scope_of: dict[str, set[str]] = {}
        self._triggers_of: dict[str, list[TriggerBinding]] = {}
        self._fades: dict[str, asyncio.Task[None]] = {}
        self._wake: asyncio.Event = asyncio.Event()
        self._closing: asyncio.Event = asyncio.Event()
        self._needs_catchup: bool = False

    async def start(self) -> None:
        """Resolve every name in the plan against the bridge.

        Raises:
            PlanError: If any name is unknown or ambiguous. Nothing is written
                before this succeeds, so a misspelled room cannot half-run a
                plan.

        """
        self._resolved = await resolve(self._client, self.plan)
        self._arbiter = Arbiter(resolved=self._resolved, zone=self._zone)
        self._index_scopes(self._resolved)
        self._index_triggers(self._resolved)
        if self._changes is not None:
            listening = self.plan.defaults.on_manual_change != "reassert" or bool(
                self._triggers_of
            )
            if listening:
                self._subscription = self._changes.on_change(self._observe)
            # A gap in the stream invalidates every belief this runner holds
            # about what is in flight, so the answer is to re-derive the whole
            # picture from the clock rather than trust any of it.
            self._resync = self._changes.on_resync(self._observe_resync)
        logger.info(
            "plan resolved: %d scenarios, %d scopes",
            len(self.plan.scenario),
            sum(len(bindings) for bindings in self._resolved.scopes.values()),
        )

    def _index_scopes(self, resolved: ResolvedPlan) -> None:
        """Map every resource id a scope covers back to that scope.

        A room is written through one ``grouped_light`` but the bridge reports
        the result on each member light, so both spellings have to lead back
        here or half the reports would look like they concerned nothing.

        Args:
            resolved: The plan, with every name bound.

        """
        for bindings in resolved.scopes.values():
            for binding in bindings:
                ids = [binding.path.rsplit("/", 1)[-1], *binding.light_ids]
                for resource_id in ids:
                    # A set, not a single path: one light can belong to both a
                    # `room:` scope and a `light:` scope, and overwriting here
                    # would leave one of them never noticing a manual change.
                    self._scope_of.setdefault(resource_id, set()).add(binding.path)

    def _index_triggers(self, resolved: ResolvedPlan) -> None:
        """Map every sensor service back to the triggers it can fire.

        Args:
            resolved: The plan, with every name bound.

        """
        for trigger in resolved.triggers.values():
            for resource_id in trigger.resource_ids:
                self._triggers_of.setdefault(resource_id, []).append(trigger)

    def _observe(self, change: Change) -> None:
        """React to a change: a sensor firing, or a human adjusting a light.

        Args:
            change: The observed transition.

        """
        triggers = self._triggers_of.get(change.resource_id)
        if triggers is not None:
            self._observe_trigger(change, triggers)
            return
        if self.plan.defaults.on_manual_change == "reassert":
            return
        if change.origin == "self":
            return
        brightness = _reported_brightness(change)
        for path in self._scope_of.get(change.resource_id, ()):
            if not self.arbiter.note_foreign_change(path, brightness, change.at):
                continue
            # Stop the rest of a chained fade. Without this, the second half of
            # a three-hour sunset would still land an hour after someone turned
            # the lights up by hand.
            self._cancel_fade(path)
            logger.info("%s: changed by hand, standing back until its next step", path)

    def _observe_trigger(self, change: Change, triggers: list[TriggerBinding]) -> None:
        """Fire every trigger a sensor change means.

        Args:
            change: The observed transition on a sensor service.
            triggers: The triggers bound to that service.

        """
        now = self._clock()
        for trigger in triggers:
            key = str(trigger.selector)
            edge = _edge(trigger.selector.kind, change)
            if edge is None:
                continue
            outcomes = (
                self.arbiter.fire(key, now)
                if edge == "start"
                else self.arbiter.ended(key, now)
            )
            for outcome in outcomes:
                logger.info("%s: %s", key, outcome)
            self._wake.set()

    def _observe_resync(self, resync: Resync) -> None:
        """React to a break in the change stream.

        Args:
            resync: The continuity marker.

        """
        logger.info("continuity lost (%s); recomputing every scope", resync.reason)
        self._needs_catchup = True
        self._wake.set()

    def _cancel_fade(self, path: str) -> None:
        """Stop the rest of a chained fade on one scope.

        Args:
            path: The scope's write path.

        """
        pending = self._fades.pop(path, None)
        if pending is not None:
            _ = pending.cancel()

    async def close(self) -> None:
        """Stop the runner, cancelling any fade still being chained.

        A fade already handed to the bridge keeps running there -- that is the
        point of a long transition -- but the chaining of later segments stops.
        """
        self._closing.set()
        self._wake.set()
        for subscription in (self._subscription, self._resync):
            if subscription is not None:
                subscription.cancel()
        self._subscription = None
        self._resync = None

        tasks = tuple(self._fades.values())
        for task in tasks:
            _ = task.cancel()
        for task in tasks:
            # A tail that failed rather than cancelled has already logged; this
            # only stops asyncio complaining that nobody retrieved the result.
            with contextlib.suppress(asyncio.CancelledError, HueError):
                await task
        self._fades.clear()

    async def __aenter__(self) -> Self:
        """Resolve the plan and return the runner.

        Returns:
            This runner, started.

        """
        await self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        """Stop the runner."""
        await self.close()

    @property
    def arbiter(self) -> Arbiter:
        """The ownership tracker.

        Returns:
            The arbiter.

        Raises:
            RuntimeError: If the runner has not been started.

        """
        if self._arbiter is None:
            msg = "the runner has not been started; use `async with` or await start()"
            raise RuntimeError(msg)
        return self._arbiter

    def fire(self, signal: str) -> None:
        """Fire an application signal.

        This is the hook for anything the bridge cannot know about -- a media
        player starting, a calendar event, a webhook. It activates every mode
        whose ``activate_on`` names the signal, releases every mode whose
        ``release_on`` does, and fires every rule whose ``when`` does. It is
        deliberately not a coroutine, so an in-loop callback can call it
        without ceremony. It is *not* thread-safe: from another thread, go
        through ``loop.call_soon_threadsafe(runner.fire, name)``.

        Args:
            signal: The name as written in the plan, without the ``signal:``
                prefix.

        """
        key = f"signal:{signal}"
        for outcome in self.arbiter.fire(key, self._clock()):
            logger.info("%s: %s", key, outcome)
        self._wake.set()

    async def catch_up(self) -> int:
        """Move every scope to where it should be right now.

        Called on start and after a reconnect. Because the target is computed
        from the clock rather than remembered, this is the whole of crash
        recovery.

        Returns:
            How many scopes were written to.

        """
        now = self._clock()
        written = 0
        for claim in self.arbiter.claims(now, catching_up=True):
            if self.arbiter.is_yielded(claim.binding.path):
                continue
            if await self._drive_safely(claim, now, ramp=claim.ramp):
                written += 1
        return written

    async def tick(self) -> int:
        """Apply everything that is due, once.

        Exposed rather than buried in :meth:`run` so a whole simulated day can
        be stepped through in a test without a clock or a sleep.

        Returns:
            How many scopes were written to.

        """
        now = self._clock()
        written = 0
        for claim in self.arbiter.claims(now):
            path = claim.binding.path
            state = self.arbiter.state_of(path)

            # A scope someone took over rejoins at its next scheduled step,
            # not before: the human wins now, the plan wins later.
            if state.yielded:
                if state.resume_at is None or now < state.resume_at:
                    continue
                self.arbiter.resume(path)
                logger.info("%s: resuming at a scheduled step", claim.binding.name)

            if state.owner == claim.source and not self._target_changed(claim):
                continue
            if await self._drive_safely(claim, now, ramp=claim.ramp):
                written += 1
        return written

    async def _drive_safely(
        self, claim: Claim, now: datetime.datetime, *, ramp: float
    ) -> bool:
        """Drive one scope, keeping its failure to itself.

        A plan is meant to run for weeks. One unreachable bulb, or a room
        someone deleted from the app, must not stop every other room being
        driven -- the same posture the state layer takes with handlers.

        Args:
            claim: What to do and where.
            now: The instant the fade starts from.
            ramp: How long it should take, in seconds.

        Returns:
            True when something was sent.

        """
        try:
            return await self._drive(claim, now, ramp=ramp)
        except HueError:
            logger.exception("%s: could not be driven", claim.binding.name)
            # Forget the fade, so the next tick tries again rather than
            # believing this scope arrived.
            self.arbiter.state_of(claim.binding.path).fade = None
            return False

    def _target_changed(self, claim: Claim) -> bool:
        """Whether a scope's claimed target differs from what is running on it.

        This is what keeps the loop idempotent: waking up mid-fade finds the
        same target already in flight and writes nothing, so a stirring tick
        costs no requests.

        Args:
            claim: The claim being considered.

        Returns:
            True when the scope needs a new write.

        """
        state = self.arbiter.state_of(claim.binding.path)
        if state.fade is None:
            return True
        return state.fade.target != claim.target

    async def _drive(
        self, claim: Claim, now: datetime.datetime, *, ramp: float
    ) -> bool:
        """Issue the writes that take one scope to its claimed target.

        The first segment is awaited, so a plain fade is on the wire by the
        time this returns, and a rejection has been raised -- including one
        the bridge reported inside a 200 body, which :func:`send` unwraps.
        Only the tail of a chained fade runs in the background, where it stays
        cancellable for when someone reaches for a switch mid-sunset.

        Args:
            claim: What to do and where.
            now: The instant the fade starts from.
            ramp: How long it should take, in seconds.

        Returns:
            True when something was actually sent.

        """
        path = claim.binding.path
        previous = self.arbiter.state_of(path).fade
        start = previous.expected_at(now) if previous is not None else None

        segments = plan_segments(
            claim.binding,
            claim.target,
            ramp=ramp,
            start=start,
            current_on=start.on if start is not None else None,
        )

        existing = self._fades.pop(path, None)
        if existing is not None:
            _ = existing.cancel()

        if not segments:
            # Already where it should be. Still record the intent, so the next
            # tick knows this target is in force and does not re-send it.
            self.arbiter.note_fade(
                Fade(
                    scope=path,
                    start=start,
                    target=claim.target,
                    started_at=now,
                    ramp=ramp,
                )
            )
            self.arbiter.state_of(path).owner = claim.source
            return False

        self.arbiter.note_fade(
            Fade(
                scope=path,
                start=start,
                target=claim.target,
                started_at=now,
                ramp=ramp,
            )
        )

        await send(self._client, segments[0])
        # Only now. A rejected write leaves the previous owner in place, so the
        # retry next tick still looks like the hand-over it is and keeps the
        # no-snap floor rather than believing this scenario already had it.
        self.arbiter.state_of(path).owner = claim.source

        # Re-check after the await: `_observe` runs on the state layer's own
        # dispatch task and may have handed this scope to a human while the
        # first segment was in flight. Scheduling the tail regardless would
        # land the second half of a sunset on a light someone just turned up.
        if len(segments) > 1 and not self.arbiter.is_yielded(path):
            task = asyncio.create_task(
                self._chain(path, segments[1:]), name=f"fade:{path}"
            )
            self._fades[path] = task
        return True

    async def _chain(self, path: str, segments: list[Segment]) -> None:
        """Send the tail of a long fade, surviving its own failures.

        A bare task would swallow the exception -- nothing ever awaits it --
        and leave the arbiter believing the scope arrived, so it would never be
        re-driven. Forgetting the fade instead makes the next tick notice the
        scope is not where it should be.

        Args:
            path: The scope's write path.
            segments: The segments still to send.

        """
        try:
            _ = await send_chain(self._client, segments, sleep=self._sleep)
        except asyncio.CancelledError:
            raise
        except HueError:
            logger.exception("%s: the rest of a chained fade failed", path)
            self.arbiter.state_of(path).fade = None
        finally:
            if self._fades.get(path) is asyncio.current_task():
                del self._fades[path]

    def _seconds_until_next(self, now: datetime.datetime) -> float:
        """How long to sleep before the next scheduled step or hold expiry.

        Args:
            now: The instant to measure from.

        Returns:
            Seconds, capped so the runner still stirs periodically.

        """
        upcoming = [
            when
            for scenario in self.plan.scenario
            if (when := next_transition(self.plan, scenario, now, self._zone))
            is not None
        ]
        expiry = self.arbiter.next_expiry(now)
        if expiry is not None:
            upcoming.append(expiry)
        if not upcoming:
            return MAX_SLEEP
        delay = (min(upcoming) - now).total_seconds()
        return max(0.0, min(delay, MAX_SLEEP))

    async def run(self) -> None:
        """Catch up, then keep the plan running until closed or cancelled."""
        self._closing.clear()
        _ = await self.catch_up()
        while not self._closing.is_set():
            delay = self._seconds_until_next(self._clock())
            waker = asyncio.ensure_future(self._wake.wait())
            try:
                _ = await asyncio.wait_for(waker, timeout=delay)
            except TimeoutError:
                pass
            finally:
                if not waker.done():
                    _ = waker.cancel()
            if self._closing.is_set():
                break
            # Cleared here, after the wait and before the work, never before
            # the wait. A trigger that lands while a tick is awaiting a write
            # sets the event mid-tick; clearing on the way back into the loop
            # would discard it, and a ninety-second motion hold would then
            # expire during the next long sleep without ever being driven.
            self._wake.clear()
            if self._needs_catchup:
                # The stream lost continuity, so nothing this runner believes
                # about what is in flight can be trusted. Re-derive it all.
                self._needs_catchup = False
                _ = await self.catch_up()
            else:
                _ = await self.tick()
