"""Build the id-to-display-name lookup behind :meth:`huepy.Hue.get_name`.

A Hue light appears under two ids: the light *service* and the *device* that
owns it. Both should resolve to the same human-readable name, so each named
resource contributes an entry for its own id and, when it is a device service,
one for its owner too.

Named resources also *contain* services -- a room owns a ``grouped_light``, a
switch owns its buttons -- and those services have no name of their own. The
event stream is largely made of them, so each one is mapped to the name of
whatever contains it. Without this, most events resolve to "Unknown".

Typical usage example:

    names = build_name_map(devices, lights, rooms)
    names[light.id]  # -> "Desk Lamp"
"""

from collections.abc import Iterable

from huepy.models.common import NamedResource
from huepy.models.device import Device
from huepy.models.group import ResourceGroup


def build_name_map(*groups: Iterable[NamedResource]) -> dict[str, str]:
    """Map every resource id, and its owning device id, to a display name.

    A resource's own id always wins over a name it inherits from a container,
    so a service that is itself named keeps its own name.

    Args:
        *groups: Iterables of named resources, applied in order. Later groups
            win where ids collide.

    Returns:
        A mapping of resource id to display name, skipping unnamed resources.

    """
    names: dict[str, str] = {}
    for group in groups:
        for resource in group:
            name = resource.metadata.name
            if not name:
                continue
            names[resource.id] = name
            if resource.owner is not None:
                names[resource.owner.rid] = name
            if isinstance(resource, ResourceGroup | Device):
                for service in resource.services:
                    # setdefault semantics: a service that carries its own
                    # name keeps it rather than inheriting its container's.
                    if service.rid not in names:
                        names[service.rid] = name
    return names
