"""Read-only checks against a real bridge.

These carry no risk -- nothing here changes state -- but they are the highest
value tests in the suite: every bug the unit tests could not catch has been a
parsing failure against real firmware, not a logic error.
"""

from typing import Any

import pytest

from huepy import Hue, ResourceNotFoundError, models
from huepy.resources.base import BaseResource, NamedResourceHandler

pytestmark = pytest.mark.integration


def handlers(hue: Hue) -> dict[str, BaseResource[Any]]:
    """Every resource handler on the client, deduplicated by identity."""
    seen: dict[int, tuple[str, BaseResource[Any]]] = {}
    for name, value in vars(hue).items():
        if isinstance(value, BaseResource):
            seen.setdefault(id(value), (name, value))
    return dict(seen.values())


class TestEveryResourceTypeParses:
    """Regression: real firmware sends shapes no fixture predicted.

    `powerup.dimming` arrives as `{"mode": "previous"}` with no brightness,
    which failed the inner model's required field and took *every* light down
    with it -- caught only by pointing this at real hardware.
    """

    async def test_all_resource_types_parse(self, hue: Hue):
        failures: list[str] = []
        for name, handler in handlers(hue).items():
            try:
                await handler.get_all()
            except Exception as exc:  # noqa: BLE001 - reporting every failure
                failures.append(f"{name}: {type(exc).__name__}: {exc}")
        assert not failures, "resource types that failed to parse:\n" + "\n".join(
            failures
        )

    async def test_lights_expose_their_convenience_properties(self, hue: Hue):
        lights = await hue.lights.all()
        assert lights, "bridge reports no lights"
        for light in lights:
            assert isinstance(light.is_on, bool)
            assert light.brightness is None or 0.0 <= light.brightness <= 100.0
            assert light.name


class TestBindingAgainstRealPayloads:
    async def test_fetched_resources_are_bound(self, hue: Hue):
        light = (await hue.lights.all())[0]
        assert light.is_bound

    async def test_refresh_returns_a_new_bound_instance(self, hue: Hue):
        light = (await hue.lights.all())[0]
        again = await light.refresh()
        assert again.id == light.id
        assert again is not light
        assert again.is_bound


class TestNameLookup:
    async def test_every_named_handler_resolves_its_own_names(self, hue: Hue):
        for name, handler in handlers(hue).items():
            if not isinstance(handler, NamedResourceHandler):
                continue
            for display in await handler.names():
                found = await handler.by_name(display)
                assert found.name == display, f"{name}.by_name({display!r}) mismatched"

    async def test_lookup_ignores_case_and_whitespace(self, hue: Hue):
        rooms = await hue.rooms.all()
        if not rooms:
            pytest.skip("no rooms on this bridge")
        wanted = rooms[0].name
        found = await hue.rooms[f"  {wanted.upper()}  "]
        assert found.id == rooms[0].id

    async def test_a_miss_lists_the_real_names(self, hue: Hue):
        with pytest.raises(ResourceNotFoundError) as caught:
            await hue.rooms["definitely-not-a-room"]
        known = {room.name for room in await hue.rooms.all()}
        assert set(caught.value.known) == known


class TestNameMap:
    """Regression: the event stream is mostly service ids.

    Before contained services inherited their container's name, a live bridge
    resolved 74 ids; afterwards, 163. Most events read "Unknown" until then.
    """

    async def test_room_grouped_lights_resolve_to_the_room(self, hue: Hue):
        for room in await hue.rooms.all():
            service = room.service_id(models.ResourceType.GROUPED_LIGHT)
            if service is not None:
                assert hue.get_name(service) == room.name

    async def test_device_services_resolve_to_the_device(self, hue: Hue):
        for device in await hue.devices.all():
            for service in device.services:
                assert hue.get_name(service.rid) != "Unknown", (
                    f"{service.rtype} service of {device.name} is unnamed"
                )

    async def test_zones_and_scenes_are_in_the_map(self, hue: Hue):
        for zone in await hue.zones.all():
            assert hue.get_name(zone.id) == zone.name
        for scene in await hue.scenes.all():
            assert hue.get_name(scene.id) == scene.name
