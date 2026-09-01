"""What the plan layer needs from a client, and nothing more.

The seam exists for the same reason :mod:`huepy.recording.protocol` does: the
plan layer must not import :mod:`huepy.client.base`, or the import graph stops
being acyclic. A :class:`~huepy.Hue` satisfies this structurally, and so does a
fake, so every test in this package runs without a bridge.

Two members are all a plan needs. ``snapshot`` answers "what is on this bridge,
and what is it called", which is how a name in a TOML file becomes a resource
id. ``http`` is how a write goes out -- and going through the transport rather
than a bound model is deliberate, because the transport is where the bridge's
write pacing is enforced.
"""

from collections.abc import Callable
from typing import Protocol, runtime_checkable

from huepy.client.protocol import Transport
from huepy.models import AnyResource
from huepy.state.records import Change, Resync


@runtime_checkable
class PlanClient(Protocol):
    """A client a plan can be resolved against and executed on."""

    @property
    def http(self) -> Transport:
        """The open transport."""
        ...

    async def snapshot(self) -> list[AnyResource]:
        """Fetch the bridge's aggregate-visible resource graph in one request."""
        ...


class Cancellable(Protocol):
    """A registration that can be undone."""

    def cancel(self) -> None:
        """Stop delivering to the handler this registration created."""
        ...


@runtime_checkable
class ChangeSource(Protocol):
    """Somewhere observed resource changes arrive from.

    Narrow on purpose. A plan needs two facts: when a light moved and whether
    this client moved it, and when the stream lost continuity so its own
    beliefs are stale. It has no business reading the state graph. Declaring
    only these means :class:`~huepy.state.HueState` satisfies the protocol
    without the plan layer importing the engine, and a test can satisfy it in a
    few lines.
    """

    def on_change(self, handler: Callable[[Change], None], /) -> Cancellable:
        """Register a handler for every observed change."""
        ...

    def on_resync(self, handler: Callable[[Resync], None], /) -> Cancellable:
        """Register a handler for every break in continuity."""
        ...
