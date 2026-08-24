"""End-to-end state-layer checks against an explicitly selected bridge."""

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from contextlib import aclosing
from types import TracebackType
from typing import Any

import pytest
from pydantic import JsonValue

from huepy import Hue, models
from huepy.client.protocol import (
    EventConnection,
    SSEFrame,
    Transport,
    WriteObserver,
)
from huepy.state import Change, Resync, ResyncReason

pytestmark = pytest.mark.integration

EVENT_TIMEOUT = 15


class OverflowGapTransport:
    """Pause the first live connection after one frame to force overflow."""

    def __init__(self, inner: Transport) -> None:
        self.inner = inner
        self.gap_started = asyncio.Event()
        self.release = asyncio.Event()
        self.resumed_from: list[str | None] = []

    async def get(self, path: str) -> JsonValue:
        return await self.inner.get(path)

    async def put(self, path: str, data: dict[str, Any]) -> JsonValue:
        return await self.inner.put(path, data)

    async def post(self, path: str, data: dict[str, Any]) -> JsonValue:
        return await self.inner.post(path, data)

    async def delete(self, path: str) -> JsonValue:
        return await self.inner.delete(path)

    async def authenticate(self, app_name: str = "huepy", timeout: int = 60) -> str:  # noqa: ASYNC109 - mirrors protocol
        return await self.inner.authenticate(app_name, timeout)

    def subscribe_events(
        self, *, max_retries: int | None = 10
    ) -> AsyncGenerator[dict[str, Any]]:
        return self.inner.subscribe_events(max_retries=max_retries)

    def subscribe_event_frames(
        self, *, max_retries: int | None = 10
    ) -> AsyncGenerator[SSEFrame]:
        return self.inner.subscribe_event_frames(max_retries=max_retries)

    async def _paused_frames(
        self, frames: AsyncIterator[SSEFrame]
    ) -> AsyncGenerator[SSEFrame]:
        async for frame in frames:
            yield frame
            if frame.event_id is not None:
                self.gap_started.set()
                await self.release.wait()
                return

    async def event_connections(
        self, *, max_retries: int | None = 10
    ) -> AsyncGenerator[EventConnection]:
        source = self.inner.event_connections(max_retries=max_retries)
        first = True
        async with aclosing(source):
            async for connection in source:
                self.resumed_from.append(connection.resumed_from)
                if first:
                    first = False
                    yield EventConnection(
                        opened_at=connection.opened_at,
                        resumed_from=connection.resumed_from,
                        frames=self._paused_frames(connection.frames),
                    )
                else:
                    yield connection

    def add_write_observer(self, observer: WriteObserver) -> Callable[[], None]:
        return self.inner.add_write_observer(observer)

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        await self.inner.__aexit__(exc_type, exc_val, exc_tb)


async def test_snapshot_preserves_known_and_future_resource_types(hue: Hue):
    snapshot = await hue.snapshot()
    assert snapshot
    assert any(type(resource) is not models.HueResource for resource in snapshot)
    assert all(resource.is_bound for resource in snapshot)


async def test_state_tracks_a_write_and_marks_a_fade_echo(
    hue: Hue,
    a_light: models.Light,
):
    async with hue.state() as state:
        current = state.lights.get(a_light.id)
        assert current is not None
        target = 20.0 if (current.brightness or 0) > 50 else 80.0

        async def matching_change() -> Change:
            async for item in state.changes():
                if isinstance(item, Change) and item.resource_id == a_light.id:
                    return item
            raise AssertionError

        waiting = asyncio.create_task(matching_change())
        await current.set(on=True, brightness=target, transition=60)
        change = await asyncio.wait_for(waiting, EVENT_TIMEOUT)

        assert isinstance(change.after, models.Light)
        assert change.after.brightness == pytest.approx(target, abs=2)
        assert change.origin == "self"
        assert change.observation == "command_echo"
        assert change.command_confirmed is True
        assert a_light.id in state.fading


async def test_live_overflow_replays_marks_and_reconciles(
    hue: Hue,
    a_light: models.Light,
):
    gap = OverflowGapTransport(hue.http)
    hue._http = gap

    async with hue.state() as state:
        current = state.lights.get(a_light.id)
        assert current is not None

        async def reconnect_marker() -> Resync:
            async for item in state.changes():
                if isinstance(item, Resync) and item.reason is ResyncReason.RECONNECT:
                    return item
            raise AssertionError

        waiting = asyncio.create_task(reconnect_marker())
        await asyncio.sleep(0)
        initial = 20.0 if (current.brightness or 0) > 50 else 80.0
        await current.set(on=True, brightness=initial)
        await asyncio.wait_for(gap.gap_started.wait(), EVENT_TIMEOUT)

        try:
            for index in range(80):
                await current.set(brightness=20.0 if index % 2 == 0 else 80.0)
                await asyncio.sleep(0.3)
        finally:
            gap.release.set()

        marker = await asyncio.wait_for(waiting, 30)
        tracked = state.lights.get(a_light.id)
        assert marker.reason is ResyncReason.RECONNECT
        assert len(gap.resumed_from) >= 2
        assert gap.resumed_from[1] is not None
        assert tracked is not None
        assert tracked.brightness == pytest.approx(80, abs=2)
