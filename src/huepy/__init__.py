"""huepy: a modern async Python wrapper for the Philips Hue v2 CLIP API.

Every call is async and every response is a validated pydantic model.

Typical usage example:

    import asyncio
    from huepy import Hue

    async def main() -> None:
        async with Hue() as hue:
            await hue.rooms.set(
                "Kitchen", brightness=30, kelvin=2200, transition=2.0
            )

    asyncio.run(main())
"""

import logging

from huepy import color, models
from huepy._version import package_version
from huepy.client.base import Hue
from huepy.client.http import HueHttpClient
from huepy.config import HueConfig, InsecureConfigWarning
from huepy.exceptions import (
    AmbiguousResourceError,
    AuthenticationError,
    BridgeConnectionError,
    DetachedResourceError,
    HueAPIError,
    HueError,
    HueResponseError,
    ResourceNotFoundError,
    StateNotStartedError,
)
from huepy.models.common import CommandResult

# A library must not configure logging for its host application; this only
# suppresses "no handler found" warnings when the host configures nothing.
logging.getLogger(__name__).addHandler(logging.NullHandler())

__version__ = package_version()

__all__ = [
    "AmbiguousResourceError",
    "AuthenticationError",
    "BridgeConnectionError",
    "CommandResult",
    "DetachedResourceError",
    "Hue",
    "HueAPIError",
    "HueConfig",
    "HueError",
    "HueHttpClient",
    "HueResponseError",
    "InsecureConfigWarning",
    "ResourceNotFoundError",
    "StateNotStartedError",
    "color",
    "models",
]
