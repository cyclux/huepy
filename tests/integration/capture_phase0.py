"""Capture and scrub Phase 0 bridge evidence for deterministic fixtures.

Run explicitly; this module is not collected as a test::

    HUEPY_INTEGRATION=1 uv run python -m tests.integration.capture_phase0
"""

import asyncio
import contextlib
import json
import os
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from huepy import Hue, models

OPT_IN_ENV = "HUEPY_INTEGRATION"
CAPTURE_SECONDS = 110
FADE_SECONDS = 60
SUBSCRIBE_SETTLE_SECONDS = 2
OUTPUT = Path("tests/fixtures")
_IDENTIFIER = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_SSE_ID = re.compile(r"^(\d{10}):(\d+)$")
_TIMESTAMP = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)
_TIMESTAMP_BASE = datetime(2020, 1, 1, tzinfo=UTC)
_SSE_BASE = 1_700_000_000
_CLEANUP_ERROR = "one or more cleanup steps failed"
_PRIVATE_FIELDS = {
    "active_timeslot",
    "configuration",
    "configuration_schema",
    "dependees",
    "state_schema",
    "sun_today",
    "trigger_schema",
    "week_timeslots",
}
_PRODUCT_FIELDS = {
    "manufacturer_name",
    "model_id",
    "product_archetype",
    "product_name",
    "software_version",
}
_TRUNCATED_LISTS = {"actions", "children", "resources", "services"}


class Scrubber:
    """Minimise bridge evidence and replace identifying values consistently."""

    def __init__(self) -> None:
        self.identifiers: dict[str, str] = {}
        self.timestamps: dict[str, str] = {}
        self.sse_epoch: int | None = None

    def snapshot(self, payload: Any) -> Any:
        """Keep one privacy-minimised representative of each resource type."""
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            return self.value(payload)

        representatives: dict[str, dict[str, Any]] = {}
        for item in payload["data"]:
            if not isinstance(item, dict) or not isinstance(item.get("type"), str):
                continue
            resource_type = item["type"]
            current = representatives.get(resource_type)
            if current is None or self._score(item) > self._score(current):
                representatives[resource_type] = item

        result = {
            "data": [
                self._resource(representatives[resource_type])
                for resource_type in sorted(representatives)
            ],
            "errors": payload.get("errors", []),
        }
        return self.value(result)

    @staticmethod
    def _score(resource: dict[str, Any]) -> int:
        """Prefer samples that exercise optional model shapes."""
        score = len(resource)
        if resource.get("type") == "grouped_light" and resource.get("color") == {}:
            score += 100
        if resource.get("type") == "scene":
            score += 10 * len(resource.get("actions", []))
            score += 20 if resource.get("status") else 0
        return score

    @staticmethod
    def _resource(resource: dict[str, Any]) -> dict[str, Any]:
        """Drop unmodelled bodies and retain only fields used by known models."""
        resource_type = str(resource["type"])
        model = models.RESOURCE_MODELS.get(resource_type)
        if model is None:
            return {key: resource[key] for key in ("id", "type") if key in resource}

        allowed = set(model.model_fields) | {"id", "id_v1", "type"}
        return {key: value for key, value in resource.items() if key in allowed}

    def value(self, value: Any, *, key: str = "") -> Any:  # noqa: C901, PLR0911
        if isinstance(value, dict):
            return {
                self.key(name): self.value(item, key=name)
                for name, item in value.items()
                if name not in _PRIVATE_FIELDS
            }
        if isinstance(value, list):
            items = value[:1] if key in _TRUNCATED_LISTS else value
            return [self.value(item, key=key) for item in items]
        if not isinstance(value, str):
            return value
        if key == "name":
            return "Scrubbed"
        if key in _PRODUCT_FIELDS:
            return "Scrubbed"
        if key in {"mac_address", "extended_pan_id"}:
            return "Scrubbed"
        if key == "time_zone":
            return "UTC"
        if key in {"id", "rid", "bridge_id", "serial_number"} or _IDENTIFIER.fullmatch(
            value
        ):
            return self.identifier(value)
        if key == "id_v1":
            return "/scrubbed/resource"
        sse_match = _SSE_ID.fullmatch(value)
        if sse_match is not None:
            seconds = int(sse_match.group(1))
            if self.sse_epoch is None:
                self.sse_epoch = seconds
            return f"{_SSE_BASE + seconds - self.sse_epoch}:{sse_match.group(2)}"
        if _TIMESTAMP.fullmatch(value):
            return self.timestamp(value)
        return _IDENTIFIER.sub(
            lambda match: self.identifier(match.group(0)),
            value,
        )

    def timestamp(self, value: str) -> str:
        """Replace an absolute timestamp with a stable synthetic timestamp."""
        if value not in self.timestamps:
            shifted = _TIMESTAMP_BASE + timedelta(milliseconds=len(self.timestamps))
            self.timestamps[value] = shifted.isoformat(timespec="milliseconds").replace(
                "+00:00", "Z"
            )
        return self.timestamps[value]

    def identifier(self, value: str) -> str:
        """Replace one identifier consistently across keys and values."""
        return self.identifiers.setdefault(
            value, f"resource-{len(self.identifiers) + 1:03d}"
        )

    def key(self, value: str) -> str:
        """Scrub an identifier-shaped object key."""
        return self.identifier(value) if _IDENTIFIER.fullmatch(value) else value


async def _run_cleanup(*steps: Callable[[], Awaitable[object]]) -> None:
    """Attempt every cleanup step before reporting any failures."""
    errors: list[BaseException] = []
    for step in steps:
        try:
            await step()
        except BaseException as exc:  # noqa: BLE001 - continue through cancellation
            errors.append(exc)
    if errors:
        raise BaseExceptionGroup(_CLEANUP_ERROR, errors)


async def _restore_light(hue: Hue, state: models.LightState) -> None:
    """Fetch a light's latest representation and restore its captured state."""
    current = await hue.light.get(state.light_id)
    await current.restore(state)


async def probe_fade(hue: Hue) -> None:
    """Create one reversible event-rich fade on a safe dimmable light."""
    lights = await hue.light.get_all()
    usable = [
        light
        for light in lights
        if light.dimming is not None and "bad" not in light.name.casefold()
    ]
    if not usable:
        return
    light = usable[0]
    before = light.capture()
    target = 20.0 if (light.brightness or 0) > 50 else 80.0
    await asyncio.sleep(SUBSCRIBE_SETTLE_SECONDS)
    try:
        await light.set(on=True, brightness=target, transition=FADE_SECONDS)
        await asyncio.sleep(FADE_SECONDS + SUBSCRIBE_SETTLE_SECONDS)
    finally:
        current = await hue.light.get(light.id)
        await current.restore(before)


async def _safe_room_members(hue: Hue) -> tuple[models.Room, list[models.Light]]:
    """Find a room whose lights are safe for reversible probe writes."""
    lights = await hue.lights.all()
    for room in await hue.rooms.all():
        device_ids = {
            child.rid
            for child in room.children
            if child.rtype == models.ResourceType.DEVICE
        }
        members = [light for light in lights if light.device_id in device_ids]
        if (
            members
            and any(light.dimming is not None for light in members)
            and all("bad" not in light.name.casefold() for light in members)
        ):
            return room, members
    msg = "no room has a safe dimmable light for the scene probe"
    raise RuntimeError(msg)


async def probe_scene(hue: Hue) -> None:
    """Capture temporary-scene add, recall, and delete events, then restore."""
    room, lights = await _safe_room_members(hue)
    before = [light.capture() for light in lights]
    actions = [
        {
            "target": {"rid": state.light_id, "rtype": models.ResourceType.LIGHT},
            "action": models.build_light_payload(
                on=state.on,
                brightness=state.brightness,
                mirek=state.mirek,
                xy=state.xy,
            ),
        }
        for state in before
    ]
    scene_id: str | None = None
    await asyncio.sleep(SUBSCRIBE_SETTLE_SECONDS)
    try:
        response = await hue.http.post(
            "/clip/v2/resource/scene",
            {
                "metadata": {"name": f"huepy-p0-{uuid4().hex[:12]}"},
                "group": {"rid": room.id, "rtype": models.ResourceType.ROOM},
                "actions": actions,
            },
        )
        created = models.unwrap(response, models.ResourceIdentifier)
        if not created:
            msg = "bridge returned no id for the temporary scene"
            raise RuntimeError(msg)
        scene_id = created[0].rid
        await asyncio.sleep(SUBSCRIBE_SETTLE_SECONDS)

        dimmable = next(light for light in lights if light.dimming is not None)
        target = 20.0 if (dimmable.brightness or 0) > 50 else 80.0
        await dimmable.set(on=True, brightness=target, transition=0.4)
        await asyncio.sleep(1)
        await (await hue.scene.get(scene_id)).activate()
        await asyncio.sleep(SUBSCRIBE_SETTLE_SECONDS)
    finally:

        async def delete_scene() -> None:
            if scene_id is not None:
                await hue.scene.delete(scene_id)
                await asyncio.sleep(SUBSCRIBE_SETTLE_SECONDS)

        await _run_cleanup(
            delete_scene,
            *(lambda state=state: _restore_light(hue, state) for state in before),
        )


async def run_probes(hue: Hue) -> None:
    """Run the reversible event-producing probes in a stable sequence."""
    await probe_fade(hue)
    await probe_scene(hue)


async def main() -> None:
    if os.getenv(OPT_IN_ENV) != "1":
        msg = f"set {OPT_IN_ENV}=1 before capturing real bridge data"
        raise RuntimeError(msg)

    scrubber = Scrubber()
    async with Hue() as hue:
        snapshot = await hue.http.get("/clip/v2/resource")
        frames = []
        stream = hue.http.subscribe_event_frames(max_retries=0)
        probe = asyncio.create_task(run_probes(hue))
        try:
            async with asyncio.timeout(CAPTURE_SECONDS):
                async for frame in stream:
                    frames.append(  # noqa: PERF401 - bounded live capture
                        {
                            "event_id": frame.event_id,
                            "received_at": frame.received_at.isoformat(),
                            "events": frame.events,
                        }
                    )
        except TimeoutError:
            pass
        finally:

            async def finish_probe() -> None:
                if not probe.done():
                    probe.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await probe

            await _run_cleanup(stream.aclose, finish_probe)

    await asyncio.to_thread(
        write_capture,
        scrubber.snapshot(snapshot),
        scrubber.value(frames),
    )


def write_capture(snapshot: Any, frames: Any) -> None:
    """Write already-scrubbed captures outside the event loop."""
    OUTPUT.mkdir(parents=True, exist_ok=True)
    for name, payload in (
        ("aggregate_snapshot.json", snapshot),
        ("event_frames.json", frames),
    ):
        (OUTPUT / name).write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )


if __name__ == "__main__":
    asyncio.run(main())
