"""Turning a desired state into the fewest writes that achieve it.

The bridge accepts a transition of up to 6,000,000 ms in a single PUT -- a
hundred minutes -- and runs it itself. That one fact shapes this module. A
ninety-minute sunset fade is *one request* followed by silence, not a tick
loop re-asserting every ninety seconds the way a vendor-agnostic controller has
to. The bridge budgets roughly ten writes a second to lights and one a second
to groups, so the difference between one write and sixty is the difference
between fitting comfortably inside that budget and crowding it.

Three rules follow from the bridge's own performance guidance, and each saves
ZigBee traffic that buys nothing:

* **A room is written through its ``grouped_light``**, one broadcast rather
  than one unicast per bulb. :mod:`huepy.plans.resolve` picks the path.
* **A ramp longer than the ceiling is chained**, not stepped. A three-hour fade
  becomes two PUTs with an interpolated waypoint between them.
* **``on`` is not re-sent to a light that is already on.** Each attribute in a
  payload is a separate ZigBee message, so a needless ``on`` makes a
  brightness change cost twice what it should.

Typical usage example:

    await run_fade(client, binding, target, ramp=7200, start=previous)
"""

import asyncio
import logging
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from huepy.models.common import ResourceIdentifier, unwrap
from huepy.models.state import (
    MAX_TRANSITION_MILLISECONDS,
    MILLISECONDS_PER_SECOND,
)
from huepy.plans.protocol import PlanClient
from huepy.plans.resolve import Binding
from huepy.plans.schema import Action
from huepy.plans.timeline import interpolate

logger = logging.getLogger(__name__)

MAX_TRANSITION_SECONDS = MAX_TRANSITION_MILLISECONDS / MILLISECONDS_PER_SECOND
"""The longest fade the bridge will run from a single PUT, in seconds.

Measured on a BSB002 at CLIP 1.78.0: 6,000,000 ms was accepted and 6,000,001
rejected. Not a documented figure -- the API reference gives no bound at all --
so it is pinned by the fixtures in ``tests/fixtures/durability_probe.json``.
"""

type Sleeper = Callable[[float], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class Segment:
    """One PUT, and how long to wait before sending it.

    Attributes:
        path: The request path.
        payload: The body, already in the bridge's shape.
        delay: Seconds to wait after the previous segment lands. Zero for the
            first.
        duration: The fade this segment asks the bridge to run, in seconds.

    """

    path: str
    payload: dict[str, Any]
    delay: float
    duration: float


def _segment_count(ramp: float) -> int:
    """How many PUTs a ramp of this length needs.

    Args:
        ramp: The fade length in seconds.

    Returns:
        At least one; more when the ramp outruns the bridge's ceiling.

    """
    if ramp <= MAX_TRANSITION_SECONDS:
        return 1
    return math.ceil(ramp / MAX_TRANSITION_SECONDS)


def _without_on(payload: dict[str, Any]) -> dict[str, Any]:
    """Drop the ``on`` key from a payload.

    Args:
        payload: A bridge payload.

    Returns:
        The payload without ``on``. The original is left alone.

    """
    return {key: value for key, value in payload.items() if key != "on"}


def _changes_nothing(payload: dict[str, Any]) -> bool:
    """Whether a payload would spend a write without moving anything.

    ``dynamics`` only says *how* to get somewhere, so a body carrying nothing
    else is a request the bridge would accept and act on by doing nothing.
    Dropping a redundant ``on`` is what usually leaves one behind: a scope
    already on, asked to be on, has nothing left to say.

    Args:
        payload: The payload about to be sent.

    Returns:
        True when the request is not worth making.

    """
    return not (set(payload) - {"dynamics"})


def _on_is_redundant(
    payload: dict[str, Any],
    *,
    index: int,
    current_on: bool | None,
) -> bool:
    """Whether a payload's ``on`` key would tell the bridge nothing new.

    Each attribute in a payload becomes its own ZigBee message, so switching a
    light on that is already on makes a brightness change cost twice what it
    should. Two cases are free to drop: a later segment of a chained fade,
    where the first segment already turned the scope on, and a scope known to
    be on already. Switching *off* is never redundant.

    Args:
        payload: The payload about to be sent.
        index: Which segment of the fade this is.
        current_on: Whether the scope is already on, when that is known.

    Returns:
        True when ``on`` can be dropped.

    """
    if "on" not in payload:
        return False
    if index > 0:
        return True
    return payload["on"] == {"on": True} and current_on is True


def plan_segments(
    binding: Binding,
    target: Action,
    *,
    ramp: float,
    start: Action | None = None,
    current_on: bool | None = None,
) -> list[Segment]:
    """Work out the writes that take a scope to a target over a ramp.

    Pure: no clock, no client. The runner schedules what comes back.

    A ramp within the bridge's ceiling is one PUT and nothing else -- the
    bridge runs the fade. A longer one is split into equal segments with
    interpolated waypoints, which needs ``start`` to interpolate *from*. Without
    it the whole change is sent at the longest single fade the bridge allows,
    so the scope still arrives at the right place, just earlier than asked.

    Args:
        binding: The resolved scope being written to.
        target: Where the scope should end up.
        ramp: How long it should take, in seconds.
        start: The settled state the fade begins from, when it is known.
        current_on: Whether the scope is already on, when that is known.

    Returns:
        The segments, in order. Never empty unless the target asks for nothing.

    """
    if ramp < 0:
        ramp = 0.0
    count = _segment_count(ramp)

    if count > 1 and start is None:
        message = (
            "%s: a %.0fs ramp needs a starting state to be chained; sending it "
            "as a single %.0fs fade instead, which will arrive early"
        )
        logger.warning(message, binding.name, ramp, MAX_TRANSITION_SECONDS)
        count = 1
        ramp = MAX_TRANSITION_SECONDS

    length = ramp / count
    segments: list[Segment] = []
    skipped = 0.0
    for index in range(count):
        fraction = (index + 1) / count
        waypoint = target if count == 1 else interpolate(start, target, fraction)
        payload = waypoint.to_payload(transition=length)

        if _on_is_redundant(payload, index=index, current_on=current_on):
            payload = _without_on(payload)

        if _changes_nothing(payload):
            # Carry this segment's slot forward. Dropping it without doing so
            # would fire everything after it one segment-length early.
            skipped += length
            continue
        segments.append(
            Segment(
                path=binding.path,
                payload=payload,
                delay=0.0 if not segments else length + skipped,
                duration=length,
            )
        )
        skipped = 0.0
    return segments


async def send(client: PlanClient, segment: Segment) -> None:
    """Send one segment, without waiting for its delay.

    The response goes through :func:`~huepy.models.common.unwrap`, which is not
    ceremony: the v2 API reports many rejections inside a 200 body, so a raw
    ``put`` would let a refused write look like a success. The runner would
    then record the fade as in force and never re-drive the scope, stranding it
    silently.

    Write pacing is deliberately not applied here: the transport enforces the
    bridge's budget for every request, and a second limiter would only make the
    two disagree.

    Args:
        client: The client to write through.
        segment: The segment to send.

    Raises:
        HueResponseError: If the bridge rejected the write in its body.

    """
    _ = unwrap(await client.http.put(segment.path, segment.payload), ResourceIdentifier)


async def send_chain(
    client: PlanClient,
    segments: list[Segment],
    *,
    sleep: Sleeper = asyncio.sleep,
) -> int:
    """Send a run of segments, waiting between them.

    Only used for the tail of a long fade. The caller sends the first segment
    itself so that a plain fade is complete -- and any write error visible --
    by the time it returns.

    Args:
        client: The client to write through.
        segments: The segments still to send, in order.
        sleep: How to wait between them. Injectable so a simulated three-hour
            fade costs no wall-clock time.

    Returns:
        How many requests were sent.

    """
    sent = 0
    for segment in segments:
        if segment.delay:
            await sleep(segment.delay)
        await send(client, segment)
        sent += 1
    return sent


async def run_fade(  # noqa: PLR0913 - a fade is defined by all of these
    client: PlanClient,
    binding: Binding,
    target: Action,
    *,
    ramp: float,
    start: Action | None = None,
    current_on: bool | None = None,
    sleep: Sleeper = asyncio.sleep,
) -> int:
    """Drive a scope to a target, chaining segments if the ramp is long.

    Awaiting this holds for the length of the fade when it needs more than one
    segment. The runner uses :func:`plan_segments` and :func:`send_chain`
    directly so it can keep that tail cancellable; this is the straightforward
    form for callers driving a scope by hand.

    Args:
        client: The client to write through.
        binding: The resolved scope.
        target: Where the scope should end up.
        ramp: How long it should take, in seconds.
        start: The settled state the fade begins from, when known.
        current_on: Whether the scope is already on, when known.
        sleep: How to wait between segments.

    Returns:
        How many requests were sent.

    """
    segments = plan_segments(
        binding, target, ramp=ramp, start=start, current_on=current_on
    )
    return await send_chain(client, segments, sleep=sleep)
