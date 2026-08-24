"""Regression checks backed by scrubbed payloads from a real Hue bridge."""

import json
import re
from pathlib import Path
from typing import Any, cast

import pytest

from huepy import models
from huepy.models.event import HueEvent

from .integration.capture_phase0 import Scrubber, _run_cleanup

FIXTURES = Path(__file__).parent / "fixtures"
UUID = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)
TIMESTAMP = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)
SSE_ID = re.compile(r"^\d{10}:\d+$")
PRIVATE_KEYS = {
    "active_timeslot",
    "configuration",
    "configuration_schema",
    "dependees",
    "state_schema",
    "sun_today",
    "trigger_schema",
    "week_timeslots",
}


def _load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_real_aggregate_snapshot_parses_every_resource() -> None:
    payload = _load("aggregate_snapshot.json")
    response = models.HueResponse[models.AnyResource].model_validate(payload)
    response.raise_for_errors()

    assert len(response.data) == 27
    assert len({resource.type for resource in response.data}) == 27
    connectivity = [
        resource
        for resource in response.data
        if isinstance(resource, models.ZigbeeConnectivity)
    ]
    assert len(connectivity) == 1
    assert all(resource.status for resource in connectivity)
    empty_group_colours = [
        resource.color
        for resource in response.data
        if isinstance(resource, models.GroupedLight)
        and resource.color is not None
        and resource.color.xy is None
    ]
    assert len(empty_group_colours) == 1


def test_real_sse_frames_preserve_ids_and_event_shapes() -> None:
    frames = cast("list[dict[str, Any]]", _load("event_frames.json"))
    events = [
        HueEvent.model_validate(event)
        for frame in frames
        for event in cast("list[dict[str, Any]]", frame["events"])
    ]

    assert frames
    assert all(frame["event_id"] for frame in frames)
    assert {event.event_type for event in events} == {
        models.EventType.UPDATE,
        models.EventType.ADD,
        models.EventType.DELETE,
    }
    assert any(len(event.data) > 1 for event in events)

    added = next(event for event in events if event.event_type is models.EventType.ADD)
    added_scene = models.parse_resource(added.data[0].model_dump())
    assert isinstance(added_scene, models.Scene)
    assert len(added_scene.actions) == 1

    recalled = next(
        resource
        for event in events
        if event.event_type is models.EventType.UPDATE
        for resource in event.data
        if resource.id == added_scene.id and resource.type == "scene"
    )
    assert recalled.model_extra is not None
    status = models.SceneStatus.model_validate(recalled.model_extra["status"])
    assert status.active == "static"
    assert status.last_recall is not None

    deleted = next(
        event for event in events if event.event_type is models.EventType.DELETE
    )
    assert [(resource.id, resource.type) for resource in deleted.data] == [
        (added_scene.id, "scene")
    ]


def test_durability_probe_records_transition_replay_overflow_and_no_keepalive() -> None:
    evidence = cast("dict[str, Any]", _load("durability_probe.json"))

    transition = cast("dict[str, dict[str, Any]]", evidence["transition_ceiling"])
    assert transition["6000000"]["errors"] == []
    assert transition["6000001"]["errors"]
    assert transition["-1"]["http_status"] == 400

    replay = cast("dict[str, Any]", evidence["replay_overflow"])
    cursor = cast("str", replay["cursor_before_gap"])
    replayed = cast("list[dict[str, Any]]", replay["replayed_frames"])
    assert replay["requested_resume_from"] == cursor
    assert replay["writes_during_gap"] == 80
    assert len(replayed) == 15
    cursor_seconds = int(cursor.partition(":")[0])
    first_replayed_seconds = int(cast("str", replayed[0]["event_id"]).partition(":")[0])
    assert first_replayed_seconds > cursor_seconds + 1
    for frame in replayed:
        for event in cast("list[dict[str, Any]]", frame["events"]):
            _ = HueEvent.model_validate(event)

    quiet = cast("dict[str, Any]", evidence["quiet_stream"])
    fields = cast("list[dict[str, Any]]", quiet["fields"])
    assert cast("float", quiet["duration_seconds"]) >= 90
    assert [field["value"] for field in fields if field["field"] == "comment"] == ["hi"]
    for field in fields:
        if field["field"] != "data":
            continue
        for event in cast("list[dict[str, Any]]", field["value"]):
            _ = HueEvent.model_validate(event)


def test_real_fixtures_contain_no_raw_uuids() -> None:
    for fixture in FIXTURES.glob("*.json"):
        assert UUID.search(fixture.read_text(encoding="utf-8")) is None, fixture.name


def _walk(value: Any) -> Any:
    if isinstance(value, dict):
        for key, item in value.items():
            yield key, item
            yield from _walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk(item)


def test_real_fixtures_are_minimised_and_time_rebased() -> None:
    for fixture in FIXTURES.glob("*.json"):
        payload = _load(fixture.name)
        fields = list(_walk(payload))
        assert not ({key for key, _value in fields} & PRIVATE_KEYS), fixture.name
        assert all(
            value.startswith("2020-01-01T")
            for _key, value in fields
            if isinstance(value, str) and TIMESTAMP.fullmatch(value)
        ), fixture.name
        assert all(
            value.startswith("170")
            for _key, value in fields
            if isinstance(value, str) and SSE_ID.fullmatch(value)
        ), fixture.name
        assert all(
            value == "UTC"
            for key, value in fields
            if key == "time_zone" and isinstance(value, str)
        ), fixture.name


def test_snapshot_scrubber_keeps_one_sample_per_type() -> None:
    scrubbed = Scrubber().snapshot(
        {
            "errors": [],
            "data": [
                {"id": "first", "type": "future", "configuration": {"a": 1}},
                {"id": "second", "type": "future", "secret": "value"},
                {
                    "id": "bridge",
                    "type": "bridge",
                    "time_zone": {"time_zone": "Europe/Berlin"},
                },
            ],
        }
    )

    assert scrubbed == {
        "data": [
            {
                "id": "resource-001",
                "time_zone": {"time_zone": "UTC"},
                "type": "bridge",
            },
            {"id": "resource-002", "type": "future"},
        ],
        "errors": [],
    }


@pytest.mark.asyncio
async def test_cleanup_attempts_every_step_before_raising() -> None:
    called: list[str] = []

    async def fail() -> None:
        called.append("fail")
        raise RuntimeError

    async def succeed() -> None:
        called.append("succeed")

    with pytest.raises(BaseExceptionGroup):
        await _run_cleanup(fail, succeed)

    assert called == ["fail", "succeed"]
