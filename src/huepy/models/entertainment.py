"""Models for the entertainment configuration resources.

These describe the REST side of Hue Entertainment -- the areas, their channels
and streaming status. The low-latency streaming itself runs over a separate
UDP/DTLS protocol that this library does not implement; here you can list,
configure, and start or stop an entertainment area.
"""

from pydantic import Field

from huepy.models.common import (
    HueModel,
    HueResource,
    NamedResource,
    ResourceIdentifier,
)


class Entertainment(HueResource):
    """The entertainment service a light exposes for streaming."""

    renderer: bool | None = None
    renderer_reference: ResourceIdentifier | None = None
    proxy: bool | None = None
    equalizer: bool | None = None
    max_streams: int | None = None


class EntertainmentChannel(HueModel):
    """One addressable channel of an entertainment configuration."""

    channel_id: int | None = None
    position: dict[str, float] | None = None
    members: list[dict[str, object]] = Field(default_factory=list)


class StreamProxy(HueModel):
    """The node relaying the entertainment stream to the lights."""

    mode: str | None = None
    node: ResourceIdentifier | None = None


class EntertainmentConfiguration(NamedResource):
    """A configured entertainment area: its channels, members and status."""

    configuration_type: str | None = None
    status: str | None = None
    active_streamer: ResourceIdentifier | None = None
    stream_proxy: StreamProxy | None = None
    channels: list[EntertainmentChannel] = Field(default_factory=list)
    light_services: list[ResourceIdentifier] = Field(default_factory=list)

    @property
    def is_streaming(self) -> bool:
        """Whether this area is currently streaming."""
        return self.status == "active"
