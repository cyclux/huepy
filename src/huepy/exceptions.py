"""Exception hierarchy for huepy.

Every error raised by this package derives from :class:`HueError`, so callers
can catch that one type and still narrow further when they care.
"""


class HueError(Exception):
    """Base exception for all Hue errors."""


class AuthenticationError(HueError):
    """Raised when authentication fails or no application key is available."""


class BridgeConnectionError(HueError):
    """Raised when the bridge cannot be reached."""


class HueAPIError(HueError):
    """Raised when the bridge answers with a non-success HTTP status.

    Attributes:
        status_code: The HTTP status returned by the bridge.
        message: The response body, as text.

    """

    def __init__(self, status_code: int, message: str) -> None:
        """Initialise the error.

        Args:
            status_code: The HTTP status returned by the bridge.
            message: The response body, as text.

        """
        self.status_code: int = status_code
        self.message: str = message
        super().__init__(f"API Error {status_code}: {message}")


class HueResponseError(HueError):
    """Raised when a 2xx response carries errors in its body.

    The v2 CLIP API reports many failures this way rather than through the
    HTTP status, so a request can "succeed" and still have been rejected.

    Attributes:
        errors: The error descriptions returned by the bridge.

    """

    def __init__(self, errors: list[str]) -> None:
        """Initialise the error.

        Args:
            errors: The error descriptions returned by the bridge.

        """
        self.errors: list[str] = errors
        super().__init__("; ".join(errors) or "Bridge reported an unspecified error")


class StateNotStartedError(HueError):
    """Raised when the local state graph is read before it is observing.

    ``hue.state`` exists from construction so handlers and sinks can be
    registered before the stream opens, but a graph that has never taken a
    snapshot holds nothing. Returning an empty list there would report "no
    lights" instead of "not tracking yet", so reads raise until the observer
    is running. Start it with ``Hue(state=True)`` or ``async with hue.state``.
    """


class DetachedResourceError(HueError):
    """Raised when a command is issued on a model that has no client.

    Models returned by a resource handler are *bound* to the client that
    fetched them, so they can act on themselves. A model built by hand --
    ``models.Light(id="abc")``, or one parsed straight from an event -- has no
    client to talk to. Fetch it via ``hue.<resource>.get(...)`` to get a bound
    one.
    """


class ResourceNotFoundError(HueError):
    """Raised when a lookup finds no resource matching the requested name.

    Attributes:
        name: The name that was looked for.
        known: The names that were available to match against.

    """

    def __init__(self, name: str, known: list[str]) -> None:
        """Initialise the error.

        Args:
            name: The name that was looked for.
            known: The names that were available to match against.

        """
        self.name: str = name
        self.known: list[str] = known
        available = ", ".join(known) if known else "none"
        super().__init__(f"No resource named {name!r}. Known names: {available}")


class AmbiguousResourceError(HueError):
    """Raised when a high-level name matches more than one resource."""

    def __init__(self, name: str, resource_ids: list[str]) -> None:
        """Record the requested name and every resource it could address."""
        self.name: str = name
        self.resource_ids: list[str] = resource_ids
        matches = ", ".join(resource_ids)
        super().__init__(
            f"More than one resource is named {name!r}. Matching ids: {matches}"
        )
