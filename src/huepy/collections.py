"""Human-facing collections for resources people address by display name."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from huepy.color import Gamut
from huepy.exceptions import AmbiguousResourceError, ResourceNotFoundError
from huepy.models import Device, Light, Room, Scene, ServiceGroup, Zone
from huepy.models.common import CommandResult, NamedResource
from huepy.models.light import Effect
from huepy.resources.base import NamedResourceHandler
from huepy.resources.device import Device as DeviceHandler
from huepy.resources.group import (
    Room as RoomHandler,
)
from huepy.resources.group import (
    Scene as SceneHandler,
)
from huepy.resources.group import (
    ServiceGroup as ServiceGroupHandler,
)
from huepy.resources.group import (
    Zone as ZoneHandler,
)
from huepy.resources.light import Light as LightHandler

if TYPE_CHECKING:
    from huepy.state import HueState


class CollectionClient(Protocol):
    """Client state needed by the high-level collection façade.

    Deliberately the non-constructing accessor rather than `state`: reading
    `hue.state` builds a graph on first access, and a stateless client should
    not import the state layer just to be told it is not tracking.
    """

    @property
    def _tracking_state(self) -> HueState | None:
        """Return the local resource graph only while it is observing."""
        ...


class NamedCollection[ModelT: NamedResource]:
    """A canonical asynchronous collection addressed by unique display name."""

    def __init__(
        self,
        hue: CollectionClient,
        handler: NamedResourceHandler[ModelT],
        model: type[ModelT],
    ) -> None:
        """Bind the collection to its client, API handler, and model type."""
        self._hue: CollectionClient = hue
        self._handler: NamedResourceHandler[ModelT] = handler
        self._model: type[ModelT] = model

    async def list(self) -> list[ModelT]:
        """Return current resources, from the bridge or the tracked graph.

        Raises:
            BridgeConnectionError: If the graph is tracked but reconnecting.
                Names resolved here target renames, deletes and commands, so a
                graph known to be stale must not answer -- it could send a
                command to the wrong light.

        """
        # Both sides of this seam are first-party: `CollectionClient` declares
        # the member and `Hue` implements it. Naming it publicly would put an
        # Optional state accessor back on the client, which is the shape this
        # release removed.
        state = self._hue._tracking_state  # noqa: SLF001 # pyright: ignore[reportPrivateUsage]
        if state is not None:
            state.ensure_resolver_healthy()
            return state.list(self._model)
        return await self._handler.list()

    async def get(self, name: str) -> ModelT:
        """Return the one resource carrying ``name``.

        Matching is case-insensitive and ignores surrounding whitespace. A
        duplicate is an error because silently choosing one is unsafe for a
        collection that can also issue commands and deletes.
        """
        wanted = name.strip().casefold()
        resources = await self.list()
        matches = [
            resource
            for resource in resources
            if wanted and resource.name and resource.name.strip().casefold() == wanted
        ]
        if not matches:
            known = sorted(resource.name for resource in resources if resource.name)
            raise ResourceNotFoundError(name, known)
        if len(matches) > 1:
            raise AmbiguousResourceError(name, [resource.id for resource in matches])
        return matches[0]

    async def names(self) -> list[str]:
        """Return sorted non-empty display names, retaining duplicates."""
        return sorted(resource.name for resource in await self.list() if resource.name)

    async def delete(self, name: str) -> CommandResult:
        """Delete the uniquely named resource."""
        return await (await self.get(name)).delete()

    async def rename(self, name: str, new_name: str) -> CommandResult:
        """Rename the uniquely named resource."""
        return await (await self.get(name)).update({"metadata": {"name": new_name}})


class LightCollection(NamedCollection[Light]):
    """Named light discovery and one-shot commands."""

    def __init__(self, hue: CollectionClient, handler: LightHandler) -> None:
        """Create a high-level light collection."""
        super().__init__(hue, handler, Light)

    async def set(  # noqa: PLR0913 - one Hue command carries the whole state
        self,
        name: str,
        *,
        on: bool | None = None,
        brightness: float | None = None,
        xy: tuple[float, float] | None = None,
        mirek: int | None = None,
        rgb: tuple[int, int, int] | None = None,
        hex_color: str | None = None,
        kelvin: int | None = None,
        gamut: Gamut | None = None,
        transition: float | None = None,
    ) -> CommandResult:
        """Resolve a light by name and apply one composed state change."""
        light = await self.get(name)
        return await light.set(
            on=on,
            brightness=brightness,
            xy=xy,
            mirek=mirek,
            rgb=rgb,
            hex_color=hex_color,
            kelvin=kelvin,
            gamut=gamut,
            transition=transition,
        )

    async def turn_on(
        self, name: str, *, transition: float | None = None
    ) -> CommandResult:
        """Switch on a uniquely named light."""
        return await (await self.get(name)).turn_on(transition=transition)

    async def turn_off(
        self, name: str, *, transition: float | None = None
    ) -> CommandResult:
        """Switch off a uniquely named light."""
        return await (await self.get(name)).turn_off(transition=transition)

    async def set_brightness(
        self,
        name: str,
        brightness: float,
        *,
        transition: float | None = None,
    ) -> CommandResult:
        """Set brightness on a uniquely named light."""
        return await (await self.get(name)).set_brightness(
            brightness, transition=transition
        )

    async def set_color(
        self,
        name: str,
        x: float,
        y: float,
        *,
        transition: float | None = None,
    ) -> CommandResult:
        """Set CIE xy colour on a uniquely named light."""
        return await (await self.get(name)).set_color(x, y, transition=transition)

    async def set_rgb(
        self,
        name: str,
        rgb: tuple[int, int, int],
        *,
        transition: float | None = None,
    ) -> CommandResult:
        """Set RGB colour on a uniquely named light."""
        return await (await self.get(name)).set_rgb(rgb, transition=transition)

    async def set_color_temperature(
        self,
        name: str,
        mirek: int,
        *,
        transition: float | None = None,
    ) -> CommandResult:
        """Set mirek colour temperature on a uniquely named light."""
        return await (await self.get(name)).set_color_temperature(
            mirek, transition=transition
        )

    async def set_kelvin(
        self,
        name: str,
        kelvin: int,
        *,
        transition: float | None = None,
    ) -> CommandResult:
        """Set Kelvin colour temperature on a uniquely named light."""
        return await (await self.get(name)).set_kelvin(kelvin, transition=transition)

    async def set_effect(self, name: str, effect: Effect | str) -> CommandResult:
        """Run an effect on a uniquely named light."""
        return await (await self.get(name)).set_effect(effect)

    async def set_gradient(
        self,
        name: str,
        colors: list[tuple[float, float]],
        *,
        mode: str | None = None,
    ) -> CommandResult:
        """Set a gradient on a uniquely named light."""
        return await (await self.get(name)).set_gradient(colors, mode=mode)

    async def set_powerup(self, name: str, preset: str) -> CommandResult:
        """Set power-up behavior on a uniquely named light."""
        return await (await self.get(name)).set_powerup(preset)

    async def alert(self, name: str) -> CommandResult:
        """Pulse a uniquely named light once."""
        return await (await self.get(name)).alert()


class GroupCollection[ModelT: Room | Zone](NamedCollection[ModelT]):
    """Commands shared by named rooms and zones."""

    async def set(  # noqa: PLR0913 - one Hue command carries the whole state
        self,
        name: str,
        *,
        on: bool | None = None,
        brightness: float | None = None,
        xy: tuple[float, float] | None = None,
        mirek: int | None = None,
        rgb: tuple[int, int, int] | None = None,
        hex_color: str | None = None,
        kelvin: int | None = None,
        gamut: Gamut | None = None,
        transition: float | None = None,
    ) -> CommandResult:
        """Resolve a group by name and apply one composed state change."""
        group = await self.get(name)
        return await group.set(
            on=on,
            brightness=brightness,
            xy=xy,
            mirek=mirek,
            rgb=rgb,
            hex_color=hex_color,
            kelvin=kelvin,
            gamut=gamut,
            transition=transition,
        )

    async def turn_on(
        self, name: str, *, transition: float | None = None
    ) -> CommandResult:
        """Switch on a uniquely named room or zone."""
        return await (await self.get(name)).turn_on(transition=transition)

    async def turn_off(
        self, name: str, *, transition: float | None = None
    ) -> CommandResult:
        """Switch off a uniquely named room or zone."""
        return await (await self.get(name)).turn_off(transition=transition)

    async def set_brightness(
        self,
        name: str,
        brightness: float,
        *,
        transition: float | None = None,
    ) -> CommandResult:
        """Set brightness in a uniquely named room or zone."""
        return await (await self.get(name)).set_brightness(
            brightness, transition=transition
        )

    async def set_color(
        self,
        name: str,
        x: float,
        y: float,
        *,
        transition: float | None = None,
    ) -> CommandResult:
        """Set CIE xy colour in a uniquely named room or zone."""
        return await (await self.get(name)).set_color(x, y, transition=transition)

    async def set_rgb(
        self,
        name: str,
        rgb: tuple[int, int, int],
        *,
        transition: float | None = None,
    ) -> CommandResult:
        """Set RGB colour in a uniquely named room or zone."""
        return await (await self.get(name)).set_rgb(rgb, transition=transition)

    async def set_color_temperature(
        self,
        name: str,
        mirek: int,
        *,
        transition: float | None = None,
    ) -> CommandResult:
        """Set mirek colour temperature in a uniquely named room or zone."""
        return await (await self.get(name)).set_color_temperature(
            mirek, transition=transition
        )

    async def set_kelvin(
        self,
        name: str,
        kelvin: int,
        *,
        transition: float | None = None,
    ) -> CommandResult:
        """Set Kelvin colour temperature in a uniquely named room or zone."""
        return await (await self.get(name)).set_kelvin(kelvin, transition=transition)


class RoomCollection(GroupCollection[Room]):
    """Named room collection."""

    def __init__(self, hue: CollectionClient, handler: RoomHandler) -> None:
        """Create a high-level room collection."""
        super().__init__(hue, handler, Room)


class ZoneCollection(GroupCollection[Zone]):
    """Named zone collection."""

    def __init__(self, hue: CollectionClient, handler: ZoneHandler) -> None:
        """Create a high-level zone collection."""
        super().__init__(hue, handler, Zone)


class SceneCollection(NamedCollection[Scene]):
    """Named scene collection."""

    def __init__(self, hue: CollectionClient, handler: SceneHandler) -> None:
        """Create a high-level scene collection."""
        super().__init__(hue, handler, Scene)

    async def activate(self, name: str) -> CommandResult:
        """Activate the uniquely named scene."""
        return await (await self.get(name)).activate()


class DeviceCollection(NamedCollection[Device]):
    """Named physical-device collection."""

    def __init__(self, hue: CollectionClient, handler: DeviceHandler) -> None:
        """Create a high-level device collection."""
        super().__init__(hue, handler, Device)


class ServiceGroupCollection(NamedCollection[ServiceGroup]):
    """Named arbitrary-service-group collection."""

    def __init__(self, hue: CollectionClient, handler: ServiceGroupHandler) -> None:
        """Create a high-level service-group collection."""
        super().__init__(hue, handler, ServiceGroup)
