"""Deciding who owns a scope, and whether a change was ours.

Three questions, all pure, all awkward enough to be worth isolating from the
async machinery that asks them.

**Who owns this scope?** Several scenarios may cover one room -- a base day
curve, a motion rule, a movie mode -- and only one of them drives it at a time.
The highest ``priority`` that is currently active and actually has something to
say wins; a scenario whose day curve has not started yet says nothing and is
skipped rather than treated as "off".

**What did that trigger do?** A sensor firing, a button, a door, an application
signal -- all of them arrive here as one ``kind:name`` key and go through
:meth:`Arbiter.fire`, which is why a mode can be activated by a motion sensor
and a rule can be fired by a signal without either being a special case. A
rule that fires places a :class:`Hold` on each of its scenario's scopes, and
the scenario claims those scopes with the rule's target for as long as the hold
lasts.

**Was that change ours?** This is the subtle one. The state layer already
separates writes this client made from everything else, but it does so with a
time window: a fade is treated as ours until it ends, plus a grace period. That
works when a fade lasts two seconds. It fails badly here, because this layer
issues fades lasting up to a hundred minutes -- someone hitting the wall switch
half an hour into a sunset fade would be masked for the next hour.

So a running fade is checked against its own arithmetic instead. At any instant
the fade has an expected value; a report near it is the fade progressing, and a
report far from it is a human. Movement consistent with the ramp is ours, a
jump is not.

Typical usage example:

    if arbiter.note_foreign_change(path, brightness=12.0, at=now):
        cancel_running_fade(path)
"""

import datetime
from dataclasses import dataclass, field

from huepy.plans.fields import TriggerKind
from huepy.plans.resolve import Binding, ResolvedPlan
from huepy.plans.schema import Action, Rule, Scenario
from huepy.plans.timeline import (
    Zone,
    current_step,
    in_window,
    interpolate,
    next_transition,
    target_at,
)

BRIGHTNESS_TOLERANCE = 8.0
"""Percentage points a report may sit from a running fade and still be ours.

Generous on purpose. The bridge reports a fade's progress on the device's own
cadence rather than continuously, dimming curves are not exactly linear, and a
false "that was a human" only costs one skipped step -- while a false "that was
us" ignores someone reaching for the switch, which is the failure that annoys
people.
"""


def _latest(*instants: datetime.datetime | None) -> datetime.datetime | None:
    """Pick the latest of some optional instants.

    Args:
        *instants: The candidates, any of which may be None.

    Returns:
        The latest, or None when none was given.

    """
    known = [instant for instant in instants if instant is not None]
    return max(known) if known else None


@dataclass(frozen=True, slots=True)
class Fade:
    """A transition this runner issued, and the arithmetic to check it.

    Attributes:
        scope: The scope it was sent to.
        start: The state it began from, when that was known.
        target: The state it is heading for.
        started_at: When the first segment went out.
        ramp: Its total length in seconds.

    """

    scope: str
    start: Action | None
    target: Action
    started_at: datetime.datetime
    ramp: float

    def ends_at(self) -> datetime.datetime:
        """When the fade is due to finish.

        Returns:
            The instant the bridge should have arrived at the target.

        """
        return self.started_at + datetime.timedelta(seconds=self.ramp)

    def expected_at(self, when: datetime.datetime) -> Action:
        """Where this fade should have reached by a given instant.

        Args:
            when: The instant to evaluate.

        Returns:
            The interpolated state, or the target once the fade has ended.

        """
        if self.ramp <= 0 or when >= self.ends_at():
            return self.target
        elapsed = (when - self.started_at).total_seconds()
        return interpolate(self.start, self.target, elapsed / self.ramp)

    def explains(
        self,
        brightness: float | None,
        at: datetime.datetime,
        *,
        on: bool | None = None,
    ) -> bool:
        """Whether a report is this fade progressing.

        Args:
            brightness: The brightness the bridge reported, if it reported one.
            at: When it was reported.
            on: The power state the bridge reported, if it reported one.

        Returns:
            True when the report sits where the fade should be, and so is this
            client's own work rather than someone else's. A power state the
            fade did not ask for is never ours: a fade to a brightness is a
            fade on a light that is on, so a reported ``off`` is a switch. A
            report carrying neither field says nothing either way and counts
            as ours -- which means a change of colour alone, with the
            brightness left where the fade expects it, is not detected.

        """
        expected = self.expected_at(at)
        if on is not None and on != (expected.on if expected.on is not None else True):
            return False
        if brightness is None or expected.brightness is None:
            return True
        return abs(brightness - expected.brightness) <= BRIGHTNESS_TOLERANCE


@dataclass(frozen=True, slots=True)
class Hold:
    """A rule that fired, and how long its scenario keeps the scope for it.

    Attributes:
        scenario: The scenario the rule belongs to.
        rule: The rule that fired.
        placed_at: When it fired. A hold placed after someone changed the
            scope by hand is what ends that yield; one placed before it is
            not.
        until: When the hold lapses. None means no clock is running on it:
            motion is still being reported, or the rule has no ``hold`` and
            nothing scheduled covers the scope. Such a hold ends when a human
            changes the scope, when its scenario is released, or when a
            higher-priority claim takes over.

    """

    scenario: str
    rule: Rule
    placed_at: datetime.datetime
    until: datetime.datetime | None

    @property
    def source(self) -> str:
        """What a claim made under this hold reports as its owner."""
        return f"{self.scenario}/{self.rule.when}"

    def expired(self, now: datetime.datetime) -> bool:
        """Whether the hold has lapsed.

        Args:
            now: The instant to test.

        Returns:
            True once ``until`` has passed. A hold without one never expires.

        """
        return self.until is not None and now >= self.until


@dataclass(slots=True)
class ScopeState:
    """What the runner remembers about one scope.

    Attributes:
        fade: The transition currently running, if any.
        owner: The source of the claim that last drove it: a scenario name,
            or ``scenario/trigger`` for a rule hold. Comparing sources rather
            than scenario names is what makes a hold lapsing back into the same
            scenario's day curve look like the hand-over it is.
        yielded_at: When someone took the scope over by hand, while the plan
            is standing back; None otherwise. The plan takes the scope back at
            the first step, hold or mode that *begins after* this instant --
            the human wins now, the plan wins later -- which is why the
            instant is kept rather than a precomputed resume time: on a day
            when nothing covering the scope runs there is no next step to
            precompute, and the scope must still come back when one arrives.
        hold: The rule currently holding this scope, if one fired.

    """

    fade: Fade | None = None
    owner: str | None = None
    yielded_at: datetime.datetime | None = None
    hold: Hold | None = None


@dataclass(frozen=True, slots=True)
class Claim:
    """One scenario's answer for one scope at one instant.

    Attributes:
        scenario: The scenario that won the scope.
        binding: The scope it drives.
        target: The state it wants.
        ramp: How long it should take, in seconds.
        source: Who is really asking: the scenario's name, or
            ``scenario/trigger`` when a rule hold is. The runner compares this
            against :attr:`ScopeState.owner` to tell "same thing, still in
            force" from "something new".
        since: When the authority behind this claim began -- the step's start,
            the hold's placement, the mode's activation, whichever is latest.
            None for a flat state that nothing switched on. Compared against
            :attr:`ScopeState.yielded_at` to decide whether a yielded scope
            comes back.

    """

    scenario: Scenario
    binding: Binding
    target: Action
    ramp: float
    source: str
    since: datetime.datetime | None


@dataclass(slots=True)
class Arbiter:
    """Tracks scope ownership, active modes and manual overrides.

    Attributes:
        resolved: The plan, with every name bound.
        zone: The plan's timezone.
        active_modes: Mode scenarios currently claiming their scope, by name,
            with the instant each was activated.
        scopes: Per-scope state, keyed by the scope's write path.

    """

    resolved: ResolvedPlan
    zone: Zone
    active_modes: dict[str, datetime.datetime] = field(default_factory=dict)
    scopes: dict[str, ScopeState] = field(default_factory=dict)

    def state_of(self, path: str) -> ScopeState:
        """Fetch a scope's state, creating it on first use.

        Args:
            path: The scope's write path.

        Returns:
            Its mutable state.

        """
        return self.scopes.setdefault(path, ScopeState())

    def activate(self, name: str, now: datetime.datetime) -> None:
        """Mark a mode scenario as claiming its scope.

        Args:
            name: The scenario's name.
            now: When it was activated.

        """
        self.active_modes[name] = now

    def release(self, name: str) -> None:
        """Give a mode scenario's scope back.

        Args:
            name: The scenario's name.

        """
        _ = self.active_modes.pop(name, None)
        for state in self.scopes.values():
            # Its rules' holds go with it. A dormant mode's hold is skipped
            # while it sleeps, but left in place it would be honoured the
            # moment the mode woke again -- days later, with no motion at all.
            if state.hold is not None and state.hold.scenario == name:
                state.hold = None
            # A hand change made while the mode held the scope was about the
            # mode -- dimming during the film -- so releasing it is the
            # trigger that ends the yield, and the curve underneath takes the
            # scope back rather than waiting for its next step.
            if state.owner == name:
                state.yielded_at = None

    def _is_eligible(self, scenario: Scenario) -> bool:
        """Whether a scenario may drive its scope at all right now.

        Args:
            scenario: The scenario to test.

        Returns:
            True unless it is a dormant mode.

        """
        return not scenario.is_mode or scenario.name in self.active_modes

    def claims(
        self, now: datetime.datetime, *, catching_up: bool = False
    ) -> list[Claim]:
        """Work out what each scope should look like, and who decides.

        A scope claimed by nobody is left alone: a plan that says nothing
        about a room at 4am should not switch it off, it should not touch it.

        Args:
            now: The instant to evaluate.
            catching_up: Ask where each scope *should already be*, rather than
                which step is due. Interpolates a part-finished fade, which is
                how a restart lands in the right place instead of at the last
                waypoint it happened to pass.

        Returns:
            One claim per scope that some scenario currently drives.

        """
        # Expired in one sweep, up front, rather than lazily inside the
        # per-scenario evaluation: a scenario outranked on its scope is never
        # evaluated there, and a hold it could not clear would keep
        # `next_expiry()` answering "now" forever -- a busy loop.
        self._expire_holds(now)

        best: dict[str, Claim] = {}
        for scenario in self.resolved.plan.scenario:
            if not scenario.enabled or not self._is_eligible(scenario):
                continue
            for binding in self.resolved.scope_of(scenario):
                held = best.get(binding.path)
                if held is not None and held.scenario.priority >= scenario.priority:
                    continue
                claim = self._claim_for(scenario, binding, now, catching_up=catching_up)
                if claim is not None:
                    best[binding.path] = claim
        return list(best.values())

    def _claim_for(
        self,
        scenario: Scenario,
        binding: Binding,
        now: datetime.datetime,
        *,
        catching_up: bool,
    ) -> Claim | None:
        """Evaluate one scenario's claim on one of its scopes.

        Args:
            scenario: The scenario to evaluate.
            binding: The scope in question.
            now: The instant to evaluate at.
            catching_up: Whether the runner is landing after a restart.

        Returns:
            The claim, or None when the scenario has nothing to say about this
            scope right now.

        """
        defaults = self.resolved.plan.defaults
        state = self.state_of(binding.path)

        hold = state.hold
        if hold is not None and hold.scenario == scenario.name:
            rule = hold.rule
            ramp = rule.ramp if rule.ramp is not None else defaults.ramp
            return Claim(
                scenario=scenario,
                binding=binding,
                target=rule.set.resolved(),
                ramp=defaults.catchup_ramp if catching_up else ramp,
                source=hold.source,
                since=hold.placed_at,
            )

        target, ramp, step_at = self._target_of(scenario, now, catching_up=catching_up)
        if target is None:
            return None
        if (
            scenario.step
            and not catching_up
            and state.owner not in (None, scenario.name)
        ):
            # The scope is coming back to its day curve from a mode or a rule
            # hold. "The remaining ramp" is the right length mid-fade, but it
            # is zero once a step has finished, and dropping from a motion
            # rule's 15% to the curve's 80% in one frame when someone leaves
            # the room is the kind of thing that gets an automation ripped
            # out. A mode keeps its own ramp -- its author wrote it.
            ramp = max(ramp, defaults.catchup_ramp)
        return Claim(
            scenario=scenario,
            binding=binding,
            target=target,
            ramp=ramp,
            source=scenario.name,
            since=_latest(step_at, self.active_modes.get(scenario.name)),
        )

    def _target_of(
        self, scenario: Scenario, now: datetime.datetime, *, catching_up: bool
    ) -> tuple[Action | None, float, datetime.datetime | None]:
        """Evaluate what one scenario's curve or flat state wants right now.

        The two modes ask genuinely different questions. Scheduling wants the
        step's *final* target and however much of its ramp is left, so one PUT
        hands the whole remaining fade to the bridge. Catching up wants the
        interpolated value the scope should already be showing, reached
        quickly, because the runner has just discovered it is in the wrong
        place.

        Args:
            scenario: The scenario to evaluate.
            now: The instant to evaluate at.
            catching_up: Which of the two questions to answer.

        Returns:
            The target, the ramp, and when the step behind them started. The
            target is None when the scenario makes no claim -- its day curve
            has not reached a step yet, or it has none. The start is None when
            no step is behind the claim.

        """
        defaults = self.resolved.plan.defaults
        plan = self.resolved.plan
        step = current_step(plan, scenario, now, self.zone) if scenario.step else None
        if step is not None:
            if catching_up:
                target = target_at(plan, scenario, now, self.zone)
                if target is not None:
                    return target, defaults.catchup_ramp, step.at
            else:
                remaining = (step.ends_at - now).total_seconds()
                return step.action, max(0.0, remaining), step.at
        if scenario.set is not None:
            ramp = scenario.ramp if scenario.ramp is not None else defaults.ramp
            return (
                scenario.set.resolved(),
                defaults.catchup_ramp if catching_up else ramp,
                None,
            )
        return None, defaults.ramp, None

    def fire(self, key: str, now: datetime.datetime) -> list[str]:
        """Apply a trigger to every scenario listening for it.

        One entry point for every kind of trigger. A motion sensor, a button,
        a door contact and an application signal all arrive as the selector
        they were written as, so ``activate_on = "motion:Hall sensor"`` and
        ``when = "signal:doorbell"`` both work without being special cases.

        Args:
            key: The trigger selector as written in the plan, such as
                ``"motion:Hall sensor"`` or ``"signal:movie_started"``.
            now: The instant it fired.

        Returns:
            A line per thing that happened, for the log. Empty when nothing
            was listening, or every rule's window was shut.

        """
        plan = self.resolved.plan
        outcomes: list[str] = []
        for scenario in plan.scenario:
            if not scenario.enabled:
                continue
            if scenario.activate_on is not None and str(scenario.activate_on) == key:
                self.activate(scenario.name, now)
                outcomes.append(f"activated {scenario.name!r}")
            if scenario.release_on is not None and str(scenario.release_on) == key:
                self.release(scenario.name)
                outcomes.append(f"released {scenario.name!r}")
            # After the activate/release checks, not before: waking a dormant
            # mode is the point, but its rules only mean something while it
            # is awake, and a hold placed now would un-yield the scope for a
            # scenario that cannot claim it.
            if not self._is_eligible(scenario):
                continue
            for rule in scenario.rule:
                if str(rule.when) != key:
                    continue
                if not in_window(rule, plan, now, self.zone):
                    outcomes.append(f"{scenario.name!r}: outside its window, ignored")
                    continue
                self._hold(scenario, rule, now)
                outcomes.append(f"{scenario.name!r} holds its scope")
        return outcomes

    def _hold(self, scenario: Scenario, rule: Rule, now: datetime.datetime) -> None:
        """Place a rule's hold on each of its scenario's scopes.

        Args:
            scenario: The scenario the rule belongs to.
            rule: The rule that fired.
            now: When it fired.

        """
        for binding in self.resolved.scope_of(scenario):
            state = self.state_of(binding.path)
            if rule.hold is None:
                # No hold means "until the schedule next has something to
                # say", not "forever": a button press should not switch the
                # day curve off for good.
                until = self.next_step_for(binding.path, now)
            elif rule.when.kind == TriggerKind.MOTION:
                # Motion is the one trigger with a duration. The hold's clock
                # starts when the sensor reports the room still, not when it
                # first saw movement -- otherwise someone standing in the
                # hall for three minutes loses the light after ninety
                # seconds. See :meth:`ended`.
                until = None
            else:
                until = now + datetime.timedelta(seconds=rule.hold)
            # Not resuming a yielded scope here. Whether this hold ends the
            # yield is decided where the claims are ranked: a hold that wins
            # the scope begins after the hand change and takes it back, one
            # outranked by an active mode changes nothing about it.
            state.hold = Hold(
                scenario=scenario.name, rule=rule, placed_at=now, until=until
            )

    def ended(self, key: str, now: datetime.datetime) -> list[str]:
        """Start the clock on every hold whose trigger just stopped.

        Args:
            key: The trigger selector, as for :meth:`fire`.
            now: The instant it stopped.

        Returns:
            A line per hold that started counting down, for the log.

        """
        outcomes: list[str] = []
        for path, state in self.scopes.items():
            hold = state.hold
            if hold is None or str(hold.rule.when) != key or hold.rule.hold is None:
                continue
            until = now + datetime.timedelta(seconds=hold.rule.hold)
            state.hold = Hold(
                scenario=hold.scenario,
                rule=hold.rule,
                placed_at=hold.placed_at,
                until=until,
            )
            outcomes.append(f"{path}: hold runs until {until.isoformat()}")
        return outcomes

    def _expire_holds(self, now: datetime.datetime) -> None:
        """Drop every hold that has lapsed.

        Args:
            now: The instant to test against.

        """
        for state in self.scopes.values():
            if state.hold is not None and state.hold.expired(now):
                state.hold = None

    def next_expiry(self, now: datetime.datetime) -> datetime.datetime | None:
        """When the earliest running hold lapses.

        Args:
            now: The instant to look forward from.

        Returns:
            The instant, or None when no hold is due to expire.

        """
        upcoming = [
            state.hold.until
            for state in self.scopes.values()
            if state.hold is not None
            and state.hold.until is not None
            and state.hold.until > now
        ]
        return min(upcoming) if upcoming else None

    def note_fade(self, fade: Fade) -> None:
        """Record a transition this runner just issued.

        Args:
            fade: The fade that went out.

        """
        # Deliberately not touching `owner`: it holds a claim source, and
        # `fade.scope` is a write path. Setting it here once made tick()'s
        # ownership check compare two different things.
        self.state_of(fade.scope).fade = fade

    def note_foreign_change(
        self,
        path: str,
        brightness: float | None,
        at: datetime.datetime,
        *,
        on: bool | None = None,
    ) -> bool:
        """Judge a reported change, and stand back from the scope if a human made it.

        Under ``on_manual_change = "reassert"`` the scope is not yielded, but
        the fade is still forgotten: whatever the runner believed about the
        light -- including that it is on -- is no longer true, and the next
        write has to carry ``on`` again rather than drop it as redundant.

        Args:
            path: The scope's write path.
            brightness: The brightness the bridge reported, if any.
            at: When it was reported.
            on: The power state the bridge reported, if any.

        Returns:
            True when the report was someone else's work, False when it was
            this runner's own fade progressing.

        """
        state = self.state_of(path)
        if state.fade is not None and state.fade.explains(brightness, at, on=on):
            return False
        state.fade = None
        if self.resolved.plan.defaults.on_manual_change == "reassert":
            return True
        state.yielded_at = at
        # A hold the human overrode is moot. Left in place, the scope would
        # rejoin at its next step by re-asserting a stale motion rule rather
        # than the schedule.
        state.hold = None
        return True

    def next_step_for(
        self, path: str, now: datetime.datetime
    ) -> datetime.datetime | None:
        """When some scenario next has something to say about a scope.

        Args:
            path: The scope's write path.
            now: The instant to look forward from.

        Returns:
            The earliest upcoming step across every scenario covering that
            scope, or None when none of them has one.

        """
        upcoming = [
            when
            for scenario in self.resolved.plan.scenario
            # A dormant mode's steps are not coming: they only run while it
            # is awake, so they are neither a resume point nor a hold's end.
            if self._is_eligible(scenario)
            and any(b.path == path for b in self.resolved.scope_of(scenario))
            and (when := next_transition(self.resolved.plan, scenario, now, self.zone))
            is not None
        ]
        return min(upcoming) if upcoming else None

    def resume(self, path: str) -> None:
        """Take a yielded scope back.

        Args:
            path: The scope's write path.

        """
        self.state_of(path).yielded_at = None

    def is_yielded(self, path: str) -> bool:
        """Whether a scope is currently left to whoever took it over.

        Args:
            path: The scope's write path.

        Returns:
            True while the plan is standing back.

        """
        return self.state_of(path).yielded_at is not None
