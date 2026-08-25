"""End-to-end checks of the high-level state surface against a real bridge.

The unit suite feeds the recorder synthesized `Change` objects. What only a
real bridge can prove is that genuine payloads survive the round trip: that a
light service resolves to the name a human gave it, that the schema's extracted
columns match what the bridge actually sends, and that the documented queries
answer on real rows.
"""

import asyncio
import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from huepy import BridgeConnectionError, Hue, models
from huepy.recording import ChangeEntry, JSONLSink, SQLiteSink
from huepy.state import Change, ChangeKind, Subscription

pytestmark = pytest.mark.integration

EVENT_TIMEOUT = 15
TRANSITION = 0.4


def other_brightness(light: models.Light) -> float:
    """Return a brightness clearly different from the current one."""
    return 20.0 if (light.brightness or 0) > 50 else 80.0


@pytest.fixture
async def tracking_hue(opt_in: None, a_light: models.Light) -> AsyncIterator[Hue]:
    """Open a second client with tracking on, as the README example does.

    Depends on `a_light` so the stateless fixture client has already snapshotted
    every light and will restore them when this test ends.
    """
    del a_light
    client = Hue(state=True)
    try:
        await client.start()
    except (BridgeConnectionError, ValueError) as exc:  # pragma: no cover - hardware
        pytest.skip(f"no reachable bridge: {exc}")
    try:
        yield client
    finally:
        await client.close()


async def test_state_true_populates_the_graph_from_the_bridge(tracking_hue: Hue):
    """The headline entry point, against real topology."""
    state = tracking_hue.state
    assert state.tracking is True
    assert state.connected is True
    if not state.lights.names():
        pytest.skip("no named lights on this bridge")
    assert state.resources


async def test_a_named_handler_sees_a_real_write(
    tracking_hue: Hue,
    a_light: models.Light,
):
    """Name resolution walks service -> owning device on the real graph.

    A fake transport cannot prove that the name a handler filters on is the
    name the bridge actually reports for that light's service.
    """
    state = tracking_hue.state
    tracked = state.lights.by_id(a_light.id)
    assert tracked is not None
    target = other_brightness(tracked)

    seen: asyncio.Queue[Change] = asyncio.Queue()
    subscription: Subscription = state.on_change(seen.put_nowait, name=tracked.name)
    assert subscription.active is True

    await tracked.set(on=True, brightness=target, transition=TRANSITION)
    change = await asyncio.wait_for(seen.get(), EVENT_TIMEOUT)

    assert change.resource_id == a_light.id
    assert change.kind is ChangeKind.UPDATE
    context = state.describe(change)
    assert context.name == tracked.name
    # Room may legitimately be None; what must hold is that it resolves without
    # raising against real topology.
    assert context.room is None or isinstance(context.room, models.Room)

    subscription.cancel()
    assert subscription.active is False


async def test_watch_yields_a_real_change_and_no_markers(
    tracking_hue: Hue,
    a_light: models.Light,
):
    state = tracking_hue.state
    tracked = state.lights.by_id(a_light.id)
    assert tracked is not None
    target = other_brightness(tracked)

    stream = state.watch(resource_id=a_light.id)
    try:
        await tracked.set(on=True, brightness=target, transition=TRANSITION)
        change = await asyncio.wait_for(anext(stream), EVENT_TIMEOUT)
        assert change.resource_id == a_light.id
        # `watch()` is typed `AsyncGenerator[Change]`, so asserting the type
        # proves nothing; what is worth pinning is that the filter narrowed to
        # the light we asked for and the record carries a usable resource.
        assert isinstance(change.after, models.Light)
    finally:
        await stream.aclose()


async def test_recording_persists_real_changes_and_answers_its_queries(
    opt_in: None,
    a_light: models.Light,
    tmp_path: Path,
):
    """The whole recording path on genuine bridge payloads.

    Real resources are far richer than the synthesized ones the unit suite
    uses -- gradients, effects, mirek schemas, unknown firmware fields -- so
    this is what proves `payload` round-trips and the extracted columns match.
    """
    database = tmp_path / "history.sqlite3"
    lines = tmp_path / "history.jsonl"
    client = Hue(record=[SQLiteSink(database), JSONLSink(lines)])
    try:
        await client.start()
    except (BridgeConnectionError, ValueError) as exc:  # pragma: no cover - hardware
        pytest.skip(f"no reachable bridge: {exc}")

    try:
        assert client.state.tracking is True, "record= must imply state=True"
        assert client.recorder is not None
        tracked = client.state.lights.by_id(a_light.id)
        assert tracked is not None
        name = tracked.name
        target = other_brightness(tracked)

        seen: asyncio.Queue[Change] = asyncio.Queue()
        client.state.on_change(seen.put_nowait, resource_id=a_light.id)
        await tracked.set(on=True, brightness=target, transition=TRANSITION)
        _ = await asyncio.wait_for(seen.get(), EVENT_TIMEOUT)
    finally:
        # close() drains and flushes the sinks before the transport goes away.
        await client.close()

    connection = sqlite3.connect(database)
    try:
        recorded = """
            SELECT resource_id, name, on_state, brightness, payload
            FROM change WHERE resource_id = ? ORDER BY id
        """
        rows = connection.execute(recorded, (a_light.id,)).fetchall()
        assert rows, "the write should have been recorded"
        assert all(row[1] == name for row in rows)
        assert all(row[2] == 1 for row in rows)
        # Over the rows, not the tail: close() drains the recorder, and the
        # bridge's slower physical progress reports for the fade can land in
        # that window, so the last row may legitimately be mid-transition.
        assert any(row[3] == pytest.approx(target, abs=2) for row in rows), (
            f"no row reached the commanded brightness: {[r[3] for r in rows]}"
        )
        payload = rows[0][4]

        # The payload is the source of truth; the columns are an index over it.
        restored = Change.model_validate_json(payload)
        assert restored.resource_id == a_light.id
        assert isinstance(restored.after, models.Light)

        # The documented "what is it now?" query, on real rows.
        current = connection.execute(
            "SELECT on_state, brightness FROM current WHERE resource_id = ?",
            (a_light.id,),
        ).fetchone()
        assert current is not None
        assert current[0] == 1
        assert current[1] == pytest.approx(target, abs=2)
    finally:
        connection.close()

    # The JSONL sink saw the same batch, enriched identically.
    entries = [
        ChangeEntry.model_validate_json(line)
        for line in lines.read_text().splitlines()
        if '"record":"change"' in line
    ]
    assert any(entry.change.resource_id == a_light.id for entry in entries)
    assert all(entry.name for entry in entries)
