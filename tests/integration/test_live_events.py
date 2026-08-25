"""Event-stream checks against a real bridge.

The stream is the one part of the library that cannot be exercised by asking
the bridge a question: something has to actually change. These tests make a
real change and assert the event that comes back, then restore.
"""

import asyncio
import contextlib

import pytest

from huepy import Hue, models
from huepy.models.event import HueEvent

pytestmark = pytest.mark.integration

LISTEN_SETTLE = 2.0
"""Seconds to let the subscription establish before changing anything."""

EVENT_TIMEOUT = 15.0
"""Seconds to wait for the bridge to push the change back."""


async def collect_until(
    hue: Hue,
    predicate,
    timeout: float = EVENT_TIMEOUT,  # noqa: ASYNC109 - a wait budget, not a cancel scope
) -> list[HueEvent]:
    """Gather events until `predicate` matches one, or the timeout expires."""
    received: list[HueEvent] = []

    async def listen() -> None:
        async for event in hue.get_event_stream():
            received.append(event)
            if predicate(event):
                return

    task = asyncio.create_task(listen())
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
    except TimeoutError:
        pass
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    return received


class TestEventStream:
    async def test_a_real_change_arrives_as_a_typed_event(
        self, hue: Hue, a_light: models.Light
    ):
        target = 33.0

        async def change() -> None:
            await asyncio.sleep(LISTEN_SETTLE)
            await a_light.set(on=True, brightness=target)

        changer = asyncio.create_task(change())
        events = await collect_until(
            hue,
            lambda event: any(
                resource.id == a_light.id and resource.dimming is not None
                for resource in event.data
            ),
        )
        await changer

        assert events, "the bridge pushed no events at all"
        assert all(isinstance(event, HueEvent) for event in events)

        matching = [
            resource
            for event in events
            for resource in event.data
            if resource.id == a_light.id and resource.dimming is not None
        ]
        assert matching, f"no dimming event for {a_light.name}"
        assert matching[0].dimming is not None
        assert matching[0].dimming.brightness == pytest.approx(target, abs=2.0)

    async def test_events_carry_resolvable_names(self, hue: Hue, a_light: models.Light):
        """Regression: service ids in events used to resolve to "Unknown".

        The name map is populated explicitly or by tracking; a stateless client
        that has done neither answers "Unknown" for everything, which would
        make this assert its own setup rather than the resolution it is here
        to check.
        """
        _ = await hue.refresh_names()
        # A no-op change pushes no event, so move brightness somewhere it
        # certainly is not already.
        target = 20.0 if (a_light.brightness or 0.0) > 50.0 else 80.0

        async def change() -> None:
            await asyncio.sleep(LISTEN_SETTLE)
            await a_light.set(on=True, brightness=target)

        changer = asyncio.create_task(change())
        events = await collect_until(
            hue, lambda event: any(r.id == a_light.id for r in event.data)
        )
        await changer

        named = [
            hue.get_name(resource.id) for event in events for resource in event.data
        ]
        assert named, "no events to check"
        assert any(name != "Unknown" for name in named), (
            f"every event id was unresolvable: {named}"
        )

    async def test_stream_is_finalised_by_close(self, hue: Hue):
        """A caller who stops iterating must not strand the response."""
        stream = hue.get_event_stream()
        task = asyncio.create_task(anext(stream, None))
        await asyncio.sleep(0.5)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, StopAsyncIteration):
            await task
        await stream.aclose()
