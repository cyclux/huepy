"""Fixtures for tests that drive a real bridge.

These tests physically change the lights, so they are guarded twice: the
`integration` marker is excluded by default (see `addopts` in pyproject), and
every fixture here refuses to run unless HUEPY_INTEGRATION=1 is set explicitly.

Every fixture that changes state snapshots it first and restores it afterwards,
including when the test fails. `restore_all_lights` is the belt-and-braces net
that puts the whole bridge back even if a test dies between snapshot and
restore.

    HUEPY_INTEGRATION=1 uv run pytest -m integration
"""

import asyncio
import os
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from types import TracebackType
from typing import Any

import pytest
from pydantic import JsonValue

from huepy import BridgeConnectionError, Hue, models
from huepy.client.http import WriteObserver

OPT_IN_ENV = "HUEPY_INTEGRATION"


@dataclass(frozen=True)
class Sent:
    """One request the client actually put on the wire."""

    method: str
    path: str
    data: dict[str, Any] | None


def _require_opt_in() -> None:
    if os.getenv(OPT_IN_ENV) != "1":
        pytest.skip(
            f"integration tests change real lights; set {OPT_IN_ENV}=1 to run them"
        )


@pytest.fixture(scope="session")
def opt_in() -> None:
    """Skip the whole suite unless the operator asked for it."""
    _require_opt_in()


@pytest.fixture
async def hue(opt_in: None) -> AsyncIterator[Hue]:
    """Open a client, or skip when no bridge answers."""
    client = Hue()
    try:
        await client.start()
    except (BridgeConnectionError, ValueError) as exc:
        pytest.skip(f"no reachable bridge: {exc}")
    try:
        yield client
    finally:
        await client.close()


SETTLE_BEFORE_SNAPSHOT = 1.5
"""Seconds to let an earlier test's transition finish before snapshotting.

Bridge state is eventually consistent. Snapshotting mid-fade captures an
intermediate brightness and then faithfully "restores" the light to a value it
never actually had -- observed drifting a light from 49.4% to 29.6% across a
suite run.
"""


@pytest.fixture
async def restore_all_lights(hue: Hue) -> AsyncIterator[None]:
    """Snapshot every light and put them all back when the test ends."""
    await asyncio.sleep(SETTLE_BEFORE_SNAPSHOT)
    before = [light.capture() for light in await hue.light.get_all()]
    try:
        yield
    finally:
        for state in before:
            light = await hue.light.get(state.light_id)
            try:
                await light.restore(state)
            except BridgeConnectionError:  # pragma: no cover - hardware dependent
                pytest.fail(f"could not restore light {state.light_id}")
        await asyncio.sleep(SETTLE_BEFORE_SNAPSHOT)


@pytest.fixture
async def a_light(hue: Hue, restore_all_lights: None) -> models.Light:
    """One dimmable light, restored by the fixture above.

    Dimmable specifically: a bridge also reports smart plugs as lights, and
    they reject `.dimming.brightness` outright. `dimming is not None` is how
    the bridge says a light can actually dim.
    """
    lights = await hue.light.get_all()
    usable = [
        light
        for light in lights
        if light.dimming is not None and "bad" not in light.name.casefold()
    ]
    if not usable:
        pytest.skip("no dimmable lights on this bridge")
    return usable[0]


@pytest.fixture
async def a_colour_light(hue: Hue, restore_all_lights: None) -> models.Light:
    """One colour-capable light, restored by the fixture above."""
    lights = await hue.light.get_all()
    usable = [light for light in lights if light.color is not None]
    if not usable:
        pytest.skip("no colour-capable lights on this bridge")
    return usable[0]


@pytest.fixture
async def a_room(hue: Hue, restore_all_lights: None) -> models.Room:
    """One room that owns a grouped_light, restored by the fixture above."""
    rooms = await hue.room.get_all()
    usable = [
        room
        for room in rooms
        if room.service_id(models.ResourceType.GROUPED_LIGHT) is not None
    ]
    if not usable:
        pytest.skip("no room with a grouped_light on this bridge")
    return usable[0]


@pytest.fixture
def request_counter() -> Callable[[Hue], list[Sent]]:  # noqa: C901
    """Wrap a client's transport so a test can inspect what it really sent."""

    def install(hue: Hue) -> list[Sent]:  # noqa: C901
        calls: list[Sent] = []
        inner = hue.http

        class Counting:
            async def get(self, path: str) -> JsonValue:
                calls.append(Sent("GET", path, None))
                return await inner.get(path)

            async def put(self, path: str, data: dict[str, Any]) -> JsonValue:
                calls.append(Sent("PUT", path, data))
                return await inner.put(path, data)

            async def post(self, path: str, data: dict[str, Any]) -> JsonValue:
                calls.append(Sent("POST", path, data))
                return await inner.post(path, data)

            async def delete(self, path: str) -> JsonValue:
                calls.append(Sent("DELETE", path, None))
                return await inner.delete(path)

            async def authenticate(self, app_name: str = "huepy", timeout: int = 60):  # noqa: ASYNC109 - mirrors the Transport protocol
                return await inner.authenticate(app_name, timeout)

            def subscribe_events(self, *, max_retries: int | None = 10):
                return inner.subscribe_events(max_retries=max_retries)

            def subscribe_event_frames(self, *, max_retries: int | None = 10):
                return inner.subscribe_event_frames(max_retries=max_retries)

            def event_connections(self, *, max_retries: int | None = 10):
                return inner.event_connections(max_retries=max_retries)

            def add_write_observer(self, observer: WriteObserver) -> Callable[[], None]:
                return inner.add_write_observer(observer)

            async def __aexit__(
                self,
                exc_type: type[BaseException] | None,
                exc_val: BaseException | None,
                exc_tb: TracebackType | None,
            ) -> None:
                await inner.__aexit__(exc_type, exc_val, exc_tb)

        # _http is the seam the unit suite uses too; this is the same trick.
        hue._http = Counting()
        return calls

    return install
