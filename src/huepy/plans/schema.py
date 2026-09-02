"""The plan file format, as pydantic models.

These classes *are* the specification: the TOML a user writes is validated
straight into them, and ``Plan.model_json_schema()`` is what editors read for
completion. Nothing here does any work -- no clock, no bridge, no I/O -- so the
whole format can be exercised without either.

Two deliberate departures from the rest of the library:

* Every model sets ``extra="forbid"``, where :class:`~huepy.models.common.HueModel`
  sets ``extra="allow"``. That inversion is the point. An unknown key in a
  bridge payload is new firmware and must not raise; an unknown key in a
  hand-written config is a typo, and silently ignoring ``brightnes = 40`` at
  2am is exactly the failure this format exists to avoid.
* An :class:`Action` is validated by actually calling
  :func:`~huepy.models.state.build_light_payload`. Rather than restate the
  bridge's rules about colour versus colour temperature, the format inherits
  them, so ``rgb`` combined with ``kelvin`` is rejected when the file loads
  rather than when the step fires.

Typical usage example:

    plan = Plan.model_validate(tomllib.loads(text))
"""

import datetime
import zoneinfo
from typing import Annotated, Any, ClassVar, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from huepy.models.group import WeekDay
from huepy.models.state import build_light_payload
from huepy.plans.fields import (
    Anchor,
    ClockAnchor,
    Duration,
    ScopeSelector,
    SunAnchor,
    TriggerKind,
    TriggerSelector,
)

FORMAT_VERSION = 1
"""The only ``version`` this loader understands."""

DEFAULT_CATCHUP_RAMP = 5.0
"""Seconds spent rejoining the curve after a restart or a reconnect."""

MIN_LATITUDE = -90.0
MAX_LATITUDE = 90.0
MIN_LONGITUDE = -180.0
MAX_LONGITUDE = 180.0

_PLAN_CONFIG = ConfigDict(frozen=True, extra="forbid")


type ManualChange = Literal["yield", "reassert"]
"""What a scope does when someone changes it by hand.

``yield`` stops asserting that scope until its next scheduled step or rule --
the human wins now, the plan wins later. ``reassert`` keeps driving it.
"""


class Location(BaseModel):
    """Where the lights are, for the solar maths.

    Required only when some step uses a sun anchor. The bridge cannot supply
    this: ``geolocation`` takes latitude and longitude on PUT but does not
    return them on GET, so there is nothing to read back.

    Attributes:
        latitude: Degrees north of the equator.
        longitude: Degrees east of Greenwich.
        timezone: IANA name, such as ``"Europe/Berlin"``. Defaults to the
            host's local zone, which is normally the same zone the bridge and
            the lights are in.

    """

    model_config: ClassVar[ConfigDict] = _PLAN_CONFIG

    latitude: float = Field(ge=MIN_LATITUDE, le=MAX_LATITUDE)
    longitude: float = Field(ge=MIN_LONGITUDE, le=MAX_LONGITUDE)
    timezone: str | None = None

    @field_validator("timezone")
    @classmethod
    def _must_be_a_known_zone(cls, value: str | None) -> str | None:
        """Reject a timezone the host cannot resolve.

        Checked at load so ``huepy plan check`` catches it, rather than the
        first clock step crashing the daemon with a lookup error.

        Args:
            value: The IANA name as written, or None.

        Returns:
            The name, unchanged.

        Raises:
            ValueError: If no such zone is known.

        """
        if value is None:
            return None
        try:
            _ = zoneinfo.ZoneInfo(value)
        except (zoneinfo.ZoneInfoNotFoundError, ValueError) as error:
            msg = (
                f"unknown timezone {value!r}; use an IANA name such as 'Europe/Berlin'"
            )
            raise ValueError(msg) from error
        return value


class Action(BaseModel):
    """A target light state, in the vocabulary of a single bridge PUT.

    The field names are exactly the keyword arguments of
    :func:`~huepy.models.state.build_light_payload`, so the format has no
    vocabulary of its own that could drift from the client's.

    Attributes:
        on: Target power state.
        brightness: Target brightness percentage, 0-100.
        xy: Target colour as a CIE ``(x, y)`` pair.
        mirek: Target colour temperature; lower is cooler.
        rgb: Target colour as 8-bit ``(red, green, blue)`` channels.
        hex_color: Target colour as ``"#rrggbb"``.
        kelvin: Target colour temperature in kelvin; higher is cooler.

    """

    model_config: ClassVar[ConfigDict] = _PLAN_CONFIG

    on: bool | None = None
    brightness: Annotated[float, Field(ge=0.0, le=100.0)] | None = None
    xy: tuple[float, float] | None = None
    mirek: int | None = None
    rgb: tuple[int, int, int] | None = None
    hex_color: str | None = None
    kelvin: int | None = None

    @model_validator(mode="after")
    def _must_be_buildable(self) -> Self:
        """Reject an action the bridge could never be sent.

        Returns:
            The validated action.

        Raises:
            ValueError: If the action is empty, or if it spells a colour and a
                colour temperature at once, or spells either of them twice.

        """
        payload = self.to_payload()
        if not payload:
            msg = (
                "a 'set' block must change something. Give it at least one of: "
                "on, brightness, xy, mirek, rgb, hex_color, kelvin"
            )
            raise ValueError(msg)
        return self

    def resolved(self) -> "Action":
        """Rewrite this action into its canonical colour spelling.

        ``rgb``, ``hex_color`` and ``xy`` are three ways of saying one thing,
        and ``kelvin`` and ``mirek`` two ways of saying another. Interpolating
        between two steps means doing arithmetic on them, which needs one
        representation rather than five, so this collapses each group to the
        form the bridge itself uses.

        Returns:
            An equivalent action spelled only in ``xy`` and ``mirek``.

        """
        payload = self.to_payload()
        color = payload.get("color")
        dimming = payload.get("dimming")
        temperature = payload.get("color_temperature")
        return Action(
            on=self.on,
            brightness=None if dimming is None else dimming["brightness"],
            xy=None if color is None else (color["xy"]["x"], color["xy"]["y"]),
            mirek=None if temperature is None else temperature["mirek"],
        )

    def describe(self) -> str:
        """Render this action as the few fields it actually sets.

        Used wherever a target is shown to a person -- ``huepy plan explain``
        and the runner's log -- so both spell it the same way. Brightness is
        rounded to a whole percent, the resolution a plan author thinks in.

        Returns:
            A compact ``key=value`` listing.

        """
        parts = [
            f"{field}={value:.0f}" if field == "brightness" else f"{field}={value}"
            for field, value in self.model_dump(exclude_none=True).items()
        ]
        return " ".join(parts) or "nothing"

    def to_payload(self, *, transition: float | None = None) -> dict[str, Any]:
        """Compose this action into a bridge PUT body.

        Args:
            transition: How long the change should take, in seconds.

        Returns:
            The payload, in the bridge's shape. Empty when nothing is set.

        Raises:
            ValueError: If the action conflicts with itself, or the transition
                is out of range.

        """
        return build_light_payload(
            on=self.on,
            brightness=self.brightness,
            xy=self.xy,
            mirek=self.mirek,
            rgb=self.rgb,
            hex_color=self.hex_color,
            kelvin=self.kelvin,
            transition=transition,
        )


class Step(BaseModel):
    """One waypoint on a scenario's day curve.

    The fade *starts* at ``at`` and completes ``ramp`` later. ``at = "sunset"``
    with ``ramp = "2h"`` therefore begins dimming as the sun goes down and
    settles two hours after, which is how someone writing that line reads it.

    Attributes:
        at: When the fade begins.
        ramp: How long it takes. Falls back to the plan's default.
        set: Where it ends up.

    """

    model_config: ClassVar[ConfigDict] = _PLAN_CONFIG

    at: Anchor
    ramp: Duration | None = None
    set: Action


type Side = Literal["below", "above"]
"""Which side of a light-level threshold fires a rule."""


class Rule(BaseModel):
    """A discrete trigger and what it does.

    Attributes:
        when: The trigger to listen for.
        between: An optional window, as a ``(from, to)`` pair of anchors, that
            the trigger only fires inside. Wraps midnight when ``from`` is
            later than ``to``, so ``["sunset", "sunrise"]`` means "at night".
        ramp: How long the change takes. Falls back to the plan's default.
        hold: How long to stay there before handing the scope back to
            whatever is underneath. For ``motion:`` and ``light_level:`` the
            clock starts when the trigger ends -- the sensor reports the room
            still, or the level goes back past its threshold by the deadband
            -- so the light stays as long as the condition does. Without a
            hold the scope is held until its next scheduled step -- or, when
            nothing scheduled covers it, until a hand change, a
            higher-priority claim, or the owning mode releasing.
        below: For a ``light_level:`` trigger, the illuminance in lux the
            reading must drop under to fire. Released once it climbs back past
            about five times that.
        above: The mirror: the illuminance the reading must climb over to
            fire. Exactly one of ``below`` and ``above`` on a ``light_level:``
            rule; neither on any other kind.
        set: The target state.

    """

    model_config: ClassVar[ConfigDict] = _PLAN_CONFIG

    when: TriggerSelector
    between: tuple[Anchor, Anchor] | None = None
    ramp: Duration | None = None
    hold: Annotated[Duration, Field(gt=0)] | None = None
    below: Annotated[float, Field(gt=0)] | None = None
    above: Annotated[float, Field(gt=0)] | None = None
    set: Action

    @property
    def threshold(self) -> tuple[Side, float] | None:
        """Which side of what illuminance fires this rule.

        Returns:
            ``("below", lux)`` or ``("above", lux)`` for a ``light_level:``
            rule, None for any other kind.

        """
        if self.below is not None:
            return ("below", self.below)
        if self.above is not None:
            return ("above", self.above)
        return None

    @model_validator(mode="after")
    def _threshold_matches_kind(self) -> Self:
        """Tie ``below`` and ``above`` to the one kind they mean something for.

        Returns:
            The validated rule.

        Raises:
            ValueError: If a ``light_level:`` rule has no threshold or both,
                or any other rule has one.

        """
        is_level = self.when.kind == TriggerKind.LIGHT_LEVEL
        given = [key for key in ("below", "above") if getattr(self, key) is not None]
        if is_level and not given:
            msg = (
                f"a light_level: rule needs 'below' or 'above': a level only "
                f"means something against a threshold ({self.when})"
            )
            raise ValueError(msg)
        if is_level and len(given) > 1:
            msg = (
                f"give a light_level: rule either 'below' or 'above', not both; "
                f"the release band is built in ({self.when})"
            )
            raise ValueError(msg)
        if given and not is_level:
            msg = (
                f"'{given[0]}' only applies to a light_level: trigger, not {self.when}"
            )
            raise ValueError(msg)
        return self


class Scenario(BaseModel):
    """One named behaviour, scoped to one or more places.

    A scenario is a day curve (``step``), a set of reactions (``rule``), a
    flat state (``set``), or any combination. It is *layered*: several
    scenarios may cover one scope, and the highest ``priority`` that is
    currently active wins.

    Giving ``activate_on`` turns it into a mode -- dormant until something
    fires that trigger, and holding the scope until ``release_on`` fires.

    Attributes:
        name: Unique within the plan; used in logs and by the CLI.
        scope: What it drives.
        priority: Higher wins. The base day curve normally sits at 0.
        enabled: Set false to park a scenario without deleting it.
        days: Restrict the day curve to these weekdays. All days by default.
        activate_on: Trigger that makes this scenario claim its scope.
        release_on: Trigger that gives the scope back.
        ramp: Default ramp for this scenario's own ``set``.
        set: A flat target applied when the scenario becomes active.
        step: The day curve.
        rule: The reactions.

    """

    model_config: ClassVar[ConfigDict] = _PLAN_CONFIG

    name: str = Field(min_length=1)
    scope: list[ScopeSelector] = Field(min_length=1)
    priority: int = 0
    enabled: bool = True
    days: Annotated[list[WeekDay], Field(min_length=1)] | None = None
    activate_on: TriggerSelector | None = None
    release_on: TriggerSelector | None = None
    ramp: Duration | None = None
    set: Action | None = None
    step: list[Step] = Field(default_factory=list)
    rule: list[Rule] = Field(default_factory=list)

    @model_validator(mode="after")
    def _must_do_something(self) -> Self:
        """Reject a scenario that could never produce a write.

        Returns:
            The validated scenario.

        Raises:
            ValueError: If it has no step, rule or set, if it declares
                ``release_on`` without ``activate_on``, or if two steps share
                a clock time.

        """
        if not self.step and not self.rule and self.set is None:
            msg = (
                f"scenario {self.name!r} does nothing. Give it a 'step', a "
                f"'rule' or a 'set'"
            )
            raise ValueError(msg)
        if self.release_on is not None and self.activate_on is None:
            msg = (
                f"scenario {self.name!r} has 'release_on' but no 'activate_on', "
                f"so nothing could ever activate it"
            )
            raise ValueError(msg)
        for key in ("activate_on", "release_on"):
            selector: TriggerSelector | None = getattr(self, key)
            if selector is not None and selector.kind == TriggerKind.LIGHT_LEVEL:
                msg = (
                    f"scenario {self.name!r}: '{key}' cannot be a light_level: "
                    f"trigger; a level needs a threshold, and only a rule "
                    f"carries one"
                )
                raise ValueError(msg)
        self._reject_duplicate_steps()
        return self

    def _reject_duplicate_steps(self) -> None:
        """Reject two steps anchored to the same instant.

        Two clock steps at 09:00, or two ``sunset+30m`` steps, are a
        copy-paste slip: one of them would silently never be reached.

        Raises:
            ValueError: If two steps share an anchor.

        """
        seen: set[str] = set()
        for step in self.step:
            key = str(step.at)
            if key in seen:
                msg = f"scenario {self.name!r} has two steps at {key!r}"
                raise ValueError(msg)
            seen.add(key)

    @property
    def is_mode(self) -> bool:
        """Whether this scenario waits for a trigger before claiming its scope."""
        return self.activate_on is not None

    def uses_sun(self) -> bool:
        """Whether any anchor in this scenario needs solar times.

        Returns:
            True if a step or a rule window is anchored to a solar event.

        """
        anchors: list[ClockAnchor | SunAnchor] = [step.at for step in self.step]
        for rule in self.rule:
            if rule.between is not None:
                anchors.extend(rule.between)
        return any(isinstance(anchor, SunAnchor) for anchor in anchors)


class Defaults(BaseModel):
    """Plan-wide fallbacks for values a scenario may omit.

    Attributes:
        ramp: Transition length for any step or rule that names none.
        on_manual_change: What happens when someone changes a scope by hand.
        catchup_ramp: How fast to rejoin the curve after a restart or a
            reconnect. Short by design -- the runner keeps no durable state
            and works out where it should be from the clock.

    """

    model_config: ClassVar[ConfigDict] = _PLAN_CONFIG

    ramp: Duration = 0.0
    on_manual_change: ManualChange = "yield"
    catchup_ramp: Duration = DEFAULT_CATCHUP_RAMP


class Plan(BaseModel):
    """A whole plan: everything one or more TOML files declared.

    Attributes:
        version: Format version. Only ``1`` exists.
        location: Needed when any scenario uses a sun anchor.
        defaults: Plan-wide fallbacks.
        scenario: The scenarios, in file order.

    """

    model_config: ClassVar[ConfigDict] = _PLAN_CONFIG

    version: Literal[1] = FORMAT_VERSION
    location: Location | None = None
    defaults: Defaults = Field(default_factory=Defaults)
    scenario: list[Scenario] = Field(default_factory=list)

    @model_validator(mode="after")
    def _must_be_coherent(self) -> Self:
        """Reject a plan that cannot be executed as written.

        Returns:
            The validated plan.

        Raises:
            ValueError: If two scenarios share a name, a sun anchor is used
                with no location to compute it from, or two rules give one
                light-level sensor different thresholds.

        """
        seen: set[str] = set()
        for scenario in self.scenario:
            if scenario.name in seen:
                msg = f"two scenarios are named {scenario.name!r}"
                raise ValueError(msg)
            seen.add(scenario.name)
        self._reject_disagreeing_thresholds()

        if self.location is None:
            solar = [s.name for s in self.scenario if s.uses_sun()]
            if solar:
                names = ", ".join(repr(name) for name in solar)
                msg = (
                    f"{names} use a sun anchor, so the plan needs a [location] "
                    f"with latitude and longitude. The bridge cannot supply it: "
                    f"geolocation accepts coordinates but never returns them"
                )
                raise ValueError(msg)
        return self

    def _reject_disagreeing_thresholds(self) -> None:
        """Reject two rules that read one sensor against different thresholds.

        A trigger reaches the arbiter as the selector string it was written
        as, and one crossing fires every rule naming it. Two thresholds on one
        selector would need two crossings, so every rule naming a sensor must
        agree on where the crossing is.

        Raises:
            ValueError: If two rules disagree.

        """
        first: dict[str, tuple[str, tuple[Side, float]]] = {}
        for scenario in self.scenario:
            for rule in scenario.rule:
                threshold = rule.threshold
                if threshold is None:
                    continue
                key = str(rule.when)
                earlier = first.setdefault(key, (scenario.name, threshold))
                if earlier[1] != threshold:
                    msg = (
                        f"{key!r} is used with two thresholds ({earlier[0]!r}: "
                        f"{earlier[1][0]} {earlier[1][1]:g} lux, {scenario.name!r}: "
                        f"{threshold[0]} {threshold[1]:g} lux). A level trigger "
                        f"fires on one crossing, so every rule naming it must agree"
                    )
                    raise ValueError(msg)

    def scenarios_for_day(self, day: datetime.date) -> list[Scenario]:
        """Select the enabled scenarios whose recurrence includes a date.

        Args:
            day: The local calendar date.

        Returns:
            Matching scenarios, in file order.

        """
        weekday = list(WeekDay)[day.weekday()]
        return [
            scenario
            for scenario in self.scenario
            if scenario.enabled and (scenario.days is None or weekday in scenario.days)
        ]
