"""The client surface resource handlers depend on.

Resource handlers need a transport and nothing else. Depending on this
protocol rather than on the concrete :class:`~huepy.client.base.Hue` keeps the
import graph acyclic: ``client.base`` imports ``resources``, and ``resources``
must therefore not import ``client.base`` back.
"""

from typing import Protocol

from huepy.client.http import Transport


class HueClient(Protocol):
    """A client that can issue requests to a bridge."""

    @property
    def http(self) -> Transport:
        """The open transport."""
        ...
