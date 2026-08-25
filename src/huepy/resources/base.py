"""Generic CRUD behaviour shared by every resource handler.

A resource handler binds a v2 resource type to the model its payloads parse
into, so ``hue.api.lights.get(...)`` returns a
:class:`~huepy.models.light.Light`
rather than an untyped dict.

Handlers whose resources carry a ``metadata.name`` use
:class:`NamedResourceHandler` as a type-level marker. Human-facing lookup
lives in :mod:`huepy.collections`; the API handlers remain strictly id-based.

Typical usage example:

    class Scene(NamedResourceHandler[models.Scene]):
        resource_type = ResourceType.SCENE
        model = models.Scene
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar, cast

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

    def __init__(self, hue: HueClient) -> None:
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

    async def list(self) -> list[ModelT]:
        """Fetch every resource of this type.

        Returns:
            All resources the bridge reports, each bound to this handler's
            client. May be an empty list.

        """
        payload = await self.hue.http.get(self.base_url)
        return [self._bind(resource) for resource in unwrap(payload, self._model)]

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
    """Id-based handler whose model happens to carry a display name."""
