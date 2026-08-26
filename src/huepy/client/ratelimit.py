"""Client-side pacing for writes to the bridge.

The bridge turns each write into ZigBee traffic and warns that exceeding roughly
ten commands per second to a light, or one per second to a group, makes it
buffer commands and eventually drop them. This limiter spaces the *start* of
each write so a burst -- a snapshot restore fanning out over a room, say -- stays
within those bounds without the caller having to think about it. The socket
concurrency cap still lets the spaced requests overlap on the wire.

Sustained high-rate updates to many lights are a different problem the REST API
is not built for; those belong on the Entertainment streaming API instead.

Typical usage example:

    limiter = RateLimiter()
    await limiter.acquire("/clip/v2/resource/light/abc")
"""

import asyncio
import time
from collections.abc import Awaitable, Callable

__all__ = ["GROUP_MIN_GAP", "LIGHT_MIN_GAP", "RateLimiter", "bucket_for"]

LIGHT_MIN_GAP = 0.1
"""Seconds between consecutive light writes (~10/s, per the performance note)."""

GROUP_MIN_GAP = 1.0
"""Seconds between consecutive broadcast writes (grouped_light and scene recall)."""

_LIGHT_BUCKET = "light"
_BROADCAST_BUCKET = "broadcast"

_GAPS = {_LIGHT_BUCKET: LIGHT_MIN_GAP, _BROADCAST_BUCKET: GROUP_MIN_GAP}

# Writes the bridge turns into a ZigBee broadcast. They share one budget because
# the ~1/s cap is on broadcasts system-wide, not per resource: a grouped_light
# PUT and a scene recall both spend from it.
_BROADCAST_TYPES = frozenset({"grouped_light", "scene"})

# The light budget is deliberately one bridge-wide floor, not per light: the
# documented ~10/s is per light, but ZigBee airtime is shared, so pacing all
# light writes through a single budget keeps a many-light burst (a room restore)
# from flooding the mesh. Key the bucket by resource id if per-light throughput
# ever needs the full headroom.


def bucket_for(path: str) -> str | None:
    """Return the rate-limit bucket a request path falls in, or None.

    Args:
        path: A request path, e.g. ``/clip/v2/resource/light/abc``.

    Returns:
        The bucket key, or None when the path is not throttled.

    """
    parts = [segment for segment in path.split("/") if segment]
    if "resource" not in parts:
        return None
    index = parts.index("resource")
    if index + 1 >= len(parts):
        return None
    resource_type = parts[index + 1]
    if resource_type == _LIGHT_BUCKET:
        return _LIGHT_BUCKET
    if resource_type in _BROADCAST_TYPES:
        return _BROADCAST_BUCKET
    return None


class RateLimiter:
    """Spaces the start of throttled writes to stay within the bridge's budget.

    Attributes:
        enabled: Whether pacing is applied at all.

    """

    def __init__(
        self,
        *,
        enabled: bool = True,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        """Initialise the limiter.

        Args:
            enabled: Whether to pace at all; False makes :meth:`acquire` a no-op.
            clock: A monotonic clock source, in seconds. Injectable for tests.
            sleep: An async sleep. Injectable so tests advance a virtual clock
                instead of waiting on the wall.

        """
        self.enabled: bool = enabled
        self._clock: Callable[[], float] = clock
        self._sleep: Callable[[float], Awaitable[None]] = sleep
        self._locks: dict[str, asyncio.Lock] = {}
        self._last: dict[str, float] = {}

    async def acquire(self, path: str) -> None:
        """Wait until a write to ``path`` may start.

        Returns immediately for paths that are not throttled, or when the
        limiter is disabled. Otherwise it sleeps just long enough that the start
        of this write is at least the bucket's minimum gap after the previous
        one. The bucket lock is held only across that sleep, not the network I/O
        that follows, so spaced requests still overlap on the wire.

        Args:
            path: The request path about to be sent.

        """
        if not self.enabled:
            return
        bucket = bucket_for(path)
        if bucket is None:
            return
        gap = _GAPS[bucket]
        lock = self._locks.setdefault(bucket, asyncio.Lock())
        async with lock:
            last = self._last.get(bucket)
            now = self._clock()
            if last is not None:
                wait = last + gap - now
                if wait > 0:
                    await self._sleep(wait)
                    now = self._clock()
            self._last[bucket] = now
