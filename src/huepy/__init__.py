"""huepy: a modern async Python wrapper for the Philips Hue v2 CLIP API.

Every call is async and every response is a validated pydantic model.

Typical usage example:

    import asyncio
    from huepy import Hue

    async def main() -> None:
        async with Hue() as hue:
            kitchen = await hue.rooms["Kitchen"]
            await kitchen.set(brightness=30, kelvin=2200, transition=2.0)

    asyncio.run(main())
"""

import logging

from huepy import color, models
from huepy._version import package_version
from huepy.client.base import Hue
from huepy.client.http import HueHttpClient
from huepy.config import HueConfig, InsecureConfigWarning
from huepy.exceptions import (
    AuthenticationError,
    BridgeConnectionError,
    DetachedResourceError,
    HueAPIError,
    HueError,
    HueResponseError,
    ResourceNotFoundError,
)

# A library must not configure logging for its host application; this only
# suppresses "no handler found" warnings when the host configures nothing.
logging.getLogger(__name__).addHandler(logging.NullHandler())

__version__ = package_version()

__all__ = [
    "AuthenticationError",
    "BridgeConnectionError",
    "DetachedResourceError",
    "Hue",
    "HueAPIError",
    "HueConfig",
    "HueError",
    "HueHttpClient",
    "HueResponseError",
    "InsecureConfigWarning",
    "ResourceNotFoundError",
    "color",
    "models",
]
