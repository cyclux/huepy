"""Turning the names in a plan file into resource ids on this bridge.

Everything that can be wrong about a plan's *names* is found here, in one pass,
before a single write goes out: a room that does not exist, a sensor that was
renamed, a room with no lights in it. All of them are reported together rather
than one per run, because fixing a config file five typos deep one error at a
time is miserable.

Resolution takes exactly one snapshot. It is also where the plan's scopes learn
the cheapest way to write to themselves: a room or a zone resolves to the
``grouped_light`` service it owns, so driving a whole room costs one broadcast
instead of one write per bulb.

Sensors need a small indirection. A ``motion`` or ``button`` service carries no
name of its own -- the *device* that owns it does -- so ``motion:Hall sensor``
means "the motion service belonging to the device called Hall sensor".

Typical usage example:

    resolved = await resolve(hue, plan)
"""

from collections import defaultdict
from dataclasses import dataclass

from huepy.exceptions import PlanError
from huepy.models import (
    AnyResource,
    Contact,
    Device,
    Light,
    LightLevel,
    Motion,
    Room,
    Zone,
)
from huepy.models.common import ResourceType
from huepy.plans.fields import ScopeKind, Selector, TriggerKind
from huepy.plans.protocol import PlanClient
from huepy.plans.schema import Plan, Scenario

_SERVICE_FOR_TRIGGER: dict[str, str] = {
    TriggerKind.MOTION: "motion",
    TriggerKind.BUTTON: "button",
    TriggerKind.CONTACT: "contact",
    TriggerKind.LIGHT_LEVEL: "light_level",
}


@dataclass(frozen=True, slots=True)
class Binding:
    """One scope selector, bound to the resource a write goes to.

    Attributes:
        selector: The selector as written in the plan.
        path: The request path a write for this scope is sent to.
        light_ids: The lights this scope actually moves. A room's write goes
            to one ``grouped_light``, but the bridge reports the change on
            each member light, so the runner needs both to tell its own fades
            apart from someone reaching for a switch.

    """

    selector: Selector
    path: str
    light_ids: tuple[str, ...]

    @property
    def name(self) -> str:
        """The name this scope was written as, for logs and errors."""
        return self.selector.name


@dataclass(frozen=True, slots=True)
class TriggerBinding:
    """One trigger selector, bound to the services that can fire it.

    Attributes:
        selector: The selector as written in the plan.
        resource_ids: The service ids whose events fire this trigger. Empty
            for a ``signal:``, which comes from the hosting application rather
            than the bridge. A device may own several services of one kind --
            a four-button dimmer switch is one device and four ``button``
            services -- so this is a set rather than a single id.

    """

    selector: Selector
    resource_ids: tuple[str, ...]

    @property
    def is_signal(self) -> bool:
        """Whether this trigger comes from the application, not the bridge."""
        return self.selector.kind == TriggerKind.SIGNAL


@dataclass(frozen=True, slots=True)
class ResolvedPlan:
    """A plan with every name in it bound to this bridge.

    Attributes:
        plan: The plan as written.
        scopes: Bindings per scenario name, in the order the scenario listed
            them.
        triggers: Bindings per trigger selector, keyed by its written form.
        warnings: Things that resolved but will not behave as the plan
            expects -- a sensor disabled on the bridge, which resolves fine
            and never fires. Not errors, because disabling a sensor in the
            app for a week should not stop the rest of the plan running.

    """

    plan: Plan
    scopes: dict[str, tuple[Binding, ...]]
    triggers: dict[str, TriggerBinding]
    warnings: tuple[str, ...] = ()

    def scope_of(self, scenario: Scenario) -> tuple[Binding, ...]:
        """Look up the bindings a scenario writes to.

        Args:
            scenario: The scenario to look up.

        Returns:
            Its bindings, in plan order.

        """
        return self.scopes.get(scenario.name, ())


@dataclass(frozen=True, slots=True)
class _Catalog:
    """A snapshot indexed by kind and display name.

    Names map to lists rather than single values so a duplicate is reported as
    ambiguous instead of one of the two silently winning a command.

    Attributes:
        rooms: Rooms by name.
        zones: Zones by name.
        lights: Lights by name.
        devices: Devices by name.
        all_lights: Every light, for resolving group membership.
        disabled: Ids of sensor services switched off on the bridge.

    """

    rooms: dict[str, list[Room]]
    zones: dict[str, list[Zone]]
    lights: dict[str, list[Light]]
    devices: dict[str, list[Device]]
    all_lights: list[Light]
    disabled: frozenset[str]


def _index(resources: list[AnyResource]) -> _Catalog:
    """Group a snapshot by kind and display name.

    Args:
        resources: Everything the aggregate endpoint returned.

    Returns:
        The indexed catalog.

    """
    rooms: dict[str, list[Room]] = defaultdict(list)
    zones: dict[str, list[Zone]] = defaultdict(list)
    lights: dict[str, list[Light]] = defaultdict(list)
    devices: dict[str, list[Device]] = defaultdict(list)
    all_lights: list[Light] = []
    disabled: set[str] = set()

    for resource in resources:
        if isinstance(resource, Room):
            rooms[resource.name].append(resource)
        elif isinstance(resource, Zone):
            zones[resource.name].append(resource)
        elif isinstance(resource, Light):
            lights[resource.name].append(resource)
            all_lights.append(resource)
        elif isinstance(resource, Device):
            devices[resource.name].append(resource)
        elif (
            isinstance(resource, (Motion, Contact, LightLevel)) and not resource.enabled
        ):
            # Buttons have no switch; these three do, and a plan naming one
            # that is off in the app would resolve cleanly and never fire.
            disabled.add(resource.id)
    return _Catalog(
        rooms=rooms,
        zones=zones,
        lights=lights,
        devices=devices,
        all_lights=all_lights,
        disabled=frozenset(disabled),
    )


def _pick[T](
    candidates: dict[str, list[T]],
    selector: Selector,
    problems: list[str],
) -> T | None:
    """Choose the single resource a selector names, or record why not.

    Args:
        candidates: Resources of the right kind, by name.
        selector: The selector being resolved.
        problems: Accumulator for human-readable failures.

    Returns:
        The matching resource, or None when there is not exactly one.

    """
    matches = candidates.get(selector.name, [])
    if not matches:
        known = ", ".join(sorted(candidates)) or "none"
        problems.append(f"{selector}: no such {selector.kind}. Known: {known}")
        return None
    if len(matches) > 1:
        msg = (
            f"{selector}: {len(matches)} {selector.kind}s share that name, "
            f"so it cannot be resolved. Rename one of them"
        )
        problems.append(msg)
        return None
    return matches[0]


def _bind_scope(
    selector: Selector,
    catalog: _Catalog,
    problems: list[str],
) -> Binding | None:
    """Bind one scope selector to the resource its writes go to.

    Args:
        selector: The scope as written.
        catalog: The indexed snapshot.
        problems: Accumulator for human-readable failures.

    Returns:
        The binding, or None when the name could not be resolved.

    """
    if selector.kind == ScopeKind.LIGHT:
        light = _pick(catalog.lights, selector, problems)
        if light is None:
            return None
        return Binding(
            selector=selector,
            path=f"/clip/v2/resource/light/{light.id}",
            light_ids=(light.id,),
        )

    group: Room | Zone | None = (
        _pick(catalog.rooms, selector, problems)
        if selector.kind == ScopeKind.ROOM
        else _pick(catalog.zones, selector, problems)
    )
    if group is None:
        return None

    # A room or zone is not writable itself; the grouped_light service it owns
    # is. Going through it costs one ZigBee broadcast instead of one unicast
    # per bulb, which is the difference between fitting in the bridge's budget
    # and not.
    service = group.service_id(ResourceType.GROUPED_LIGHT)
    if service is None:
        msg = (
            f"{selector}: that {selector.kind} has no grouped_light service, "
            f"so it cannot be driven as a whole"
        )
        problems.append(msg)
        return None

    members = tuple(
        light.id for light in catalog.all_lights if group.contains_light(light)
    )
    if not members:
        problems.append(f"{selector}: that {selector.kind} contains no lights")
        return None
    return Binding(
        selector=selector,
        path=f"/clip/v2/resource/grouped_light/{service}",
        light_ids=members,
    )


def _bind_trigger(
    selector: Selector,
    catalog: _Catalog,
    problems: list[str],
    warnings: list[str],
) -> TriggerBinding | None:
    """Bind one trigger selector to the services that can fire it.

    Args:
        selector: The trigger as written.
        catalog: The indexed snapshot.
        problems: Accumulator for human-readable failures.
        warnings: Accumulator for things that bind but will not fire.

    Returns:
        The binding, or None when the name could not be resolved.

    """
    if selector.kind == TriggerKind.SIGNAL:
        # Nothing on the bridge to bind: the application fires this one.
        return TriggerBinding(selector=selector, resource_ids=())

    device = _pick(catalog.devices, selector, problems)
    if device is None:
        return None

    wanted = _SERVICE_FOR_TRIGGER[selector.kind]
    services = tuple(
        service.rid for service in device.services if service.rtype == wanted
    )
    if not services:
        available = ", ".join(sorted({s.rtype for s in device.services})) or "none"
        msg = (
            f"{selector}: the device {selector.name!r} has no {wanted} service. "
            f"It exposes: {available}"
        )
        problems.append(msg)
        return None
    if all(service in catalog.disabled for service in services):
        msg = (
            f"{selector}: the sensor is disabled on the bridge, "
            f"so this trigger will never fire"
        )
        warnings.append(msg)
    return TriggerBinding(selector=selector, resource_ids=services)


def _triggers_of(scenario: Scenario) -> list[Selector]:
    """Every trigger selector a scenario mentions.

    Args:
        scenario: The scenario to scan.

    Returns:
        Its activate, release and rule triggers, in that order.

    """
    found = [
        selector
        for selector in (scenario.activate_on, scenario.release_on)
        if selector is not None
    ]
    found.extend(rule.when for rule in scenario.rule)
    return found


def bind(resources: list[AnyResource], plan: Plan) -> ResolvedPlan:
    """Bind every name in a plan against one snapshot.

    Pure: the snapshot is a parameter, so a plan can be bound against a
    fixture, or against a snapshot the caller already holds, without a second
    request.

    Args:
        resources: Everything the aggregate endpoint returned.
        plan: The plan to bind.

    Returns:
        The plan with every scope and trigger bound.

    Raises:
        PlanError: If any name is unknown, ambiguous, or names something that
            cannot do what the plan asks of it. Every such problem is reported
            together, so one run of ``huepy plan validate`` finds them all.

    """
    catalog = _index(resources)

    problems: list[str] = []
    warnings: list[str] = []
    scopes: dict[str, tuple[Binding, ...]] = {}
    triggers: dict[str, TriggerBinding] = {}

    for scenario in plan.scenario:
        bound = [
            _bind_scope(selector, catalog, problems) for selector in scenario.scope
        ]
        scopes[scenario.name] = tuple(item for item in bound if item is not None)

        for selector in _triggers_of(scenario):
            key = str(selector)
            if key in triggers:
                continue
            trigger = _bind_trigger(selector, catalog, problems, warnings)
            if trigger is not None:
                triggers[key] = trigger

    if problems:
        body = "\n".join(f"  {problem}" for problem in problems)
        count = len(problems)
        noun = "name" if count == 1 else "names"
        msg = f"could not resolve {count} {noun} against this bridge:\n{body}"
        raise PlanError(msg)

    return ResolvedPlan(
        plan=plan, scopes=scopes, triggers=triggers, warnings=tuple(warnings)
    )


async def resolve(client: PlanClient, plan: Plan) -> ResolvedPlan:
    """Bind every name in a plan to a resource on this bridge.

    Args:
        client: The client to resolve against. One snapshot is taken.
        plan: The plan to resolve.

    Returns:
        The plan with every scope and trigger bound.

    Raises:
        PlanError: If any name does not resolve; see :func:`bind`.

    """
    return bind(await client.snapshot(), plan)
