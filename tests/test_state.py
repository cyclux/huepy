"""Tests for the opt-in, event-folded bridge state."""

import asyncio
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any, Literal, override
from uuid import uuid4

import pytest
from pydantic import ValidationError

from huepy import models
from huepy.client.http import EventConnection, PendingWrite, SSEFrame
from huepy.exceptions import HueResponseError
from huepy.state import Change, ChangeKind, Resync, ResyncReason
from huepy.state.core import _observed_at

from .conftest import FakeHttp, envelope


class EventStreamExhaustedError(RuntimeError):
    """Terminal error raised by the scripted event transport."""

    def __init__(self) -> None:
        super().__init__("event stream exhausted")


def light(
    brightness: float,
    *,
    light_id: str = "light-1",
    owner: str = "device-1",
) -> dict[str, Any]:
    return {
        "id": light_id,
        "type": "light",
        "owner": {"rid": owner, "rtype": "device"},
        "metadata": {"name": "Desk"},
        "on": {"on": True},
        "dimming": {"brightness": brightness},
        "color_temperature": {
            "mirek": 300,
            "mirek_valid": True,
            "mirek_schema": {"mirek_minimum": 153, "mirek_maximum": 500},
        },
    }


def update_frame(
    brightness: float,
    *,
    event_id: str = "1:1",
    entries: list[dict[str, Any]] | None = None,
) -> SSEFrame:
    return SSEFrame(
        event_id=event_id,
        received_at=datetime.now(UTC),
        events=[
            {
                "id": f"event-{event_id}",
                "type": "update",
                "creationtime": "2026-08-24T10:00:00Z",
                "data": entries
                or [
                    {
                        "id": "light-1",
                        "type": "light",
                        "dimming": {"brightness": brightness},
                    }
                ],
            }
        ],
    )


def event_frame(
    event_type: str,
    *entries: dict[str, Any],
    event_id: str = "1:1",
    creationtime: str = "2026-08-24T10:00:00Z",
) -> SSEFrame:
    """Build one complete Hue event frame around arbitrary resource entries."""
    return SSEFrame(
        event_id=event_id,
        received_at=datetime.now(UTC),
        events=[
            {
                "id": f"event-{event_id}",
                "type": event_type,
                "creationtime": creationtime,
                "data": list(entries),
            }
        ],
    )


class StateHttp(FakeHttp):
    """A controllable live connection plus a sequence of snapshots."""

    def __init__(self, snapshots: list[list[dict[str, Any]]]) -> None:
        super().__init__()
        self.snapshots = snapshots
        self.snapshot_index = 0
        self.connections: list[asyncio.Queue[SSEFrame | None]] = [asyncio.Queue()]

    @override
    async def get(self, path: str) -> Any:
        if path == "/clip/v2/resource":
            index = min(self.snapshot_index, len(self.snapshots) - 1)
            self.snapshot_index += 1
            return envelope(*self.snapshots[index])
        return await super().get(path)

    async def _frames(
        self,
        queue: asyncio.Queue[SSEFrame | None],
    ) -> AsyncGenerator[SSEFrame]:
        while True:
            frame = await queue.get()
            if frame is None:
                return
            yield frame

    @override
    async def event_connections(
        self,
        *,
        max_retries: int | None = 10,
    ) -> AsyncGenerator[EventConnection]:
        del max_retries
        for index, queue in enumerate(self.connections):
            yield EventConnection(
                opened_at=datetime.now(UTC),
                resumed_from=None if index == 0 else f"{index}:0",
                frames=self._frames(queue),
            )


class DeferredWriteHttp(StateHttp):
    """Hold a PUT between its pending and terminal observer notifications."""

    def __init__(
        self,
        snapshots: list[list[dict[str, Any]]],
        outcome: Literal["rejected", "unknown"],
    ) -> None:
        super().__init__(snapshots)
        self.outcome = outcome
        self.write_started = asyncio.Event()
        self.release_write = asyncio.Event()

    @override
    async def put(self, path: str, data: dict[str, Any]) -> Any:
        self.calls.append(("PUT", path, data))
        pending = PendingWrite(
            command_id=uuid4(),
            path=path,
            payload=data,
            sent_at=datetime.now(UTC),
        )
        for observer in tuple(self._write_observers):
            observer(pending.model_copy(deep=True))
        self.write_started.set()
        await self.release_write.wait()
        completed = pending.model_copy(
            update={
                "completed_at": datetime.now(UTC),
                "status": self.outcome,
            },
            deep=True,
        )
        for observer in tuple(self._write_observers):
            observer(completed.model_copy(deep=True))
        return self.write_result


@pytest.fixture
def state_http(hue) -> StateHttp:
    http = StateHttp([[light(10)]])
    hue._http = http
    return http


class TestResourceRegistry:
    def test_known_unknown_and_persisted_subtypes_round_trip(self):
        known = models.parse_resource(light(25))
        unknown = models.parse_resource({"id": "x", "type": "future", "answer": 42})
        assert isinstance(known, models.Light)
        assert type(unknown) is models.HueResource
        assert unknown.model_extra == {"answer": 42}

        restored = models.RESOURCE_LIST.validate_python(
            models.RESOURCE_LIST.dump_python([known, unknown], mode="json")
        )
        assert isinstance(restored[0], models.Light)
        assert restored[0].brightness == 25
        assert type(restored[1]) is models.HueResource

    def test_every_resource_type_has_the_expected_registry_model(self, hue):
        handlers = {
            str(handler.resource_type): handler.model
            for handler in vars(hue).values()
            if hasattr(handler, "resource_type") and hasattr(handler, "model")
        }
        assert handlers == dict(models.RESOURCE_MODELS)

    def test_malformed_type_uses_validation_instead_of_leaking_type_error(self):
        with pytest.raises(ValidationError):
            models.parse_resource({"id": "bad", "type": []})

    async def test_unknown_snapshot_resources_remain_bound_and_preserve_fields(
        self, hue, http
    ):
        http.queue(
            "/clip/v2/resource",
            envelope({"id": "future-1", "type": "future", "answer": 42}),
        )

        resources = await hue.snapshot()

        assert http.calls == [("GET", "/clip/v2/resource", None)]
        assert len(resources) == 1
        assert type(resources[0]) is models.HueResource
        assert resources[0].is_bound is True
        assert resources[0].model_extra == {"answer": 42}


class TestSnapshot:
    @pytest.mark.parametrize(
        ("payload", "expected_exception"),
        [
            (
                envelope(errors=["bridge rejected aggregate read"]),
                HueResponseError,
            ),
            ({"errors": [], "data": "not-a-list"}, ValidationError),
        ],
        ids=["bridge-envelope-error", "malformed-data"],
    )
    async def test_invalid_aggregate_envelopes_fail_without_fallback_reads(
        self, hue, http, payload, expected_exception
    ):
        http.queue("/clip/v2/resource", payload)

        with pytest.raises(expected_exception):
            await hue.snapshot()

        assert http.calls == [("GET", "/clip/v2/resource", None)]


class TestLifecycleAndFold:
    async def test_startup_buffers_frames_and_returns_bound_isolated_views(
        self, hue, state_http
    ):
        await state_http.connections[0].put(update_frame(20))
        async with hue.state() as state:
            assert state.lights["desk"].brightness == 20
            first = state.lights.get("light-1")
            second = state.lights.get("light-1")
            assert first is not second
            assert first is not None
            assert first.is_bound
            first.dimming.brightness = 99
            assert state.lights["Desk"].brightness == 20

    async def test_multi_entry_event_emits_one_complete_change(self, hue, state_http):
        async with hue.state() as state:
            changes = state.changes()
            waiting = asyncio.create_task(anext(changes))
            await asyncio.sleep(0)
            await state_http.connections[0].put(
                update_frame(
                    40,
                    entries=[
                        {"id": "light-1", "type": "light", "on": {"on": False}},
                        {
                            "id": "light-1",
                            "type": "light",
                            "dimming": {"brightness": 40},
                        },
                    ],
                )
            )
            item = await asyncio.wait_for(waiting, 1)
            assert isinstance(item, Change)
            assert item.kind is ChangeKind.UPDATE
            assert isinstance(item.after, models.Light)
            assert item.after.is_on is False
            assert item.after.brightness == 40
            assert item.after.color_temperature is not None
            assert item.after.color_temperature.mirek_schema is not None
            await changes.aclose()

    async def test_change_json_round_trip_keeps_light_subtype(self):
        before = models.parse_resource(light(10))
        after = models.parse_resource(light(20))
        change = Change(
            kind=ChangeKind.UPDATE,
            received_at=datetime.now(UTC),
            resource_id="light-1",
            resource_type="light",
            before=before,
            after=after,
            delta={"dimming": {"brightness": 20}},
        )
        restored = Change.model_validate_json(change.model_dump_json())
        assert isinstance(restored.before, models.Light)
        assert isinstance(restored.after, models.Light)
        assert restored.after.brightness == 20

    async def test_report_timestamp_takes_precedence_over_event_and_receipt_time(
        self, hue, state_http
    ):
        observed = datetime(2026, 8, 24, 9, 59, tzinfo=UTC)
        async with hue.state() as state:
            stream = state.changes()
            waiting = asyncio.create_task(anext(stream))
            await asyncio.sleep(0)

            await state_http.connections[0].put(
                event_frame(
                    "update",
                    {
                        "id": "light-1",
                        "type": "light",
                        "dimming": {
                            "brightness": 35,
                            "dimming_report": {"changed": observed.isoformat()},
                        },
                    },
                    creationtime="2026-08-24T10:00:00Z",
                )
            )

            change = await asyncio.wait_for(waiting, 1)
            assert isinstance(change, Change)
            assert change.observed_at == observed
            assert change.event_at == datetime(2026, 8, 24, 10, tzinfo=UTC)
            assert change.at == observed
            await stream.aclose()

    def test_report_timestamp_finds_top_level_and_direct_button_shapes(self):
        contact_at = datetime(2026, 8, 24, 9, 58, tzinfo=UTC)
        button_at = datetime(2026, 8, 24, 9, 59, tzinfo=UTC)

        assert (
            _observed_at({"contact_report": {"changed": contact_at.isoformat()}})
            == contact_at
        )
        assert (
            _observed_at(
                {
                    "motion": {"motion_report": {"changed": "malformed"}},
                    "button": {"updated": button_at.isoformat()},
                }
            )
            == button_at
        )

    async def test_add_then_delete_emits_complete_resource_transitions(
        self, hue, state_http
    ):
        added_payload = light(45, light_id="light-2", owner="device-2")
        async with hue.state() as state:
            stream = state.changes()
            waiting = asyncio.create_task(anext(stream))
            await asyncio.sleep(0)
            await state_http.connections[0].put(event_frame("add", added_payload))
            added = await asyncio.wait_for(waiting, 1)

            waiting = asyncio.create_task(anext(stream))
            await state_http.connections[0].put(
                event_frame("delete", {"id": "light-2", "type": "light"})
            )
            deleted = await asyncio.wait_for(waiting, 1)

            assert isinstance(added, Change)
            assert added.kind is ChangeKind.ADD
            assert added.before is None
            assert isinstance(added.after, models.Light)
            assert added.after.brightness == 45
            assert state.get("light-2") is None
            assert isinstance(deleted, Change)
            assert deleted.kind is ChangeKind.DELETE
            assert isinstance(deleted.before, models.Light)
            assert deleted.after is None
            await stream.aclose()

    async def test_unknown_update_fetches_the_resource_and_treats_it_as_an_add(
        self, hue, state_http
    ):
        state_http.queue_resource(
            "light", "light-2", light(55, light_id="light-2", owner="device-2")
        )
        async with hue.state() as state:
            stream = state.changes()
            waiting = asyncio.create_task(anext(stream))
            await asyncio.sleep(0)

            await state_http.connections[0].put(
                event_frame(
                    "update",
                    {
                        "id": "light-2",
                        "type": "light",
                        "dimming": {"brightness": 50},
                    },
                )
            )

            change = await asyncio.wait_for(waiting, 1)
            assert state_http.calls == [
                ("GET", "/clip/v2/resource/light/light-2", None)
            ]
            assert isinstance(change, Change)
            assert change.kind is ChangeKind.ADD
            assert change.before is None
            assert isinstance(change.after, models.Light)
            assert change.after.brightness == 55
            assert state.lights.get("light-2") is not None
            await stream.aclose()

    async def test_invalid_delta_marks_inconsistent_without_corrupting_state(
        self, hue, state_http
    ):
        async with hue.state() as state:
            stream = state.changes()
            waiting = asyncio.create_task(anext(stream))
            await asyncio.sleep(0)
            await state_http.connections[0].put(
                event_frame(
                    "update",
                    {
                        "id": "light-1",
                        "type": "light",
                        "dimming": {"brightness": "not-a-number"},
                    },
                )
            )

            item = await asyncio.wait_for(waiting, 1)
            assert isinstance(item, Resync)
            assert item.reason is ResyncReason.INCONSISTENT
            assert state.lights["Desk"].brightness == 10
            await stream.aclose()

    @pytest.mark.parametrize(
        "entry",
        [
            {"id": "", "type": "light", "dimming": {"brightness": 50}},
            {"type": "light", "dimming": {"brightness": 50}},
        ],
        ids=["empty-id", "missing-id"],
    )
    async def test_missing_resource_ids_mark_the_frame_inconsistent(
        self, hue, state_http, entry
    ):
        async with hue.state() as state:
            stream = state.changes()
            waiting = asyncio.create_task(anext(stream))
            await asyncio.sleep(0)
            await state_http.connections[0].put(event_frame("update", entry))

            marker = await asyncio.wait_for(waiting, 1)
            assert isinstance(marker, Resync)
            assert marker.reason is ResyncReason.INCONSISTENT
            assert state.lights["Desk"].brightness == 10
            await stream.aclose()

    async def test_mismatched_unknown_fetch_is_not_installed(self, hue, state_http):
        state_http.queue_resource(
            "light", "light-2", light(55, light_id="different-id")
        )
        async with hue.state() as state:
            stream = state.changes()
            waiting = asyncio.create_task(anext(stream))
            await asyncio.sleep(0)
            await state_http.connections[0].put(
                event_frame(
                    "update",
                    {
                        "id": "light-2",
                        "type": "light",
                        "dimming": {"brightness": 50},
                    },
                )
            )

            marker = await asyncio.wait_for(waiting, 1)
            assert isinstance(marker, Resync)
            assert marker.reason is ResyncReason.INCONSISTENT
            assert state.get("light-2") is None
            assert state.get("different-id") is None
            await stream.aclose()


class TestTopology:
    async def test_helpers_resolve_valid_edges_and_skip_dangling_children(self, hue):
        http = StateHttp(
            [
                [
                    {
                        "id": "device-1",
                        "type": "device",
                        "metadata": {"name": "Desk device"},
                        "services": [
                            {"rid": "light-1", "rtype": "light"},
                            {"rid": "temperature-1", "rtype": "temperature"},
                            {"rid": "motion-area-candidate", "rtype": "motion"},
                        ],
                    },
                    light(25),
                    {
                        "id": "room-1",
                        "type": "room",
                        "metadata": {"name": "Office"},
                        "children": [
                            {"rid": "device-1", "rtype": "device"},
                            {"rid": "missing-device", "rtype": "device"},
                        ],
                    },
                    {
                        "id": "zone-1",
                        "type": "zone",
                        "metadata": {"name": "Work"},
                        "children": [
                            {"rid": "light-1", "rtype": "light"},
                            {"rid": "missing-light", "rtype": "light"},
                        ],
                    },
                    {
                        "id": "temperature-1",
                        "type": "temperature",
                        "owner": {"rid": "device-1", "rtype": "device"},
                    },
                ]
            ]
        )
        hue._http = http

        async with hue.state() as state:
            room = state.rooms["Office"]
            zone = state.zones["Work"]

            assert [item.id for item in state.lights_in(room)] == ["light-1"]
            assert [item.id for item in state.lights_in(zone)] == ["light-1"]
            assert state.device_of("light-1").id == "device-1"
            assert state.room_of("light-1").id == "room-1"
            assert [item.id for item in state.zones_of("light-1")] == ["zone-1"]
            assert state.name_of("temperature-1") == "Desk device"
            assert state.name_of("motion-area-candidate") == "Desk device"


class TestSubscribers:
    async def test_lag_is_coalesced_into_one_marker_before_the_newest_change(
        self, hue, state_http
    ):
        async with hue.state() as state:
            stream = state.changes(maxsize=2)
            waiting = asyncio.create_task(anext(stream))
            await asyncio.sleep(0)
            await state_http.connections[0].put(update_frame(20, event_id="1:1"))
            _ = await asyncio.wait_for(waiting, 1)

            await state_http.connections[0].put(update_frame(30, event_id="1:2"))
            await state_http.connections[0].put(update_frame(40, event_id="1:3"))
            await state_http.connections[0].put(update_frame(50, event_id="1:4"))
            for _ in range(10):
                await asyncio.sleep(0)
                if state.lights["Desk"].brightness == 50:
                    break

            marker = await asyncio.wait_for(anext(stream), 1)
            newest = await asyncio.wait_for(anext(stream), 1)
            assert isinstance(marker, Resync)
            assert marker.reason is ResyncReason.LAGGED
            assert marker.dropped == 2
            assert isinstance(newest, Change)
            assert newest.event_id == "1:4"
            assert newest.after is not None
            assert isinstance(newest.after, models.Light)
            assert newest.after.brightness == 50
            await stream.aclose()

    async def test_each_subscriber_receives_an_independent_record(
        self, hue, state_http
    ):
        async with hue.state() as state:
            first_stream = state.changes()
            second_stream = state.changes()
            first_waiting = asyncio.create_task(anext(first_stream))
            second_waiting = asyncio.create_task(anext(second_stream))
            await asyncio.sleep(0)

            await state_http.connections[0].put(update_frame(20))

            first = await asyncio.wait_for(first_waiting, 1)
            second = await asyncio.wait_for(second_waiting, 1)
            assert isinstance(first, Change)
            assert isinstance(second, Change)
            first.delta["dimming"]["brightness"] = 99
            assert second.delta == {
                "id": "light-1",
                "type": "light",
                "dimming": {"brightness": 20},
            }
            await first_stream.aclose()
            await second_stream.aclose()


class TestReconnectAndCorrelation:
    async def test_reconnect_emits_replay_marker_and_snapshot_diff(self, hue):
        http = StateHttp([[light(10)], [light(30)]])
        http.connections.append(asyncio.Queue())
        hue._http = http

        async with hue.state() as state:
            stream = state.changes()
            first_item = asyncio.create_task(anext(stream))
            await asyncio.sleep(0)
            await http.connections[1].put(update_frame(20, event_id="2:1"))
            await http.connections[0].put(None)

            replay = await asyncio.wait_for(first_item, 1)
            marker = await asyncio.wait_for(anext(stream), 1)
            diff = await asyncio.wait_for(anext(stream), 1)
            assert isinstance(replay, Change)
            assert replay.after is not None
            assert isinstance(replay.after, models.Light)
            assert replay.after.brightness == 20
            assert isinstance(marker, Resync)
            assert marker.reason is ResyncReason.RECONNECT
            assert isinstance(diff, Change)
            assert diff.resynced is True
            assert isinstance(diff.after, models.Light)
            assert diff.after.brightness == 30
            await stream.aclose()

    async def test_local_fade_marks_echo_and_exposes_fading(self, hue, state_http):
        async with hue.state() as state:
            stream = state.changes()
            current = state.lights["Desk"]
            await current.set(brightness=80, transition=60)
            waiting = asyncio.create_task(anext(stream))
            await asyncio.sleep(0)
            await state_http.connections[0].put(update_frame(80))
            change = await asyncio.wait_for(waiting, 1)
            assert isinstance(change, Change)
            assert change.origin == "self"
            assert change.command_confirmed is True
            assert change.observation == "command_echo"
            assert change.transition_ends_at is not None
            assert state.fading["light-1"].target == {"dimming": {"brightness": 80.0}}
            await stream.aclose()

    async def test_only_first_fade_target_is_an_echo_and_unrelated_fields_are_free(
        self, hue, state_http
    ):
        async with hue.state() as state:
            stream = state.changes()
            await state.lights["Desk"].set(brightness=80, transition=60)

            await state_http.connections[0].put(update_frame(80, event_id="1:1"))
            first = await asyncio.wait_for(anext(stream), 1)
            await state_http.connections[0].put(
                event_frame(
                    "update",
                    {
                        "id": "light-1",
                        "type": "light",
                        "dimming": {"brightness": 80},
                        "on": {"on": False},
                    },
                    event_id="1:2",
                )
            )
            duplicate = await asyncio.wait_for(anext(stream), 1)
            await state_http.connections[0].put(
                event_frame(
                    "update",
                    {"id": "light-1", "type": "light", "on": {"on": True}},
                    event_id="1:3",
                )
            )
            unrelated = await asyncio.wait_for(anext(stream), 1)

            assert isinstance(first, Change)
            assert first.observation == "command_echo"
            assert isinstance(duplicate, Change)
            assert duplicate.observation == "reported"
            assert isinstance(unrelated, Change)
            assert unrelated.origin == "unattributed"
            await stream.aclose()

    async def test_pending_write_does_not_reorder_later_changes(self, hue):
        http = DeferredWriteHttp(
            [[light(10), light(20, light_id="light-2", owner="device-2")]],
            "unknown",
        )
        hue._http = http
        async with hue.state() as state:
            stream = state.changes()
            first_waiting = asyncio.create_task(anext(stream))
            await asyncio.sleep(0)
            command = asyncio.create_task(state.lights["Desk"].set_brightness(70))
            await asyncio.wait_for(http.write_started.wait(), 1)
            await http.connections[0].put(update_frame(70, event_id="1:1"))
            await http.connections[0].put(
                update_frame(
                    30,
                    event_id="1:2",
                    entries=[
                        {
                            "id": "light-2",
                            "type": "light",
                            "dimming": {"brightness": 30},
                        }
                    ],
                )
            )
            await asyncio.sleep(0)
            http.release_write.set()
            await asyncio.wait_for(command, 1)

            first = await asyncio.wait_for(first_waiting, 1)
            second = await asyncio.wait_for(anext(stream), 1)
            assert isinstance(first, Change)
            assert isinstance(second, Change)
            assert [first.event_id, second.event_id] == ["1:1", "1:2"]
            await stream.aclose()

    async def test_terminal_error_follows_an_already_folded_pending_change(self, hue):
        class TerminalDeferredHttp(DeferredWriteHttp):
            @override
            async def event_connections(
                self,
                *,
                max_retries: int | None = 10,
            ) -> AsyncGenerator[EventConnection]:
                del max_retries
                yield EventConnection(
                    opened_at=datetime.now(UTC),
                    resumed_from=None,
                    frames=self._frames(self.connections[0]),
                )
                raise EventStreamExhaustedError

        http = TerminalDeferredHttp([[light(10)]], "unknown")
        hue._http = http
        async with hue.state() as state:
            stream = state.changes()
            first_waiting = asyncio.create_task(anext(stream))
            await asyncio.sleep(0)
            command = asyncio.create_task(state.lights["Desk"].set_brightness(70))
            await asyncio.wait_for(http.write_started.wait(), 1)
            await http.connections[0].put(update_frame(70))
            await http.connections[0].put(None)
            await asyncio.sleep(0)
            assert first_waiting.done() is False

            http.release_write.set()
            await asyncio.wait_for(command, 1)
            first = await asyncio.wait_for(first_waiting, 1)
            assert isinstance(first, Change)

            with pytest.raises(
                EventStreamExhaustedError, match="event stream exhausted"
            ):
                await asyncio.wait_for(anext(stream), 1)
            await stream.aclose()

    @pytest.mark.parametrize(
        ("outcome", "expected_origin", "expected_command", "expected_confirmed"),
        [
            ("rejected", "unattributed", False, None),
            ("unknown", "self", True, False),
        ],
    )
    async def test_terminal_write_outcome_controls_event_attribution(
        self,
        hue,
        outcome,
        expected_origin,
        expected_command,
        expected_confirmed,
    ):
        http = DeferredWriteHttp([[light(10)]], outcome)
        hue._http = http
        async with hue.state() as state:
            stream = state.changes()
            waiting = asyncio.create_task(anext(stream))
            await asyncio.sleep(0)

            command = asyncio.create_task(state.lights["Desk"].set_brightness(70))
            await asyncio.wait_for(http.write_started.wait(), 1)
            await http.connections[0].put(update_frame(70))
            http.release_write.set()
            _ = await asyncio.wait_for(command, 1)
            change = await asyncio.wait_for(waiting, 1)

            assert http.writes == [
                (
                    "PUT",
                    "/clip/v2/resource/light/light-1",
                    {"dimming": {"brightness": 70.0}},
                )
            ]
            assert isinstance(change, Change)
            assert change.origin == expected_origin
            assert (change.command_id is not None) is expected_command
            assert change.command_confirmed is expected_confirmed
            assert change.observation == "reported"
            await stream.aclose()

    async def test_terminal_connection_error_reaches_each_subscriber(self, hue):
        class TerminalStateHttp(StateHttp):
            @override
            async def event_connections(
                self,
                *,
                max_retries: int | None = 10,
            ) -> AsyncGenerator[EventConnection]:
                del max_retries
                yield EventConnection(
                    opened_at=datetime.now(UTC),
                    resumed_from=None,
                    frames=self._frames(self.connections[0]),
                )
                raise EventStreamExhaustedError

        http = TerminalStateHttp([[light(10)]])
        hue._http = http
        async with hue.state() as state:
            stream = state.changes()
            waiting = asyncio.create_task(anext(stream))
            await asyncio.sleep(0)

            await http.connections[0].put(None)

            with pytest.raises(
                EventStreamExhaustedError, match="event stream exhausted"
            ):
                await asyncio.wait_for(waiting, 1)
            assert state.connected is False
            assert state.lights["Desk"].brightness == 10
            await stream.aclose()

            with pytest.raises(
                EventStreamExhaustedError, match="event stream exhausted"
            ):
                await anext(state.changes())
