"""Generic CRUD behaviour shared by every resource handler.

A resource handler binds a v2 resource type to the model its payloads parse
into, so ``hue.light.get(...)`` returns a :class:`~huepy.models.light.Light`
rather than an untyped dict.

Handlers whose resources carry a ``metadata.name`` extend
:class:`NamedResourceHandler` instead, which adds lookup by the name a human
uses rather than by the bridge's opaque id.

Typical usage example:

    class Scene(NamedResourceHandler[models.Scene]):
        resource_type = ResourceType.SCENE
        model = models.Scene
"""

from collections.abc import Coroutine, Sequence
from typing import TYPE_CHECKING, Any, ClassVar, cast

from huepy.exceptions import ResourceNotFoundError
from huepy.models.common import (
    HueModel,
    HueResource,
    NamedResource,
    ResourceIdentifier,
    ResourceType,
    unwrap,
    unwrap_one,
)

if TYPE_CHECKING:
    from huepy.client.protocol import HueClient


class BaseResource[ModelT: HueModel]:
    """Async handler for one v2 resource type.

    Attributes:
        resource_type: The API resource type this handler addresses.
        model: The model class payloads are parsed into.

    """

    resource_type: ClassVar[ResourceType]
    model: ClassVar[type[HueModel]]

    def __init__(self, hue: "HueClient") -> None:
        """Bind the handler to a client.

        Args:
            hue: The client used to issue requests.

        """
        self.hue: HueClient = hue
        self.base_url: str = f"/clip/v2/resource/{self.resource_type}"

    @property
    def _model(self) -> type[ModelT]:
        """Return the model class, narrowed to this handler's type parameter.

        ``model`` must be a ClassVar so subclasses can set it declaratively,
        and a ClassVar cannot be written in terms of a type parameter -- hence
        the cast. Each subclass pairs ``model`` with its own ``ModelT``.
        """
        return cast("type[ModelT]", self.model)

    def _bind(self, resource: ModelT) -> ModelT:
        """Attach this handler's client to a freshly parsed resource.

        Only addressable resources can be bound; the handful of models that
        are pure payloads rather than resources are passed through untouched.

        Args:
            resource: The resource just parsed out of a response.

        Returns:
            The same instance, bound where binding applies.

        """
        if isinstance(resource, HueResource):
            _ = resource.bind(self.hue, self.resource_type)
        return resource

    async def get_all(self) -> list[ModelT]:
        """Fetch every resource of this type.

        Returns:
            All resources the bridge reports, each bound to this handler's
            client. May be an empty list.

        """
        payload = await self.hue.http.get(self.base_url)
        return [self._bind(resource) for resource in unwrap(payload, self._model)]

    async def all(self) -> list[ModelT]:
        """Fetch every resource of this type.

        The short form of :meth:`get_all`, for the common case where the verb
        adds nothing: ``await hue.light.all()``.

        Returns:
            All resources the bridge reports, each bound to this handler's
            client. May be an empty list.

        """
        return await self.get_all()

    async def get(self, resource_id: str) -> ModelT:
        """Fetch one resource by id.

        Args:
            resource_id: The resource's id.

        Returns:
            The parsed resource, bound to this handler's client so it can
            issue its own commands.

        Raises:
            HueResponseError: If the bridge returns no such resource.

        """
        payload = await self.hue.http.get(f"{self.base_url}/{resource_id}")
        return self._bind(unwrap_one(payload, self._model))

    async def update(
        self,
        resource_id: str,
        data: dict[str, Any],
    ) -> list[ResourceIdentifier]:
        """Apply a partial update to one resource.

        Args:
            resource_id: The resource's id.
            data: The fields to change, in the bridge's payload shape.

        Returns:
            References to the resources the bridge reports as updated.

        Raises:
            HueResponseError: If the bridge rejects the update.

        """
        payload = await self.hue.http.put(f"{self.base_url}/{resource_id}", data)
        return unwrap(payload, ResourceIdentifier)

    async def delete(self, resource_id: str) -> list[ResourceIdentifier]:
        """Delete one resource.

        Args:
            resource_id: The resource's id.

        Returns:
            References to the resources the bridge reports as deleted.

        Raises:
            HueResponseError: If the bridge rejects the deletion.

        """
        payload = await self.hue.http.delete(f"{self.base_url}/{resource_id}")
        if payload is None:
            return []
        return unwrap(payload, ResourceIdentifier)

    async def _create(self, data: dict[str, Any]) -> list[ResourceIdentifier]:
        """Create a resource of this type.

        Args:
            data: The new resource's payload.

        Returns:
            References to the resources the bridge created.

        Raises:
            HueResponseError: If the bridge rejects the creation.

        """
        payload = await self.hue.http.post(self.base_url, data)
        return unwrap(payload, ResourceIdentifier)


class NamedResourceHandler[ModelT: NamedResource](BaseResource[ModelT]):
    """Handler for a resource type whose resources carry a display name.

    The bridge addresses everything by opaque id, but people think in names:
    "Kitchen", not "a1b2c3d4-...". The lookups here close that gap, and fail
    with the names that *do* exist so a typo is self-correcting.

    The v2 API offers no server-side name filter, so every method here fetches
    the whole collection and matches locally. One call is one round trip --
    resolving several names in a loop issues one request per iteration, and is
    better served by a single :meth:`get_all` matched over in the caller.
    """

    @staticmethod
    def _display_names(resources: Sequence[NamedResource]) -> list[str]:
        """Collect the display names actually in use.

        Args:
            resources: The resources to read names off.

        Returns:
            Their names, sorted alphabetically. Resources the bridge left
            unnamed are skipped; duplicates are kept, since the bridge allows
            two resources to share a name.

        """
        return sorted(resource.name for resource in resources if resource.name)

    async def by_name(self, name: str) -> ModelT:
        """Fetch the resource with the given display name.

        Matching ignores case and surrounding whitespace, so ``"kitchen"``,
        ``"Kitchen"`` and ``" Kitchen "`` all find the same room.

        A Hue bridge permits two resources to share a name. When several
        match, the first in the bridge's own order is returned: duplicates are
        legal, so refusing to answer would be less useful than answering
        predictably.

        Costs one round trip -- the whole collection is fetched, then matched
        in the client.

        Args:
            name: The display name to look for.

        Returns:
            The first matching resource, bound to this handler's client so it
            can issue its own commands.

        Raises:
            ResourceNotFoundError: If no resource carries that name. The error
                carries the names that do, in its ``known`` attribute and in
                its message.

        """
        wanted = name.strip().casefold()
        resources = await self.get_all()
        match = next(
            (
                resource
                for resource in resources
                if resource.name.strip().casefold() == wanted
            ),
            None,
        )
        if match is None:
            raise ResourceNotFoundError(name, self._display_names(resources))
        return match

    def __getitem__(self, name: str) -> Coroutine[Any, Any, ModelT]:
        """Look a resource up by display name, as a subscript.

        This subscript is *asynchronous*: it does not return a resource, it
        returns the coroutine :meth:`by_name` would have, so the result must be
        awaited::

            kitchen = await hue.rooms["Kitchen"]

        Awaiting it costs one round trip, and matches exactly as
        :meth:`by_name` does -- case-insensitively, ignoring surrounding
        whitespace, first match wins.

        Args:
            name: The display name to look for.

        Returns:
            An awaitable resolving to the matching resource. Awaiting it
            raises :class:`~huepy.exceptions.ResourceNotFoundError` when no
            resource carries that name.

        """
        return self.by_name(name)

    async def names(self) -> list[str]:
        """List the display names of this resource type.

        For discovery -- ``await hue.rooms.names()`` answers "what may I ask
        for?" -- and for reporting a bad name back to a user.

        Costs one round trip.

        Returns:
            Every display name in use, sorted alphabetically. Resources the
            bridge left unnamed are omitted, as they cannot be looked up by
            name; duplicates are kept, since the bridge allows them.

        """
        return self._display_names(await self.get_all())
