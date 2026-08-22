"""Models for the bridge's Server-Sent Events stream.

The bridge pushes one JSON *array* per SSE ``data:`` line, each entry an
event describing one or more resources whose state just changed. Parsing
that array with :func:`parse_events` turns it into typed objects while
staying deliberately forgiving: a long-running stream must not die because
a future firmware sends an event shape this library has never seen.

:meth:`huepy.Hue.get_event_stream` parses for you and yields these models
directly; :func:`parse_events` is for callers reading the raw stream through
``hue.http.subscribe_events()``.

Typical usage example:

    async for event in hue.get_event_stream():
        if event.is_update:
            print(event.resource_ids)
"""

from enum import StrEnum
from typing import cast

from pydantic import Field

from huepy.models.common import (
    Color,
    ColorTemperature,
    Dimming,
    HueModel,
    On,
    ResourceIdentifier,
)


class EventType(StrEnum):
    """The `type` values the event stream is known to send."""

    UPDATE = "update"
    ADD = "add"
    DELETE = "delete"
    ERROR = "error"


class EventResource(HueModel):
    """One resource's changed state inside an event.

    Only the fields that actually change are present, so every state
    section is optional. The typed sections cover the states that change
    most often; anything else the bridge sends is still preserved on
    ``model_extra``.
    """

    id: str = ""
    type: str = ""
    id_v1: str | None = None
    owner: ResourceIdentifier | None = None
    on: On | None = None
    dimming: Dimming | None = None
    color: Color | None = None
    color_temperature: ColorTemperature | None = None


class HueEvent(HueModel):
    """A single event from the bridge's event stream.

    ``type`` is kept as a plain ``str`` rather than an :class:`EventType`
    on purpose: an event type this library does not know about would
    otherwise raise a validation error and kill a stream that is meant to
    run for weeks. Use :attr:`event_type` to get the enum member when the
    value is one of the recognised ones.
    """

    id: str = ""
    type: str = ""
    creationtime: str | None = None
    data: list[EventResource] = Field(default_factory=list)

    @property
    def event_type(self) -> EventType | None:
        """The event's type as an enum member, or None if it is unrecognised."""
        return EventType(self.type) if self.type in EventType else None

    @property
    def resource_ids(self) -> list[str]:
        """The ids of every resource carried by this event."""
        return [resource.id for resource in self.data]

    @property
    def is_update(self) -> bool:
        """Whether this event reports changed state on existing resources."""
        return self.event_type is EventType.UPDATE

    @property
    def is_delete(self) -> bool:
        """Whether this event reports resources that no longer exist."""
        return self.event_type is EventType.DELETE


def parse_events(payload: object) -> list[HueEvent]:
    """Parse one decoded SSE payload into events.

    The bridge sends an array, but a bare object is accepted too. Entries
    that are not objects are skipped rather than raised on, so one piece of
    garbage cannot end the stream for the events either side of it.

    Args:
        payload: The decoded JSON of one SSE ``data:`` line.

    Returns:
        The parsed events, which is an empty list when the payload is
        neither an object nor an array.

    """
    if isinstance(payload, dict):
        entries: list[object] = [payload]
    elif isinstance(payload, list):
        entries = cast("list[object]", payload)
    else:
        return []
    return [
        HueEvent.model_validate(entry) for entry in entries if isinstance(entry, dict)
    ]
