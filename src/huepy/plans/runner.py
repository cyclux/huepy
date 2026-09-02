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
        plan = load_plans("./plans")
        async with PlanRunner(hue, plan, changes=hue.state) as runner:
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
from huepy.plans.fields import TriggerKind, format_duration
from huepy.plans.protocol import Cancellable, ChangeSource, PlanClient
from huepy.plans.resolve import Binding, ResolvedPlan, TriggerBinding, resolve
from huepy.plans.schema import Plan
from huepy.plans.timeline import Zone, combine, in_zone, next_transition, zone_of
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


def _reported_on(change: Change) -> bool | None:
    """Pull a power state out of a change's delta, if it carried one.

    Args:
        change: The observed transition.

    Returns:
        The reported state, or None when the change was about something else.

    """
    raw = change.delta.get("on")
    if not isinstance(raw, dict):
        return None
    value = cast("dict[str, Any]", raw).get("on")
    return value if isinstance(value, bool) else None


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
            sleep: How to wait for a chained segment, or a catch-up fade, to
                finish. Injectable for the same reason. The wait between
                scheduled steps is not a sleep -- it has to be interruptible
                by a trigger -- so it does not go through here.

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
        self._binding_of: dict[str, Binding] = {}
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
            # Subscribed under `reassert` too: not yielding is a different
            # thing from not looking. A hand switch-off still has to reset
            # what the runner believes about the light.
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
        for binding in self._binding_of.values():
            logger.debug(
                "%s -> %s (%d lights)",
                binding.selector,
                binding.path,
                len(binding.light_ids),
            )
        for key, trigger in self._resolved.triggers.items():
            logger.debug(
                "%s -> %s", key, ", ".join(trigger.resource_ids) or "application signal"
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
                # Two scenarios on one room bind it twice; either spelling
                # names the same path, so the first is as good as any.
                _ = self._binding_of.setdefault(binding.path, binding)
                ids = [binding.path.rsplit("/", 1)[-1], *binding.light_ids]
                for resource_id in ids:
                    # A set, not a single path: one light can belong to both a
                    # `room:` scope and a `light:` scope, and overwriting here
                    # would leave one of them never noticing a manual change.
                    self._scope_of.setdefault(resource_id, set()).add(binding.path)

    def _label(self, path: str) -> str:
        """Name a scope the way the plan wrote it, for logs.

        ``room:Living Room`` is unambiguous where a bare name is not -- a light
        and the room it sits in can share one -- and the write path is
        meaningless to anyone reading a log.

        Args:
            path: The scope's write path.

        Returns:
            The selector as written, or the path if it is not a scope.

        """
        binding = self._binding_of.get(path)
        return str(binding.selector) if binding is not None else path

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
        if change.observation == "command_echo":
            # The bridge repeating a transition's *target* back the moment it
            # accepts the write. Judged against the fade's expectation at that
            # instant it is a jump, and it is the one report that is ours by
            # construction. `origin == "self"` is deliberately *not* trusted:
            # it is the state layer's time window, which attributes every
            # report on the light to us until the fade ends -- the masking
            # the arbiter's own arithmetic exists to avoid.
            return
        brightness = _reported_brightness(change)
        on = _reported_on(change)
        # The runner's clock, not the report's own timestamp: the instant a
        # yield began is compared against hold placements and mode
        # activations, which come from this clock, and a trigger arriving
        # within bridge-clock skew of the hand change must not lose to it.
        now = self._clock()
        for path in self._scope_of.get(change.resource_id, ()):
            if not self.arbiter.note_foreign_change(path, brightness, now, on=on):
                # One line per progress report the bridge sends during a fade.
                # It is the override arithmetic's verdict, which is the thing
                # to read when a light is yielded that should not have been.
                logger.debug(
                    "%s: report explained by the running fade", self._label(path)
                )
                continue
            # Stop the rest of a chained fade. Without this, the second half of
            # a three-hour sunset would still land an hour after someone turned
            # the lights up by hand.
            self._cancel_fade(path)
            if self.arbiter.is_yielded(path):
                logger.info("%s: changed by hand, standing back", self._label(path))
            else:
                logger.info("%s: changed by hand, re-asserting", self._label(path))
                self._wake.set()

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

    def stop(self) -> None:
        """Ask :meth:`run` to return after the write it is on.

        Synchronous and idempotent, so it can be installed as a signal
        handler. The loop checks between scopes and after every wait, so the
        lag is at most one write -- or the catch-up ramp, when a restart is
        still settling. A fade already handed to the bridge keeps running
        there. Not thread-safe: from another thread, go through
        ``loop.call_soon_threadsafe(runner.stop)``.
        """
        if not self._closing.is_set():
            logger.info("stop requested")
        self._closing.set()
        self._wake.set()

    async def close(self) -> None:
        """Stop the runner, cancelling any fade still being chained.

        A fade already handed to the bridge keeps running there -- that is the
        point of a long transition -- but the chaining of later segments stops.
        """
        self.stop()
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

    @property
    def signals(self) -> frozenset[str]:
        """The names the plan's ``signal:`` triggers listen for.

        Returns:
            Every name, without the ``signal:`` prefix, that :meth:`fire`
            would do something with.

        Raises:
            RuntimeError: If the runner has not been started.

        """
        if self._resolved is None:
            msg = "the runner has not been started; use `async with` or await start()"
            raise RuntimeError(msg)
        return frozenset(
            trigger.selector.name
            for trigger in self._resolved.triggers.values()
            if trigger.is_signal
        )

    def fire(self, signal: str) -> tuple[str, ...]:
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

        Returns:
            What the signal did, one phrase per scenario it reached. Empty
            when nothing in the plan listens for it.

        """
        key = f"signal:{signal}"
        outcomes = tuple(self.arbiter.fire(key, self._clock()))
        for outcome in outcomes:
            logger.info("%s: %s", key, outcome)
        if not outcomes:
            # A shut window still produces an outcome, so silence really does
            # mean the name matches no trigger: most likely a typo in the
            # caller, which is worth more than an INFO line.
            logger.warning("%s: nothing in the plan listens for it", key)
        self._wake.set()
        return outcomes

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
        claims = self.arbiter.claims(now, catching_up=True)
        for claim in claims:
            if self._closing.is_set():
                break
            if self.arbiter.is_yielded(claim.binding.path):
                continue
            logger.debug(
                "%s: catching up to %s over %s",
                claim.binding.selector,
                claim.target.describe(),
                format_duration(claim.ramp),
            )
            if await self._drive_safely(claim, now, ramp=claim.ramp):
                written += 1
        logger.info("catch-up: %d of %d scopes written", written, len(claims))
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
            if self._closing.is_set():
                # close() landed during another scope's write; finishing the
                # pass would keep writing after the caller was told it stopped.
                break
            path = claim.binding.path
            state = self.arbiter.state_of(path)

            # A scope someone took over comes back at the first step, hold or
            # mode that began after the hand change, not before: the human
            # wins now, the plan wins later.
            if state.yielded_at is not None:
                if claim.since is None or claim.since < state.yielded_at:
                    continue
                self.arbiter.resume(path)
                logger.info("%s: taking the scope back", claim.binding.selector)

            if (
                state.owner is not None
                and state.owner.source == claim.source
                and not self._target_changed(claim)
            ):
                logger.debug(
                    "%s: %s still in force, nothing to send",
                    claim.binding.selector,
                    claim.source,
                )
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
        state = self.arbiter.state_of(claim.binding.path)
        before = state.fade
        try:
            return await self._drive(claim, now, ramp=ramp)
        except HueError:
            logger.exception("%s: could not be driven", claim.binding.selector)
            # The refused write did not move the light, so where the previous
            # fade had taken it is still the best belief. Kept as the last
            # known state rather than as a fade, so the next tick retries
            # instead of believing this scope arrived -- and retries from
            # there, `on` included. Starting from the last *foreign* report
            # instead once dropped `on` on a room the plan itself had
            # switched off the night before.
            if before is not None:
                state.reported = before.expected_at(now)
            state.fade = None
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
        state = self.arbiter.state_of(path)
        # From the running fade if there is one, else from where a human
        # last left the light. Without a start, a long ramp cannot be chained
        # and the fade has no brightness expectation to judge reports by.
        previous = state.fade
        start = previous.expected_at(now) if previous is not None else state.reported

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

        fade = Fade(
            scope=path, start=start, target=claim.target, started_at=now, ramp=ramp
        )
        if not segments:
            # Already where it should be. Still record the intent, so the next
            # tick knows this target is in force and does not re-send it.
            self.arbiter.note_fade(fade)
            state.owner = claim
            logger.debug(
                "%s: already at %s, nothing to send",
                self._label(path),
                claim.target.describe(),
            )
            return False

        self.arbiter.note_fade(fade)

        await send(self._client, segments[0])
        # Only now. A rejected write leaves the previous owner in place, so the
        # retry next tick still looks like the hand-over it is and keeps the
        # no-snap floor rather than believing this scenario already had it.
        state.owner = claim
        logger.info(
            "%s: %s -> %s over %s, %d request%s, ends %s",
            self._label(path),
            claim.source,
            claim.target.describe(),
            format_duration(ramp),
            len(segments),
            "" if len(segments) == 1 else "s",
            self._local(fade.ends_at()),
        )

        # Re-check after the await: `_observe` runs on the state layer's own
        # dispatch task and may have handed this scope to a human while the
        # first segment was in flight. Scheduling the tail regardless would
        # land the second half of a sunset on a light someone just turned up.
        # Also re-checked: closing. A tail scheduled after close() cleared the
        # fade table would never be cancelled and would outlive the runner.
        if (
            len(segments) > 1
            and not self.arbiter.is_yielded(path)
            and not self._closing.is_set()
        ):
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
            sent = await send_chain(self._client, segments, sleep=self._sleep)
            logger.info(
                "%s: chained fade landed, %d more request%s",
                self._label(path),
                sent,
                "" if sent == 1 else "s",
            )
        except asyncio.CancelledError:
            raise
        except HueError:
            logger.exception("%s: the rest of a chained fade failed", self._label(path))
            state = self.arbiter.state_of(path)
            if state.fade is not None:
                # Where the segments that did go out have taken the light, so
                # the retry can be chained from there instead of degraded to
                # one ceiling-length fade from nowhere.
                state.reported = state.fade.expected_at(self._clock())
            state.fade = None
        finally:
            if self._fades.get(path) is asyncio.current_task():
                del self._fades[path]

    def _local(self, when: datetime.datetime) -> str:
        """Render an instant as a clock time in the plan's zone, for logs.

        Args:
            when: The instant.

        Returns:
            ``HH:MM:SS`` in the plan's zone, or the host's when it has none.

        """
        return in_zone(when, self._zone).strftime("%H:%M:%S")

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
        gated = (s for s in self.plan.scenario if s.enabled and s.days is not None)
        if any(gated):
            # `days` gates by the local calendar date, so a scenario can start
            # or stop claiming its scope at midnight with no step to wake for.
            tomorrow = in_zone(now, self._zone).date() + datetime.timedelta(days=1)
            upcoming.append(combine(tomorrow, datetime.time(), self._zone))
        if not upcoming:
            return MAX_SLEEP
        delay = (min(upcoming) - now).total_seconds()
        return max(0.0, min(delay, MAX_SLEEP))

    async def _settle(self) -> None:
        """Catch up, let the catch-up fade land, then carry on with the schedule.

        Catching up moves each scope to where it should already be, over the
        short catch-up ramp. That is only half of a restart mid-fade: the rest
        of the step's ramp still has to be handed to the bridge, and nothing
        scheduled would wake the loop for it -- the step has already started.
        Waiting for the catch-up fade first matters too: a second PUT straight
        after the first overrides it, and the light would run the whole
        remaining ramp from wherever it happened to be.
        """
        if await self.catch_up():
            logger.debug(
                "waiting %s for the catch-up fade to land",
                format_duration(self.plan.defaults.catchup_ramp),
            )
            await self._sleep(self.plan.defaults.catchup_ramp)
            if self._closing.is_set():
                # close() returned while this was asleep; a tick now would
                # write after the caller believes the runner has stopped.
                return
        _ = await self.tick()

    async def run(self) -> None:
        """Catch up, then keep the plan running until closed or cancelled."""
        self._closing.clear()
        await self._settle()
        while not self._closing.is_set():
            now = self._clock()
            delay = self._seconds_until_next(now)
            until = self._local(now + datetime.timedelta(seconds=delay))
            if delay >= MAX_SLEEP:
                logger.debug("nothing due before %s; stirring then", until)
            else:
                logger.debug("sleeping %s, until %s", format_duration(delay), until)
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
                await self._settle()
            else:
                _ = await self.tick()
