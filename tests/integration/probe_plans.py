"""Measure what the plan runner still assumes about a bulb, and capture sensor frames.

Two probes, both opt-in and both against the room named by ``PLAN_ROOM``::

    HUEPY_INTEGRATION=1 uv run python -m tests.integration.probe_plans

``resume_after_switch_off`` starts a sixty-second fade on one light, switches
it off part-way, switches it back on with no other field, and samples where the
brightness goes from there. The runner's override logic treats the fade after a
bare switch-off as blind to brightness because nothing had measured whether a
bulb resumes an interrupted transition; this is that measurement.

``passive_sensors`` writes nothing. It keeps one minimised representative of
each sensor type from a snapshot and then listens for the first event frame of
each, so a ``light_level`` trigger can be checked against a real delta.

Every write is restored even when a step fails, and no display name is ever
written into the evidence: the scrubber replaces ``name`` keys but cannot
recognise a name inside free text.
"""

import argparse
import asyncio
import contextlib
import json
import os
from pathlib import Path
from time import monotonic
from typing import Any, Literal, cast

from huepy import Hue, models
from huepy.client.protocol import SSEFrame

from .capture_phase0 import (
    OPT_IN_ENV,
    OUTPUT,
    SUBSCRIBE_SETTLE_SECONDS,
    Scrubber,
    _restore_light,
    _run_cleanup,
)
from .conftest import PLAN_ROOM

PROBE_LIGHT = "Arbeitszimmer Mitte"
"""The tunable-white member to fade; the first dimmable member if it is absent."""
FADE_SECONDS = 60
START_BRIGHTNESS = 20.0
END_BRIGHTNESS = 100.0
SWITCH_OFF_AT = 10
SWITCH_ON_AT = 20
SAMPLE_AT = (11, 21, 30, 61)
"""When to read the light back: once while off, then three times after switch-on."""
CLASSIFY_TOLERANCE = 5.0
GROUP_FADE_SECONDS = 40
GROUP_FADE_FROM = 30.0
GROUP_FADE_TO = 90.0
"""The room fade the live yield test runs, so its progress reports can be measured."""
LISTEN_MINUTES = 5
WANTED_SENSORS = ("light_level", "motion", "button", "contact")
PROGRESS_EVERY = 30
OUTPUT_FILE = Path(OUTPUT, "plan_probe.json")

type Outcome = Literal["frozen", "target", "continued", "resumed", "unclassified"]


def expected_brightness(at_seconds: float) -> float:
    """Where an uninterrupted fade would be."""
    fraction = min(at_seconds, FADE_SECONDS) / FADE_SECONDS
    return START_BRIGHTNESS + (END_BRIGHTNESS - START_BRIGHTNESS) * fraction


def _near(left: float, right: float) -> bool:
    return abs(left - right) <= CLASSIFY_TOLERANCE


def classify_resume(samples: list[dict[str, Any]]) -> Outcome:
    """Name what the bulb did with the transition across the switch-off.

    Pure, and imported by the offline fixture test so the recorded outcome and
    the samples it was read from can never disagree.

    Args:
        samples: The probe's readings, each with ``at_seconds``, ``on`` and
            ``brightness``.

    Returns:
        ``target`` when the bulb lands on the fade's end the moment it is
        switched on; ``frozen`` when it holds the level it had at switch-off;
        ``continued`` when the fade's clock kept running while the light was
        off; ``resumed`` when the remaining ramp restarted from the retained
        level at switch-on; ``unclassified`` for anything else.

    """
    levels: dict[int, float] = {}
    for sample in samples:
        brightness = sample.get("brightness")
        if sample.get("on") is True and isinstance(brightness, (int, float)):
            levels[round(cast("float", sample["at_seconds"]))] = float(brightness)
    if any(at not in levels for at in SAMPLE_AT[1:]):
        return "unclassified"
    first, middle, last = (levels[at] for at in SAMPLE_AT[1:])
    finished = END_BRIGHTNESS - CLASSIFY_TOLERANCE
    retained = expected_brightness(SWITCH_OFF_AT)
    if first >= finished:
        return "target"
    if _near(first, middle) and _near(middle, last) and last < finished:
        return "frozen"
    if all(_near(levels[at], expected_brightness(at)) for at in SAMPLE_AT[1:]):
        return "continued"
    if _near(first, retained) and first < middle < last and last >= finished:
        return "resumed"
    return "unclassified"


def _say(message: str) -> None:
    """Report progress to the operator running the probe."""
    print(message)  # noqa: T201 - a runnable probe reports to whoever runs it


def _frame_record(frame: SSEFrame) -> dict[str, Any]:
    return {
        "event_id": frame.event_id,
        "received_at": frame.received_at.isoformat(),
        "events": frame.events,
    }


def _mentions(frame: SSEFrame, resource_id: str) -> bool:
    """Whether any event in a frame is about one resource."""
    for event in frame.events:
        data = cast("list[dict[str, Any]]", event.get("data", []))
        if any(item.get("id") == resource_id for item in data):
            return True
    return False


async def _sleep_until(origin: float, at_seconds: float) -> None:
    await asyncio.sleep(max(0.0, origin + at_seconds - monotonic()))


async def _probe_light(hue: Hue) -> models.Light:
    room = await hue.rooms.get(PLAN_ROOM)
    members = [light for light in await room.lights() if light.dimming is not None]
    if not members:
        msg = f"{PLAN_ROOM!r} has no dimmable light to probe"
        raise RuntimeError(msg)
    return next((light for light in members if light.name == PROBE_LIGHT), members[0])


async def probe_resume_after_switch_off(hue: Hue) -> dict[str, Any]:
    """Fade one light, switch it off and on mid-fade, and record where it goes."""
    light = await _probe_light(hue)
    before = light.capture()
    frames: list[dict[str, Any]] = []
    samples: list[dict[str, Any]] = []
    origin = monotonic()

    async def collect() -> None:
        async for frame in hue.http.subscribe_event_frames(max_retries=0):
            if _mentions(frame, light.id):
                frames.append(  # noqa: PERF401 - one record per frame, as it arrives
                    {"at_seconds": monotonic() - origin, "frame": _frame_record(frame)}
                )

    collector = asyncio.create_task(collect())
    try:
        await asyncio.sleep(SUBSCRIBE_SETTLE_SECONDS)
        _ = await light.set(on=True, brightness=START_BRIGHTNESS)
        await asyncio.sleep(SUBSCRIBE_SETTLE_SECONDS)
        origin = monotonic()
        _ = await light.set(brightness=END_BRIGHTNESS, transition=FADE_SECONDS)
        started = f"{START_BRIGHTNESS:.0f} -> {END_BRIGHTNESS:.0f} over {FADE_SECONDS}s"
        _say(f"fade started: {started}")

        schedule: list[tuple[float, str]] = sorted(
            [(SWITCH_OFF_AT, "off"), (SWITCH_ON_AT, "on")]
            + [(at, "sample") for at in SAMPLE_AT]
        )
        for at, action in schedule:
            await _sleep_until(origin, at)
            if action == "off":
                _ = await light.set(on=False)
                _say(f"+{at}s: switched off")
            elif action == "on":
                _ = await light.set(on=True)
                _say(f"+{at}s: switched on, no other field")
            else:
                fresh = await light.refresh()
                samples.append(
                    {
                        "at_seconds": monotonic() - origin,
                        "on": fresh.is_on,
                        "brightness": fresh.brightness,
                    }
                )
                _say(f"+{at}s: on={fresh.is_on} brightness={fresh.brightness}")
    finally:
        _ = collector.cancel()
        await _run_cleanup(
            lambda: asyncio.gather(collector, return_exceptions=True),
            lambda: _restore_light(hue, before),
        )

    outcome = classify_resume(samples)
    _say(f"outcome: {outcome}")
    return {
        "light": light.id,
        "powerup": None
        if light.powerup is None
        else light.powerup.model_dump(mode="json"),
        "fade": {
            "from": START_BRIGHTNESS,
            "to": END_BRIGHTNESS,
            "seconds": FADE_SECONDS,
            "switch_off_at": SWITCH_OFF_AT,
            "switch_on_at": SWITCH_ON_AT,
        },
        "level_at_switch_off": expected_brightness(SWITCH_OFF_AT),
        "expected_if_uninterrupted": {
            str(at): expected_brightness(at) for at in SAMPLE_AT
        },
        "samples": samples,
        "frames": frames,
        "outcome": outcome,
    }


async def probe_progress_during_group_fade(hue: Hue) -> dict[str, Any]:
    """Fade the whole room and record every progress report against the ramp.

    The override arithmetic judges each report against the fade's own
    interpolation, within ``BRIGHTNESS_TOLERANCE``. Whether real bulbs report
    inside that band -- and on what cadence -- is what this measures.
    """
    room = await hue.rooms.get(PLAN_ROOM)
    members = [light for light in await room.lights() if light.dimming is not None]
    service = room.service_id(models.ResourceType.GROUPED_LIGHT)
    watched = {light.id for light in members} | ({service} if service else set())
    before = await room.capture()
    reports: list[dict[str, Any]] = []
    origin = monotonic()

    async def collect() -> None:
        async for frame in hue.http.subscribe_event_frames(max_retries=0):
            for event in frame.events:
                for item in cast("list[dict[str, Any]]", event.get("data", [])):
                    dimming = item.get("dimming")
                    if item.get("id") in watched and isinstance(dimming, dict):
                        reports.append(
                            {
                                "at_seconds": monotonic() - origin,
                                "resource": item.get("id"),
                                "type": item.get("type"),
                                "brightness": cast("dict[str, Any]", dimming).get(
                                    "brightness"
                                ),
                            }
                        )

    collector = asyncio.create_task(collect())
    try:
        await asyncio.sleep(SUBSCRIBE_SETTLE_SECONDS)
        _ = await room.set(on=True, brightness=GROUP_FADE_FROM, transition=1)
        await asyncio.sleep(3)
        reports.clear()
        origin = monotonic()
        _ = await room.set(brightness=GROUP_FADE_TO, transition=GROUP_FADE_SECONDS)
        _say(f"room fade started: {GROUP_FADE_FROM:.0f} -> {GROUP_FADE_TO:.0f}")
        await asyncio.sleep(GROUP_FADE_SECONDS + 5)
    finally:
        _ = collector.cancel()
        await _run_cleanup(
            lambda: asyncio.gather(collector, return_exceptions=True),
            lambda: room.restore(before),
        )

    for report in reports:
        fraction = min(cast("float", report["at_seconds"]), GROUP_FADE_SECONDS)
        fraction /= GROUP_FADE_SECONDS
        expected = GROUP_FADE_FROM + (GROUP_FADE_TO - GROUP_FADE_FROM) * fraction
        report["expected"] = expected
        brightness = report["brightness"]
        report["deviation"] = (
            None if brightness is None else cast("float", brightness) - expected
        )
        at = f"+{report['at_seconds']:5.1f}s"
        where = f"{at} {report['type']:<13} {report['resource'][:8]}"
        _say(f"{where} brightness={brightness} expected={expected:.1f}")
    deviations = [
        abs(cast("float", r["deviation"]))
        for r in reports
        if r["deviation"] is not None
    ]
    return {
        "fade": {
            "from": GROUP_FADE_FROM,
            "to": GROUP_FADE_TO,
            "seconds": GROUP_FADE_SECONDS,
            "lights": len(members),
        },
        "reports": reports,
        "max_deviation": max(deviations) if deviations else None,
    }


def _minimised(resource: dict[str, Any]) -> dict[str, Any]:
    """Keep only the fields the resource's model knows, as the scrubber does."""
    model = models.RESOURCE_MODELS.get(str(resource.get("type")))
    if model is None:
        return {key: resource[key] for key in ("id", "type") if key in resource}
    allowed = set(model.model_fields) | {"id", "type"}
    return {key: value for key, value in resource.items() if key in allowed}


async def probe_passive_sensors(hue: Hue, minutes: float) -> dict[str, Any]:
    """Keep one sensor resource of each kind, then wait for a real event of each."""
    snapshot = cast("dict[str, Any]", await hue.http.get("/clip/v2/resource"))
    representatives: dict[str, dict[str, Any]] = {}
    for item in cast("list[dict[str, Any]]", snapshot.get("data", [])):
        kind = item.get("type")
        if kind in WANTED_SENSORS and kind not in representatives:
            representatives[kind] = _minimised(item)

    frames: dict[str, dict[str, Any]] = {}
    started = monotonic()
    last_report = started
    hint = "walk past a sensor or press a dimmer"
    _say(f"listening up to {minutes:g} min for sensor events; {hint}")
    try:
        async with asyncio.timeout(minutes * 60):
            async for frame in hue.http.subscribe_event_frames(max_retries=0):
                for event in frame.events:
                    for item in cast("list[dict[str, Any]]", event.get("data", [])):
                        kind = item.get("type")
                        if kind in WANTED_SENSORS and kind not in frames:
                            frames[kind] = {
                                "at_seconds": monotonic() - started,
                                "frame": _frame_record(frame),
                            }
                            _say(f"captured a {kind} frame")
                if "light_level" in frames:
                    break
                if monotonic() - last_report >= PROGRESS_EVERY:
                    last_report = monotonic()
                    missing = ", ".join(k for k in WANTED_SENSORS if k not in frames)
                    _say(f"still waiting for: {missing}")
    except TimeoutError:
        pass
    return {
        "listened_seconds": monotonic() - started,
        "representatives": representatives,
        "frames": frames,
    }


def parse_args() -> argparse.Namespace:
    """Parse which probes to run."""
    parser = argparse.ArgumentParser()
    _ = parser.add_argument("--skip-resume", action="store_true")
    _ = parser.add_argument("--skip-passive", action="store_true")
    _ = parser.add_argument("--skip-progress", action="store_true")
    _ = parser.add_argument("--listen-minutes", type=float, default=LISTEN_MINUTES)
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
    return cast("dict[str, Any]", json.loads(OUTPUT_FILE.read_text(encoding="utf-8")))


async def main() -> None:
    """Run the selected probes and write the scrubbed evidence."""
    if os.getenv(OPT_IN_ENV) != "1":
        msg = f"set {OPT_IN_ENV}=1 before probing a real bridge"
        raise RuntimeError(msg)
    args = parse_args()
    evidence = await asyncio.to_thread(load_evidence)
    async with Hue() as hue:
        if not args.skip_resume:
            evidence["resume_after_switch_off"] = await probe_resume_after_switch_off(
                hue
            )
        if not args.skip_progress:
            evidence[
                "progress_during_group_fade"
            ] = await probe_progress_during_group_fade(hue)
        if not args.skip_passive:
            evidence["passive_sensors"] = await probe_passive_sensors(
                hue, max(0.1, cast("float", args.listen_minutes))
            )

    scrubbed = Scrubber().value(evidence)
    await asyncio.to_thread(write_evidence, cast("dict[str, Any]", scrubbed))
    _say(f"wrote {OUTPUT_FILE}")


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
