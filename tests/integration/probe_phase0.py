"""Capture opt-in real-bridge durability evidence.

This complements :mod:`tests.integration.capture_phase0` with probes that
need deliberate connection gaps or a long quiet listen::

    HUEPY_INTEGRATION=1 uv run python -m tests.integration.probe_phase0
"""

import argparse
import asyncio
import contextlib
import json
import os
from collections.abc import AsyncGenerator, AsyncIterator
from pathlib import Path
from time import monotonic
from typing import Any, cast

from huepy import Hue, HueAPIError
from huepy.client.http import HueHttpClient
from huepy.client.protocol import SSEFrame

from .capture_phase0 import (
    OPT_IN_ENV,
    OUTPUT,
    SUBSCRIBE_SETTLE_SECONDS,
    Scrubber,
    _restore_light,
    _run_cleanup,
    _safe_room_members,
)

QUIET_SECONDS = 90
OVERFLOW_WRITES = 80
REPLAY_SECONDS = 8
OUTPUT_FILE = Path(OUTPUT, "durability_probe.json")


async def _next_data_frame(frames: AsyncIterator[SSEFrame]) -> SSEFrame:
    """Wait for one frame that advances the replay cursor."""
    async with asyncio.timeout(15):
        async for frame in frames:
            if frame.event_id is not None and frame.events:
                return frame
    msg = "event stream ended before it produced a cursor-bearing frame"
    raise RuntimeError(msg)


async def _collect_for(
    frames: AsyncIterator[SSEFrame], seconds: float
) -> list[SSEFrame]:
    """Collect frames for a bounded interval."""
    captured: list[SSEFrame] = []
    try:
        async with asyncio.timeout(seconds):
            async for frame in frames:
                captured.append(frame)  # noqa: PERF401 - exits on timeout
    except TimeoutError:
        pass
    return captured


def _frame_record(frame: SSEFrame) -> dict[str, Any]:
    return {
        "event_id": frame.event_id,
        "received_at": frame.received_at.isoformat(),
        "events": frame.events,
    }


async def probe_replay(hue: Hue, writes: int) -> dict[str, Any]:
    """Disconnect beyond the measured bridge buffer, then request replay."""
    _room, members = await _safe_room_members(hue)
    light = next(member for member in members if member.dimming is not None)
    before = light.capture()
    connections = hue.http.event_connections(max_retries=0)
    try:
        first = await anext(connections)
        waiting = asyncio.create_task(_next_data_frame(first.frames))
        await asyncio.sleep(SUBSCRIBE_SETTLE_SECONDS)
        initial_target = 20.0 if (light.brightness or 0) > 50 else 80.0
        await light.set(on=True, brightness=initial_target)
        cursor_frame = await waiting
        await cast("AsyncGenerator[SSEFrame]", first.frames).aclose()

        for index in range(writes):
            await light.set(brightness=20.0 if index % 2 == 0 else 80.0)
            await asyncio.sleep(0.3)

        second = await anext(connections)
        replayed = await _collect_for(second.frames, REPLAY_SECONDS)
        current = await hue.api.lights.get(light.id)
        return {
            "cursor_before_gap": cursor_frame.event_id,
            "requested_resume_from": second.resumed_from,
            "writes_during_gap": writes,
            "replayed_frames": [_frame_record(frame) for frame in replayed],
            "final_light": current.model_dump(mode="json"),
        }
    finally:
        await _run_cleanup(
            connections.aclose,
            lambda: _restore_light(hue, before),
        )


async def probe_quiet_stream(hue: Hue, seconds: float) -> dict[str, Any]:
    """Record raw SSE fields during a quiet interval, including comments."""
    http = hue.http
    if not isinstance(http, HueHttpClient) or http.session is None:
        msg = "quiet-stream probe requires HueHttpClient's open aiohttp session"
        raise RuntimeError(msg)

    started = monotonic()
    fields: list[dict[str, Any]] = []
    response = await http.session.get(
        "/eventstream/clip/v2",
        headers={"Accept": "text/event-stream"},
    )
    try:
        async with asyncio.timeout(seconds):
            async for raw_line in response.content:
                line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
                elapsed = monotonic() - started
                if line.startswith("data:"):
                    encoded = line.removeprefix("data:").lstrip()
                    try:
                        value: Any = json.loads(encoded)
                    except json.JSONDecodeError:
                        value = "malformed-data"
                    fields.append(
                        {"at_seconds": elapsed, "field": "data", "value": value}
                    )
                elif line.startswith("id:"):
                    fields.append(
                        {
                            "at_seconds": elapsed,
                            "field": "id",
                            "value": line.removeprefix("id:").lstrip(),
                        }
                    )
                elif line.startswith(":"):
                    fields.append(
                        {
                            "at_seconds": elapsed,
                            "field": "comment",
                            "value": line.removeprefix(":").lstrip(),
                        }
                    )
                elif line:
                    fields.append(
                        {"at_seconds": elapsed, "field": "other", "value": line}
                    )
    except TimeoutError:
        pass
    finally:
        response.close()

    return {"duration_seconds": monotonic() - started, "fields": fields}


async def probe_transition_ceiling(hue: Hue) -> dict[str, Any]:
    """Repeat the exact accepted/rejected transition-boundary requests."""
    _room, members = await _safe_room_members(hue)
    light = next(member for member in members if member.dimming is not None)
    before = light.capture()
    path = f"/clip/v2/resource/light/{light.id}"
    result: dict[str, Any] = {}
    try:
        brightness = light.brightness or 1.0
        for duration in (6_000_000, 6_000_001):
            result[str(duration)] = await hue.http.put(
                path,
                {
                    "dimming": {"brightness": brightness},
                    "dynamics": {"duration": duration},
                },
            )
        try:
            _ = await hue.http.put(
                path,
                {
                    "dimming": {"brightness": brightness},
                    "dynamics": {"duration": -1},
                },
            )
        except HueAPIError as exc:
            result["-1"] = {"http_status": exc.status_code}
    finally:
        current = await hue.api.lights.get(light.id)
        await current.restore(before)
    return result


def parse_args() -> argparse.Namespace:
    """Parse durations while retaining safe evidence-oriented defaults."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--quiet-seconds", type=float, default=QUIET_SECONDS)
    parser.add_argument("--overflow-writes", type=int, default=OVERFLOW_WRITES)
    parser.add_argument("--skip-quiet", action="store_true")
    parser.add_argument("--skip-replay", action="store_true")
    parser.add_argument("--skip-transition", action="store_true")
    return parser.parse_args()


def write_evidence(evidence: dict[str, Any]) -> None:
    """Write scrubbed evidence outside the event loop."""
    OUTPUT_FILE.write_text(
        json.dumps(evidence, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def load_evidence() -> dict[str, Any]:
    """Load prior sections so one probe can be rerun independently."""
    if not OUTPUT_FILE.exists():
        return {}
    return cast(
        "dict[str, Any]",
        json.loads(OUTPUT_FILE.read_text(encoding="utf-8")),
    )


async def main() -> None:
    if os.getenv(OPT_IN_ENV) != "1":
        msg = f"set {OPT_IN_ENV}=1 before probing a real bridge"
        raise RuntimeError(msg)
    args = parse_args()
    evidence = await asyncio.to_thread(load_evidence)
    async with Hue() as hue:
        if not args.skip_transition:
            evidence["transition_ceiling"] = await probe_transition_ceiling(hue)
        if not args.skip_replay:
            evidence["replay_overflow"] = await probe_replay(
                hue, max(1, args.overflow_writes)
            )
        if not args.skip_quiet:
            evidence["quiet_stream"] = await probe_quiet_stream(
                hue, max(1, args.quiet_seconds)
            )

    scrubbed = Scrubber().value(evidence)
    await asyncio.to_thread(write_evidence, cast("dict[str, Any]", scrubbed))


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
